"""``ctx fit`` — repository-specific AI coding stack optimization.

Milestone 1 scope: understand the repository and report a structured Fit
profile.  This command performs **no model execution and spends nothing**;
later milestones add candidate evaluation behind an explicit budget.

The output deliberately leads with decisions rather than internals. Graph
statistics, entity taxonomy, and planner detail belong in diagnostic output,
not in the answer a developer reads.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ctx.fit.profile import FitProfile


def register(sub: argparse._SubParsersAction) -> None:
    """Attach the ``fit`` subcommand to the ``ctx`` umbrella parser."""

    parser = sub.add_parser(
        "fit",
        help="Analyze a repository's AI coding setup.",
        description=(
            "Analyze a repository and report which AI coding setup suits it. "
            "This milestone profiles the repository only; it runs no model and "
            "spends nothing."
        ),
    )
    parser.add_argument(
        "repo",
        nargs="?",
        default=".",
        help="Repository path to analyze (default: the current directory).",
    )
    parser.add_argument("--json", action="store_true", help="Emit the Fit profile as JSON.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Describe what a full Fit evaluation would do without doing it. "
            "Profiling itself never executes a model."
        ),
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=4,
        help="Maximum repository scan depth (default: 4).",
    )


def _format_profile(profile: FitProfile) -> str:
    lines: list[str] = []
    stack = profile.stack or {}
    languages = ", ".join(item["name"] for item in stack.get("languages", [])[:4]) or "unknown"

    lines.append(f"Repository: {profile.repo_path}")
    lines.append(f"Languages:  {languages}")
    lines.append("")

    config = profile.existing_ai_config
    lines.append("Current AI coding setup")
    if config.is_configured:
        if config.instruction_files:
            lines.append(f"  Instructions:  {', '.join(config.instruction_files)}")
        if config.tool_config_files:
            lines.append(f"  Tool config:   {', '.join(config.tool_config_files)}")
        for label, count in config.capability_counts:
            lines.append(f"  Installed {label}: {count}")
    else:
        lines.append("  none detected")
    lines.append("")

    lines.append("How this repository verifies itself")
    if profile.verification.commands:
        for command in profile.verification.commands:
            rendered = " ".join(command.command)
            lines.append(f"  {command.kind:<10} {rendered}")
            lines.append(f"  {'':<10} from {command.source} ({command.confidence} confidence)")
    else:
        lines.append("  no verification commands discovered")
    lines.append("")

    from ctx.fit.readiness import score_readiness

    readiness = score_readiness(profile)
    lines.append("AI agent readiness")
    if readiness.score is None:
        lines.append("  could not be assessed for this repository")
    else:
        lines.append(f"  {readiness.score}/100")
        for dimension in readiness.dimensions:
            if dimension.is_assessable:
                lines.append(
                    f"    {dimension.title:<22}{dimension.earned:>3}/{dimension.assessable}"
                )
    lines.append("")

    if readiness.blockers:
        lines.append("Blocking")
        for blocker in readiness.blockers:
            lines.append(f"  - {blocker.title}: {blocker.evidence[0] if blocker.evidence else ''}")
            lines.append(f"    fix: {blocker.remedy}")
        lines.append("")

    top_fixes = readiness.improvements[:3]
    if top_fixes:
        lines.append("Highest-impact improvements")
        for index, fix in enumerate(top_fixes, start=1):
            gain = fix.possible - fix.earned
            lines.append(f"  {index}. {fix.remedy} (+{gain})")
            if fix.evidence:
                lines.append(f"     {fix.evidence[0]}")
        lines.append("")

    # The full dimension breakdown is CTX's own view of its experimental rig,
    # not a fact about the user's repository, so it stays in --json. One human
    # sentence carries the honest scope limit.
    if profile.is_fit_evaluable:
        lines.append(
            "This repository can be evaluated: it has deterministic tests, so a "
            "candidate configuration can be judged on evidence rather than on an "
            "agent's own claim."
        )
        lines.append(
            "We can compare capability sets, instructions and models here — not "
            "coding agents, because only one is currently supported."
        )
    else:
        lines.append(
            "This repository cannot yet be evaluated honestly: without runnable "
            "tests there is no way to tell a configuration that solved a task "
            "from one that only claimed to."
        )

    if profile.warnings:
        lines.append("")
        lines.append("What to fix first")
        for warning in profile.warnings:
            lines.append(f"  - {warning}")

    lines.append("")
    lines.append(
        "Next: `ctx fit --dry-run` shows what a full evaluation would involve."
        if profile.is_fit_evaluable
        else "Next: add runnable tests, then re-run `ctx fit`."
    )
    return "\n".join(lines)


def _format_dry_run(profile: FitProfile) -> str:
    evaluable = [dimension.name for dimension in profile.dimensions if dimension.evaluable]
    lines = [
        "",
        "Dry run — a full Fit evaluation would:",
        "  1. profile this repository            (done; no cost)",
        "  2. derive representative tasks        (not implemented yet)",
        f"  3. generate bounded candidates over   {', '.join(evaluable) or 'nothing evaluable'}",
        "  4. run each candidate against the baseline in a counterbalanced pair",
        "  5. verify every trial with the repository's own commands above",
        "  6. compare verified work against attributable cost",
        "",
        "Estimated cost: not calculable yet — candidate generation and execution "
        "are not implemented, and CTX Fit never guesses a number it cannot derive.",
        "No model was invoked and nothing was spent.",
    ]
    return "\n".join(lines)


def cmd_fit(args: argparse.Namespace) -> int:
    """Run the Fit profiler. Returns a process exit code."""

    from ctx.fit.profile import build_fit_profile

    try:
        profile = build_fit_profile(args.repo, max_depth=args.max_depth)
    except NotADirectoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        from ctx.fit.readiness import score_readiness

        payload = profile.to_dict()
        payload["readiness"] = score_readiness(profile).to_dict()
        if args.dry_run:
            payload["dry_run"] = True
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(_format_profile(profile))
    if args.dry_run:
        print(_format_dry_run(profile))
    return 0


__all__ = ["cmd_fit", "register"]
