"""CLI for the shared ctx recommendation engine."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ctx import recommend_bundle
from ctx_config import cfg


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx-recommend",
        description="Recommend up to five skills, agents, or MCPs for an intent.",
    )
    parser.add_argument(
        "query",
        nargs="+",
        help="Free-text user intent, e.g. 'build a FastAPI API with auth'.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=cfg.recommendation_top_k,
        help=f"Maximum results to show (default {cfg.recommendation_top_k}, max 5).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def _render_row(row: dict[str, Any]) -> str:
    name = str(row.get("name") or row.get("slug") or "")
    entity_type = str(row.get("type") or row.get("entity_type") or "skill")
    score = row.get("normalized_score", row.get("score", 0.0))
    try:
        score_text = f"{float(score):.3f}"
    except (TypeError, ValueError):
        score_text = str(score)
    tags = row.get("matching_tags") or row.get("shared_tags") or []
    tag_text = ", ".join(str(t) for t in tags[:5]) if isinstance(tags, list) else ""
    suffix = f"  [{tag_text}]" if tag_text else ""
    return f"{entity_type:>10}  {name:<40} score={score_text}{suffix}"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    query = " ".join(args.query).strip()
    top_k = max(1, min(int(args.top_k), cfg.recommendation_top_k))
    results = recommend_bundle(query, top_k=top_k)
    if args.json:
        print(json.dumps({"query": query, "results": results}, indent=2))
        return 0
    if not results:
        print("No recommendations above the configured score threshold.", file=sys.stderr)
        return 0
    for row in results:
        print(_render_row(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
