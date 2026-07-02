"""Shadow validation for incremental graph attach."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import random
from pathlib import Path
import sys
from typing import Any

import networkx as nx
import numpy as np

from ctx.core.graph.resolve_graph import load_graph
from ctx.core.graph.vector_index import load_vector_index

_DEFAULT_TOP_KS = (5, 10, 20)


@dataclass(frozen=True)
class RankedEdge:
    target: str
    score: float


def run_shadow_validation(
    *,
    index_dir: Path,
    graph: nx.Graph | None = None,
    sample_size: int = 100,
    seed: int = 42,
    node_ids: list[str] | None = None,
    top_ks: tuple[int, ...] = _DEFAULT_TOP_KS,
    min_score: float = 0.75,
    min_final_weight: float = 0.03,
    min_overlap: float = 0.85,
) -> dict[str, Any]:
    """Compare incremental ANN attach results against batch semantic neighbors."""
    index = _load_index(index_dir)
    max_k = max(top_ks)
    sampled = _select_nodes(
        index.node_ids,
        sample_size=sample_size,
        seed=seed,
        requested=node_ids,
    )
    row_by_node = {node_id: idx for idx, node_id in enumerate(index.node_ids)}
    totals = {k: {"expected": 0, "predicted": 0, "overlap": 0} for k in top_ks}
    score_deltas: list[float] = []
    bad_examples: list[dict[str, Any]] = []

    for node_id in sampled:
        row = row_by_node[node_id]
        expected = (
            _expected_from_graph(graph, node_id, max_k=max_k, min_score=min_final_weight)
            if graph is not None
            else _expected_from_vectors(
                index.vectors,
                index.node_ids,
                row,
                max_k=max_k,
                min_score=min_score,
            )
        )
        predicted = [
            RankedEdge(neighbor.node_id, float(neighbor.score))
            for neighbor in index.query(
                index.vectors[row : row + 1],
                top_k=max_k,
                min_score=min_score,
                exclude_node_ids={node_id},
            )[0]
            if float(neighbor.score) >= min_final_weight
        ]
        expected_by_target = {edge.target: edge.score for edge in expected}
        predicted_by_target = {edge.target: edge.score for edge in predicted}
        for target, expected_score in expected_by_target.items():
            if target in predicted_by_target:
                score_deltas.append(abs(predicted_by_target[target] - expected_score))

        for k in top_ks:
            expected_k = {edge.target for edge in expected[:k]}
            predicted_k = {edge.target for edge in predicted[:k]}
            overlap = expected_k & predicted_k
            totals[k]["expected"] += len(expected_k)
            totals[k]["predicted"] += len(predicted_k)
            totals[k]["overlap"] += len(overlap)

        max_expected = {edge.target for edge in expected[:max_k]}
        max_predicted = {edge.target for edge in predicted[:max_k]}
        missing = sorted(max_expected - max_predicted)
        extra = sorted(max_predicted - max_expected)
        recall = _ratio(len(max_expected & max_predicted), len(max_expected))
        if missing or extra:
            bad_examples.append(
                {
                    "node_id": node_id,
                    f"recall_at_{max_k}": round(recall, 4),
                    "missing": missing[:5],
                    "extra": extra[:5],
                    "expected_top": [edge.target for edge in expected[:5]],
                    "predicted_top": [edge.target for edge in predicted[:5]],
                }
            )

    metrics = {f"top_{k}": _metric_summary(totals[k]) for k in top_ks}
    gate_metric = metrics[f"top_{max_k}"]["recall"]
    return {
        "sampled_nodes": len(sampled),
        "index_nodes": len(index.node_ids),
        "baseline": "graph-semantic-edges" if graph is not None else "exact-vector-topk",
        "top_k": list(top_ks),
        "min_score": min_score,
        "min_final_weight": min_final_weight,
        "min_overlap": min_overlap,
        "metrics": metrics,
        "score_deltas": {
            "count": len(score_deltas),
            "mean_abs": round(float(np.mean(score_deltas)), 6) if score_deltas else 0.0,
            "max_abs": round(float(np.max(score_deltas)), 6) if score_deltas else 0.0,
        },
        "bad_examples": sorted(
            bad_examples,
            key=lambda item: (item[f"recall_at_{max_k}"], item["node_id"]),
        )[:10],
        "gate_passed": gate_metric >= min_overlap,
        "gate_metric": "recall",
        "gate_top_k": max_k,
        "gate_value": gate_metric,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ctx-incremental-shadow",
        description="Shadow-validate incremental ANN graph attach.",
    )
    parser.add_argument("--index-dir", required=True)
    graph_source = parser.add_mutually_exclusive_group()
    graph_source.add_argument("--graph", help="Optional graphify-out/graph.json baseline")
    graph_source.add_argument(
        "--graph-dir",
        help="Optional graphify-out directory; supports active packs without graph.json",
    )
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--node", action="append", default=[])
    parser.add_argument("--top-k", action="append", type=int, default=[])
    parser.add_argument("--min-score", type=float, default=0.75)
    parser.add_argument("--min-final-weight", type=float, default=0.03)
    parser.add_argument("--min-overlap", type=float, default=0.85)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-fail", action="store_true")
    args = parser.parse_args(argv)

    graph_path = Path(args.graph) if args.graph else None
    if args.graph_dir:
        graph_path = Path(args.graph_dir) / "graph.json"
    graph = load_graph(graph_path) if graph_path is not None else None
    report = run_shadow_validation(
        index_dir=Path(args.index_dir),
        graph=graph,
        sample_size=args.sample_size,
        seed=args.seed,
        node_ids=list(args.node or []) or None,
        top_ks=tuple(sorted(set(args.top_k or _DEFAULT_TOP_KS))),
        min_score=args.min_score,
        min_final_weight=args.min_final_weight,
        min_overlap=args.min_overlap,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_render_markdown(report), end="")
    if args.no_fail or report["gate_passed"]:
        return 0
    return 2


def _load_index(index_dir: Path) -> Any:
    meta_path = index_dir / "vector-index.meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"vector index metadata not found at {meta_path}") from exc
    index = load_vector_index(
        index_dir,
        expected_model_id=str(meta.get("model_id")),
        expected_content_fingerprint=str(meta.get("content_fingerprint")),
    )
    if index is None:
        raise ValueError(f"vector index at {index_dir} is unreadable or stale")
    return index


def _select_nodes(
    all_node_ids: list[str],
    *,
    sample_size: int,
    seed: int,
    requested: list[str] | None,
) -> list[str]:
    if requested:
        available = set(all_node_ids)
        missing = sorted(set(requested) - available)
        if missing:
            raise ValueError(f"shadow nodes are not in the index: {missing}")
        return list(dict.fromkeys(requested))
    if sample_size <= 0 or sample_size >= len(all_node_ids):
        return list(all_node_ids)
    rng = random.Random(seed)
    return sorted(rng.sample(all_node_ids, sample_size))


def _expected_from_vectors(
    vectors: np.ndarray,
    node_ids: list[str],
    row: int,
    *,
    max_k: int,
    min_score: float,
) -> list[RankedEdge]:
    scores = vectors[row : row + 1] @ vectors.T
    scores[0, row] = -np.inf
    order = np.argsort(-scores[0])
    out: list[RankedEdge] = []
    for idx in order:
        score = float(scores[0, int(idx)])
        if score < min_score:
            continue
        out.append(RankedEdge(node_ids[int(idx)], score))
        if len(out) >= max_k:
            break
    return out


def _expected_from_graph(
    graph: nx.Graph,
    node_id: str,
    *,
    max_k: int,
    min_score: float,
) -> list[RankedEdge]:
    if node_id not in graph:
        return []
    edges: list[RankedEdge] = []
    for neighbor in graph.neighbors(node_id):
        attrs = graph.edges[node_id, neighbor]
        score = _edge_score(attrs)
        if score >= min_score:
            edges.append(RankedEdge(str(neighbor), score))
    return sorted(edges, key=lambda edge: (-edge.score, edge.target))[:max_k]


def _edge_score(attrs: dict[str, Any]) -> float:
    for key in ("semantic_sim", "final_weight", "weight"):
        value = attrs.get(key)
        if isinstance(value, int | float):
            return float(value)
    return 0.0


def _metric_summary(total: dict[str, int]) -> dict[str, float | int]:
    overlap = total["overlap"]
    expected = total["expected"]
    predicted = total["predicted"]
    union = expected + predicted - overlap
    return {
        "expected": expected,
        "predicted": predicted,
        "overlap": overlap,
        "precision": round(_ratio(overlap, predicted), 4),
        "recall": round(_ratio(overlap, expected), 4),
        "jaccard": round(_ratio(overlap, union), 4),
    }


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return numerator / denominator


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Incremental Attach Shadow Report",
        "",
        f"- sampled_nodes: {report['sampled_nodes']}",
        f"- index_nodes: {report['index_nodes']}",
        f"- baseline: {report['baseline']}",
        f"- min_score: {report['min_score']}",
        f"- min_final_weight: {report['min_final_weight']}",
        f"- gate: {report['gate_metric']}@{report['gate_top_k']} "
        f"{report['gate_value']:.4f} >= {report['min_overlap']} "
        f"({'pass' if report['gate_passed'] else 'fail'})",
        "",
        "## Top-K Agreement",
    ]
    for key, metric in report["metrics"].items():
        lines.append(
            f"- {key}: precision={metric['precision']} recall={metric['recall']} "
            f"jaccard={metric['jaccard']} overlap={metric['overlap']}"
        )
    lines.extend(
        [
            "",
            "## Score Deltas",
            f"- count: {report['score_deltas']['count']}",
            f"- mean_abs: {report['score_deltas']['mean_abs']}",
            f"- max_abs: {report['score_deltas']['max_abs']}",
            "",
            "## Bad Examples",
        ]
    )
    if not report["bad_examples"]:
        lines.append("- none")
    else:
        for example in report["bad_examples"]:
            lines.append(f"- {example['node_id']}: {example}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
