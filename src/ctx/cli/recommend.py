"""CLI for the shared ctx recommendation engine."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ctx import recommend_bundle, recommend_related
from ctx.adapters.generic.ctx_core_tools import (
    _DEFAULT_BASELINE_CONTEXT,
    _recommendation_context_from_args,
    _recommendation_context_skip_reason,
)
from ctx.api import recommendation_rejections
from ctx_config import cfg


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ctx.cli.recommend",
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
    parser.add_argument(
        "--selected",
        action="append",
        default=[],
        help=(
            "Selected recommendation ID/name. May be repeated or comma-separated; "
            "enables related recommendations."
        ),
    )
    parser.add_argument(
        "--rejected",
        action="append",
        default=[],
        help="Rejected recommendation ID/name. May be repeated or comma-separated.",
    )
    parser.add_argument(
        "--session-id",
        help="Optional host/session ID used to remember canonical rejected recommendations.",
    )
    parser.add_argument(
        "--rejection-mode",
        choices=("use", "replace", "ignore"),
        default="use",
        help="How explicit rejections interact with remembered session state (default: use).",
    )
    parser.add_argument(
        "--active",
        action="append",
        default=[],
        help="Already active ctx entity ID/name. May be repeated or comma-separated.",
    )
    parser.add_argument(
        "--baseline-context",
        action="append",
        default=[],
        help=(
            "Baseline host ctx entity ID/name to suppress as an optional recommendation. "
            "May be repeated or comma-separated."
        ),
    )
    parser.add_argument(
        "--include-baseline-context",
        action="store_true",
        help="Show baseline context as normal recommendations.",
    )
    parser.add_argument(
        "--show-unavailable",
        action="store_true",
        help="Include non-local or non-loadable recommendations.",
    )
    parser.add_argument(
        "--local-code-task",
        action="store_true",
        help="Apply local repo/code-task recommendation filters.",
    )
    parser.add_argument(
        "--no-api-keys",
        action="store_true",
        help="Suppress recommendations that require external API/service credentials.",
    )
    parser.add_argument("--language", help="Optional scenario/programming language hint.")
    parser.add_argument(
        "--related-top-n",
        type=int,
        default=cfg.recommendation_top_k,
        help=f"Maximum related results to show (default {cfg.recommendation_top_k}, max 5).",
    )
    return parser


def _split_selection_values(values: list[str] | None) -> list[str]:
    selections: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        for part in value.split(","):
            item = part.strip()
            if item and item not in seen:
                selections.append(item)
                seen.add(item)
    return selections


def _effective_baseline_context(
    baseline_context: list[str],
    *,
    include_baseline_context: bool,
) -> list[str]:
    if include_baseline_context:
        return []
    return baseline_context or list(_DEFAULT_BASELINE_CONTEXT)


def _recommendation_filter_args(args: argparse.Namespace) -> dict[str, Any]:
    context_args: dict[str, Any] = {}
    if args.show_unavailable:
        context_args["include_unavailable"] = True
    if args.local_code_task:
        context_args["local_code_task"] = True
    if args.no_api_keys:
        context_args["no_api_keys"] = True
    if args.language:
        context_args["language"] = args.language
    return context_args


def _filter_related_results(
    rows: list[dict[str, Any]],
    *,
    context: dict[str, Any],
    top_n: int,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if _recommendation_context_skip_reason(row, context) is not None:
            continue
        filtered.append(row)
        if len(filtered) >= top_n:
            break
    return filtered


def _recommendation_context_filters_active(context: dict[str, Any]) -> bool:
    return any(bool(context.get(key)) for key in ("local_code_task", "no_api_keys", "language"))


def _related_fetch_top_n(
    *,
    top_n: int,
    excluded_count: int,
    context: dict[str, Any],
) -> int:
    if not _recommendation_context_filters_active(context):
        return top_n
    return min(50, top_n + excluded_count + 25)


def _render_row(row: dict[str, Any], *, index: int | None = None) -> str:
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
    action = row.get("invoke_command")
    action_text = f"  run={action}" if action else ""
    category = row.get("category")
    category_text = f"  category={category}" if category else ""
    row_id = row.get("id")
    row_id_text = f"  id={row_id}" if row_id else ""
    state = row.get("selection_state")
    state_text = f"  state={state}" if state else ""
    related_to = row.get("related_to")
    related_text = f"  related_to={related_to}" if related_to else ""
    prefix = f"{index:>2}. " if index is not None else ""
    lines = [
        (
            f"{prefix}{entity_type:>10}  {name:<40} "
            f"score={score_text}{suffix}{category_text}{row_id_text}"
            f"{state_text}{related_text}{action_text}"
        )
    ]
    tldr = row.get("tldr")
    if tldr:
        lines.append(f"    {tldr}")
    reason = row.get("reason")
    if reason:
        lines.append(f"    reason={reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.rejection_mode != "use" and not args.session_id:
        parser.error("--rejection-mode requires --session-id")
    query = " ".join(args.query).strip()
    top_k = max(1, min(int(args.top_k), cfg.recommendation_top_k))
    related_top_n = max(1, min(int(args.related_top_n), cfg.recommendation_top_k))
    selected = _split_selection_values(args.selected)
    explicit_rejected = _split_selection_values(args.rejected)
    rejected = (
        recommendation_rejections(
            explicit_rejected,
            session_id=args.session_id,
            rejection_mode=args.rejection_mode,
        )
        if args.session_id
        else explicit_rejected
    )
    active = _split_selection_values(args.active)
    baseline_context = _split_selection_values(args.baseline_context)
    bundle_kwargs: dict[str, Any] = {"top_k": top_k}
    if selected:
        bundle_kwargs["selected"] = selected
    if rejected:
        bundle_kwargs["rejected"] = rejected
    if args.session_id:
        bundle_kwargs["session_id"] = args.session_id
        bundle_kwargs["rejection_mode"] = "ignore"
    if active:
        bundle_kwargs["active_context"] = active
    if baseline_context:
        bundle_kwargs["baseline_context"] = baseline_context
    if args.include_baseline_context:
        bundle_kwargs["include_baseline_context"] = True
    if args.show_unavailable:
        bundle_kwargs["include_unavailable"] = True
    if args.local_code_task:
        bundle_kwargs["local_code_task"] = True
    if args.no_api_keys:
        bundle_kwargs["no_api_keys"] = True
    if args.language:
        bundle_kwargs["language"] = args.language
    results = recommend_bundle(query, **bundle_kwargs)
    related_baseline_context = _effective_baseline_context(
        baseline_context,
        include_baseline_context=args.include_baseline_context,
    )
    related_rejected = _split_selection_values(rejected + active + related_baseline_context)
    related_context = _recommendation_context_from_args(query, _recommendation_filter_args(args))
    related_fetch_top_n = _related_fetch_top_n(
        top_n=related_top_n,
        excluded_count=len(related_rejected),
        context=related_context,
    )
    related_kwargs: dict[str, Any] = {
        "rejected": related_rejected,
        "top_n": related_fetch_top_n,
    }
    if args.session_id:
        related_kwargs["session_id"] = args.session_id
        related_kwargs["rejection_mode"] = "ignore"
    raw_related_results = recommend_related(selected, **related_kwargs) if selected else []
    related_results = _filter_related_results(
        raw_related_results,
        context=related_context,
        top_n=related_top_n,
    )
    if args.json:
        payload: dict[str, Any] = {"query": query, "results": results}
        if selected or rejected or active or baseline_context or args.session_id:
            payload["selection"] = {
                "selected": selected,
                "rejected": explicit_rejected,
                "active_context": active,
                "baseline_context": baseline_context,
                "related_results": related_results,
            }
            if args.session_id:
                payload["selection"]["effective_rejected"] = rejected
                payload["selection"]["session_id"] = args.session_id
                payload["selection"]["rejection_mode"] = args.rejection_mode
        print(json.dumps(payload, indent=2))
        return 0
    if not results:
        print("No recommendations above the configured score threshold.", file=sys.stderr)
    else:
        for index, row in enumerate(results, start=1):
            print(_render_row(row, index=index))
    if selected:
        print("\nRelated recommendations:")
        if related_results:
            for index, row in enumerate(related_results, start=1):
                print(_render_row(row, index=index))
        else:
            print("  No related recommendations above the configured score threshold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
