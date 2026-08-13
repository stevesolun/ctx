---
name: diagnosing-bugs
description: Diagnose hard bugs and performance regressions with reproducible evidence, focused hypotheses, and proportional instrumentation. Use when something is broken, failing, throwing, intermittently wrong, or unexpectedly slow.
---

# Diagnosing Bugs

Start from evidence and adapt the depth of the investigation to the problem.
Read relevant repository context and architectural decisions when they exist.

## Establish a useful signal

Reproduce the reported symptom with the cheapest signal that distinguishes
broken from fixed. A focused test or script is ideal when practical; logs,
traces, snapshots, comparisons, or measured timings may be better for other
failures. Tighten the loop by improving speed, specificity, and determinism.

If an exact reproduction is unavailable, continue with the strongest evidence
available and state the limitation. Request an artifact, access, or temporary
instrumentation only when it would materially improve the diagnosis.

For feedback-loop options and debugging tactics, load the
[debugging playbook](references/debugging-playbook.md) as needed. For a rare
manual reproduction, adapt
[`scripts/hitl-loop.template.sh`](scripts/hitl-loop.template.sh).

## Narrow and explain

Minimize the reproducing scenario when doing so will shrink the search space.
Form a small ranked set of falsifiable hypotheses from the evidence, then choose
probes that best distinguish them. Share hypotheses with the user when their
domain knowledge could redirect the investigation; do not make routine progress
depend on a checkpoint.

Use targeted instrumentation at boundaries that separate plausible causes.
Change as little as practical per probe. For performance regressions, establish
a baseline and use profiling, query plans, or bisection before optimizing.

## Fix and verify

Add a regression test when a stable seam can represent the real failure. If it
cannot, explain the coverage gap rather than adding a misleading test. Apply the
smallest justified fix, then rerun both the focused signal and relevant nearby
checks.

Remove temporary instrumentation and artifacts unless the user wants to retain
them. Report the observed cause, the evidence that supports it, what was
verified, and any remaining uncertainty. Recommend architectural follow-up only
when the diagnosis exposes a concrete recurring weakness.
