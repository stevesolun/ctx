# Debugging playbook

Load the sections that fit the failure. These are options, not mandatory phases.

## Feedback-loop options

- A focused unit, integration, or end-to-end test.
- A CLI or HTTP invocation with fixture input and a precise assertion.
- A browser script that observes relevant DOM, console, or network behavior.
- Replay of a captured request, trace, event stream, or crash artifact.
- A small harness around the affected service or function.
- Property or fuzz testing for input-dependent failures.
- `git bisect run` when the failure appeared between known revisions.
- Differential execution across versions, configurations, or environments.
- A human-assisted script when automation cannot reach the symptom directly.

Prefer a signal that exercises the user's actual symptom. Improve it where
useful by narrowing setup, pinning time or randomness, isolating external state,
or increasing the reproduction rate of intermittent failures.

## Intermittent failures

Measure a baseline reproduction rate. Repetition, controlled concurrency,
stress, or targeted timing changes can make the failure easier to observe.
Preserve seeds, inputs, schedules, and environment details that explain a
successful reproduction.

## Hypotheses and probes

Frame a hypothesis as a prediction:

> If X is the cause, changing or observing Y should produce Z.

Favor probes that distinguish several plausible causes. Debuggers and REPL
inspection often provide cleaner evidence than broad logging. If temporary logs
are appropriate, use a unique searchable prefix and remove them after use.

For performance problems, compare measured baselines and inspect profiles,
query plans, allocation patterns, or revision history before choosing a fix.

## Regression coverage

A useful regression test represents the real failure at a stable seam. Avoid a
shallow test that cannot exercise the failing interaction. When no honest seam
exists, record that architectural limitation and rely on the strongest
available reproduction for verification.

After the fix, consider:

- whether the original symptom is gone;
- whether focused and nearby regression checks pass;
- whether temporary instrumentation and harnesses remain;
- whether the explanation separates observation from inference; and
- whether the same structural weakness is likely to recur.
