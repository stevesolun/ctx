"""Trusted single-file skill installer backed by a private content-addressed store."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from ctx.engine.content import MaterialIdentity
from ctx.engine.installation import InstallExecutionBinding
from ctx.runtime._skill_cas_posix import (
    ChildInspection,
    FileIdentity,
    PinnedSkillCasDirectory,
    RootIdentity,
    SkillCasFilesystemConflict,
    SkillCasFilesystemError,
    open_skill_cas_directory,
    skill_cas_root_identity,
)
from ctx.runtime.install_execution import (
    HeldInstallDriver,
    InstallDriverObservation,
    InstallDriverRegistration,
    InstallDriverRequest,
)
from ctx.utils._file_lock import secure_file_lock


_MAX_STAGE_ENTRIES = 64
_RECOVERY_LINK_COUNTS = frozenset(range(1, _MAX_STAGE_ENTRIES + 2))


class SkillCasError(RuntimeError):
    """Base class for safe skill-CAS failures."""


class SkillCasConflict(SkillCasError):
    """The content address is occupied by unsafe or different material."""


class SkillCasBodySource(Protocol):
    """Trusted body resolver called only after a durable install claim exists."""

    def load(self, request: InstallDriverRequest, material: MaterialIdentity) -> str: ...


class SkillCasDriverFactory:
    """Build a reconciliation-capable driver after an exact durable claim."""

    def __init__(
        self,
        *,
        skill_store_root: Path,
        body_source: SkillCasBodySource,
        expected_target_identity_digest: str,
    ) -> None:
        if not callable(getattr(body_source, "load", None)):
            raise TypeError("body_source must expose load")
        _require_digest(expected_target_identity_digest, "expected_target_identity_digest")
        self._root_identity = _root_identity(skill_store_root)
        if self._root_identity.digest != expected_target_identity_digest:
            raise ValueError("skill CAS root does not match its target identity")
        self._body_source = body_source

    @contextmanager
    def connect(self, request: InstallDriverRequest) -> Iterator[HeldInstallDriver]:
        if not isinstance(request, InstallDriverRequest):
            raise TypeError("request must be an InstallDriverRequest")
        action = request.action
        descriptor = request.descriptor
        current_identity = _root_identity(Path(self._root_identity.canonical_root))
        if (
            current_identity != self._root_identity
            or request.binding.target_identity_digest != self._root_identity.digest
            or descriptor.kind != "skill"
            or descriptor.schema_version != 2
            or descriptor.permission_expansion
            or descriptor.credential_requirement
            or action.payload.get("capability_kind") != "skill"
        ):
            raise SkillCasError("install authority is not eligible for the skill CAS")
        raw_material = action.payload.get("result_material")
        if not isinstance(raw_material, Mapping):
            raise SkillCasError("install action has no typed result material")
        try:
            material = MaterialIdentity.from_dict(raw_material)
        except (TypeError, ValueError) as exc:
            raise SkillCasError("install result material is invalid") from exc
        if not descriptor.matches_result_material(material) or material.kind != "skill":
            raise SkillCasError("skill CAS material does not match the install descriptor")
        yield _SkillCasDriver(
            root_identity=self._root_identity,
            action_content_digest=action.content_digest,
            binding_digest=request.binding.binding_digest,
            material=material,
            body_source=self._body_source,
            request=request,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillCasRuntimeConfig:
    """Trusted composition input for the built-in POSIX skill actuator."""

    skill_store_root: Path
    body_source: SkillCasBodySource
    installer_id: str
    host_identity_digest: str
    _target_identity: RootIdentity = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.skill_store_root, Path):
            raise TypeError("skill_store_root must be a Path")
        if not callable(getattr(self.body_source, "load", None)):
            raise TypeError("body_source must expose load")
        if (
            not isinstance(self.installer_id, str)
            or not self.installer_id
            or self.installer_id != self.installer_id.strip()
        ):
            raise ValueError("installer_id must be non-empty trimmed text")
        _require_digest(self.host_identity_digest, "host_identity_digest")
        object.__setattr__(self, "_target_identity", _root_identity(self.skill_store_root))

    def registration(self, *, driver_digest: str) -> InstallDriverRegistration:
        _require_digest(driver_digest, "driver_digest")
        root_identity = _root_identity(self.skill_store_root)
        if root_identity != self._target_identity:
            raise SkillCasError("skill CAS root changed after runtime configuration")
        binding = InstallExecutionBinding(
            driver_id=self.installer_id,
            driver_digest=driver_digest,
            host_identity_digest=self.host_identity_digest,
            target_identity_digest=self._target_identity.digest,
        )
        return InstallDriverRegistration(
            binding=binding,
            capability_kind="skill",
            factory=SkillCasDriverFactory(
                skill_store_root=Path(self._target_identity.canonical_root),
                body_source=self.body_source,
                expected_target_identity_digest=self._target_identity.digest,
            ),
        )


class _SkillCasDriver:
    def __init__(
        self,
        *,
        root_identity: RootIdentity,
        action_content_digest: str,
        binding_digest: str,
        material: MaterialIdentity,
        body_source: SkillCasBodySource,
        request: InstallDriverRequest,
    ) -> None:
        self._root_identity = root_identity
        self._action_content_digest = action_content_digest
        self._binding_digest = binding_digest
        self._material = material
        self._body_source = body_source
        self._request = request
        self._apply_failed = False

    @property
    def _target_name(self) -> str:
        return self._material.content_sha256

    @property
    def _stage_prefix(self) -> str:
        return f".ctx-skill-{self._material.content_sha256}-"

    @property
    def _stage_name(self) -> str:
        return f"{self._stage_prefix}{self._action_content_digest}.pending"

    @property
    def _lock_target(self) -> Path:
        return Path(self._root_identity.canonical_root) / self._target_name

    def apply_once(self) -> None:
        try:
            with secure_file_lock(self._lock_target, timeout=30.0):
                with open_skill_cas_directory(self._root_identity) as directory:
                    state = self._recover_locked(directory)
                    if state == "exact":
                        return
                    if state != "absent":
                        raise SkillCasConflict("skill CAS address is not safely writable")
                    body = self._body_source.load(self._request, self._material)
                    encoded = _authenticate_body(body, self._material)
                    directory.create_exact(self._stage_name, encoded)
                    if self._recover_locked(directory) != "exact":
                        raise SkillCasConflict("skill CAS publication could not be verified")
        except BaseException:
            # The coordinator intentionally reconciles after apply failures.  A
            # same-process failure must not use cache-resident bytes to mint an
            # applied outcome.  A fresh process/handle can safely reconcile the
            # durable crash shape later.
            self._apply_failed = True
            raise

    def reconcile(self) -> InstallDriverObservation:
        if self._apply_failed:
            return self._observation("indeterminate")
        state: Literal["exact", "absent", "conflict", "indeterminate"]
        try:
            with secure_file_lock(self._lock_target, timeout=30.0):
                with open_skill_cas_directory(self._root_identity) as directory:
                    state = self._recover_locked(directory)
        except (SkillCasConflict, SkillCasFilesystemConflict):
            state = "conflict"
        except Exception:
            state = "indeterminate"
        return self._observation(state)

    def _observation(
        self,
        state: Literal["exact", "absent", "conflict", "indeterminate"],
    ) -> InstallDriverObservation:
        observation_state = {
            "exact": "installed-exact",
            "absent": "absent",
            "conflict": "conflict",
            "indeterminate": "indeterminate",
        }[state]
        verification_digest = _observation_digest(
            state=observation_state,
            material_identity_digest=self._material.identity_digest,
            binding_digest=self._binding_digest,
        )
        return InstallDriverObservation(
            state=observation_state,  # type: ignore[arg-type]
            verification_digest=verification_digest,
            observed_material_identity_digest=(
                self._material.identity_digest if state == "exact" else None
            ),
        )

    def _inspect(
        self,
        directory: PinnedSkillCasDirectory,
        name: str,
        *,
        allowed_links: frozenset[int] = _RECOVERY_LINK_COUNTS,
        durable: bool = False,
    ) -> ChildInspection:
        return directory.inspect_exact_utf8(
            name,
            expected_sha256=self._material.content_sha256,
            expected_bytes=self._material.content_bytes,
            allowed_links=allowed_links,
            durable=durable,
        )

    def _recover_locked(
        self,
        directory: PinnedSkillCasDirectory,
        *,
        collision_retry: bool = False,
    ) -> Literal["exact", "absent", "conflict"]:
        stage_names = directory.stage_names(
            prefix=self._stage_prefix,
            suffix=".pending",
            limit=_MAX_STAGE_ENTRIES,
        )
        final = self._inspect(directory, self._target_name)
        stages = {name: self._inspect(directory, name) for name in stage_names}
        if final.state == "exact":
            return self._repair_exact_final(directory, final=final, stages=stages)
        if final.state == "absent":
            return self._repair_absent_final(
                directory,
                stages=stages,
                collision_retry=collision_retry,
            )
        return "conflict"

    def _repair_exact_final(
        self,
        directory: PinnedSkillCasDirectory,
        *,
        final: ChildInspection,
        stages: dict[str, ChildInspection],
    ) -> Literal["exact", "conflict"]:
        if final.identity is None or final.link_count is None:
            return "conflict"
        if any(
            inspection.state == "conflict" and not inspection.safely_removable
            for inspection in stages.values()
        ):
            return "conflict"

        exact_groups = _exact_identity_groups(stages)
        final_aliases = exact_groups.get(final.identity, ())
        if final.link_count != 1 + len(final_aliases):
            return "conflict"
        for group_identity, names in exact_groups.items():
            if group_identity != final.identity:
                first = stages[names[0]]
                if first.link_count != len(names):
                    return "conflict"

        for name in sorted(stages):
            inspection = stages[name]
            stage_identity = inspection.identity
            if stage_identity is None or not (
                inspection.state == "exact" or inspection.safely_removable
            ):
                return "conflict"
            directory.unlink_child_if_identity(
                name,
                expected=stage_identity,
                allowed_links=_RECOVERY_LINK_COUNTS,
            )
        return self._verify_exact_final(directory)

    def _repair_absent_final(
        self,
        directory: PinnedSkillCasDirectory,
        *,
        stages: dict[str, ChildInspection],
        collision_retry: bool,
    ) -> Literal["exact", "absent", "conflict"]:
        if any(
            inspection.state == "conflict" and not inspection.safely_removable
            for inspection in stages.values()
        ):
            return "conflict"
        exact_groups = _exact_identity_groups(stages)
        for _group_identity, names in exact_groups.items():
            first = stages[names[0]]
            if first.link_count != len(names):
                return "conflict"

        exact_names = sorted(
            name for name, inspection in stages.items() if inspection.state == "exact"
        )
        chosen = exact_names[0] if exact_names else None
        durable_stage = None
        if chosen is not None:
            durable_stage = self._inspect(
                directory,
                chosen,
                allowed_links=_RECOVERY_LINK_COUNTS,
                durable=True,
            )
            if durable_stage.state != "exact" or durable_stage.identity is None:
                return "conflict"
            directory.fsync()
        for name in sorted(stages):
            if name == chosen:
                continue
            inspection = stages[name]
            stage_identity = inspection.identity
            if stage_identity is None or not (
                inspection.state == "exact" or inspection.safely_removable
            ):
                return "conflict"
            directory.unlink_child_if_identity(
                name,
                expected=stage_identity,
                allowed_links=_RECOVERY_LINK_COUNTS,
            )

        if chosen is None:
            return self._verify_absent(directory)

        assert durable_stage is not None
        publication_stage = self._inspect(
            directory,
            chosen,
            allowed_links=frozenset({1}),
        )
        if (
            publication_stage.state != "exact"
            or publication_stage.identity is None
            or publication_stage.identity != durable_stage.identity
        ):
            return "conflict"
        created = directory.link_child_exclusive(
            chosen,
            self._target_name,
            expected_source=publication_stage.identity,
        )
        if not created:
            if collision_retry:
                return "conflict"
            return self._recover_locked(directory, collision_retry=True)
        directory.unlink_child_if_identity(
            chosen,
            expected=publication_stage.identity,
            allowed_links=frozenset({2}),
        )
        return self._verify_exact_final(directory)

    def _verify_exact_final(
        self,
        directory: PinnedSkillCasDirectory,
    ) -> Literal["exact", "conflict"]:
        final = self._inspect(
            directory,
            self._target_name,
            allowed_links=frozenset({1}),
            durable=True,
        )
        if final.state != "exact":
            return "conflict"
        directory.fsync()
        directory.revalidate_root()
        final_after = self._inspect(
            directory,
            self._target_name,
            allowed_links=frozenset({1}),
        )
        remaining = directory.stage_names(
            prefix=self._stage_prefix,
            suffix=".pending",
            limit=_MAX_STAGE_ENTRIES,
        )
        return "exact" if final_after.state == "exact" and not remaining else "conflict"

    def _verify_absent(
        self,
        directory: PinnedSkillCasDirectory,
    ) -> Literal["absent", "conflict"]:
        directory.fsync()
        directory.revalidate_root()
        final = self._inspect(directory, self._target_name)
        remaining = directory.stage_names(
            prefix=self._stage_prefix,
            suffix=".pending",
            limit=_MAX_STAGE_ENTRIES,
        )
        return "absent" if final.state == "absent" and not remaining else "conflict"


def _exact_identity_groups(
    stages: dict[str, ChildInspection],
) -> dict[FileIdentity, tuple[str, ...]]:
    grouped: defaultdict[FileIdentity, list[str]] = defaultdict(list)
    for name, inspection in stages.items():
        if inspection.state == "exact" and inspection.identity is not None:
            grouped[inspection.identity].append(name)
    return {identity: tuple(sorted(names)) for identity, names in grouped.items()}


def skill_cas_target_identity_digest(skill_store_root: Path) -> str:
    return _root_identity(skill_store_root).digest


def _root_identity(skill_store_root: Path) -> RootIdentity:
    try:
        return skill_cas_root_identity(skill_store_root)
    except SkillCasFilesystemError as exc:
        raise SkillCasError("skill CAS root is unavailable or unsupported") from exc


def _authenticate_body(body: object, material: MaterialIdentity) -> bytes:
    if not isinstance(body, str) or not body or "\x00" in body:
        raise SkillCasError("skill body must be non-empty NUL-free UTF-8 text")
    try:
        encoded = body.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise SkillCasError("skill body is not strict UTF-8 text") from exc
    if (
        len(encoded) != material.content_bytes
        or hashlib.sha256(encoded).hexdigest() != material.content_sha256
    ):
        raise SkillCasError("skill body does not match authenticated material")
    return encoded


def _observation_digest(
    *,
    state: str,
    material_identity_digest: str,
    binding_digest: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "binding_digest": binding_digest,
                "material_identity_digest": material_identity_digest,
                "schema": "ctx.skill-cas-observation-v1",
                "state": state,
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _require_digest(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


__all__ = [
    "SkillCasBodySource",
    "SkillCasConflict",
    "SkillCasDriverFactory",
    "SkillCasError",
    "SkillCasRuntimeConfig",
    "skill_cas_target_identity_digest",
]
