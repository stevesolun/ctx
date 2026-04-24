#!/usr/bin/env python3
"""
mcp_quality.py -- Quality orchestrator and three-sink persistence for MCP servers.

Phase 4 of the MCP integration plan.

Flow:

  1. ``extract_signals_for_slug`` resolves the entity .md on disk, parses
     its frontmatter, looks up graph degrees from a pre-built index, and
     calls all six signal functions from ``mcp_quality_signals``.
  2. ``compute_quality`` aggregates those ``SignalResult`` instances via a
     weighted sum and maps the result to an A/B/C/D grade.
  3. ``persist_quality`` mirrors the result to three on-disk sinks so every
     downstream consumer — Obsidian, machine-readable automations, the wiki
     UI — sees the same number.

Persistence sinks:

  - Sidecar JSON — ``~/.claude/skill-quality/mcp/<slug>.json`` (canonical
    machine-readable form; the ``mcp/`` subdirectory keeps MCP scores
    isolated from skill scores so ``wiki_graphify``'s existing
    ``_attach_quality_attrs`` does not pick them up under ``skill:`` node IDs).
  - Frontmatter — ``quality_score``, ``quality_grade``, ``quality_updated_at``
    keys on the wiki entity page.
  - Wiki body — a ``## Quality`` section rendered as a Markdown table,
    between ``<!-- quality:begin -->`` and ``<!-- quality:end -->`` markers.

CLI verbs:

  - ``recompute`` — recompute one slug (--slug) or every MCP entity (--all).
  - ``show``      — print the current sidecar JSON for a slug.
  - ``explain``   — print signal breakdown + evidence for a slug.
  - ``list``      — print every MCP slug with its grade, tab-separated.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ctx.utils._fs_utils import atomic_write_text as _atomic_write
from mcp_entity import MCP_SLUG_RE, McpRecord
from quality_signals import SignalResult
from wiki_utils import parse_frontmatter_and_body

_logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Config defaults
# ────────────────────────────────────────────────────────────────────

_DEFAULT_MCP_WEIGHTS: dict[str, float] = {
    "popularity": 0.30,
    "freshness":  0.20,
    "structural": 0.15,
    "graph":      0.15,
    "trust":      0.10,
    "runtime":    0.10,
}  # sums to 1.0

_MCP_WEIGHT_KEYS: frozenset[str] = frozenset(_DEFAULT_MCP_WEIGHTS)

_DEFAULT_MCP_GRADE_THRESHOLDS: dict[str, float] = {
    "A": 0.80,
    "B": 0.60,
    "C": 0.40,
}

# Graph node-ID prefix used by wiki_graphify for MCP-server nodes.
_MCP_NODE_PREFIX = "mcp-server:"


# ────────────────────────────────────────────────────────────────────
# Config dataclass
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class McpQualityConfig:
    """All tunable knobs for the MCP quality scorer. Frozen — safe to share."""

    weights: Mapping[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_MCP_WEIGHTS)
    )
    grade_thresholds: Mapping[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_MCP_GRADE_THRESHOLDS)
    )
    star_saturation: int = 1000
    freshness_half_life_days: float = 90.0
    graph_degree_saturation: int = 20

    def __post_init__(self) -> None:
        # --- weight vector ---
        if set(self.weights) != _MCP_WEIGHT_KEYS:
            raise ValueError(
                f"weights must supply exactly: {sorted(_MCP_WEIGHT_KEYS)}"
            )
        total = sum(self.weights.values())
        if not 0.99 <= total <= 1.01:
            raise ValueError(f"weights must sum to 1.0; got {total:.4f}")
        for k, v in self.weights.items():
            if v < 0:
                raise ValueError(f"weight for {k!r} must be >= 0, got {v}")

        # --- grade thresholds ---
        if set(self.grade_thresholds) != {"A", "B", "C"}:
            raise ValueError("grade_thresholds must supply A, B, C cutoffs")
        a = self.grade_thresholds["A"]
        b = self.grade_thresholds["B"]
        c = self.grade_thresholds["C"]
        if not 0.0 <= c <= b <= a <= 1.0:
            raise ValueError(
                "grade thresholds must satisfy 0 <= C <= B <= A <= 1"
            )

        # --- numeric bounds ---
        if self.star_saturation <= 0:
            raise ValueError("star_saturation must be > 0")
        if self.freshness_half_life_days <= 0:
            raise ValueError("freshness_half_life_days must be > 0")
        if self.graph_degree_saturation <= 0:
            raise ValueError("graph_degree_saturation must be > 0")


# ────────────────────────────────────────────────────────────────────
# Result dataclass
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class McpQualityScore:
    """One MCP server's quality score snapshot — frozen for safe sharing."""

    slug: str
    raw_score: float                    # weighted sum before clamping
    score: float                        # final, clamped to [0, 1]
    grade: str                          # A / B / C / D
    signals: Mapping[str, SignalResult]
    weights: Mapping[str, float]
    computed_at: str                    # ISO-8601 UTC

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for JSON persistence."""
        return {
            "slug": self.slug,
            "raw_score": round(self.raw_score, 4),
            "score": round(self.score, 4),
            "grade": self.grade,
            "signals": {
                name: {
                    "score": round(sig.score, 4),
                    "evidence": dict(sig.evidence),
                }
                for name, sig in self.signals.items()
            },
            "weights": dict(self.weights),
            "computed_at": self.computed_at,
        }


# ────────────────────────────────────────────────────────────────────
# Slug safety
# ────────────────────────────────────────────────────────────────────


def _ensure_safe_slug(slug: str) -> str:
    """Reject slugs that do not match the MCP Tier-2 contract.

    MCP slugs are stricter than skill slugs: lowercase, hyphens only,
    no consecutive hyphens, no leading/trailing hyphens.
    """
    if not isinstance(slug, str) or not MCP_SLUG_RE.match(slug):
        raise ValueError(f"invalid MCP slug: {slug!r}")
    return slug


# ────────────────────────────────────────────────────────────────────
# Utility
# ────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_F_FLOOR: float = 0.20


def _grade_from_score(score: float, thresholds: Mapping[str, float]) -> str:
    """Map a numeric score to a letter grade A / B / C / D / F.

    F is reserved for very low scores (below ``_F_FLOOR = 0.20``) to
    distinguish broken or empty entries from merely low-quality ones.
    Skills use F only on hard-floor failures (intake_fail) but MCP
    quality has no hard floors today, so the F band is purely score-
    driven. A future hard-floor mechanism (e.g. dead-link detection
    in Phase 6) can override this band.
    """
    if score >= thresholds["A"]:
        return "A"
    if score >= thresholds["B"]:
        return "B"
    if score >= thresholds["C"]:
        return "C"
    if score >= _F_FLOOR:
        return "D"
    return "F"


# ────────────────────────────────────────────────────────────────────
# Pure scoring
# ────────────────────────────────────────────────────────────────────


def compute_quality(
    *,
    slug: str,
    signals: Mapping[str, SignalResult],
    config: McpQualityConfig | None = None,
    computed_at: str | None = None,
) -> McpQualityScore:
    """Aggregate six MCP signals into a weighted score and letter grade.

    Args:
        slug: MCP server slug (validated against MCP_SLUG_RE).
        signals: Mapping of signal name → SignalResult. Must contain
            exactly the six MCP signal names.
        config: Optional scorer config; defaults to ``McpQualityConfig()``.
        computed_at: Optional ISO-8601 timestamp; defaults to now (UTC).

    Returns:
        Frozen ``McpQualityScore``.

    Raises:
        ValueError: If ``slug`` is invalid or ``signals`` keys don't
            match the six MCP signal names.
    """
    _ensure_safe_slug(slug)
    cfg = config or McpQualityConfig()

    if set(signals) != _MCP_WEIGHT_KEYS:
        missing = _MCP_WEIGHT_KEYS - set(signals)
        extra = set(signals) - _MCP_WEIGHT_KEYS
        raise ValueError(
            f"signals keys mismatch: missing={sorted(missing)}, extra={sorted(extra)}"
        )

    raw = sum(cfg.weights[name] * signals[name].score for name in _MCP_WEIGHT_KEYS)
    score = max(0.0, min(1.0, raw))
    grade = _grade_from_score(score, cfg.grade_thresholds)

    return McpQualityScore(
        slug=slug,
        raw_score=raw,
        score=score,
        grade=grade,
        signals=dict(signals),
        weights=dict(cfg.weights),
        computed_at=computed_at or _now_iso(),
    )


# ────────────────────────────────────────────────────────────────────
# Entity path resolution
# ────────────────────────────────────────────────────────────────────


def _resolve_mcp_entity_path(slug: str, wiki_dir: Path) -> Path:
    """Find ``<wiki>/entities/mcp-servers/<shard>/<slug>.md``.

    Shard is ``slug[0]`` for alphabetic slugs or ``'0-9'`` for
    digit-leading slugs — mirrors ``McpRecord.entity_relpath`` logic.
    """
    _ensure_safe_slug(slug)
    first = slug[0]
    shard = "0-9" if first.isdigit() else first
    return wiki_dir / "entities" / "mcp-servers" / shard / f"{slug}.md"


def _read_mcp_entity(
    slug: str, wiki_dir: Path
) -> tuple[McpRecord, dict[str, Any]]:
    """Read entity .md, parse frontmatter, reconstruct McpRecord.

    Args:
        slug: MCP server slug.
        wiki_dir: Root of the wiki (contains the ``entities/`` tree).

    Returns:
        ``(record, frontmatter_dict)`` where ``record`` is an
        ``McpRecord`` reconstructed from frontmatter fields.

    Raises:
        FileNotFoundError: If the entity page does not exist.
        ValueError: If the frontmatter cannot produce a valid McpRecord.
    """
    path = _resolve_mcp_entity_path(slug, wiki_dir)
    if not path.is_file():
        raise FileNotFoundError(
            f"MCP entity not found: {path}"
        )
    raw = path.read_text(encoding="utf-8", errors="replace")
    fm, _body = parse_frontmatter_and_body(raw)
    # McpRecord.from_dict is tolerant of missing optional fields.
    record = McpRecord.from_dict({**fm, "slug": slug})
    return record, fm


# ────────────────────────────────────────────────────────────────────
# Graph index
# ────────────────────────────────────────────────────────────────────


def load_graph_index(wiki_dir: Path) -> dict[str, dict[str, Any]]:
    """Load ``<wiki>/graphify-out/graph.json`` and build a degree index.

    Returns a mapping of ``{node_id: {"degree": int, "cross_type_degree": int}}``.
    Cross-type degree counts neighbours whose ``node_id`` starts with a
    different type prefix (e.g. ``skill:`` or ``agent:`` vs ``mcp-server:``).
    Returns an empty dict if the file is missing or malformed.
    """
    graph_path = wiki_dir / "graphify-out" / "graph.json"
    if not graph_path.is_file():
        return {}
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _logger.warning("load_graph_index: could not parse %s", graph_path)
        return {}

    if not isinstance(data, dict) or "nodes" not in data:
        return {}

    # Build neighbour lists from links/edges.
    edge_key = "links" if "links" in data else "edges"
    raw_edges = data.get(edge_key) or []

    # adjacency: node_id -> set of neighbour node_ids
    adjacency: dict[str, set[str]] = {}
    for node in data.get("nodes", []):
        nid = node.get("id")
        if isinstance(nid, str):
            adjacency[nid] = set()

    for edge in raw_edges:
        if not isinstance(edge, dict):
            continue
        src = edge.get("source") or edge.get("from")
        tgt = edge.get("target") or edge.get("to")
        if isinstance(src, str) and isinstance(tgt, str):
            adjacency.setdefault(src, set()).add(tgt)
            adjacency.setdefault(tgt, set()).add(src)

    index: dict[str, dict[str, Any]] = {}
    for node_id, neighbours in adjacency.items():
        # Derive this node's type prefix (e.g. "skill", "mcp-server").
        node_prefix = node_id.split(":")[0] if ":" in node_id else ""
        cross_type = sum(
            1
            for nb in neighbours
            if (nb.split(":")[0] if ":" in nb else "") != node_prefix
        )
        index[node_id] = {
            "degree": len(neighbours),
            "cross_type_degree": cross_type,
        }
    return index


# ────────────────────────────────────────────────────────────────────
# Age computation helper
# ────────────────────────────────────────────────────────────────────


def _commit_age_days(last_commit_at: str | None) -> float | None:
    """Parse an ISO-8601 timestamp and return age in days from now (UTC).

    Returns ``None`` if the input is ``None`` or unparseable.
    """
    if last_commit_at is None:
        return None
    try:
        parsed = datetime.fromisoformat(last_commit_at)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - parsed
    return max(0.0, delta.total_seconds() / 86400.0)


# ────────────────────────────────────────────────────────────────────
# Signal extraction
# ────────────────────────────────────────────────────────────────────


def extract_signals_for_slug(
    slug: str,
    *,
    wiki_dir: Path,
    config: McpQualityConfig | None = None,
    graph_index: Mapping[str, dict[str, Any]] | None = None,
) -> Mapping[str, SignalResult]:
    """Read entity, compute graph degrees, call all six signal functions.

    Args:
        slug: MCP server slug.
        wiki_dir: Root of the wiki.
        config: Optional scorer config; defaults to ``McpQualityConfig()``.
        graph_index: Optional pre-loaded ``{node_id: {"degree": int,
            "cross_type_degree": int}}`` dict. When ``None``, graph
            signals receive degree=0 (isolated). Callers processing many
            slugs should pre-load via ``load_graph_index()`` and pass it
            in once.

    Returns:
        Dict keyed by the six signal names.

    Raises:
        FileNotFoundError: If the entity page is missing.
        ImportError: If ``mcp_quality_signals`` is not yet installed.
    """
    from mcp_quality_signals import (  # deferred: sibling module may not exist yet
        freshness_signal,
        graph_signal,
        popularity_signal,
        runtime_signal,
        structural_signal,
        trust_signal,
    )

    _ensure_safe_slug(slug)
    cfg = config or McpQualityConfig()

    record, fm = _read_mcp_entity(slug, wiki_dir)

    # Graph degrees.
    node_id = f"{_MCP_NODE_PREFIX}{slug}"
    if graph_index is not None:
        node_data = graph_index.get(node_id, {})
        degree = int(node_data.get("degree", 0))
        cross_type_degree = int(node_data.get("cross_type_degree", 0))
    else:
        degree = 0
        cross_type_degree = 0

    # Runtime data from frontmatter (enriched by the runtime-tracker later).
    invocation_count = int(fm.get("invocation_count") or 0)
    error_count = int(fm.get("error_count") or 0)
    last_invoked_raw = fm.get("last_invoked_at")
    last_invoked_age: float | None = _commit_age_days(
        last_invoked_raw if isinstance(last_invoked_raw, str) else None
    )

    pop = popularity_signal(
        stars=record.stars,
        star_saturation=cfg.star_saturation,
    )
    fresh = freshness_signal(
        last_commit_age_days=_commit_age_days(record.last_commit_at),
        half_life_days=cfg.freshness_half_life_days,
    )
    struct = structural_signal(record=record)
    graph = graph_signal(
        degree=degree,
        cross_type_degree=cross_type_degree,
        degree_saturation=cfg.graph_degree_saturation,
    )
    trust = trust_signal(record=record)
    runtime = runtime_signal(
        invocation_count=invocation_count,
        error_count=error_count,
        last_invoked_age_days=last_invoked_age,
    )

    return {
        "popularity": pop,
        "freshness": fresh,
        "structural": struct,
        "graph": graph,
        "trust": trust,
        "runtime": runtime,
    }


# ────────────────────────────────────────────────────────────────────
# Paths
# ────────────────────────────────────────────────────────────────────


def default_sidecar_dir() -> Path:
    """Return the directory where per-slug MCP quality JSONs land.

    Resolution order:
      1. ``mcp_quality.paths.sidecar_dir`` from ``ctx_config.cfg``
         — the configured path. Writing ``~`` at the start is expanded
         via ``Path.home()`` so tests that monkeypatch ``Path.home``
         redirect writes into tmp_path. Without this,
         ``os.path.expanduser`` would consult ``$HOME`` / ``%USERPROFILE%``
         directly and bypass the monkeypatch.
      2. Fallback: ``Path.home() / .claude / skill-quality / mcp``.

    The configured path points at the parent ``skill-quality/`` dir;
    we append ``mcp/`` to keep MCP scores in their own subtree
    alongside skill+agent scores.
    """
    try:
        from ctx_config import cfg  # local import — avoid cost on test import
        raw = cfg.get("mcp_quality", {}) or {}
        paths = raw.get("paths", {}) if isinstance(raw, dict) else {}
        configured = paths.get("sidecar_dir") if isinstance(paths, dict) else None
        if isinstance(configured, str) and configured.strip():
            # Honor Path.home() monkeypatching for tests. os.path.
            # expanduser would shortcut through the env and miss it.
            expanded = configured
            if expanded.startswith("~"):
                expanded = str(Path.home()) + expanded[1:]
            return Path(expanded)
    except Exception:  # noqa: BLE001 — config unavailable in some test contexts
        pass
    return Path.home() / ".claude" / "skill-quality" / "mcp"


def sidecar_path(slug: str, *, sidecar_dir: Path | None = None) -> Path:
    """Return the sidecar JSON path for *slug*."""
    _ensure_safe_slug(slug)
    root = sidecar_dir if sidecar_dir is not None else default_sidecar_dir()
    return root / f"{slug}.json"


# ────────────────────────────────────────────────────────────────────
# Persistence (three sinks)
# ────────────────────────────────────────────────────────────────────

_QUALITY_SECTION_HEADER = "## Quality"
_QUALITY_SECTION_BEGIN = "<!-- quality:begin -->"
_QUALITY_SECTION_END = "<!-- quality:end -->"

_QUALITY_BLOCK_RE = re.compile(
    re.escape(_QUALITY_SECTION_BEGIN) + r".*?" + re.escape(_QUALITY_SECTION_END),
    re.DOTALL,
)

_SIGNAL_ORDER = ("popularity", "freshness", "structural", "graph", "trust", "runtime")


def _render_quality_section(score: McpQualityScore) -> str:
    """Build the ``## Quality`` Markdown block for injection into the entity page."""
    lines: list[str] = [
        _QUALITY_SECTION_BEGIN,
        _QUALITY_SECTION_HEADER,
        "",
        f"- **Grade:** {score.grade}",
        f"- **Score:** {score.score:.2f} (raw {score.raw_score:.2f})",
        f"- **Computed:** {score.computed_at}",
        "",
        "| Signal | Score | Weight |",
        "| --- | --- | --- |",
    ]
    for name in _SIGNAL_ORDER:
        sig = score.signals.get(name)
        w = score.weights.get(name, 0.0)
        if sig is not None:
            lines.append(f"| {name} | {sig.score:.2f} | {w:.2f} |")
    lines.append("")
    lines.append(_QUALITY_SECTION_END)
    return "\n".join(lines)


def _inject_quality_section(body: str, block: str) -> str:
    """Replace any existing quality block or append at the end."""
    if _QUALITY_BLOCK_RE.search(body):
        return _QUALITY_BLOCK_RE.sub(block, body, count=1)
    sep = "" if body.endswith("\n") else "\n"
    return body + sep + "\n" + block + "\n"


def _update_frontmatter_quality(raw_md: str, score: McpQualityScore) -> str:
    """Update ``quality_*`` keys in frontmatter; preserve all other keys.

    Surgical edit: only rewrites the three quality lines to keep diffs
    minimal on every recompute.
    """
    if not raw_md.startswith("---"):
        return raw_md
    end_idx = raw_md.find("\n---", 3)
    if end_idx == -1:
        return raw_md
    fm_block = raw_md[3 : end_idx + 1]
    after_fm = raw_md[end_idx + 4 :]

    pairs: list[str] = [
        f"quality_score: {score.score:.4f}",
        f"quality_grade: {score.grade}",
        f"quality_updated_at: {score.computed_at}",
    ]

    lines = fm_block.splitlines()
    kept: list[str] = [
        ln for ln in lines
        if not ln.lstrip().startswith(
            ("quality_score:", "quality_grade:", "quality_updated_at:")
        )
    ]
    while kept and not kept[-1].strip():
        kept.pop()
    new_fm = "\n".join(kept + pairs)
    return "---" + "\n" + new_fm + "\n---" + after_fm


def persist_quality(
    score: McpQualityScore,
    *,
    wiki_dir: Path,
    sidecar_dir: Path | None = None,
    update_frontmatter: bool = True,
) -> dict[str, Path]:
    """Write the quality result to the three on-disk sinks atomically.

    Sinks:
      1. Sidecar JSON at ``~/.claude/skill-quality/mcp/<slug>.json``.
      2. Frontmatter ``quality_*`` keys on the entity .md page.
      3. Body ``## Quality`` section between marker comments.

    Returns:
        Mapping of sink-name → ``Path`` that was written.
    """
    written: dict[str, Path] = {}

    # Sink 1 — sidecar JSON.
    sc_path = sidecar_path(score.slug, sidecar_dir=sidecar_dir)
    _atomic_write(
        sc_path,
        json.dumps(score.to_dict(), indent=2, sort_keys=True, ensure_ascii=False),
    )
    written["sidecar"] = sc_path

    if not update_frontmatter:
        return written

    # Sinks 2 + 3 — entity .md (frontmatter + body).
    entity_path = _resolve_mcp_entity_path(score.slug, wiki_dir)
    if not entity_path.is_file():
        _logger.info(
            "mcp_quality: no entity page at %s; frontmatter/body sinks skipped",
            entity_path,
        )
        return written

    raw = entity_path.read_text(encoding="utf-8", errors="replace")

    # Sink 2 — frontmatter.
    updated = _update_frontmatter_quality(raw, score)

    # Sink 3 — body block.
    # Split at the frontmatter boundary then operate only on the body.
    if updated.startswith("---"):
        fm_end = updated.find("\n---", 3)
        if fm_end != -1:
            header = updated[: fm_end + 4]
            body = updated[fm_end + 4 :]
            new_body = _inject_quality_section(body, _render_quality_section(score))
            updated = header + new_body

    _atomic_write(entity_path, updated)
    written["frontmatter"] = entity_path
    written["wiki_body"] = entity_path

    return written


# ────────────────────────────────────────────────────────────────────
# Load back from sidecar
# ────────────────────────────────────────────────────────────────────


def load_quality(
    slug: str, *, sidecar_dir: Path | None = None
) -> McpQualityScore | None:
    """Read a previously-persisted ``McpQualityScore`` from disk.

    Returns ``None`` if no sidecar exists. Partial/corrupt files raise
    ``json.JSONDecodeError`` or ``ValueError`` — caller decides whether
    to skip or recompute.
    """
    path = sidecar_path(slug, sidecar_dir=sidecar_dir)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    signals: dict[str, SignalResult] = {}
    for name, payload in data.get("signals", {}).items():
        signals[name] = SignalResult(
            score=float(payload.get("score", 0.0)),
            evidence=dict(payload.get("evidence", {})),
        )
    return McpQualityScore(
        slug=data["slug"],
        raw_score=float(data.get("raw_score", 0.0)),
        score=float(data.get("score", 0.0)),
        grade=data.get("grade", "D"),
        signals=signals,
        weights=dict(data.get("weights", {})),
        computed_at=data.get("computed_at", ""),
    )


# ────────────────────────────────────────────────────────────────────
# High-level orchestration
# ────────────────────────────────────────────────────────────────────


def recompute_slug(
    slug: str,
    *,
    wiki_dir: Path,
    config: McpQualityConfig | None = None,
    graph_index: Mapping[str, dict[str, Any]] | None = None,
    sidecar_dir: Path | None = None,
    update_frontmatter: bool = True,
) -> McpQualityScore:
    """End-to-end recompute: extract signals → compute → persist."""
    signals = extract_signals_for_slug(
        slug,
        wiki_dir=wiki_dir,
        config=config,
        graph_index=graph_index,
    )
    score = compute_quality(
        slug=slug,
        signals=signals,
        config=config,
        computed_at=_now_iso(),
    )
    persist_quality(
        score,
        wiki_dir=wiki_dir,
        sidecar_dir=sidecar_dir,
        update_frontmatter=update_frontmatter,
    )
    return score


def discover_mcp_slugs(wiki_dir: Path) -> list[str]:
    """Enumerate every MCP server slug in the wiki entity tree.

    Walks ``<wiki>/entities/mcp-servers/`` shards, collecting ``*.md``
    stems that pass ``MCP_SLUG_RE``. Returns sorted list.
    """
    mcp_root = wiki_dir / "entities" / "mcp-servers"
    if not mcp_root.is_dir():
        return []
    slugs: list[str] = []
    for shard_dir in sorted(mcp_root.iterdir()):
        if not shard_dir.is_dir():
            continue
        for entry in sorted(shard_dir.glob("*.md")):
            slug = entry.stem
            if MCP_SLUG_RE.match(slug):
                slugs.append(slug)
    return slugs


def recompute_all(
    *,
    wiki_dir: Path,
    config: McpQualityConfig | None = None,
    sidecar_dir: Path | None = None,
    update_frontmatter: bool = True,
) -> tuple[list[McpQualityScore], list[tuple[str, Exception]]]:
    """Recompute every MCP entity in the wiki, loading the graph index once.

    Returns:
        ``(successes, failures)`` where failures is a list of
        ``(slug, exception)`` pairs.
    """
    slugs = discover_mcp_slugs(wiki_dir)
    graph_index = load_graph_index(wiki_dir)

    successes: list[McpQualityScore] = []
    failures: list[tuple[str, Exception]] = []
    for slug in slugs:
        try:
            score = recompute_slug(
                slug,
                wiki_dir=wiki_dir,
                config=config,
                graph_index=graph_index,
                sidecar_dir=sidecar_dir,
                update_frontmatter=update_frontmatter,
            )
            successes.append(score)
        except (FileNotFoundError, ValueError, OSError, ImportError) as exc:
            failures.append((slug, exc))
    return successes, failures


# ────────────────────────────────────────────────────────────────────
# CLI helpers
# ────────────────────────────────────────────────────────────────────


def _wiki_dir_from_config() -> Path:
    """Return wiki_dir from ctx_config.cfg or a sensible fallback.

    Uses ``Path.home()`` rather than ``os.path.expanduser("~")`` so
    tests can monkeypatch ``Path.home`` for isolation.
    """
    try:
        from ctx_config import cfg  # local import: avoid cost on unit-test import
        return cfg.wiki_dir
    except Exception:  # noqa: BLE001
        return Path.home() / ".claude" / "skill-wiki"


def _resolve_wiki_dir(args: argparse.Namespace) -> Path:
    """CLI helper: --wiki-dir override on parent parser wins over config."""
    explicit = getattr(args, "wiki_dir", None)
    if explicit is not None:
        return Path(explicit)
    return _wiki_dir_from_config()


def _config_from_cfg() -> McpQualityConfig:
    """Build McpQualityConfig from ctx_config.cfg's mcp_quality block."""
    try:
        from ctx_config import cfg
        raw = cfg.get("mcp_quality", {}) or {}
    except Exception:  # noqa: BLE001
        return McpQualityConfig()
    if not isinstance(raw, dict):
        return McpQualityConfig()

    kwargs: dict[str, Any] = {}
    weights = raw.get("weights")
    thresholds = raw.get("grade_thresholds")
    if isinstance(weights, dict) and weights:
        kwargs["weights"] = {k: float(v) for k, v in weights.items()}
    if isinstance(thresholds, dict) and thresholds:
        kwargs["grade_thresholds"] = {k: float(v) for k, v in thresholds.items()}
    for key in ("star_saturation", "graph_degree_saturation"):
        val = raw.get(key)
        if isinstance(val, int) and val > 0:
            kwargs[key] = val
    half_life = raw.get("freshness_half_life_days")
    if isinstance(half_life, (int, float)) and half_life > 0:
        kwargs["freshness_half_life_days"] = float(half_life)

    try:
        return McpQualityConfig(**kwargs)
    except ValueError:
        _logger.warning("mcp_quality: invalid config block; using defaults")
        return McpQualityConfig()


# ────────────────────────────────────────────────────────────────────
# CLI command handlers
# ────────────────────────────────────────────────────────────────────


def cmd_recompute(args: argparse.Namespace) -> int:
    wiki_dir = _resolve_wiki_dir(args)
    cfg = _config_from_cfg()

    results: list[dict[str, Any]] = []
    failures = 0

    if args.all:
        scores, errs = recompute_all(wiki_dir=wiki_dir, config=cfg)
        results = [s.to_dict() for s in scores]
        failures = len(errs)
        for slug, exc in errs:
            print(f"[recompute] {slug}: {exc}", file=sys.stderr)
    elif args.slug:
        try:
            graph_index = load_graph_index(wiki_dir)
            score = recompute_slug(
                args.slug, wiki_dir=wiki_dir, config=cfg, graph_index=graph_index
            )
            results.append(score.to_dict())
        except (FileNotFoundError, ValueError, OSError, ImportError) as exc:
            failures += 1
            print(f"[recompute] {args.slug}: {exc}", file=sys.stderr)
    else:
        print("recompute: pass --slug SLUG or --all", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {"count": len(results), "failures": failures, "results": results},
                indent=2,
            )
        )
    else:
        for r in results:
            print(
                f"{r['grade']}  {r['slug']:<50}  score={r['score']:.2f}"
            )
        print(f"{len(results)} recomputed, {failures} failed", file=sys.stderr)
    return 0 if failures == 0 else 1


def cmd_show(args: argparse.Namespace) -> int:
    loaded = load_quality(args.slug)
    if loaded is None:
        print(
            f"no sidecar for {args.slug!r} (run recompute first)",
            file=sys.stderr,
        )
        return 1
    if args.json:
        print(json.dumps(loaded.to_dict(), indent=2))
    else:
        print(f"{loaded.slug}")
        print(f"  grade:    {loaded.grade}")
        print(f"  score:    {loaded.score:.2f} (raw {loaded.raw_score:.2f})")
        print(f"  computed: {loaded.computed_at}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    loaded = load_quality(args.slug)
    if loaded is None:
        print(
            f"no sidecar for {args.slug!r} (run recompute first)",
            file=sys.stderr,
        )
        return 1
    print(f"{loaded.slug} — grade {loaded.grade}")
    print(f"  raw={loaded.raw_score:.4f}  score={loaded.score:.4f}")
    print("")
    for name in _SIGNAL_ORDER:
        sig = loaded.signals.get(name)
        w = loaded.weights.get(name, 0.0)
        if sig is None:
            print(f"  {name}: MISSING")
            continue
        print(f"  {name}: score={sig.score:.2f}  weight={w:.2f}")
        for k, v in sig.evidence.items():
            print(f"    {k}: {v}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    sd = default_sidecar_dir()
    if not sd.is_dir():
        print("no MCP quality data yet (run recompute --all)", file=sys.stderr)
        return 0

    rows: list[dict[str, Any]] = []
    for p in sorted(sd.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if "grade" not in data:
            continue
        rows.append(data)

    if getattr(args, "grade", None):
        rows = [r for r in rows if r.get("grade") == args.grade]

    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
    else:
        for r in sorted(
            rows, key=lambda x: (x.get("grade", "Z"), x.get("slug", ""))
        ):
            # Format: <slug>\t<grade>\tscore=N.NN
            # Slug-first matches the convention used by the existing
            # ctx-skill-quality list and aligns with how users grep
            # ("ctx-mcp-quality list | grep my-mcp").
            print(
                f"{r.get('slug', '?')}\t{r.get('grade', '?')}\t"
                f"score={float(r.get('score', 0)):.2f}"
            )
        print(f"{len(rows)} MCP entries", file=sys.stderr)
    return 0


# ────────────────────────────────────────────────────────────────────
# Argparser + main
# ────────────────────────────────────────────────────────────────────


def _add_wiki_dir(parser: argparse.ArgumentParser) -> None:
    """Attach the --wiki-dir flag to *parser*.

    Lives on every subparser (not just the parent) so users can put the
    flag either before or after the verb -- argparse with subparsers
    requires parent flags to precede the verb, which trips up natural
    `ctx-mcp-quality recompute --slug X --wiki-dir Y` usage.
    """
    parser.add_argument(
        "--wiki-dir",
        metavar="PATH",
        type=Path,
        default=None,
        help="Wiki root (default: ctx_config.cfg.wiki_dir or ~/.claude/skill-wiki)",
    )


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ctx-mcp-quality",
        description="Score and persist quality for MCP server catalog entries.",
    )
    _add_wiki_dir(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("recompute", help="Recompute quality for one or all MCP slugs")
    _add_wiki_dir(r)
    r_group = r.add_mutually_exclusive_group(required=True)
    r_group.add_argument("--slug", metavar="SLUG", help="recompute a single MCP slug")
    r_group.add_argument("--all", action="store_true", help="recompute every MCP entity")
    r.add_argument("--json", action="store_true", help="emit JSON result")
    r.set_defaults(func=cmd_recompute)

    s = sub.add_parser("show", help="Show the current persisted score for a slug")
    _add_wiki_dir(s)
    s.add_argument("slug")
    s.add_argument("--json", action="store_true", help="emit JSON")
    s.set_defaults(func=cmd_show)

    e = sub.add_parser("explain", help="Print signal breakdown and evidence for a slug")
    _add_wiki_dir(e)
    e.add_argument("slug")
    e.set_defaults(func=cmd_explain)

    ls = sub.add_parser("list", help="List all MCP slugs with their grades (tab-separated)")
    _add_wiki_dir(ls)
    ls.add_argument("--grade", metavar="GRADE", help="filter by grade (A/B/C/D)")
    ls.add_argument("--json", action="store_true", help="emit JSON")
    ls.set_defaults(func=cmd_list)

    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``ctx-mcp-quality`` console script."""
    parser = build_argparser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "McpQualityConfig",
    "McpQualityScore",
    "build_argparser",
    "compute_quality",
    "default_sidecar_dir",
    "discover_mcp_slugs",
    "extract_signals_for_slug",
    "load_graph_index",
    "load_quality",
    "main",
    "persist_quality",
    "recompute_all",
    "recompute_slug",
    "sidecar_path",
]
