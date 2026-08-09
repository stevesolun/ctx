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
        help="Find the cheapest AI coding setup that works on this repository.",
        description=(
            "Analyze a repository, optionally test candidate AI coding "
            "configurations against real tasks from its history, and generate "
            "the winning configuration. Bare `ctx fit` runs no model and spends "
            "nothing."
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
        "--test",
        action="store_true",
        help=(
            "Evaluate candidate configurations against real tasks. Requires "
            "--budget. Without provider credentials this runs in simulation, "
            "which proves the pipeline but never the repository."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Generate the winning configuration. Shows every change before writing.",
    )
    parser.add_argument(
        "--pr",
        action="store_true",
        help="Prepare a branch and pull-request body. Never merges.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt when applying changes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Describe what a full Fit evaluation would do without doing it. "
            "Profiling itself never executes a model."
        ),
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        metavar="USD",
        help=(
            "Maximum dollars CTX Fit may spend on an evaluation. Required before "
            "any paid execution; without it CTX Fit only plans."
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
        "  4. run each candidate against the baseline, repeated for reliability",
        "  5. verify every trial with the repository's own commands above",
        "  6. keep only candidates that reliably pass, then pick the cheapest",
        "  7. open a PR containing the winning configuration",
        "",
        "The winner is the cheapest configuration that reliably works — "
        "reliability is a requirement, not a tie-break. If nothing beats your "
        "current setup, CTX Fit says so and recommends keeping it.",
        "",
        "No model was invoked and nothing was spent.",
    ]
    return "\n".join(lines)


DEFAULT_MODEL = "gpt-4o-mini"


def _build_plan(profile: FitProfile, budget: float | None, *, model: str = DEFAULT_MODEL) -> object:
    """Plan the experiment without calling a model or spending anything."""

    from ctx.engine.planner import BoundedCapabilityPlanner
    from ctx.fit.candidates import CandidateSet, generate_candidates
    from ctx.fit.experiment import ModelPrice, plan_experiment
    from ctx.fit.release_catalog import open_release_candidate_source
    from ctx.fit.tasks import derive_tasks

    source = open_release_candidate_source()
    if source is None:
        candidates = CandidateSet(
            abstained=True,
            abstention_reason="the capability catalog could not be opened",
        )
    else:
        candidates = generate_candidates(profile, BoundedCapabilityPlanner(source=source))

    test_command = profile.verification.best("test")
    verify = test_command.command if test_command else ("python", "-m", "pytest", "-q")
    tasks = derive_tasks(profile.repo_path, verify_command=verify, limit=3)

    return plan_experiment(
        profile,
        candidates,
        task_count=len(tasks.tasks),
        budget_usd=budget,
        price=ModelPrice.from_litellm(model),
    )


def _format_plan(plan: object) -> str:
    """Render the experiment plan and the budget decision."""

    from ctx.fit.experiment import ExperimentPlan

    assert isinstance(plan, ExperimentPlan)
    lines = ["", "Experiment plan"]
    lines.append(f"  Candidates:        {plan.candidate_count}")
    lines.append(f"  Tasks:             {plan.task_count or 'not yet derived'}")
    lines.append(f"  Trials per task:   {plan.trials_per_task} (for reliability)")
    lines.append(f"  Total executions:  {plan.executions}")
    if plan.verification:
        lines.append(f"  Verified with:     {plan.verification[0]}")

    cost = plan.cost
    if plan.executions == 0:
        # A cost for zero executions is arithmetically $0 and completely
        # meaningless. Printing it would read as "this is free" rather than
        # "there is no experiment to price".
        lines.append("  Estimated cost:    not applicable — nothing would run")
    elif cost.is_known:
        lines.append(f"  Estimated cost:    ${cost.low_usd}-${cost.high_usd}")
    else:
        lines.append("  Estimated cost:    unknown")
        lines.append(f"                     {cost.basis}")

    lines.append("")
    lines.append(
        f"Ready to run: {plan.explanation}."
        if plan.can_execute
        else f"Not runnable: {plan.explanation}."
    )
    for warning in plan.warnings:
        lines.append(f"  - {warning}")
    return "\n".join(lines)


def default_namespace(repo: str = ".") -> argparse.Namespace:
    """Arguments for a bare ``ctx`` invocation: analyze, spend nothing."""

    return argparse.Namespace(
        repo=repo,
        json=False,
        test=False,
        apply=False,
        pr=False,
        yes=False,
        dry_run=False,
        budget=None,
        max_depth=4,
    )


def _provider_available() -> bool:
    """Whether real execution is possible. Absence means simulation, not failure."""

    import os

    return any(
        os.environ.get(name) for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CTX_FIT_API_KEY")
    )


def _run_evaluation(
    profile: FitProfile, args: argparse.Namespace
) -> tuple[object, tuple[object, ...], str]:
    """Run the full evaluation loop. Returns (recommendation, candidates, banner)."""

    from dataclasses import replace

    from ctx.engine.planner import BoundedCapabilityPlanner
    from ctx.fit.candidates import CandidateSet, generate_candidates
    from ctx.fit.execution import execute_trials, make_simulated_runner
    from ctx.fit.recommend import recommend
    from ctx.fit.release_catalog import open_release_candidate_source
    from ctx.fit.tasks import derive_tasks

    source = open_release_candidate_source()
    if source is None:
        candidates = CandidateSet(
            abstained=True, abstention_reason="the capability catalog could not be opened"
        )
    else:
        candidates = generate_candidates(profile, BoundedCapabilityPlanner(source=source))

    test_command = profile.verification.best("test")
    verify = test_command.command if test_command else ("python", "-m", "pytest", "-q")
    derived = derive_tasks(profile.repo_path, verify_command=verify, limit=3)

    # A task is only usable once observed to start red. Real red-gating runs the
    # test against the reverted tree; in simulation there is nothing to observe,
    # so tasks are accepted only under the simulated banner.
    simulated = not _provider_available()
    if simulated:
        # Nothing is executed, so redness cannot be observed; tasks are accepted
        # only under the simulated banner, which claims nothing.
        tasks = tuple(replace(task, starts_red=True) for task in derived.tasks)
        runner = make_simulated_runner()
    else:
        # The live runner proves redness itself, per trial, inside an isolated
        # workspace, so tasks enter unvalidated and are gated there.
        from ctx.fit.live_runner import make_live_runner
        from ctx.fit.providers import build_agent_driver

        tasks = tuple(replace(task, starts_red=True) for task in derived.tasks)
        runner = make_live_runner(profile.repo_path, build_agent_driver())

    report = execute_trials(
        candidates.candidates,
        tasks,
        runner,
        trials_per_task=3,
        simulated=simulated,
    )
    recommendation = recommend(
        report, candidates.candidates, task_count=len(tasks), trials_per_task=3
    )

    banner = (
        "No provider credentials found, so this ran in SIMULATION. It proves the "
        "evaluation pipeline works end to end and proves nothing about this "
        "repository. Set OPENAI_API_KEY or ANTHROPIC_API_KEY for a real run."
        if simulated
        else "Real execution is not wired yet; this run was simulated."
    )
    return recommendation, candidates.candidates, banner


def _format_recommendation(recommendation: object) -> str:
    from ctx.fit.recommend import Recommendation

    assert isinstance(recommendation, Recommendation)
    lines = ["", recommendation.headline, ""]
    lines.append(f"{'Candidate':<14}{'Verified':>10}{'Cost':>10}  Qualified")
    for item in recommendation.ranked:
        cost = f"${item.total_cost_usd}" if item.total_cost_usd is not None else "unknown"
        mark = "yes" if item.qualified else f"no ({item.exclusion_reason})"
        lines.append(f"{item.candidate_id:<14}{item.verified}/{item.scored:<8}{cost:>10}  {mark}")
    lines.append("")
    for line in recommendation.reasoning:
        lines.append(f"  {line}")
    lines.append("")
    lines.append(f"Confidence: {recommendation.confidence}")
    if recommendation.limitations:
        lines.append("")
        lines.append("Limitations")
        for line in recommendation.limitations:
            lines.append(f"  - {line}")
    return "\n".join(lines)


def cmd_fit(args: argparse.Namespace) -> int:
    """Run the Fit profiler. Returns a process exit code."""

    from ctx.fit.profile import build_fit_profile

    try:
        profile = build_fit_profile(args.repo, max_depth=args.max_depth)
    except NotADirectoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    wants_plan = bool(args.dry_run or args.budget is not None or args.test)

    # Evaluation is the only step that can spend, so it is gated twice: an
    # explicit --test, and a budget the plan must fit.
    evaluating = bool(args.test) and not args.dry_run
    recommendation: object | None = None
    candidates: tuple[object, ...] = ()
    banner = ""
    if evaluating:
        plan = _build_plan(profile, args.budget)
        if not plan.can_execute:  # type: ignore[attr-defined]
            if not args.json:
                print(_format_profile(profile))
                print(_format_plan(plan))
                print("\nNothing was run and nothing was spent.")
            else:
                payload = profile.to_dict()
                payload["plan"] = plan.to_dict()  # type: ignore[attr-defined]
                print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        recommendation, candidates, banner = _run_evaluation(profile, args)

    if args.json:
        from ctx.fit.readiness import score_readiness

        payload = profile.to_dict()
        payload["readiness"] = score_readiness(profile).to_dict()
        if wants_plan:
            plan = _build_plan(profile, args.budget)
            payload["plan"] = plan.to_dict()  # type: ignore[attr-defined]
            payload["dry_run"] = bool(args.dry_run)
        if recommendation is not None:
            payload["recommendation"] = recommendation.to_dict()  # type: ignore[attr-defined]
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(_format_profile(profile))
    if args.dry_run:
        print(_format_dry_run(profile))
    if wants_plan and not evaluating:
        print(_format_plan(_build_plan(profile, args.budget)))

    if recommendation is not None:
        print(_format_recommendation(recommendation))
        if banner:
            print(f"\n{banner}")
        if args.apply or args.pr:
            return _handle_apply(recommendation, candidates, args)
    elif args.apply or args.pr:
        print(
            "\nNothing to apply: run `ctx fit --test --budget N` first so there is "
            "evidence to act on."
        )
        return 1
    return 0


def _handle_apply(
    recommendation: object,
    candidates: tuple[object, ...],
    args: argparse.Namespace,
) -> int:
    """Preview, then optionally write, the winning configuration."""

    from ctx.fit.apply import apply_plan, plan_apply

    plan = plan_apply(recommendation, candidates, repo_path=args.repo)  # type: ignore[arg-type]
    if not plan.can_apply:
        print(f"\nNo changes proposed: {plan.explanation}.")
        return 0

    print("\nProposed changes")
    for artifact in plan.artifacts:
        print(f"  {artifact.action}: {artifact.path} ({artifact.reason})")
    if args.pr:
        print(f"\nBranch: {plan.branch}")
        print(f"PR:     {plan.pr_title}")
        print("\n--- pull request body ---")
        print(plan.pr_body)
        print("--- end ---")
        print("\nCTX Fit never merges. Review before merging.")

    if not args.apply:
        return 0
    if not args.yes:
        print("\nRe-run with --yes to write these files.")
        return 0

    written = apply_plan(plan, args.repo)
    print(f"\nWrote: {', '.join(written)}")
    return 0


__all__ = ["cmd_fit", "register"]
