"""Compatibility candidate adapter for the secondary Fit evaluator.

CTX no longer ships a capability catalog (ADR-017). The default source is
absent and evaluation must honestly abstain; user/organization recommendation
is provided by the shared engine, not a fallback set of packaged tools.
The value adapter remains useful for explicitly supplied candidate entries.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from ctx.engine.planner import CapabilityCandidate, WorkObservation

CATALOG_RESOURCE = "runtime-availability.json"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_catalog() -> tuple[dict[str, Any], ...]:
    """There is no distributed candidate catalog (ADR-017)."""
    return ()


@dataclass(frozen=True, slots=True)
class ReleaseCandidateSource:
    """Candidate retrieval over explicitly supplied capability entries."""

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
                    source_digest=_digest(capability_id),
                    normalized_score_ppm=max(1, min(1_000_000, score)),
                    matching_signals=matched,
                    reason_codes=("catalog-signal-match",) if matched else ("catalog-default",),
                    actionability="load",
                )
            )

        return tuple(candidates)


def open_release_candidate_source() -> ReleaseCandidateSource | None:
    """Return no default catalog; empty knowledge is a normal successful state."""

    entries = _load_catalog()
    if not entries:
        return None
    return ReleaseCandidateSource(entries=entries)


__all__ = [
    "CATALOG_RESOURCE",
    "ReleaseCandidateSource",
    "open_release_candidate_source",
]
