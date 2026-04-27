"""dedup_check.py — Pre-ship duplicate-detection gate.

Compares every entity in the wiki against every other (skill ↔ skill,
skill ↔ agent, skill ↔ MCP, agent ↔ agent, agent ↔ MCP, MCP ↔ MCP) by
cosine similarity of their embeddings. Pairs at or above the threshold
are flagged for human review. Never auto-deletes anything.

Why pre-ship: a 1,800-skill catalog with silent near-duplicates produces
recommendation noise — the user sees four versions of "TDD" in their
top-5 and loses trust on day one. The 15% that distinguishes two
near-duplicates can be load-bearing, so the policy is "flag and review",
not "drop one".

Design notes:
  - Reuses the embedding cache produced by graphify
    (``~/.claude/skill-wiki/.embedding-cache/graph/embeddings.npz``).
    No fresh embedding pass — the cache must be current. Run
    ``python -m ctx.core.wiki.wiki_graphify`` first.
  - Cross-type by default (no within-type restriction).
  - Incremental via ``dedup-state.json`` next to ``embeddings.npz``:
    we persist ``{node_id: content_hash}`` plus the last
    ``verified_pairs`` so a follow-up run only re-checks pairs that
    involve a changed entity. The other 99%+ of pair comparisons are
    skipped.
  - Allowlist: ``.dedup-allowlist.txt`` in the repo root, one
    "<slug_a> <slug_b> # reason" per line. Pairs in the allowlist are
    suppressed from the report but still tracked in state.
  - Output: a markdown report at ``graph/dedup-report.md`` plus a
    machine-readable ``graph/dedup-report.json``. Exit code 1 when
    actionable findings exist; 0 otherwise. Pre-ship hooks gate on
    the exit code.

Wiring:
  - CLI: ``ctx-dedup-check``.
  - Pre-ship gate: invoke after graphify, before ``tar -czf`` of the
    wiki tarball. Documented in ``graph/README.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    import numpy as np

_logger = logging.getLogger(__name__)

DEDUP_STATE_FILENAME = "dedup-state.json"
DEDUP_STATE_VERSION = 1


# ── Data shapes ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EntityRef:
    """One indexed entity. ``node_id`` is canonicalised as ``type:slug``."""
    node_id: str            # "skill:tdd", "agent:foo", "mcp-server:bar"
    type: str               # "skill" | "agent" | "mcp-server"
    slug: str
    path: Path              # source markdown path
    description: str        # frontmatter description (first 250 chars)
    tags: tuple[str, ...]


@dataclass(frozen=True)
class DedupPair:
    """One similarity ≥ threshold pair surfaced by the gate."""
    a: EntityRef
    b: EntityRef
    similarity: float       # cosine in [threshold, 1.0]
    shared_tags: tuple[str, ...]
    reason: str = ""        # "auto: above threshold" | "allowlisted: <reason>"


@dataclass
class DedupReport:
    """Aggregate output of one run."""
    threshold: float
    model_id: str
    total_entities: int
    pairs_evaluated: int
    findings: list[DedupPair] = field(default_factory=list)
    allowlisted: list[DedupPair] = field(default_factory=list)
    skipped_unchanged: int = 0


# ── Allowlist ──────────────────────────────────────────────────────────


def load_allowlist(path: Path) -> set[tuple[str, str]]:
    """Parse a plain-text allowlist. Each line is ``slug_a slug_b [# reason]``.

    Slugs are canonicalised to lex-sorted (low, high) so order in the
    file doesn't matter.
    """
    if not path.is_file():
        return set()
    out: set[tuple[str, str]] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            _logger.warning(
                "dedup-allowlist: ignoring malformed line %r", raw,
            )
            continue
        a, b = parts[0].strip(), parts[1].strip()
        out.add(tuple(sorted([a, b])))
    return out


# ── State persistence ─────────────────────────────────────────────────


@dataclass
class DedupState:
    """On-disk state for incremental runs."""
    version: int
    model_id: str
    threshold: float
    entity_hashes: dict[str, str]  # node_id -> sha256(text)
    last_findings: list[dict]      # serialised DedupPair list

    @classmethod
    def empty(cls, *, model_id: str, threshold: float) -> "DedupState":
        return cls(
            version=DEDUP_STATE_VERSION,
            model_id=model_id,
            threshold=threshold,
            entity_hashes={},
            last_findings=[],
        )


def _state_path(cache_dir: Path) -> Path:
    return cache_dir / DEDUP_STATE_FILENAME


def load_state(cache_dir: Path, *, model_id: str, threshold: float) -> DedupState:
    """Load the prior state. Returns empty state when file missing,
    schema mismatched, or model/threshold changed (forces full re-check).
    """
    path = _state_path(cache_dir)
    if not path.is_file():
        return DedupState.empty(model_id=model_id, threshold=threshold)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning("dedup_check: state load failed (%s); fresh", exc)
        return DedupState.empty(model_id=model_id, threshold=threshold)
    if not isinstance(raw, dict) or raw.get("version") != DEDUP_STATE_VERSION:
        return DedupState.empty(model_id=model_id, threshold=threshold)
    if raw.get("model_id") != model_id:
        return DedupState.empty(model_id=model_id, threshold=threshold)
    # Threshold change: anything above the new (lower) threshold may now
    # be a new finding; below the old (higher) threshold is unaffected.
    # The simplest safe behavior is to re-check everything when the
    # threshold changes. Edge case: same threshold value with float
    # rounding noise, hence the epsilon.
    if abs(float(raw.get("threshold", 0.0)) - threshold) > 1e-9:
        return DedupState.empty(model_id=model_id, threshold=threshold)
    return DedupState(
        version=int(raw["version"]),
        model_id=str(raw["model_id"]),
        threshold=float(raw["threshold"]),
        entity_hashes=dict(raw.get("entity_hashes", {})),
        last_findings=list(raw.get("last_findings", [])),
    )


def save_state(cache_dir: Path, state: DedupState) -> None:
    """Atomically persist state."""
    from ctx.utils._fs_utils import atomic_write_text

    payload = {
        "version": state.version,
        "model_id": state.model_id,
        "threshold": state.threshold,
        "entity_hashes": state.entity_hashes,
        "last_findings": state.last_findings,
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(_state_path(cache_dir), json.dumps(payload, indent=2))


# ── Walk + hashing ────────────────────────────────────────────────────


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_frontmatter(path: Path) -> dict:
    """Tiny YAML-ish frontmatter parser (matches graphify's tolerance)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    try:
        end = text.index("\n---", 3)
    except ValueError:
        return {}
    block = text[3:end].strip()
    out: dict = {}
    pending: str | None = None
    pending_list: list[str] | None = None
    for raw in block.splitlines():
        if pending_list is not None and raw.startswith(("  -", "\t-")):
            v = raw.split("-", 1)[1].strip().strip('"').strip("'")
            if v:
                pending_list.append(v)
            continue
        if pending_list is not None:
            out[pending] = pending_list
            pending, pending_list = None, None
        if ":" not in raw:
            continue
        k, _, v = raw.partition(":")
        k = k.strip()
        v = v.strip()
        if not v:
            pending, pending_list = k, []
            continue
        out[k] = v.strip('"').strip("'")
    if pending_list is not None:
        out[pending] = pending_list
    return out


def discover_entities(wiki_dir: Path) -> list[EntityRef]:
    """Walk the wiki entities tree and return one ``EntityRef`` per page.

    Reads frontmatter to extract ``description`` + ``tags`` for use in
    the report. The returned list is sorted by ``node_id`` for
    deterministic output.
    """
    entities: list[EntityRef] = []
    type_dirs = {
        "skill": wiki_dir / "entities" / "skills",
        "agent": wiki_dir / "entities" / "agents",
        "mcp-server": wiki_dir / "entities" / "mcp-servers",
    }
    for entity_type, root in type_dirs.items():
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            slug = path.stem
            fm = _read_frontmatter(path)
            desc = fm.get("description", "")
            if isinstance(desc, list):
                desc = " ".join(str(x) for x in desc)
            desc = str(desc).strip()[:250]
            tags = fm.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            tags_t = tuple(str(t) for t in tags if t)
            entities.append(EntityRef(
                node_id=f"{entity_type}:{slug}",
                type=entity_type,
                slug=slug,
                path=path,
                description=desc,
                tags=tags_t,
            ))
    entities.sort(key=lambda e: e.node_id)
    return entities


# ── Embedding alignment ───────────────────────────────────────────────


def load_vectors(
    entities: list[EntityRef],
    cache_dir: Path,
) -> tuple[list[EntityRef], "np.ndarray", str]:
    """Match each entity to its cached embedding vector.

    Strategy: graphify persists ``{node_id: {content_hash, top_k}}`` in
    ``topk-state.json`` and ``{content_hash: vec}`` in ``embeddings.npz``.
    We look up each entity by its ``node_id`` to find its content_hash,
    then look up the vec by hash. This is guaranteed to use the same
    embedding text shape that graphify embedded, regardless of how that
    text was constructed. We never re-derive the text.

    Returns the subset of entities that have a vector + the matching
    matrix + the cache's recorded ``model_id``.
    """
    import numpy as np  # noqa: PLC0415

    npz_path = cache_dir / "embeddings.npz"
    state_path = cache_dir / "topk-state.json"
    if not npz_path.is_file():
        raise FileNotFoundError(
            f"Embeddings cache not found at {npz_path}. "
            "Run `python -m ctx.core.wiki.wiki_graphify` first."
        )
    if not state_path.is_file():
        raise FileNotFoundError(
            f"Top-K state not found at {state_path}. "
            "Run `python -m ctx.core.wiki.wiki_graphify` first."
        )

    state_raw = json.loads(state_path.read_text(encoding="utf-8"))
    state_nodes = state_raw.get("nodes", {})
    if not isinstance(state_nodes, dict):
        raise RuntimeError(f"Malformed topk-state.json at {state_path}")

    data = np.load(npz_path, allow_pickle=False)
    hashes = [
        h.decode("utf-8") if isinstance(h, bytes) else str(h)
        for h in data["hashes"]
    ]
    vecs = data["vecs"]
    model_arr = data["model"] if "model" in data.files else None
    model_id = ""
    if model_arr is not None and model_arr.size:
        model_id = (
            str(model_arr.item()) if model_arr.ndim == 0 else str(model_arr[0])
        )

    hash_to_idx = {h: i for i, h in enumerate(hashes)}

    matched: list[EntityRef] = []
    indices: list[int] = []
    misses = 0
    for e in entities:
        entry = state_nodes.get(e.node_id)
        if not isinstance(entry, dict):
            misses += 1
            continue
        ch = entry.get("content_hash")
        if not isinstance(ch, str):
            misses += 1
            continue
        idx = hash_to_idx.get(ch)
        if idx is None:
            misses += 1
            continue
        matched.append(e)
        indices.append(idx)

    if not matched:
        raise RuntimeError(
            "No entities matched a cached embedding by node_id. "
            "The cache is stale relative to the wiki — re-run graphify."
        )

    if misses:
        _logger.warning(
            "dedup_check: %d entities had no cached vector (skipping); "
            "they're missing from the embeddings cache. Re-run graphify "
            "to include them.",
            misses,
        )

    sub_vecs = vecs[indices]
    # L2-normalize so dot product == cosine
    norms = np.linalg.norm(sub_vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized = (sub_vecs / norms).astype("float32")
    return matched, normalized, model_id


# ── Pair finding ──────────────────────────────────────────────────────


def find_high_similarity_pairs(
    entities: list[EntityRef],
    vecs: "np.ndarray",
    *,
    threshold: float,
    chunk_size: int = 512,
) -> list[tuple[int, int, float]]:
    """Find every pair (i, j) with i < j and cosine ≥ threshold.

    Streamed in chunks so peak memory is O(chunk_size × N), not O(N²).
    For 13K entities × 384 dim, default chunk_size=512 → ~25MB peak.
    """
    import numpy as np  # noqa: PLC0415

    n = len(entities)
    out: list[tuple[int, int, float]] = []
    for chunk_start in range(0, n, chunk_size):
        chunk_end = min(chunk_start + chunk_size, n)
        chunk = vecs[chunk_start:chunk_end]
        # Score against the FULL vector set, not just downstream — we
        # need pairs (i, j) where i is in the chunk and j is anywhere.
        # We then dedupe by enforcing i < j to only emit each pair once.
        sims = chunk @ vecs.T  # (chunk_rows, N)
        for i_local in range(chunk_end - chunk_start):
            i_abs = chunk_start + i_local
            row = sims[i_local]
            # We only want j > i. Mask everything else.
            for j_abs in np.nonzero(row >= threshold)[0]:
                j_abs = int(j_abs)
                if j_abs <= i_abs:
                    continue
                score = float(row[j_abs])
                out.append((i_abs, j_abs, score))
    return out


# ── Orchestration ─────────────────────────────────────────────────────


def _entity_hash_for_state(entity: EntityRef) -> str:
    """Stable per-entity hash used for incremental change detection."""
    parts = [
        entity.node_id,
        entity.description,
        "|".join(sorted(entity.tags)),
    ]
    return _content_hash(" ".join(parts))


def run_dedup_check(
    *,
    wiki_dir: Path,
    cache_dir: Path,
    threshold: float = 0.85,
    allowlist_path: Path | None = None,
    incremental: bool = True,
) -> DedupReport:
    """End-to-end: discover → embed-match → find pairs → produce report.

    When ``incremental`` is True (default), prior state at
    ``<cache_dir>/dedup-state.json`` is consulted: pairs whose
    ``(a, b)`` are both unchanged since the last run are carried
    forward from the prior findings without recomputation. Pairs
    involving at least one changed/new entity are recomputed fresh.
    State is saved on success so the next run can be incremental too.

    The state file is invalidated automatically (full re-check) when
    the model_id or threshold changes, since those parameters change
    what counts as a finding.
    """
    entities = discover_entities(wiki_dir)
    if not entities:
        raise RuntimeError(f"No entities found under {wiki_dir / 'entities'}.")

    matched, vecs, model_id = load_vectors(entities, cache_dir)
    matched_by_id = {e.node_id: e for e in matched}

    # ── Incremental partition ─────────────────────────────────────────
    state = load_state(cache_dir, model_id=model_id, threshold=threshold)
    current_hashes = {e.node_id: _entity_hash_for_state(e) for e in matched}

    if incremental and state.entity_hashes:
        unchanged_ids = {
            nid for nid, h in current_hashes.items()
            if state.entity_hashes.get(nid) == h
        }
    else:
        unchanged_ids = set()

    # Carry-forward: keep prior findings whose BOTH endpoints are
    # unchanged AND still present (their similarity can't have moved).
    carried: list[DedupPair] = []
    if incremental and unchanged_ids and state.last_findings:
        for raw in state.last_findings:
            a_id = raw.get("a")
            b_id = raw.get("b")
            sim = raw.get("similarity")
            if (a_id in unchanged_ids and b_id in unchanged_ids
                    and a_id in matched_by_id and b_id in matched_by_id
                    and isinstance(sim, (int, float))
                    and sim >= threshold):
                a = matched_by_id[a_id]
                b = matched_by_id[b_id]
                shared = tuple(sorted(set(a.tags) & set(b.tags)))
                carried.append(DedupPair(
                    a=a, b=b, similarity=float(sim), shared_tags=shared,
                ))

    # Compute fresh pairs only for entities that changed or are new.
    # If incremental is off (or there's no usable state), the changed
    # set is "everyone" and we run the full pass — same as before.
    if incremental and unchanged_ids and len(unchanged_ids) < len(matched):
        changed_indices = [
            i for i, e in enumerate(matched)
            if e.node_id not in unchanged_ids
        ]
        raw_pairs = _find_pairs_for_changed(
            matched, vecs, changed_indices, threshold=threshold,
        )
        skipped_unchanged_pair_count = (
            len(matched) * (len(matched) - 1) // 2
            - len(changed_indices) * (len(matched) - len(changed_indices))
            - len(changed_indices) * (len(changed_indices) - 1) // 2
        )
    else:
        raw_pairs = find_high_similarity_pairs(matched, vecs, threshold=threshold)
        carried = []  # full re-check supersedes any prior carry-forward
        skipped_unchanged_pair_count = 0

    allowlist = load_allowlist(allowlist_path) if allowlist_path else set()

    fresh: list[DedupPair] = []
    allowlisted: list[DedupPair] = []
    for i, j, score in raw_pairs:
        a = matched[i]
        b = matched[j]
        slug_pair = tuple(sorted([a.slug, b.slug]))
        shared = tuple(sorted(set(a.tags) & set(b.tags)))
        pair = DedupPair(
            a=a, b=b, similarity=score, shared_tags=shared,
        )
        if slug_pair in allowlist:
            allowlisted.append(pair)
        else:
            fresh.append(pair)

    # Re-apply allowlist on carried pairs too (allowlist might have grown
    # since the prior run).
    for pair in carried:
        slug_pair = tuple(sorted([pair.a.slug, pair.b.slug]))
        if slug_pair in allowlist:
            allowlisted.append(pair)
        else:
            fresh.append(pair)

    findings = fresh
    findings.sort(key=lambda p: -p.similarity)
    allowlisted.sort(key=lambda p: -p.similarity)

    # Persist updated state for the next run.
    new_state = DedupState(
        version=DEDUP_STATE_VERSION,
        model_id=model_id,
        threshold=threshold,
        entity_hashes=current_hashes,
        last_findings=[
            {"a": p.a.node_id, "b": p.b.node_id, "similarity": p.similarity}
            for p in findings
        ],
    )
    try:
        save_state(cache_dir, new_state)
    except OSError as exc:
        _logger.warning("dedup_check: state save failed (%s)", exc)

    return DedupReport(
        threshold=threshold,
        model_id=model_id,
        total_entities=len(matched),
        pairs_evaluated=len(matched) * (len(matched) - 1) // 2,
        findings=findings,
        allowlisted=allowlisted,
        skipped_unchanged=skipped_unchanged_pair_count,
    )


def _find_pairs_for_changed(
    entities: list[EntityRef],
    vecs: "np.ndarray",
    changed_indices: list[int],
    *,
    threshold: float,
    chunk_size: int = 512,
) -> list[tuple[int, int, float]]:
    """Compute high-similarity pairs that involve at least one changed entity.

    Equivalent to: ``[(i, j, sim) for i,j,sim in all_pairs if i in changed
    or j in changed]``, but materialised efficiently — we only score
    rows in ``changed_indices`` against the full vector set, then sort
    pair endpoints into canonical (low, high) order to dedupe.
    """
    import numpy as np  # noqa: PLC0415

    if not changed_indices:
        return []
    changed_set = set(changed_indices)
    out_seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int, float]] = []
    for chunk_start in range(0, len(changed_indices), chunk_size):
        chunk_idx = changed_indices[chunk_start: chunk_start + chunk_size]
        chunk = vecs[chunk_idx]
        sims = chunk @ vecs.T
        for local_i, abs_i in enumerate(chunk_idx):
            row = sims[local_i]
            for j_abs in np.nonzero(row >= threshold)[0]:
                j_abs = int(j_abs)
                if j_abs == abs_i:
                    continue
                lo, hi = (abs_i, j_abs) if abs_i < j_abs else (j_abs, abs_i)
                # If the OTHER endpoint is also changed AND its abs index
                # is < ours, we'd double-emit when we get to that row.
                # Dedupe via the (lo, hi) key.
                if (lo, hi) in out_seen:
                    continue
                out_seen.add((lo, hi))
                out.append((lo, hi, float(row[j_abs])))
    return out


# ── Reporting ─────────────────────────────────────────────────────────


def render_markdown(report: DedupReport, *, top_n: int = 100) -> str:
    """Produce a human-review markdown report (capped at ``top_n`` findings).

    The JSON sidecar carries the full set; the markdown is for fast
    visual review and stays committable in the repo.
    """
    out: list[str] = []
    out.append("# Dedup Report")
    out.append("")
    out.append(f"- **Model**: `{report.model_id}`")
    out.append(f"- **Threshold**: cosine ≥ {report.threshold}")
    out.append(f"- **Entities indexed**: {report.total_entities:,}")
    out.append(f"- **Pairs evaluated**: {report.pairs_evaluated:,}")
    out.append(f"- **Findings (above threshold, not allowlisted)**: {len(report.findings):,}")
    out.append(f"- **Allowlisted pairs**: {len(report.allowlisted):,}")
    if len(report.findings) > top_n:
        out.append(
            f"- **Showing**: top {top_n} by similarity. Full set lives "
            f"in the JSON sidecar (`dedup-report.json`)."
        )
    # Distribution buckets help the reader gauge severity at a glance.
    if report.findings:
        b = {"≥0.99": 0, "0.95-0.99": 0, "0.90-0.95": 0, "0.85-0.90": 0}
        for p in report.findings:
            s = p.similarity
            if s >= 0.99:
                b["≥0.99"] += 1
            elif s >= 0.95:
                b["0.95-0.99"] += 1
            elif s >= 0.90:
                b["0.90-0.95"] += 1
            else:
                b["0.85-0.90"] += 1
        out.append("- **By similarity bucket**: " + ", ".join(
            f"{k} → {v:,}" for k, v in b.items() if v
        ))
        # Type-pair distribution
        tp: dict[str, int] = {}
        for p in report.findings:
            key = " ↔ ".join(sorted([p.a.type, p.b.type]))
            tp[key] = tp.get(key, 0) + 1
        out.append("- **By type pair**: " + ", ".join(
            f"{k} → {v:,}" for k, v in sorted(tp.items(), key=lambda x: -x[1])
        ))
    out.append("")
    if not report.findings:
        out.append("✓ No actionable findings. Catalog is clean at this threshold.")
        out.append("")
    else:
        out.append("## Findings (review required)")
        out.append("")
        out.append(
            "Each pair below has cosine similarity at or above the "
            "threshold. **Do not auto-drop.** The intent is human review: "
            "either confirm both entries are legitimately distinct (and "
            "add to `.dedup-allowlist.txt`), or merge/remove one with a "
            "PR that explains why."
        )
        out.append("")
        for pair in report.findings[:top_n]:
            out.append(f"### {pair.a.slug}  ↔  {pair.b.slug}  ({pair.similarity:.3f})")
            out.append("")
            out.append(f"- **Types**: {pair.a.type} ↔ {pair.b.type}")
            shared = ", ".join(pair.shared_tags) if pair.shared_tags else "(none)"
            out.append(f"- **Shared tags**: {shared}")
            a_only = ", ".join(t for t in pair.a.tags if t not in pair.shared_tags) or "(none)"
            b_only = ", ".join(t for t in pair.b.tags if t not in pair.shared_tags) or "(none)"
            out.append(f"- **{pair.a.slug} unique tags**: {a_only}")
            out.append(f"- **{pair.b.slug} unique tags**: {b_only}")
            out.append(f"- **{pair.a.slug}** path: `{pair.a.path}`")
            out.append(f"  - desc: {pair.a.description or '(none)'}")
            out.append(f"- **{pair.b.slug}** path: `{pair.b.path}`")
            out.append(f"  - desc: {pair.b.description or '(none)'}")
            out.append("")
    if report.allowlisted:
        out.append("## Allowlisted (not actioned, kept for audit)")
        out.append("")
        for p in report.allowlisted[:top_n]:
            out.append(f"- {p.a.slug} ↔ {p.b.slug}  ({p.similarity:.3f})")
        if len(report.allowlisted) > top_n:
            out.append(f"- … and {len(report.allowlisted) - top_n:,} more (see JSON sidecar)")
        out.append("")
    return "\n".join(out)


def render_json(report: DedupReport) -> str:
    """Machine-readable counterpart to ``render_markdown``."""
    payload = {
        "model_id": report.model_id,
        "threshold": report.threshold,
        "total_entities": report.total_entities,
        "pairs_evaluated": report.pairs_evaluated,
        "findings": [
            {
                "a": p.a.node_id, "b": p.b.node_id,
                "similarity": p.similarity,
                "a_type": p.a.type, "b_type": p.b.type,
                "shared_tags": list(p.shared_tags),
                "a_path": str(p.a.path),
                "b_path": str(p.b.path),
            }
            for p in report.findings
        ],
        "allowlisted_count": len(report.allowlisted),
    }
    return json.dumps(payload, indent=2)


# ── CLI ───────────────────────────────────────────────────────────────


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pre-ship dedup gate: flag entity pairs (any types) at or "
            "above the given cosine similarity threshold."
        ),
    )
    parser.add_argument(
        "--threshold", type=float, default=0.85,
        help="Cosine similarity threshold in (0, 1) (default: 0.85)",
    )
    parser.add_argument(
        "--wiki", type=Path,
        default=Path.home() / ".claude" / "skill-wiki",
        help="Wiki directory (default: ~/.claude/skill-wiki)",
    )
    parser.add_argument(
        "--cache", type=Path,
        default=None,
        help="Embedding cache dir (default: <wiki>/.embedding-cache/graph)",
    )
    parser.add_argument(
        "--report", type=Path, default=Path("graph") / "dedup-report.md",
        help="Markdown report output path (default: graph/dedup-report.md)",
    )
    parser.add_argument(
        "--report-json", type=Path, default=Path("graph") / "dedup-report.json",
        help="JSON report output path (default: graph/dedup-report.json)",
    )
    parser.add_argument(
        "--allowlist", type=Path,
        default=Path(".dedup-allowlist.txt"),
        help="Allowlist file (default: ./.dedup-allowlist.txt)",
    )
    parser.add_argument(
        "--exit-on-findings", action="store_true",
        help="Exit with non-zero if any actionable findings exist (CI gate).",
    )
    parser.add_argument(
        "--full", dest="incremental", action="store_false",
        help=(
            "Force full pairwise re-check (ignore prior dedup-state). "
            "Use after large catalog changes or when changing what counts "
            "as 'similar'. Default: incremental."
        ),
    )
    parser.set_defaults(incremental=True)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not (0.0 < args.threshold < 1.0):
        parser.error("--threshold must be strictly in (0, 1)")

    cache_dir = args.cache or (args.wiki / ".embedding-cache" / "graph")

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    report = run_dedup_check(
        wiki_dir=args.wiki,
        cache_dir=cache_dir,
        threshold=args.threshold,
        allowlist_path=args.allowlist,
        incremental=args.incremental,
    )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_markdown(report), encoding="utf-8")
    json_text = render_json(report)
    # The full JSON sidecar can balloon to 5+ MB when there are 10K+
    # findings (mostly MCP-MCP near-dups). Gzip-compress it on disk so
    # the artifact stays git-friendly while still being machine-readable.
    if args.report_json.suffix == ".gz" or len(json_text) > 1 * 1024 * 1024:
        import gzip  # noqa: PLC0415
        gz_path = args.report_json if args.report_json.suffix == ".gz" \
            else args.report_json.with_suffix(args.report_json.suffix + ".gz")
        with gzip.open(gz_path, "wt", encoding="utf-8") as f:
            f.write(json_text)
        if gz_path != args.report_json and args.report_json.exists():
            args.report_json.unlink()
        report_json_actual = gz_path
    else:
        args.report_json.write_text(json_text, encoding="utf-8")
        report_json_actual = args.report_json

    print(
        f"dedup_check: {len(report.findings):,} findings | "
        f"{len(report.allowlisted):,} allowlisted | "
        f"threshold={report.threshold} | "
        f"entities={report.total_entities:,} | "
        f"md={args.report} | json={report_json_actual}",
        flush=True,
    )

    if args.exit_on_findings and report.findings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
