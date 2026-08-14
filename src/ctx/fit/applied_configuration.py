"""Strict, non-executing activation of an applied CTX Fit configuration.

The applied sidecar is an executable *input* to ``ctx run`` even though loading
it executes no code.  This module therefore treats every field as untrusted,
rebuilds the candidate through the same domain types that produced it, and
accepts the file only when its content-addressed identity is internally exact.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ctx.fit.candidates import (
    CANDIDATE_SCHEMA,
    MAX_CANDIDATE_USER_CONTEXT_BYTES,
    ROLE_INTENT,
    CapabilityDeliveryMode,
    CapabilityMaterial,
    CandidateConfiguration,
    CandidateRole,
    InstructionDeliveryMode,
    InstructionMaterial,
    render_candidate_user_context,
)

APPLIED_CONFIGURATION_SCHEMA = "ctx.fit.applied-configuration-v1"
APPLIED_CONFIGURATION_PATH = Path(".ctx/fit-configuration.json")
MAX_APPLIED_CONFIGURATION_BYTES = 512 * 1024


class AppliedConfigurationError(ValueError):
    """An applied sidecar is present but cannot safely reproduce its candidate."""


@dataclass(frozen=True, slots=True)
class AppliedConfiguration:
    """A validated candidate reduced to the authority ``ctx run`` needs."""

    configuration_hash: str
    model: str
    user_context: str
    candidate: CandidateConfiguration


def _fail(message: str) -> AppliedConfigurationError:
    return AppliedConfigurationError(f"invalid applied CTX Fit configuration: {message}")


def _read_manifest(workspace: Path) -> bytes | None:
    root = workspace.resolve()
    if not root.is_dir():
        raise _fail("workspace is not a directory")

    ctx_dir = root / APPLIED_CONFIGURATION_PATH.parts[0]
    target = root.joinpath(*APPLIED_CONFIGURATION_PATH.parts)
    if ctx_dir.is_symlink() or target.is_symlink():
        raise _fail("the applied-configuration path is or traverses a symbolic link")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _fail(f"the sidecar could not be opened safely: {exc}") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise _fail("the sidecar is not a regular file")
        if metadata.st_size > MAX_APPLIED_CONFIGURATION_BYTES:
            raise _fail(f"the sidecar exceeds {MAX_APPLIED_CONFIGURATION_BYTES} bytes")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_APPLIED_CONFIGURATION_BYTES + 1)
    except OSError as exc:
        raise _fail(f"the sidecar could not be read safely: {exc}") from exc
    finally:
        os.close(descriptor)

    if len(raw) > MAX_APPLIED_CONFIGURATION_BYTES:
        raise _fail(f"the sidecar exceeds {MAX_APPLIED_CONFIGURATION_BYTES} bytes")
    return raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _parse_json(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                _fail(f"non-finite JSON value {value!r}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail(f"the sidecar is not canonical UTF-8 JSON: {exc}") from exc
    if type(parsed) is not dict:
        raise _fail("the sidecar root must be an object")
    return cast(dict[str, object], parsed)


def _exact_keys(value: dict[str, object], expected: set[str], label: str) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if extra:
            parts.append(f"unknown {', '.join(extra)}")
        raise _fail(f"{label} fields are invalid: {'; '.join(parts)}")


def _string(value: object, label: str, *, nonempty: bool = True) -> str:
    if type(value) is not str:
        raise _fail(f"{label} must be a string")
    result = cast(str, value)
    if nonempty and not result:
        raise _fail(f"{label} must not be empty")
    try:
        result.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _fail(f"{label} is not valid UTF-8") from exc
    return result


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise _fail(f"{label} must be an array")
    return tuple(_string(item, f"{label} item") for item in cast(list[object], value))


def _integer(value: object, label: str) -> int:
    if type(value) is not int or cast(int, value) < 0:
        raise _fail(f"{label} must be a non-negative integer")
    return cast(int, value)


_CAPABILITY_FIELDS = {
    "capability_id",
    "delivery_mode",
    "source_identity",
    "catalog_entry_digest",
    "encoding",
    "content_bytes",
    "content_sha256",
    "content",
}


def _capability_material(value: object, index: int) -> CapabilityMaterial:
    label = f"candidate.capability_materials[{index}]"
    if type(value) is not dict:
        raise _fail(f"{label} must be an object")
    data = cast(dict[str, object], value)
    _exact_keys(data, _CAPABILITY_FIELDS, label)
    if _string(data["encoding"], f"{label}.encoding") != "utf-8":
        raise _fail(f"{label}.encoding must be 'utf-8'")
    content = _string(data["content"], f"{label}.content")
    if _integer(data["content_bytes"], f"{label}.content_bytes") != len(content.encode("utf-8")):
        raise _fail(f"{label}.content_bytes does not match its UTF-8 content")
    try:
        capability_id = _string(data["capability_id"], f"{label}.capability_id")
        source_identity = _string(data["source_identity"], f"{label}.source_identity")
        if source_identity.rpartition("#")[2] != capability_id:
            raise _fail(f"{label}.source_identity does not name its capability ID")
        return CapabilityMaterial(
            capability_id=capability_id,
            delivery_mode=cast(
                CapabilityDeliveryMode,
                _string(data["delivery_mode"], f"{label}.delivery_mode"),
            ),
            source_identity=source_identity,
            catalog_entry_digest=_string(
                data["catalog_entry_digest"], f"{label}.catalog_entry_digest"
            ),
            content=content,
            content_sha256=_string(data["content_sha256"], f"{label}.content_sha256"),
        )
    except ValueError as exc:
        raise _fail(f"{label}: {exc}") from exc


_INSTRUCTION_FIELDS = {
    "path",
    "delivery_mode",
    "source_identity",
    "encoding",
    "content_bytes",
    "content_sha256",
    "content",
}


def _instruction_material(value: object, index: int) -> InstructionMaterial:
    label = f"candidate.instruction_materials[{index}]"
    if type(value) is not dict:
        raise _fail(f"{label} must be an object")
    data = cast(dict[str, object], value)
    _exact_keys(data, _INSTRUCTION_FIELDS, label)
    if _string(data["encoding"], f"{label}.encoding") != "utf-8":
        raise _fail(f"{label}.encoding must be 'utf-8'")
    content = _string(data["content"], f"{label}.content", nonempty=False)
    if _integer(data["content_bytes"], f"{label}.content_bytes") != len(content.encode("utf-8")):
        raise _fail(f"{label}.content_bytes does not match its UTF-8 content")
    try:
        return InstructionMaterial(
            path=_string(data["path"], f"{label}.path"),
            delivery_mode=cast(
                InstructionDeliveryMode,
                _string(data["delivery_mode"], f"{label}.delivery_mode"),
            ),
            source_identity=_string(data["source_identity"], f"{label}.source_identity"),
            content=content,
            content_sha256=_string(data["content_sha256"], f"{label}.content_sha256"),
        )
    except ValueError as exc:
        raise _fail(f"{label}: {exc}") from exc


_CANDIDATE_FIELDS = {
    "schema",
    "candidate_id",
    "role",
    "role_intent",
    "capability_ids",
    "model",
    "instructions",
    "selection_reason",
    "evidence",
    "capability_materials",
    "instruction_materials",
    "configuration_hash",
}


def _candidate(value: object) -> CandidateConfiguration:
    if type(value) is not dict:
        raise _fail("candidate must be an object")
    data = cast(dict[str, object], value)
    _exact_keys(data, _CANDIDATE_FIELDS, "candidate")
    if _string(data["schema"], "candidate.schema") != CANDIDATE_SCHEMA:
        raise _fail(f"candidate.schema must be {CANDIDATE_SCHEMA!r}")

    role_value = _string(data["role"], "candidate.role")
    if role_value not in ROLE_INTENT:
        raise _fail("candidate.role is unsupported")
    role = cast(CandidateRole, role_value)
    if _string(data["role_intent"], "candidate.role_intent") != ROLE_INTENT[role]:
        raise _fail("candidate.role_intent does not match candidate.role")

    raw_capabilities = data["capability_materials"]
    raw_instructions = data["instruction_materials"]
    if type(raw_capabilities) is not list or type(raw_instructions) is not list:
        raise _fail("candidate material collections must be arrays")
    capabilities = tuple(
        _capability_material(item, index)
        for index, item in enumerate(cast(list[object], raw_capabilities))
    )
    instructions = tuple(
        _instruction_material(item, index)
        for index, item in enumerate(cast(list[object], raw_instructions))
    )
    model = _string(data["model"], "candidate.model")
    if model != model.strip():
        raise _fail("candidate.model must be a normalized pinned model")

    try:
        candidate = CandidateConfiguration(
            candidate_id=_string(data["candidate_id"], "candidate.candidate_id"),
            role=role,
            capability_ids=_string_list(data["capability_ids"], "candidate.capability_ids"),
            model=model,
            instructions=_string_list(data["instructions"], "candidate.instructions"),
            selection_reason=_string(data["selection_reason"], "candidate.selection_reason"),
            evidence=_string_list(data["evidence"], "candidate.evidence"),
            capability_materials=capabilities,
            instruction_materials=instructions,
        )
    except ValueError as exc:
        raise _fail(f"candidate: {exc}") from exc

    recorded_hash = _string(data["configuration_hash"], "candidate.configuration_hash")
    if recorded_hash != candidate.configuration_hash:
        raise _fail("candidate.configuration_hash does not match the candidate")
    if data != candidate.to_dict():
        raise _fail("candidate serialization is not canonical")
    if error := candidate.reproducibility_error:
        raise _fail(f"candidate is not reproducible: {error}")
    return candidate


def load_applied_configuration(workspace: Path) -> AppliedConfiguration | None:
    """Load one exact applied configuration, or ``None`` when none is present.

    A present invalid file always raises.  No repository instruction path is
    opened here: the sidecar already binds the exact bytes evaluated by Fit,
    and those bytes are the only instruction/capability authority activated.
    """

    raw = _read_manifest(workspace)
    if raw is None:
        return None
    payload = _parse_json(raw)
    _exact_keys(payload, {"schema", "configuration_hash", "candidate"}, "sidecar")
    if _string(payload["schema"], "schema") != APPLIED_CONFIGURATION_SCHEMA:
        raise _fail(f"schema must be {APPLIED_CONFIGURATION_SCHEMA!r}")

    candidate = _candidate(payload["candidate"])
    outer_hash = _string(payload["configuration_hash"], "configuration_hash")
    if outer_hash != candidate.configuration_hash:
        raise _fail("configuration_hash does not match the candidate")
    context = render_candidate_user_context(candidate)
    if len(context.encode("utf-8")) > MAX_CANDIDATE_USER_CONTEXT_BYTES:
        raise _fail("candidate user context exceeds the runtime byte limit")
    assert candidate.model is not None  # established by the strict parser above
    return AppliedConfiguration(
        configuration_hash=outer_hash,
        model=candidate.model,
        user_context=context,
        candidate=candidate,
    )


def load_applied_configuration_for_path(start: Path) -> AppliedConfiguration | None:
    """Load the nearest configuration inside ``start``'s Git repository.

    Ordinary development commands are often launched from ``repo/src`` rather
    than the repository root.  Search only as far as the nearest real Git
    boundary so a nested repository never inherits a parent repository's
    applied authority. Non-Git callers retain the exact-directory behavior.
    """

    current = start.resolve()
    if not current.is_dir():
        raise _fail("workspace is not a directory")

    repository_root: Path | None = None
    for ancestor in (current, *current.parents):
        marker = ancestor / ".git"
        try:
            metadata = marker.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _fail(f"the repository boundary could not be inspected: {exc}") from exc
        if stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
            repository_root = ancestor
            break
        raise _fail("the nearest .git repository boundary is not a regular file or directory")

    if repository_root is None:
        return load_applied_configuration(current)

    # The apply command owns exactly the repository-root sidecar. A nested
    # source directory must not be able to shadow that authority merely because
    # the user launches ``ctx run`` from below it.
    return load_applied_configuration(repository_root)


__all__ = [
    "APPLIED_CONFIGURATION_PATH",
    "APPLIED_CONFIGURATION_SCHEMA",
    "AppliedConfiguration",
    "AppliedConfigurationError",
    "load_applied_configuration",
    "load_applied_configuration_for_path",
]
