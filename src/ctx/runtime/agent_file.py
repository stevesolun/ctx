"""Verified POSIX installer for one inactive, host-neutral agent document.

The installed file is an inert CTX workshop artifact.  This actuator does not
write to a host auto-discovery directory, activate an agent, grant tools, or
invoke a child run.  A later host adapter must perform those separately
receipt-backed transitions.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import yaml

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


AGENT_FILE_SCANNER_VERSION = "ctx-agent-file-scanner-v1"
_MAX_FRONTMATTER_BYTES = 2_048
_MAX_DESCRIPTION_CHARS = 512
_MAX_STAGE_ENTRIES = 64
_RECOVERY_LINK_COUNTS = frozenset(range(1, _MAX_STAGE_ENTRIES + 2))
_ALLOWED_FRONTMATTER_FIELDS = frozenset({"description", "name"})
_YAML_INDICATOR_PREFIXES = frozenset("-?:,[]{}#&*!|>'\"%@`")
_YAML_IMPLICIT_KEYWORDS = frozenset(
    {"~", "null", "true", "false", "yes", "no", "on", "off", ".nan", ".inf"}
)
_YAML_NUMBER = re.compile(
    r"[+-]?(?:"
    r"0[bBoOxX][0-9A-Fa-f_]+|"
    r"[0-9][0-9_]*(?::[0-9_]+)+|"
    r"(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9_]+)"
    r"(?:[eE][+-]?[0-9][0-9_]*)?"
    r")"
)
_YAML_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt ].*)?")


class AgentFileError(RuntimeError):
    """Base class for safe inactive-agent installation failures."""


class AgentFileConflict(AgentFileError):
    """An agent target or recovery entry is unsafe or ambiguous."""


class AgentFileFormatError(AgentFileError):
    """Agent bytes do not conform to the closed host-neutral format."""


class AgentFileBodySource(Protocol):
    """Trusted body resolver called only after a durable install claim exists."""

    def load(self, request: InstallDriverRequest, material: MaterialIdentity) -> str: ...


class AgentFileDriverFactory:
    """Build a reconciliation-capable agent driver after an exact claim."""

    def __init__(
        self,
        *,
        inactive_agent_root: Path,
        body_source: AgentFileBodySource,
        expected_target_identity_digest: str,
    ) -> None:
        if not callable(getattr(body_source, "load", None)):
            raise TypeError("body_source must expose load")
        _require_digest(expected_target_identity_digest, "expected_target_identity_digest")
        self._root_identity = _root_identity(inactive_agent_root)
        if _target_identity_digest(self._root_identity) != expected_target_identity_digest:
            raise ValueError("inactive agent root does not match its target identity")
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
            or request.binding.target_identity_digest
            != _target_identity_digest(self._root_identity)
            or descriptor.kind != "agent"
            or descriptor.schema_version != 2
            or descriptor.permission_expansion
            or descriptor.credential_requirement
            or action.payload.get("capability_kind") != "agent"
        ):
            raise AgentFileError("install authority is not eligible for the agent actuator")
        raw_material = action.payload.get("result_material")
        if not isinstance(raw_material, Mapping):
            raise AgentFileError("install action has no typed result material")
        try:
            material = MaterialIdentity.from_dict(raw_material)
        except (TypeError, ValueError) as exc:
            raise AgentFileError("install result material is invalid") from exc
        if not descriptor.matches_result_material(material) or material.kind != "agent":
            raise AgentFileError("agent material does not match the install descriptor")
        slug = _agent_slug(material.capability_id)
        yield _AgentFileDriver(
            root_identity=self._root_identity,
            action_content_digest=action.content_digest,
            binding_digest=request.binding.binding_digest,
            material=material,
            body_source=self._body_source,
            request=request,
            slug=slug,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentFileRuntimeConfig:
    """Trusted composition input for a POSIX inactive-agent artifact root."""

    inactive_agent_root: Path
    body_source: AgentFileBodySource
    installer_id: str
    host_identity_digest: str
    _root_identity: RootIdentity = field(init=False, repr=False)
    _target_identity_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.inactive_agent_root, Path):
            raise TypeError("inactive_agent_root must be a Path")
        if not callable(getattr(self.body_source, "load", None)):
            raise TypeError("body_source must expose load")
        if (
            not isinstance(self.installer_id, str)
            or not self.installer_id
            or self.installer_id != self.installer_id.strip()
        ):
            raise ValueError("installer_id must be non-empty trimmed text")
        _require_digest(self.host_identity_digest, "host_identity_digest")
        root_identity = _root_identity(self.inactive_agent_root)
        object.__setattr__(self, "_root_identity", root_identity)
        object.__setattr__(
            self,
            "_target_identity_digest",
            _target_identity_digest(root_identity),
        )

    def registration(self, *, driver_digest: str) -> InstallDriverRegistration:
        _require_digest(driver_digest, "driver_digest")
        root_identity = _root_identity(self.inactive_agent_root)
        if root_identity != self._root_identity:
            raise AgentFileError("inactive agent root changed after runtime configuration")
        binding = InstallExecutionBinding(
            driver_id=self.installer_id,
            driver_digest=driver_digest,
            host_identity_digest=self.host_identity_digest,
            target_identity_digest=self._target_identity_digest,
        )
        return InstallDriverRegistration(
            binding=binding,
            capability_kind="agent",
            factory=AgentFileDriverFactory(
                inactive_agent_root=Path(self._root_identity.canonical_root),
                body_source=self.body_source,
                expected_target_identity_digest=self._target_identity_digest,
            ),
        )


class _AgentFileDriver:
    def __init__(
        self,
        *,
        root_identity: RootIdentity,
        action_content_digest: str,
        binding_digest: str,
        material: MaterialIdentity,
        body_source: AgentFileBodySource,
        request: InstallDriverRequest,
        slug: str,
    ) -> None:
        self._root_identity = root_identity
        self._action_content_digest = action_content_digest
        self._binding_digest = binding_digest
        self._material = material
        self._body_source = body_source
        self._request = request
        self._slug = slug
        self._apply_failed = False

    @property
    def _target_name(self) -> str:
        return f"{self._slug}.md"

    @property
    def _stage_prefix(self) -> str:
        return f".ctx-agent-{_stage_namespace_digest(self._material)}-"

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
                        raise AgentFileConflict("agent target is not safely writable")
                    body = self._body_source.load(self._request, self._material)
                    encoded = _authenticate_body(
                        body,
                        material=self._material,
                        expected_slug=self._slug,
                    )
                    directory.create_exact(self._stage_name, encoded)
                    if self._recover_locked(directory) != "exact":
                        raise AgentFileConflict("agent publication could not be verified")
        except BaseException:
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
        except (AgentFileConflict, SkillCasFilesystemConflict):
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
            target_name=self._target_name,
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
        inspection = directory.inspect_exact_utf8(
            name,
            expected_sha256=self._material.content_sha256,
            expected_bytes=self._material.content_bytes,
            allowed_links=allowed_links,
            durable=durable,
        )
        if inspection.state != "exact":
            return inspection
        try:
            authenticated = directory.read_exact_utf8_bytes(
                name,
                expected_sha256=self._material.content_sha256,
                expected_bytes=self._material.content_bytes,
                allowed_links=allowed_links,
                durable=durable,
            )
            _validate_agent_document(authenticated, expected_slug=self._slug)
        except AgentFileFormatError:
            return ChildInspection(
                "conflict",
                identity=inspection.identity,
                link_count=inspection.link_count,
                safely_removable=inspection.safely_removable,
            )
        return inspection

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


def agent_file_target_identity_digest(inactive_agent_root: Path) -> str:
    return _target_identity_digest(_root_identity(inactive_agent_root))


def _root_identity(inactive_agent_root: Path) -> RootIdentity:
    try:
        return skill_cas_root_identity(inactive_agent_root)
    except SkillCasFilesystemError as exc:
        raise AgentFileError("inactive agent root is unavailable or unsupported") from exc


def _target_identity_digest(root_identity: RootIdentity) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "filesystem_root_identity_digest": root_identity.digest,
                "format_scanner_version": AGENT_FILE_SCANNER_VERSION,
                "schema": "ctx.agent-file-target-v1",
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _agent_slug(capability_id: str) -> str:
    prefix, separator, slug = capability_id.partition(":")
    if (
        prefix != "agent"
        or separator != ":"
        or not slug
        or "/" in slug
        or "\\" in slug
        or len(f"{slug}.md".encode("utf-8")) >= 255
    ):
        raise AgentFileError("agent capability has no safe deterministic slug")
    return slug


def _stage_namespace_digest(material: MaterialIdentity) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "capability_id": material.capability_id,
                "content_sha256": material.content_sha256,
                "format_scanner_version": AGENT_FILE_SCANNER_VERSION,
                "schema": "ctx.agent-file-stage-namespace-v1",
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _authenticate_body(
    body: object,
    *,
    material: MaterialIdentity,
    expected_slug: str,
) -> bytes:
    if not isinstance(body, str) or not body or "\x00" in body:
        raise AgentFileFormatError("agent body must be non-empty NUL-free UTF-8 text")
    try:
        encoded = body.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise AgentFileFormatError("agent body is not strict UTF-8 text") from exc
    if (
        len(encoded) != material.content_bytes
        or hashlib.sha256(encoded).hexdigest() != material.content_sha256
    ):
        raise AgentFileError("agent body does not match authenticated material")
    _validate_agent_document(encoded, expected_slug=expected_slug)
    return encoded


def _validate_agent_document(raw: bytes, *, expected_slug: str) -> None:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise AgentFileFormatError("agent document is not strict UTF-8") from exc
    if not text.startswith("---\n") or "\r" in text or "\x00" in text:
        raise AgentFileFormatError("agent document must start with canonical frontmatter")
    closing = text.find("\n---\n", 4)
    if closing < 0 or len(text[4:closing].encode("utf-8")) > _MAX_FRONTMATTER_BYTES:
        raise AgentFileFormatError("agent frontmatter is missing or oversized")
    frontmatter = text[4:closing]
    body = text[closing + len("\n---\n") :]
    if not frontmatter or not body.strip():
        raise AgentFileFormatError("agent frontmatter and instruction body are required")
    if any(line in {"---", "..."} for line in body.splitlines()):
        raise AgentFileFormatError("agent document cannot contain multiple documents")

    values: dict[str, str] = {}
    for line in frontmatter.split("\n"):
        if not line or line != line.strip() or "\t" in line or ":" not in line:
            raise AgentFileFormatError("agent frontmatter contains a malformed field")
        key, value = line.split(":", 1)
        value = value.lstrip(" ")
        if (
            key not in _ALLOWED_FRONTMATTER_FIELDS
            or key in values
            or not value
            or not _is_canonical_scalar(value)
        ):
            raise AgentFileFormatError("agent frontmatter contains an unsafe field")
        values[key] = value
    if set(values) != _ALLOWED_FRONTMATTER_FIELDS:
        raise AgentFileFormatError("agent frontmatter has missing or unknown fields")
    if values["name"] != expected_slug:
        raise AgentFileFormatError("agent name does not match its capability identity")
    if len(values["description"]) > _MAX_DESCRIPTION_CHARS:
        raise AgentFileFormatError("agent description is oversized")
    try:
        parsed = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise AgentFileFormatError("agent frontmatter is not canonical YAML") from exc
    if (
        not isinstance(parsed, dict)
        or set(parsed) != _ALLOWED_FRONTMATTER_FIELDS
        or any(type(value) is not str for value in parsed.values())
        or parsed != values
    ):
        raise AgentFileFormatError("agent frontmatter does not resolve to exact strings")


def _is_canonical_scalar(value: str) -> bool:
    folded = value.casefold()
    if (
        value != value.strip()
        or value[0] in _YAML_INDICATOR_PREFIXES
        or ": " in value
        or " #" in value
        or folded in _YAML_IMPLICIT_KEYWORDS
        or folded in {"+.inf", "-.inf"}
        or _YAML_NUMBER.fullmatch(value) is not None
        or _YAML_TIMESTAMP.fullmatch(value) is not None
    ):
        return False
    return all(
        character == " "
        or (
            not character.isspace()
            and not unicodedata.category(character).startswith("C")
            and unicodedata.category(character) not in {"Zl", "Zp"}
        )
        for character in value
    )


def _observation_digest(
    *,
    state: str,
    material_identity_digest: str,
    binding_digest: str,
    target_name: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "binding_digest": binding_digest,
                "format_scanner_version": AGENT_FILE_SCANNER_VERSION,
                "material_identity_digest": material_identity_digest,
                "schema": "ctx.agent-file-observation-v1",
                "state": state,
                "target_name": target_name,
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
    "AGENT_FILE_SCANNER_VERSION",
    "AgentFileBodySource",
    "AgentFileConflict",
    "AgentFileDriverFactory",
    "AgentFileError",
    "AgentFileFormatError",
    "AgentFileRuntimeConfig",
    "agent_file_target_identity_digest",
]
