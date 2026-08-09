---
name: code-review
description: Review a Git diff against repository standards and its originating requirements. Use for pull requests, branches, work-in-progress changes, or changes since a commit, tag, or merge-base.
---

# Review code changes

## Establish scope

Honor a fixed point supplied by the user. Otherwise infer a sensible comparison
from the repository state, such as the default-branch merge-base. Ask only when
different choices would materially change the review.

Verify that the ref resolves and inspect the diff and relevant commit messages.
Report an empty or unavailable comparison instead of manufacturing findings.

## Collect references

Use the repository's actual standards and the originating issue, PRD, or spec
when available. Follow repository conventions for fetching tracker context. If
requirements cannot be found, review the standards axis and state that spec
conformance was not verified.

## Evaluate two axes

- **Standards:** Does the change follow documented repository conventions and
  avoid material design regressions?
- **Spec:** Does it implement the requested behavior without omissions,
  incorrect behavior, or unrelated scope?

Use deterministic checks for properties tooling can decide. For qualitative
review, load [review heuristics](references/review-heuristics.md) only when the
diff warrants deeper design analysis.

The axes are independent and may be evaluated concurrently when the diff is
large enough to benefit. Keep a small review local, and avoid parallel work
when coordination would cost more than it saves.

## Report

Lead with actionable findings, citing the affected file and evidence. Separate
documented violations from judgment calls and keep Standards and Spec findings
distinct so one axis cannot mask the other. State unavailable evidence and
report when no material findings remain.
