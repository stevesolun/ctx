"""Incremental graph attach helpers."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import ceil
from pathlib import Path
import sys
from typing import Any, Iterable

import networkx as nx
import numpy as np

from ctx.core.graph.edge_scoring import type_affinity_score
from ctx.core.graph.entity_overlays import upsert_overlay_record
from ctx.core.graph.vector_index import load_vector_index

_PERCENTILES = (50, 60, 75, 90, 95)
_DEFAULT_MIN_SEMANTIC_SCORE = 0.80
_DEFAULT_MIN_FINAL_WEIGHT = 0.03
_ATTACH_METHOD = "ann_attach_v1"


@dataclass(frozen=True)
class AttachCalibrationSummary:
    """Calibration snapshot used to pick incremental attach defaults."""

    node_count: int
    edge_count: int
    semantic_score_percentiles: dict[int, float]
    final_weight_percentiles: dict[int, float]
    degree_percentiles_by_type: dict[str, dict[int, float]]
    recommended_min_semantic_score: float
    recommended_max_edges_per_node: int
    recommended_min_final_weight: float


def calibrate_attach_defaults(
    graph: nx.Graph,
    *,
    semantic_percentile: int = 60,
    degree_percentile: int = 75,
    max_edges_hard_cap: int = 20,
    min_final_weight: float = _DEFAULT_MIN_FINAL_WEIGHT,
) -> AttachCalibrationSummary:
    """Summarize current graph density and return conservative defaults.

    The semantic floor follows the existing semantic edge distribution. The
    final-weight floor intentionally defaults to the graph builder floor because
    ctx already treats that as the calibrated inclusion gate.
    """
    semantic_scores: list[float] = []
    final_weights: list[float] = []
    for _source, _target, data in graph.edges(data=True):
        semantic = _float_or_none(data.get("semantic_sim"))
        if semantic is not None and semantic > 0.0:
            semantic_scores.append(semantic)
        weight = _float_or_none(data.get("final_weight", data.get("weight")))
        if weight is not None:
            final_weights.append(weight)

    degree_by_type: dict[str, list[float]] = {}
    for node_id, data in graph.nodes(data=True):
        entity_type = str(data.get("type") or str(node_id).split(":", 1)[0])
        degree_by_type.setdefault(entity_type, []).append(float(graph.degree(node_id)))

    semantic_percentiles = _percentiles(semantic_scores)
    final_weight_percentiles = _percentiles(final_weights)
    degree_percentiles_by_type = {
        entity_type: _percentiles(degrees)
        for entity_type, degrees in sorted(degree_by_type.items())
    }

    recommended_semantic = semantic_percentiles.get(
        semantic_percentile,
        _DEFAULT_MIN_SEMANTIC_SCORE,
    )
    recommended_max_edges = _recommended_degree_cap(
        degree_percentiles_by_type,
        degree_percentile=degree_percentile,
        hard_cap=max_edges_hard_cap,
    )
    return AttachCalibrationSummary(
        node_count=graph.number_of_nodes(),
        edge_count=graph.number_of_edges(),
        semantic_score_percentiles=semantic_percentiles,
        final_weight_percentiles=final_weight_percentiles,
        degree_percentiles_by_type=degree_percentiles_by_type,
        recommended_min_semantic_score=round(recommended_semantic, 4),
        recommended_max_edges_per_node=recommended_max_edges,
        recommended_min_final_weight=round(float(min_final_weight), 4),
    )


def render_calibration_markdown(summary: AttachCalibrationSummary) -> str:
    """Render a compact calibration report for review before setting defaults."""
    lines = [
        "# Incremental Attach Calibration",
        "",
        f"- node_count: {summary.node_count}",
        f"- edge_count: {summary.edge_count}",
        f"- recommended_min_semantic_score: {summary.recommended_min_semantic_score}",
        f"- recommended_max_edges_per_node: {summary.recommended_max_edges_per_node}",
        f"- recommended_min_final_weight: {summary.recommended_min_final_weight}",
        "",
        "## Semantic Score Percentiles",
        _render_percentiles(summary.semantic_score_percentiles),
        "",
        "## Final Weight Percentiles",
        _render_percentiles(summary.final_weight_percentiles),
        "",
        "## Degree Percentiles By Type",
    ]
    if summary.degree_percentiles_by_type:
        for entity_type, percentiles in summary.degree_percentiles_by_type.items():
            lines.append(f"- {entity_type}: {_inline_percentiles(percentiles)}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def attach_entity(
    *,
    index_dir: Path,
    overlay_path: Path,
    node_id: str,
    entity_type: str,
    label: str,
    tags: list[str],
    text: str | None,
    vector_json: str | None,
    model_id: str | None,
    top_k: int,
    min_score: float,
    min_final_weight: float,
    dry_run: bool = False,
    embedding_backend: str = "sentence-transformers",
    embedding_model: str | None = None,
) -> dict[str, Any]:
    """Attach one new/updated entity to an existing semantic vector index."""
    meta = _read_index_meta(index_dir)
    vector, resolved_model_id, content_hash = _resolve_attach_vector(
        text=text,
        vector_json=vector_json,
        model_id=model_id,
        fallback_model_id=str(meta["model_id"]),
        embedding_backend=embedding_backend,
        embedding_model=embedding_model,
    )
    index = load_vector_index(
        index_dir,
        expected_model_id=resolved_model_id,
        expected_content_fingerprint=str(meta["content_fingerprint"]),
    )
    if index is None:
        raise ValueError(
            "vector index metadata mismatch or index files are unreadable "
            f"for model {resolved_model_id!r}"
        )

    neighbors = index.query(
        vector,
        top_k=top_k,
        min_score=min_score,
        exclude_node_ids={node_id},
    )[0]
    now = _utc_now()
    record = _build_attach_record(
        node_id=node_id,
        entity_type=entity_type,
        label=label,
        tags=tags,
        content_hash=content_hash,
        model_id=resolved_model_id,
        created_at=now,
        candidates_considered=len(neighbors),
        min_final_weight=min_final_weight,
        neighbors=[
            {
                "node_id": neighbor.node_id,
                "score": round(float(neighbor.score), 6),
                "rank": rank,
            }
            for rank, neighbor in enumerate(neighbors, 1)
        ],
    )
    status = "dry-run" if dry_run else upsert_overlay_record(overlay_path, record)
    return {"status": status, "record": record}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ctx.core.graph.incremental_attach",
        description="Incremental graph attach utilities.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    calibrate = sub.add_parser("calibrate", help="Calibrate attach defaults from graph.json")
    calibrate.add_argument("--graph", required=True, help="Path to graphify-out/graph.json")
    calibrate.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown")
    attach = sub.add_parser("attach", help="Attach one entity through the semantic vector index")
    attach.add_argument("--index-dir", required=True, help="Path to a persisted vector-index directory")
    attach.add_argument("--overlay", required=True, help="Path to graphify-out/entity-overlays.jsonl")
    attach.add_argument("--node-id", required=True, help="Graph node id, e.g. skill:my-skill")
    attach.add_argument("--type", required=True, dest="entity_type", help="Entity type")
    attach.add_argument("--label", help="Display label; defaults to the slug part of --node-id")
    attach.add_argument("--tag", action="append", default=[], help="Entity tag; repeatable")
    attach.add_argument("--text", help="Entity text to embed and hash")
    attach.add_argument("--text-file", help="Read entity text from a UTF-8 file")
    attach.add_argument(
        "--vector-json",
        help="Precomputed vector JSON for tests/advanced callers; skips embedding",
    )
    attach.add_argument("--model-id", help="Embedding model id expected by the vector index")
    attach.add_argument("--embedding-backend", default="sentence-transformers")
    attach.add_argument("--embedding-model")
    attach.add_argument("--top-k", type=int, default=20)
    attach.add_argument("--min-score", type=float)
    attach.add_argument("--min-final-weight", type=float, default=_DEFAULT_MIN_FINAL_WEIGHT)
    attach.add_argument("--dry-run", action="store_true", help="Print the overlay record without writing")
    attach.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)
    if args.command == "calibrate":
        from ctx.core.graph.resolve_graph import load_graph  # noqa: PLC0415

        graph = load_graph(Path(args.graph))
        summary = calibrate_attach_defaults(graph)
        if args.json:
            print(json.dumps(asdict(summary), indent=2))
        else:
            print(render_calibration_markdown(summary), end="")
        return 0
    if args.command == "attach":
        try:
            result = attach_entity(
                index_dir=Path(args.index_dir),
                overlay_path=Path(args.overlay),
                node_id=args.node_id,
                entity_type=args.entity_type,
                label=args.label or _slug_label(args.node_id),
                tags=list(args.tag or []),
                text=_resolve_text_input(args.text, args.text_file),
                vector_json=args.vector_json,
                model_id=args.model_id,
                top_k=args.top_k,
                min_score=(
                    args.min_score
                    if args.min_score is not None
                    else _default_min_semantic_score()
                ),
                min_final_weight=args.min_final_weight,
                dry_run=args.dry_run,
                embedding_backend=args.embedding_backend,
                embedding_model=args.embedding_model,
            )
        except Exception as exc:  # noqa: BLE001 - CLI reports concise errors.
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json or args.dry_run:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(result["status"])
        return 0
    return 1


def _resolve_text_input(text: str | None, text_file: str | None) -> str | None:
    if text and text_file:
        raise ValueError("use only one of --text or --text-file")
    if not text_file:
        return text
    path = Path(text_file).expanduser()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read --text-file {path}") from exc


def _default_min_semantic_score() -> float:
    try:
        from ctx_config import cfg  # noqa: PLC0415

        return float(cfg.graph_semantic_min_cosine)
    except Exception:  # pragma: no cover - standalone CLI fallback.
        return _DEFAULT_MIN_SEMANTIC_SCORE


def _read_index_meta(index_dir: Path) -> dict[str, Any]:
    meta_path = index_dir / "vector-index.meta.json"
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"vector index metadata not found at {meta_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"vector index metadata is invalid JSON at {meta_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"vector index metadata is not an object at {meta_path}")
    for field in ("model_id", "content_fingerprint"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ValueError(f"vector index metadata missing {field!r}")
    return payload


def _resolve_attach_vector(
    *,
    text: str | None,
    vector_json: str | None,
    model_id: str | None,
    fallback_model_id: str,
    embedding_backend: str,
    embedding_model: str | None,
) -> tuple[np.ndarray, str, str]:
    if vector_json:
        vector = _parse_vector_json(vector_json)
        return vector, model_id or fallback_model_id, _content_hash(text or vector_json)
    if not text:
        raise ValueError("attach requires --text or --vector-json")

    from embedding_backend import get_embedder  # noqa: PLC0415

    embedder = get_embedder(embedding_backend, model=embedding_model)
    resolved_model_id = embedder.name
    if model_id and model_id != resolved_model_id:
        raise ValueError(
            f"--model-id {model_id!r} does not match embedder {resolved_model_id!r}"
        )
    return embedder.embed([text]), resolved_model_id, _content_hash(text)


def _parse_vector_json(vector_json: str) -> np.ndarray:
    try:
        payload = json.loads(vector_json)
    except json.JSONDecodeError as exc:
        raise ValueError("--vector-json must be valid JSON") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError("--vector-json must be a non-empty JSON list")
    try:
        vector = np.asarray([float(value) for value in payload], dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("--vector-json must contain only numbers") from exc
    return vector.reshape(1, -1)


def _build_attach_record(
    *,
    node_id: str,
    entity_type: str,
    label: str,
    tags: list[str],
    content_hash: str,
    model_id: str,
    created_at: str,
    candidates_considered: int,
    min_final_weight: float,
    neighbors: list[dict[str, Any]],
) -> dict[str, Any]:
    attach_key = f"ann:v1:{model_id}:{node_id}:{content_hash}"
    edges: list[dict[str, Any]] = []
    for neighbor in neighbors:
        semantic = float(neighbor["score"])
        target_type = _node_type_from_id(str(neighbor["node_id"]))
        type_affinity = type_affinity_score(entity_type, target_type)
        final, components = _blend_ann_score(
            semantic=semantic,
            type_affinity=type_affinity,
        )
        if final < min_final_weight:
            continue
        edges.append(
            {
                "source": node_id,
                "target": neighbor["node_id"],
                "weight": final,
                "final_weight": final,
                "semantic_sim": semantic,
                "similarity_score": semantic,
                "type_affinity": type_affinity,
                "score_components": components,
                "method": _ATTACH_METHOD,
                "provenance": _ATTACH_METHOD,
                "rank": int(neighbor["rank"]),
                "candidates_considered": candidates_considered,
                "edge_reasons": [
                    reason
                    for reason, enabled in (
                        ("semantic-ann", semantic > 0.0),
                        ("type-affinity", type_affinity > 0.0),
                    )
                    if enabled
                ],
                "created_at": created_at,
            }
        )
    return {
        "schema_version": 1,
        "kind": "ann_attach",
        "attach_key": attach_key,
        "replace_scope": f"ann:v1:{model_id}:{node_id}",
        "node_id": node_id,
        "content_hash": content_hash,
        "model_id": model_id,
        "method": _ATTACH_METHOD,
        "provenance": _ATTACH_METHOD,
        "created_at": created_at,
        "candidates_considered": candidates_considered,
        "nodes": [
            {
                "id": node_id,
                "label": label,
                "title": label,
                "type": entity_type,
                "tags": tags,
                "source": "incremental-attach",
                "content_hash": content_hash,
                "updated": created_at,
            }
        ],
        "edges": edges,
    }


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slug_label(node_id: str) -> str:
    return node_id.split(":", 1)[-1]


def _node_type_from_id(node_id: str) -> str:
    return node_id.split(":", 1)[0] if ":" in node_id else ""


def _blend_ann_score(
    *,
    semantic: float,
    type_affinity: float,
) -> tuple[float, dict[str, float]]:
    try:
        from ctx_config import cfg  # noqa: PLC0415

        semantic_weight = float(cfg.graph_edge_weight_semantic)
        type_weight = float(cfg.graph_edge_boost_type_affinity)
    except Exception:  # pragma: no cover - defensive fallback for standalone CLI use.
        semantic_weight = 0.70
        type_weight = 0.03
    components = {
        "semantic": round(semantic_weight * semantic, 4),
        "type_affinity": round(type_weight * type_affinity, 4),
    }
    final = min(sum(components.values()), 1.0)
    return round(final, 4), {
        key: value for key, value in components.items() if value > 0.0
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _percentiles(values: Iterable[float]) -> dict[int, float]:
    series = [value for value in values if value >= 0.0]
    if not series:
        return {}
    array = np.asarray(series, dtype=np.float64)
    return {
        percentile: round(float(np.percentile(array, percentile)), 4)
        for percentile in _PERCENTILES
    }


def _recommended_degree_cap(
    degree_percentiles_by_type: dict[str, dict[int, float]],
    *,
    degree_percentile: int,
    hard_cap: int,
) -> int:
    if not degree_percentiles_by_type:
        return 1
    cap = max(
        percentiles.get(degree_percentile, 0.0)
        for percentiles in degree_percentiles_by_type.values()
    )
    return max(1, min(int(hard_cap), int(ceil(cap))))


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _render_percentiles(percentiles: dict[int, float]) -> str:
    if not percentiles:
        return "- none"
    return "\n".join(f"- p{key}: {value}" for key, value in percentiles.items())


def _inline_percentiles(percentiles: dict[int, float]) -> str:
    return ", ".join(f"p{key}={value}" for key, value in percentiles.items())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
