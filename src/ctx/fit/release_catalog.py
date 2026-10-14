"""Compatibility candidate adapter for the secondary Fit evaluator.

CTX no longer ships a capability catalog (ADR-017). The default source is
absent and evaluation must honestly abstain; user/organization recommendation
is provided by the shared engine, not a fallback set of packaged tools.
The value adapter remains useful for explicitly supplied candidate entries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ctx.engine.capability_schema import MAX_CANONICAL_TOKEN_CHARS, validate_capability_identity
from ctx.engine.planner import MAX_CANDIDATES, MAX_SIGNALS, CapabilityCandidate, WorkObservation

if TYPE_CHECKING:
    from ctx.fit.candidates import CapabilityMaterial

CATALOG_RESOURCE = "runtime-availability.json"
USER_CATALOG_SCHEMA = "ctx.fit.catalog-v1"
MAX_USER_CATALOG_BYTES = 2 * 1024 * 1024
_SIGNAL_RE = re.compile(rf"\A[a-z0-9][a-z0-9._:@-]{{0,{MAX_CANONICAL_TOKEN_CHARS - 1}}}\Z")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_catalog() -> tuple[dict[str, Any], ...]:
    """There is no distributed candidate catalog (ADR-017)."""
    return ()


@dataclass(frozen=True, slots=True)
class ReleaseCandidateSource:
    """Candidate retrieval over explicitly supplied capability entries."""

    entries: tuple[dict[str, Any], ...]
    capability_materials: tuple[CapabilityMaterial, ...] | None = None

    @property
    def catalog_snapshot_digest(self) -> str:
        identity = json.dumps(
            {
                "entries": sorted(self.entries, key=lambda entry: str(entry["id"])),
                "materials": [
                    material.to_dict()
                    for material in sorted(
                        self.capability_materials or (), key=lambda material: material.capability_id
                    )
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _digest(identity)

    def retrieve(self, observation: WorkObservation) -> tuple[CapabilityCandidate, ...]:
        wanted = {token.lower() for token in (*observation.signals, *observation.languages)}
        candidates: list[CapabilityCandidate] = []

        for entry in self.entries:
            capability_id = str(entry["id"])
            kind = str(entry.get("type", ""))
            if ":" not in capability_id or not kind:
                continue
            name = capability_id.split(":", 1)[1]

            declared = tuple(str(signal).lower() for signal in entry.get("signals", ()))
            matched = tuple(sorted(token for token in declared if token in wanted))
            if declared and not matched:
                # Nothing about this repository suggests the capability. Offering
                # it anyway would be the "more recommendations" failure mode the
                # product explicitly rejects.
                continue

            # Overlap fraction, expressed in the planner's parts-per-million
            # scale. A capability with no declared signals (a general-purpose
            # server) scores at the floor rather than being excluded.
            score = int(1_000_000 * len(matched) / len(declared)) if declared else 400_000

            candidates.append(
                CapabilityCandidate(
                    capability_id=capability_id,
                    kind=kind,
                    name=name,
                    source_digest=_digest(
                        json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    ),
                    normalized_score_ppm=max(1, min(1_000_000, score)),
                    matching_signals=matched,
                    reason_codes=("catalog-signal-match",) if matched else ("catalog-default",),
                    actionability="load",
                )
            )

        return tuple(candidates)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate catalog field: {key}")
        result[key] = value
    return result


def open_user_candidate_source(path: str | Path) -> ReleaseCandidateSource:
    """Read a bounded local catalog with inline, content-addressed skill bodies."""

    from ctx.fit.candidates import (
        MAX_CANDIDATE_USER_CONTEXT_BYTES,
        CapabilityMaterial,
        _canonical_catalog_entry_digest,
        _skill_frontmatter_error,
    )

    catalog_path = Path(path).absolute()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(catalog_path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("catalog must be a regular file")
            if metadata.st_size > MAX_USER_CATALOG_BYTES:
                raise ValueError(f"catalog exceeds {MAX_USER_CATALOG_BYTES} bytes")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(MAX_USER_CATALOG_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValueError(f"catalog could not be safely read: {exc}") from exc
    if len(raw) > MAX_USER_CATALOG_BYTES:
        raise ValueError(f"catalog exceeds {MAX_USER_CATALOG_BYTES} bytes")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"catalog is not valid UTF-8 JSON: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "entries"}
        or payload["schema"] != USER_CATALOG_SCHEMA
        or not isinstance(payload["entries"], list)
        or len(payload["entries"]) > MAX_CANDIDATES
    ):
        raise ValueError(
            f"catalog must use {USER_CATALOG_SCHEMA} with at most {MAX_CANDIDATES} entries"
        )

    entries: list[dict[str, Any]] = []
    materials: list[CapabilityMaterial] = []
    identities: set[str] = set()
    for entry in payload["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"id", "type", "signals", "content"}:
            raise ValueError("catalog entries require only id, type, signals and inline content")
        capability_id, kind = validate_capability_identity(entry["id"], entry["type"])
        if kind != "skill":
            raise ValueError("Fit catalogs support only skill material")
        if capability_id in identities:
            raise ValueError(f"duplicate catalog identity: {capability_id}")
        identities.add(capability_id)
        signals = entry["signals"]
        if (
            not isinstance(signals, list)
            or len(signals) > MAX_SIGNALS
            or any(
                not isinstance(signal, str) or _SIGNAL_RE.fullmatch(signal) is None
                for signal in signals
            )
            or len(set(signals)) != len(signals)
        ):
            raise ValueError(f"{capability_id} signals must be unique bounded canonical tokens")
        content = entry["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"{capability_id} requires nonempty inline UTF-8 content")
        try:
            content_bytes = len(content.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError(f"{capability_id} content is not valid UTF-8") from exc
        if content_bytes > MAX_CANDIDATE_USER_CONTEXT_BYTES:
            raise ValueError(f"{capability_id} content exceeds the Fit context limit")
        if error := _skill_frontmatter_error(content, expected_name=capability_id.split(":", 1)[1]):
            raise ValueError(f"{capability_id} skill frontmatter: {error}")
        normalized = {**entry, "signals": sorted(signals)}
        entries.append(normalized)
        materials.append(
            CapabilityMaterial.from_content(
                capability_id=capability_id,
                delivery_mode="task-user-context",
                source_identity=f"user-catalog:{catalog_path}#{capability_id}",
                catalog_entry_digest=_canonical_catalog_entry_digest(normalized),
                content=content,
            )
        )
    return ReleaseCandidateSource(entries=tuple(entries), capability_materials=tuple(materials))


def open_release_candidate_source() -> ReleaseCandidateSource | None:
    """Return no default catalog; empty knowledge is a normal successful state."""

    entries = _load_catalog()
    if not entries:
        return None
    return ReleaseCandidateSource(entries=entries)


__all__ = [
    "CATALOG_RESOURCE",
    "ReleaseCandidateSource",
    "USER_CATALOG_SCHEMA",
    "open_user_candidate_source",
    "open_release_candidate_source",
]
