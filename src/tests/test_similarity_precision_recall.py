"""
test_similarity_precision_recall.py -- Integration test for the intake gate's
similarity detection against a curated corpus.

Purpose
-------
The intake gate's usefulness depends on two numbers:

  - **Recall** on near-duplicates: when two skills genuinely overlap, the
    gate must flag at least 90% of them (``DUPLICATE`` or
    ``NEAR_DUPLICATE``). Missed duplicates let redundant skills into the
    corpus, which is the whole failure mode the gate exists to prevent.

  - **Precision** against distinct and adversarial pairs: when two
    skills are genuinely different, the gate must *not* flag them. False
    positives block legitimate skills and train users to override the
    gate — the worst failure mode.

This test runs all three fixture sets (30 near-duplicates, 30 distinct,
10 adversarial) through the real embedder and asserts precision/recall
≥ 0.90.

Tuning workflow
---------------
Fixtures are hand-written under ``src/tests/fixtures/similarity/``. If
the test fails:

  1. Read the per-pair breakdown printed on failure — each misclassified
     pair lists its cosine score and reasoning.
  2. Adjust ``intake_dup_threshold`` / ``intake_near_dup_threshold`` in
     ``config.json`` (documented as tunable in ``ctx_config.py``).
  3. Re-run.

If tuning thresholds cannot hit 0.9 on both axes, the bug is upstream —
either ``compose_corpus_text`` is dropping signal, or the fixtures need
revision. Do not lower the 0.9 bar without a plan.

Markers
-------
Marked ``@pytest.mark.integration`` because it loads the real MiniLM
model (~100MB on first run). Skip in fast CI with ``-m 'not integration'``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sentence_transformers = pytest.importorskip("sentence_transformers")

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from corpus_cache import CorpusCache  # noqa: E402
from cosine_ranker import CosineRanker  # noqa: E402
from ctx_config import cfg  # noqa: E402
from intake_gate import compose_corpus_text, run_intake_gate  # noqa: E402


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "similarity"

# Minimum precision and recall the gate must clear to be shipped.
# Raising these is easy; lowering them requires a plan, not a fixup.
_MIN_PRECISION = 0.90
_MIN_RECALL = 0.90


# ────────────────────────────────────────────────────────────────────
# Fixture loading
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Pair:
    id: str
    label: str  # "near_duplicate" | "distinct" | "adversarial"
    a_md: str
    b_md: str
    note: str

    @property
    def should_flag(self) -> bool:
        """A pair should be flagged iff its label is near_duplicate.

        Distinct and adversarial pairs must pass unflagged — they exist
        to prove the gate does not false-positive on legitimate skills.
        """
        return self.label == "near_duplicate"


def _compose_md(entry: dict) -> str:
    """Build a full markdown document from a fixture entry.

    Fixture files store only the semantic content (name, description,
    body) so they stay focused and editable. This helper assembles the
    full markdown with frontmatter + H1 so structural checks pass.
    """
    name = entry["name"]
    description = entry["description"]
    body = entry["body"]
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        f"# {name}\n\n"
        f"{body}\n"
    )


def _load_pairs(filename: str) -> list[_Pair]:
    path = FIXTURE_DIR / filename
    if not path.exists():
        pytest.skip(f"fixture file missing: {path}")
    pairs: list[_Pair] = []
    for line_num, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError as exc:
            pytest.fail(f"{path}:{line_num} invalid JSON: {exc}")
        pairs.append(_Pair(
            id=entry["id"],
            label=entry["label"],
            a_md=_compose_md(entry["a"]),
            b_md=_compose_md(entry["b"]),
            note=entry.get("note", ""),
        ))
    return pairs


# ────────────────────────────────────────────────────────────────────
# Per-pair evaluation
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Outcome:
    pair: _Pair
    flagged: bool
    top_score: float
    top_code: str  # "DUPLICATE" | "NEAR_DUPLICATE" | "" when not flagged


def _evaluate_pair(pair: _Pair, embedder, cache_root: Path) -> _Outcome:
    """Record A, check B, return whether B was flagged against A.

    Each pair gets its own CorpusCache namespace so earlier pairs don't
    leak into later ones. The embedder is reused across pairs — loading
    MiniLM is the dominant cost, and it's immutable for this run.
    """
    cache = CorpusCache(f"fixture-{pair.id}", root=cache_root)

    # Record A into the per-pair cache.
    a_text = compose_corpus_text(pair.a_md)
    a_vec = embedder.embed([a_text])[0]
    cache.put(f"{pair.id}-a", a_text, a_vec)

    # Rank B against the single-entry corpus.
    ranker = CosineRanker.from_cache(cache)
    config = cfg.build_intake_config()
    decision = run_intake_gate(
        pair.b_md,
        embedder=embedder,
        ranker=ranker,
        config=config,
    )

    codes = {f.code for f in decision.findings}
    flagged = "DUPLICATE" in codes or "NEAR_DUPLICATE" in codes
    top_score = decision.nearest[0].score if decision.nearest else 0.0
    top_code = (
        "DUPLICATE" if "DUPLICATE" in codes
        else "NEAR_DUPLICATE" if "NEAR_DUPLICATE" in codes
        else ""
    )
    return _Outcome(pair=pair, flagged=flagged, top_score=float(top_score), top_code=top_code)


# ────────────────────────────────────────────────────────────────────
# Module-scoped setup — embedder loads once
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def _embedder():
    """Load the real configured embedder once per module run.

    Skips the whole module if the embedding backend can't be built —
    typical cause is sentence-transformers not being installed or
    network access being blocked on first download.
    """
    try:
        return cfg.build_intake_embedder()
    except Exception as exc:  # noqa: BLE001 — environment failures should skip, not fail
        pytest.skip(f"cannot build intake embedder: {exc}")


@pytest.fixture
def _tmp_cache_root(tmp_path, monkeypatch):
    """Isolate cache writes so tests never touch ~/.claude."""
    root = tmp_path / "intake-cache"
    root.mkdir()
    monkeypatch.setattr(cfg, "intake_cache_root", root)
    return root


# ────────────────────────────────────────────────────────────────────
# The three fixture sets — evaluated together for precision/recall
# ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_similarity_precision_and_recall(_embedder, _tmp_cache_root):
    """Precision and recall across the full curated fixture set.

    Confusion matrix:
        TP = near_duplicate pairs correctly flagged
        FN = near_duplicate pairs NOT flagged (missed duplicates)
        FP = distinct or adversarial pairs incorrectly flagged
        TN = distinct or adversarial pairs correctly passed

    precision = TP / (TP + FP)
    recall    = TP / (TP + FN)
    """
    near = _load_pairs("near_duplicates.jsonl")
    distinct = _load_pairs("distinct_pairs.jsonl")
    adversarial = _load_pairs("adversarial.jsonl")

    # Sanity-check fixture counts so a truncated JSONL doesn't silently
    # lower the bar the test claims to enforce.
    assert len(near) == 30, f"expected 30 near-duplicate pairs, got {len(near)}"
    assert len(distinct) == 30, f"expected 30 distinct pairs, got {len(distinct)}"
    assert len(adversarial) == 10, f"expected 10 adversarial pairs, got {len(adversarial)}"

    all_pairs = near + distinct + adversarial
    outcomes = [_evaluate_pair(p, _embedder, _tmp_cache_root) for p in all_pairs]

    tp = sum(1 for o in outcomes if o.pair.should_flag and o.flagged)
    fn = sum(1 for o in outcomes if o.pair.should_flag and not o.flagged)
    fp = sum(1 for o in outcomes if not o.pair.should_flag and o.flagged)
    tn = sum(1 for o in outcomes if not o.pair.should_flag and not o.flagged)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0

    # Per-pair breakdown in the failure message so tuning is data-driven.
    misses = [o for o in outcomes if o.pair.should_flag and not o.flagged]
    false_pos = [o for o in outcomes if not o.pair.should_flag and o.flagged]

    failure_lines = [
        f"precision={precision:.3f} (need >= {_MIN_PRECISION})",
        f"recall={recall:.3f} (need >= {_MIN_RECALL})",
        f"TP={tp} FN={fn} FP={fp} TN={tn}",
        "",
    ]
    if misses:
        failure_lines.append(f"Missed duplicates ({len(misses)}):")
        for o in misses:
            failure_lines.append(
                f"  {o.pair.id}: top_score={o.top_score:.3f} label={o.pair.label}"
            )
    if false_pos:
        failure_lines.append(f"False positives ({len(false_pos)}):")
        for o in false_pos:
            failure_lines.append(
                f"  {o.pair.id}: top_score={o.top_score:.3f} "
                f"flagged={o.top_code} label={o.pair.label}"
            )
    message = "\n".join(failure_lines)

    assert recall >= _MIN_RECALL, message
    assert precision >= _MIN_PRECISION, message


@pytest.mark.integration
def test_fixture_schema_integrity():
    """Fail fast if any fixture file is malformed — catches JSONL typos
    without having to load the embedder.
    """
    for filename, expected_label in [
        ("near_duplicates.jsonl", "near_duplicate"),
        ("distinct_pairs.jsonl", "distinct"),
        ("adversarial.jsonl", "adversarial"),
    ]:
        pairs = _load_pairs(filename)
        for p in pairs:
            assert p.label == expected_label, (
                f"{filename} contains pair {p.id!r} labeled "
                f"{p.label!r}, expected {expected_label!r}"
            )
            # Structural minimum: description + H1 + H2 + enough body.
            # If these fail the intake gate will reject on structure
            # before similarity is even checked, poisoning recall.
            assert "description:" in p.a_md
            assert "description:" in p.b_md
            assert "## " in p.a_md
            assert "## " in p.b_md
