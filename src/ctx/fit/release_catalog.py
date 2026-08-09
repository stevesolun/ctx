"""The shipped capability catalog, as a candidate source.

CTX Fit needs somewhere to draw candidates from before it can propose an
experiment. The shipped ``runtime-availability`` asset is the smallest honest
source: a handful of project-owned, MIT-licensed capabilities that need no API
key, each with its content already present in the package.

Scoring here is deliberately simple and explainable — token overlap between the
repository's normalized signals and the capability's own declared signals. It
is not a ranking model. Its only job is to answer "which of these is plausibly
relevant to this repository?" so that the bounded planner can cut the list to a
handful. A more capable retrieval layer can replace this without changing
anything downstream, because the planner only depends on the
:class:`~ctx.engine.planner.CandidateSource` protocol.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from ctx.engine.planner import CapabilityCandidate, WorkObservation

CATALOG_RESOURCE = "runtime-availability.json"

#: Signals a capability serves, derived from its identity. The catalog does not
#: carry tags, so they are declared here rather than inferred from prose — an
#: inferred tag would be a guess presented as a fact.
_CAPABILITY_SIGNALS: dict[str, tuple[str, ...]] = {
    "skill:ctx-python-testing": ("python", "pytest", "test"),
    "skill:ctx-python-state-protocols": ("python",),
    "skill:ctx-python-input-boundaries": ("python",),
    "skill:ctx-python-api-compatibility": ("python",),
    "skill:ctx-javascript-testing": ("javascript", "jest", "vitest", "test"),
    "skill:ctx-rust-patterns": ("rust", "cargo"),
    "skill:ctx-typescript": ("typescript", "javascript"),
    "agent:ctx-python-reviewer": ("python", "lint", "typecheck"),
    "mcp-server:ctx-core": (),
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _load_catalog() -> tuple[dict[str, Any], ...]:
    try:
        package = resources.files("ctx.assets")
        raw = (package / CATALOG_RESOURCE).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return ()
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    entries = loaded.get("entries")
    if not isinstance(entries, list):
        return ()
    return tuple(entry for entry in entries if isinstance(entry, dict) and entry.get("id"))


@dataclass(frozen=True, slots=True)
class ReleaseCandidateSource:
    """Candidate retrieval over the shipped, no-API-key capability catalog."""

    entries: tuple[dict[str, Any], ...]

    @property
    def catalog_snapshot_digest(self) -> str:
        identity = "|".join(sorted(str(entry["id"]) for entry in self.entries))
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

            declared = _CAPABILITY_SIGNALS.get(capability_id, ())
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
                    source_digest=_digest(capability_id),
                    normalized_score_ppm=max(1, min(1_000_000, score)),
                    matching_signals=matched,
                    reason_codes=("catalog-signal-match",) if matched else ("catalog-default",),
                    actionability="load",
                )
            )

        return tuple(candidates)


def open_release_candidate_source() -> ReleaseCandidateSource | None:
    """Open the shipped catalog, or return None when it is unavailable.

    An absent catalog degrades the product rather than breaking it: readiness
    analysis still works, and the caller reports that no candidate could be
    proposed.
    """

    entries = _load_catalog()
    if not entries:
        return None
    return ReleaseCandidateSource(entries=entries)


__all__ = [
    "CATALOG_RESOURCE",
    "ReleaseCandidateSource",
    "open_release_candidate_source",
]
