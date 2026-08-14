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
import shlex
import sys
import time
from typing import TYPE_CHECKING

from ctx.fit.verification import (
    VERIFICATION_ENVIRONMENT_ASSUMPTION,
    VERIFIER_TRUST_ASSUMPTION,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ctx.fit.apply import ApplyPlan
    from ctx.fit.execution import ExecutionReport
    from ctx.fit.experiment import ExperimentOutcome, ExperimentPlan, ResolvedExperiment
    from ctx.fit.profile import FitProfile
    from ctx.fit.recommend import Recommendation


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
        help=(
            "Write the winning configuration into the working tree, showing "
            "every change first. Runs no git command: review with `git diff`, "
            "discard with `git checkout`."
        ),
    )
    parser.add_argument(
        "--pr",
        action="store_true",
        help=(
            "Open a pull request with the winning configuration: creates a "
            "branch, commits, pushes and runs `gh pr create`. Requires a clean "
            "working tree and an authenticated `gh`. Every command is printed "
            "before any of them runs, and nothing is ever merged."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Confirm requested actions without prompting. With --test, this "
            "authorizes the displayed experiment up to --budget. It never "
            "requests --apply or --pr by itself; those remain separate flags."
        ),
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
            "This repository has the static evidence needed to plan an evaluation: "
            "it declares deterministic tests. Whether those tests can execute is "
            "checked only inside the campaign."
        )
        lines.append(VERIFICATION_ENVIRONMENT_ASSUMPTION)
        lines.append(
            "What varies between candidates here is the capability set. The model "
            "is one global choice applied to every candidate, the instruction files "
            "are the repository's own and identical across candidates, and only one "
            "coding agent is currently supported — so none of those three is being "
            "compared."
        )
    else:
        lines.append(
            "This repository cannot yet be evaluated honestly: without declared "
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
        else "Next: add a declared test suite, then re-run `ctx fit`."
    )
    return "\n".join(lines)


def _format_dry_run(profile: FitProfile) -> str:
    evaluable = [dimension.name for dimension in profile.dimensions if dimension.evaluable]
    lines = [
        "",
        "Dry run — a full Fit evaluation would:",
        "  1. profile this repository            (done; no cost)",
        "  2. derive representative tasks        from recent commits (no cost)",
        f"  3. generate bounded candidates over   {', '.join(evaluable) or 'nothing evaluable'}",
        "  4. run each candidate against the baseline, repeated for reliability",
        "  5. verify every trial with the repository's own commands above",
        "  6. keep only candidates that reliably pass, then pick the cheapest",
        "  7. with --apply, write the winning configuration into your working "
        "tree; with --pr, branch, commit, push and open the pull request "
        "(nothing is ever merged)",
        "",
        "The winner is the cheapest configuration that reliably works — "
        "reliability is a requirement, not a tie-break. If nothing beats your "
        "current setup, CTX Fit says so and recommends keeping it.",
        "",
        VERIFIER_TRUST_ASSUMPTION,
        VERIFICATION_ENVIRONMENT_ASSUMPTION,
        "",
        "No model was invoked and nothing was spent.",
    ]
    return "\n".join(lines)


def _format_plan(plan: ExperimentPlan) -> str:
    """Render the experiment plan and the budget decision."""

    lines = ["", "Experiment plan"]
    lines.append(f"  Candidates:        {plan.candidate_count}")
    for candidate in plan.candidates:
        capability_count = len(candidate.capability_ids)
        lines.append(
            f"    - {candidate.candidate_id} ({candidate.role}; {capability_count} capabilities)"
        )
        capabilities = ", ".join(candidate.capability_ids) or "none added"
        lines.append(f"      capabilities: {capabilities}")
        lines.append(f"      model: {candidate.model or 'provider default'}")
        lines.append(f"      configuration: {candidate.configuration_hash}")
        instructions = ", ".join(candidate.instructions) or "none"
        lines.append(f"      repository instructions: {instructions}")
    lines.append(f"  Tasks:             {plan.task_count or 'not yet derived'}")
    for task in plan.tasks:
        lines.append(f"    - {task.title} [{task.task_id}]")
        lines.append(f"      provenance: {task.provenance}")
        lines.append(f"      editable source: {', '.join(task.source_paths)}")
        lines.append(f"      protected tests: {', '.join(task.test_paths)}")
        lines.append(f"      verify: {' '.join(task.verify_command)}")
    lines.append(f"  Trials per task:   {plan.trials_per_task} (for reliability)")
    lines.append(f"  Reliability floor: {plan.reliability_floor:.0%}")
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
        # The basis is what makes the number checkable. Hiding it whenever the
        # estimate succeeds leaves the user approving a figure they cannot audit.
        lines.append(f"                     {cost.basis}")
    else:
        lines.append("  Estimated cost:    unknown")
        lines.append(f"                     {cost.basis}")
    budget = f"${plan.budget_usd}" if plan.budget_usd is not None else "not supplied"
    lines.append(f"  Budget ceiling:    {budget}")

    lines.append("")
    lines.append(
        f"Ready to confirm: {plan.explanation}."
        if plan.can_authorize
        else f"Not runnable: {plan.explanation}."
    )
    if plan.can_authorize:
        status = "confirmed" if plan.authorized else "confirmation required before execution"
        lines.append(f"  Authorization:     {status}")
        lines.append(f"  Plan digest:       {plan.executable_digest}")
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


def _provider_available(model: str) -> bool:
    """Whether the selected model's matching credential requests a live run."""

    from ctx.fit.providers import resolve_model_credential

    return resolve_model_credential(model).configured


def _confirm_evaluation(plan: ExperimentPlan, args: argparse.Namespace) -> bool:
    """Obtain consent for this displayed plan, or fail closed without a TTY.

    JSON is a machine contract and must remain one parseable document, so it
    never prompts. Non-interactive callers confirm explicitly with ``--yes``;
    an interactive caller may answer after seeing the full plan.
    """

    if args.yes:
        return True
    if args.json or not sys.stdin.isatty():
        return False
    budget = plan.budget_usd
    try:
        answer = input(f"\nAuthorize up to ${budget} to run exactly this experiment? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


def _banner(outcome: ExperimentOutcome) -> str:
    """The last line the user reads, which has to match what happened."""

    report = outcome.report
    if outcome.simulated:
        return (
            "No provider credentials found, so this ran in SIMULATION. It proves the "
            "evaluation pipeline works end to end and proves nothing about this "
            "repository. Set OPENAI_API_KEY or ANTHROPIC_API_KEY for a real run."
        )
    # This arm used to claim the run was simulated, contradicting both the
    # report and the JSON payload of the same run: real trials had executed and
    # real money had been spent.
    spend = (
        f"${report.spent_usd} was spent"
        if report.spent_usd is not None
        else "total spend was not tracked"
    )
    return (
        f"This was a real run: {report.trials_run} trial(s) executed against "
        f"throwaway copies of this repository and {spend}."
    )


def _format_recommendation(recommendation: Recommendation) -> str:
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


def _format_budget_stop(report: ExecutionReport) -> str:
    """Say that the campaign stopped early, and what that costs the comparison."""

    if not report.budget_stop:
        return ""
    return "\n".join(
        [
            "",
            f"Stopped on budget: ${report.spent_usd} of the ${report.budget_usd} "
            f"authorized was spent and {report.trials_skipped_budget} trial(s) "
            "never ran.",
            f"  Reason: {report.budget_stop}.",
            "  Candidates did not all get the same number of trials, so the "
            "comparison above rests on truncated evidence. Re-run with a larger "
            "--budget to finish it.",
        ]
    )


def _json_payload(
    profile: FitProfile,
    args: argparse.Namespace,
    *,
    plan: object | None = None,
    recommendation: object | None = None,
    report: object | None = None,
) -> dict[str, object]:
    """The one JSON document every ``--json`` path emits.

    Each branch used to assemble its own, so a blocked plan silently dropped
    ``readiness`` and ``dry_run`` while still declaring the same schema version.
    A consumer's key set must depend on the schema and the flags, never on which
    branch happened to produce the answer.
    """

    from ctx.fit.readiness import score_readiness

    payload = profile.to_dict()
    payload["verification_environment_assumption"] = VERIFICATION_ENVIRONMENT_ASSUMPTION
    payload["readiness"] = score_readiness(profile).to_dict()
    if plan is not None:
        payload["plan"] = plan.to_dict()  # type: ignore[attr-defined]
        payload["dry_run"] = bool(args.dry_run)
    if recommendation is not None:
        payload["recommendation"] = recommendation.to_dict()  # type: ignore[attr-defined]
    if report is not None:
        # What was actually spent against the authorization, and whether the
        # campaign was cut short by it: a machine-readable consumer needs
        # that as much as a human does.
        payload["execution"] = report.to_dict()  # type: ignore[attr-defined]
    return payload


def cmd_fit(args: argparse.Namespace) -> int:
    """Run the Fit profiler. Returns a process exit code."""

    from ctx.fit.profile import build_fit_profile

    if args.json and (args.apply or args.pr):
        # The JSON branch returns before the apply handling, so honouring these
        # would have meant claiming success for work that never happened.
        print(
            "error: --apply and --pr cannot be combined with --json. Re-run "
            "without --json to review and write the winning configuration.",
            file=sys.stderr,
        )
        return 2

    try:
        profile = build_fit_profile(args.repo, max_depth=args.max_depth)
    except NotADirectoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Imported here rather than at the top of the module: `ctx` dispatches every
    # subcommand through this file, and a run that never plans must not pay for
    # the experiment stack.
    from ctx.fit.experiment import authorize_experiment, resolve_experiment, run_experiment

    wants_plan = bool(args.dry_run or args.budget is not None or args.test)
    # One derivation, read twice. The plan below and the campaign further down
    # are views of this object, so the experiment the user approves is the
    # experiment their money buys.
    experiment: ResolvedExperiment | None = (
        resolve_experiment(profile, budget_usd=args.budget) if wants_plan else None
    )
    plan = experiment.plan if experiment is not None else None

    # Evaluation is the only step that can spend, so it is gated twice: an
    # explicit --test, and a budget the plan must fit.
    evaluating = bool(args.test) and not args.dry_run
    outcome: ExperimentOutcome | None = None
    profile_rendered = False
    if evaluating:
        assert experiment is not None and plan is not None
        if not experiment.can_authorize:
            if not args.json:
                print(_format_profile(profile))
                print(_format_plan(plan))
                print("\nNothing was run and nothing was spent.")
            else:
                print(json.dumps(_json_payload(profile, args, plan=plan), indent=2, sort_keys=True))
            return 1

        if args.json:
            # One JSON response cannot be both the review shown before spend
            # and the result emitted after spend. Until a separate
            # content-addressed authorization token is supplied by a second
            # invocation, JSON remains a plan-only, zero-spend surface.
            payload = _json_payload(profile, args, plan=plan)
            payload["execution_refusal"] = (
                "JSON evaluation is plan-only; review this plan and run without "
                "--json to authorize it interactively or with --yes"
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1

        # Human output is the pre-spend review, so it must reach the terminal
        # before the provider stack is even imported. JSON cannot prompt or
        # emit a second document; its safe non-interactive contract is --yes.
        print(_format_profile(profile))
        print(_format_plan(plan))
        # ``--yes`` may be used with redirected output, where stdout is
        # block-buffered. Ensure the preview is observable before the next
        # line can enter the provider boundary.
        sys.stdout.flush()
        profile_rendered = True
        if not _confirm_evaluation(plan, args):
            if args.json:
                print(json.dumps(_json_payload(profile, args, plan=plan), indent=2, sort_keys=True))
            else:
                print(
                    "\nNothing was run and nothing was spent. Re-run with --yes "
                    "to authorize this exact plan non-interactively."
                )
            return 1

        experiment = authorize_experiment(experiment, expected_digest=plan.executable_digest)
        plan = experiment.plan

        # Imported here rather than at the top of the function: a bare profile
        # run must not pay for the provider stack it will never touch.
        from ctx.fit.providers import ProviderUnavailable

        try:
            outcome = run_experiment(experiment, live=_provider_available(experiment.model))
        except ProviderUnavailable as exc:
            # Credentials are present, so a real run is what was asked for.
            # Falling back to simulation would answer a different question, and
            # letting this escape printed a traceback out of a product whose
            # contract is to refuse cleanly.
            print(f"error: a real evaluation cannot run here: {exc}", file=sys.stderr)
            print(
                "Nothing was run and nothing was spent. Install CTX so `ctx` is on "
                "PATH, or unset the selected model's matching credential to run in simulation.",
                file=sys.stderr,
            )
            return 1

    if args.json:
        print(
            json.dumps(
                _json_payload(
                    profile,
                    args,
                    plan=plan,
                    recommendation=outcome.recommendation if outcome is not None else None,
                    report=outcome.report if outcome is not None else None,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if not profile_rendered:
        print(_format_profile(profile))
    if args.dry_run:
        print(_format_dry_run(profile))
    if plan is not None and not evaluating:
        print(_format_plan(plan))

    if outcome is not None:
        print(_format_recommendation(outcome.recommendation))
        if stopped := _format_budget_stop(outcome.report):
            print(stopped)
        print(f"\n{_banner(outcome)}")
        if args.apply or args.pr:
            return _handle_apply(outcome.recommendation, outcome.candidates, args)
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
    """Preview, then write the winning configuration — or open a pull request."""

    from ctx.fit.apply import apply_plan, plan_apply

    plan = plan_apply(
        recommendation,  # type: ignore[arg-type]
        candidates,  # type: ignore[arg-type]
        repo_path=args.repo,
        # Per-run, because a branch name is now a branch: two runs a week apart
        # must not collide on `ctx-fit/run` and make the second one unopenable.
        run_id=time.strftime("%Y%m%d-%H%M%S"),
    )
    if not plan.can_apply:
        # Not an error: "your current setup already won" is a correct answer to
        # the question that was asked, and exiting non-zero would fail a CI job
        # for the product working properly.
        print(f"\nNo changes proposed: {plan.explanation}.")
        return 0

    print("\nProposed changes")
    for artifact in plan.artifacts:
        print(f"  {artifact.action}: {artifact.path} ({artifact.reason})")

    if args.pr:
        if args.apply:
            # Said out loud rather than silently dropped: --pr writes the same
            # files on its way to the commit, so --apply adds nothing, and a
            # user who passed both should not have to guess which one ran.
            print("\n--apply adds nothing here: --pr writes the same files before it commits.")
        return _handle_pull_request(plan, args)

    if not args.apply:
        return 0
    if not args.yes:
        print("\nRe-run with --yes to write these files.")
        return 0

    written = apply_plan(plan, args.repo)
    print(f"\nWrote: {', '.join(written)}")
    # --apply runs no git command by design, so the review and the undo are the
    # two the user already trusts. Naming them is the whole handover.
    print(
        "Nothing was committed and no branch was created. Review with `git diff`, "
        f"discard with `git checkout -- {' '.join(written)}`."
    )
    return 0


def _handle_pull_request(plan: ApplyPlan, args: argparse.Namespace) -> int:
    """Gate, announce, and then actually open the pull request."""

    from ctx.fit.apply import open_pull_request, plan_pull_request

    print(f"\nPR title: {plan.pr_title}")
    print("\n--- pull request body ---")
    print(plan.pr_body)
    print("--- end ---")

    pull_request = plan_pull_request(plan, args.repo)
    if not pull_request.can_open:
        # A refusal here is the environment blocking work the user asked for,
        # unlike "no change is warranted" above, so it exits non-zero.
        print(f"\nNo pull request was opened: {pull_request.explanation}.", file=sys.stderr)
        # Precise rather than reassuring: the gate does run read-only probes,
        # and this command's whole problem was claiming more than it did.
        print(
            "The repository is unchanged: nothing written, no branch, no commit, no push.",
            file=sys.stderr,
        )
        return 1

    # Announced in full before a single one of them runs: this is the only path
    # in CTX that writes to a remote.
    print(
        f"\nTo open it, CTX Fit will write {', '.join(pull_request.writes)} into the "
        "working tree, then run, in order:"
    )
    for rendered in pull_request.rendered_commands:
        print(f"    {rendered}")
    print("(the pull-request body above is piped to `gh` on standard input)")

    if not args.yes:
        print("\nNothing has been changed. Re-run with --yes to run these commands.")
        return 0

    result = open_pull_request(plan, pull_request, args.repo)
    if not result.opened:
        # Rendered the same way it was announced, so the user can match the two.
        print(
            f"\nStopped at `{shlex.join(result.failed or ())}`: {result.detail}.",
            file=sys.stderr,
        )
        print(
            f"{len(result.ran)} of {len(pull_request.commands)} commands ran. "
            # The write happens before the first command, so it has happened
            # whatever went wrong; recovery advice that omits it is incomplete.
            f"{', '.join(result.written)} is written into the working tree. Return "
            f"to your branch with `git checkout {pull_request.original_branch}`, "
            f"remove the new one with `git branch -D {pull_request.branch}` if it "
            f"was created, and discard the file with `git checkout -- "
            f"{' '.join(result.written)}`.",
            file=sys.stderr,
        )
        return 1

    print(f"\nOpened: {result.url or 'the pull request (gh printed no URL)'}")
    print(f"Branch {pull_request.branch} is pushed. CTX Fit never merges.")
    return 0


__all__ = ["cmd_fit", "register"]
