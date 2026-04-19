"""
tune_similarity_thresholds.py -- Sweep thresholds against the fixture corpus
and print the precision/recall surface so we can pick sensible defaults.

Run once after editing fixtures or changing the embedder; not part of CI.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from corpus_cache import CorpusCache  # noqa: E402
from cosine_ranker import CosineRanker  # noqa: E402
from ctx_config import cfg  # noqa: E402
from intake_gate import compose_corpus_text  # noqa: E402

FIXTURE_DIR = SRC_DIR / "tests" / "fixtures" / "similarity"


@dataclass(frozen=True)
class _Pair:
    id: str
    label: str
    a_md: str
    b_md: str


def _compose_md(entry: dict) -> str:
    return (
        "---\n"
        f"name: {entry['name']}\n"
        f"description: {entry['description']}\n"
        "---\n"
        f"# {entry['name']}\n\n"
        f"{entry['body']}\n"
    )


def _load(filename: str) -> list[_Pair]:
    pairs: list[_Pair] = []
    for raw in (FIXTURE_DIR / filename).read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        e = json.loads(raw)
        pairs.append(_Pair(e["id"], e["label"], _compose_md(e["a"]), _compose_md(e["b"])))
    return pairs


def _score(pair: _Pair, embedder, root: Path) -> float:
    cache = CorpusCache(f"tune-{pair.id}", root=root)
    a_text = compose_corpus_text(pair.a_md)
    a_vec = embedder.embed([a_text])[0]
    cache.put(f"{pair.id}-a", a_text, a_vec)
    ranker = CosineRanker.from_cache(cache)
    b_text = compose_corpus_text(pair.b_md)
    b_vec = embedder.embed([b_text])[0]
    top = ranker.topk(b_vec, k=1)
    return float(top[0].score) if top else 0.0


def main() -> None:
    import tempfile

    embedder = cfg.build_intake_embedder()
    near = _load("near_duplicates.jsonl")
    distinct = _load("distinct_pairs.jsonl")
    adversarial = _load("adversarial.jsonl")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        near_scores = [(p.id, _score(p, embedder, root)) for p in near]
        distinct_scores = [(p.id, _score(p, embedder, root)) for p in distinct]
        adv_scores = [(p.id, _score(p, embedder, root)) for p in adversarial]

    print("\n=== Near-duplicate scores (should be HIGH) ===")
    for pid, s in sorted(near_scores, key=lambda x: x[1]):
        print(f"  {pid}: {s:.4f}")
    print(f"  min={min(s for _, s in near_scores):.4f} "
          f"median={sorted(s for _, s in near_scores)[len(near_scores)//2]:.4f}")

    print("\n=== Distinct scores (should be LOW) ===")
    for pid, s in sorted(distinct_scores, key=lambda x: -x[1])[:10]:
        print(f"  {pid}: {s:.4f}")
    print(f"  max={max(s for _, s in distinct_scores):.4f} "
          f"median={sorted(s for _, s in distinct_scores)[len(distinct_scores)//2]:.4f}")

    print("\n=== Adversarial scores (should be LOW — precision traps) ===")
    for pid, s in sorted(adv_scores, key=lambda x: -x[1]):
        print(f"  {pid}: {s:.4f}")
    print(f"  max={max(s for _, s in adv_scores):.4f}")

    # Sweep: at each candidate near_dup threshold, compute P/R assuming a pair
    # is flagged iff top_score >= threshold.
    print("\n=== Threshold sweep (flag if score >= t) ===")
    print(f"{'threshold':>10} {'recall':>8} {'precision':>10} {'TP':>4} {'FN':>4} {'FP':>4}")
    for t in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.82, 0.85, 0.88, 0.90, 0.93]:
        tp = sum(1 for _, s in near_scores if s >= t)
        fn = len(near_scores) - tp
        fp = sum(1 for _, s in distinct_scores + adv_scores if s >= t)
        recall = tp / (tp + fn) if (tp + fn) else 0
        precision = tp / (tp + fp) if (tp + fp) else 0
        print(f"{t:>10.2f} {recall:>8.3f} {precision:>10.3f} {tp:>4} {fn:>4} {fp:>4}")


if __name__ == "__main__":
    main()
