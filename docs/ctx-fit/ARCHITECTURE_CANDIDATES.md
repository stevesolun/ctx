# CTX Fit — Architecture Candidates

Ten refactors, ARCH-1 through ARCH-10, produced by a single architecture walk
over `src/ctx/fit/` and `src/ctx/cli/`. Each one names a place where the same
question is answered twice, or where a value is computed and then thrown away
before anyone can act on it. They are candidates, not commitments: nothing here
is scheduled, and an outside contributor is welcome to pick one up.

Two have landed. Eight are open.

## Where this list came from, and what it is not

The walk that produced these ran against the codebase as it stood immediately
before the first public push. Its ledger was a machine-local file that the
repository does not track, so this document is now the list's only home.

That has one consequence worth stating plainly. The ledger recorded a headline,
a reason, and — for some items — a reproduction. It did not record a design.
So each entry below tells you what is wrong and where, not how to fix it. Where
this document says something is "reproduced", that reproduction was run; where
it says a claim was "not separately recorded", nobody has verified it since the
walk and you should treat it as a lead rather than a finding.

Line numbers move. Every file reference below was re-checked against the tree at
the time of writing, but grep for the symbol rather than trusting the number.

## Status at a glance

| ID | One line | Status |
|---|---|---|
| ARCH-1 | Resolve the experiment once | **Done** — `56457cc5` |
| ARCH-2 | Verification owns the executable-tests question | **Done** — `81b71baa` |
| ARCH-3 | `ctx doctor` renders the plan decision instead of re-deriving it | Open |
| ARCH-4 | A trial carries its authorization | Open |
| ARCH-5 | Typed trial verdict carrying attribution, plus per-run logs | Open — design settled in [ADR-015](DECISIONS.md) |
| ARCH-6 | Validate tasks once, above the trial | Open |
| ARCH-7 | Give `CandidateSet.warnings` and `TaskSet.warnings` a delivery channel | Open |
| ARCH-8 | `recommend` reads its own evidence base | Open |
| ARCH-9 | A recommendation carries its comparison | Open |
| ARCH-10 | A recommendation carries the identity of what it ranks | Open |

To confirm the two completed items yourself:

```bash
git log --oneline | grep ARCH
```

---

## ARCH-1 — Resolve the experiment once · **Done** (`56457cc5`)

**What it was.** `cli/fit.py` derived the experiment twice: once to build the
plan shown to the user for authorization, and again to build the campaign that
actually spent money. Two derivations of the same thing can drift, and the one
drift a spend gate cannot tolerate is being priced for one set of tasks and
running another.

**What landed.** A single `resolve_experiment()` in `src/ctx/fit/experiment.py`,
called once at `src/ctx/cli/fit.py:414`, with the plan read off the resolved
object (`plan = experiment.plan`). The plan the user approves and the campaign
their money buys are now the same object. The commit deleted 185 lines from
`cli/fit.py` and added 277 to `fit/experiment.py`, moving the logic behind a
tested seam rather than leaving it in the command handler.

**Why it was blocked for a while.** Both call sites lived in `cli/fit.py`, which
a concurrent session was editing. This is worth knowing because several open
items below touch the same file.

---

## ARCH-2 — Verification owns the executable-tests question · **Done** (`81b71baa`)

**What it was.** "Does this repository have tests that can actually run?" was
answered in two places — once inside `readiness.py` via private helpers, once
inside `verification.py` — with a duplicated skip set between them.

**What landed.** `has_deterministic_verification` is now composed from
`declares_test_command` plus a new `has_executable_tests`, both on
`VerificationInventory` (`src/ctx/fit/verification.py:92`, `:104`). Seven private
names were deleted from `readiness.py`; `_declared_tests` and `_inspectable` no
longer exist there. The readiness check `V1` stopped needing a `root` argument,
which was the measure that the duplication was genuinely gone rather than
merely moved.

**Evidence recorded at the time.** Ten tests added, five of which were proven red
when the composition alone was reverted. Verified by execution: the `ctx`
repository itself holds 83/100 with Verification 30/30 and no blocking check; a
repository whose only test file is empty scores 25 against a real one's 36;
discovery time unchanged at 0.04s.

One detail from that commit generalizes to the rest of this list: the CLI renders
only `evidence[0]`, so every readiness check must lead with the reason it did not
pass. If you add a check, put the reason first.

---

## ARCH-3 — `ctx doctor` renders the plan decision instead of re-deriving refusal from proxy checks · Open

**What it is.** `doctor` decides whether a repository is ready by asking three
proxy questions of its own — is there a `.git`, was a test command discovered,
can a task be derived — and then prints
`This repository is ready for 'ctx fit --test --budget N'`
(`src/ctx/cli/doctor.py:174`). The experiment planner decides the same question
from a different basis and can return any of seven `blocked-*` decisions
(`src/ctx/fit/experiment.py:77-83`). `doctor.py` never imports
`resolve_experiment` or `plan_experiment`, so the two verdicts are computed
independently and are free to disagree.

**Why it matters.** `doctor` is the command a newcomer runs first. If it says
ready and `ctx fit` then refuses, the tool has contradicted itself at the exact
moment the user is deciding whether to trust it.

**Evidence.** The architecture walk recorded a reproduction in which `doctor`
printed ready while the plan returned `blocked-unknown-cost`. That exact pairing
depends on pricing being unavailable and does not reproduce on a machine where
`litellm` has a price table. The structural cause is directly checkable and still
present:

```bash
grep -n "plan_experiment\|resolve_experiment" src/ctx/cli/doctor.py   # no hits
```

**Depended on ARCH-1**, which has landed, so this is now unblocked. There is now
a single object to render.

---

## ARCH-5 — Typed trial verdict carrying attribution, plus per-run drilldown logs · Open

**What it is.** The verdict branch in `src/ctx/fit/live_runner.py` looks only at
the test exit code: anything non-zero becomes `outcome="failed"`, which counts
toward the candidate's reliability. The harness's `stop_reason` — carried on
`AgentOutcome.detail`, set at `src/ctx/fit/providers.py:338-339` — is never read
there. So a trial that CTX itself cut short at the $2.00 per-trial budget cap
(`providers.py:44`) or at 25 iterations (`providers.py:40`) is recorded as the
candidate failing. The 900-second subprocess timeout and a genuine provider `OSError` are
**not** in that set: both spend nothing, so `live_runner`'s spent-nothing
guard already records `infrastructure-failure` with
`counts_toward_reliability False`. Only the two caps reach the `failed`
branch.

**Why it matters.** With the default reliability floor of 1.0, one such trial
disqualifies a candidate outright and adaptive stopping abandons the rest of its
trials. Reliability is a hard constraint in this product ([ADR-014](DECISIONS.md)),
so a bound CTX imposed on itself silently becomes a verdict about the user's
repository.

**Evidence.** Tracker row FITBUG-040, confirmed by execution: a stand-in harness
returned `stop_reason='cost_budget'` having spent the full $2.00 without
finishing, and the trial was recorded `outcome='failed'`, `cost 2.0`,
`counts_toward_reliability True`. A transient `OSError` is *not* billed to
the candidate — it spends nothing and is already excluded.

**What is settled and what is not.** The product question — which stops are the
candidate's fault — is decided and written down as
[ADR-015](DECISIONS.md), so you do not need an owner ruling to start. Budget
caps and timeouts become `inconclusive`; the iteration cap stays a real failure;
every outcome keeps its `stop_reason` and its logs. Note that ADR-015
deliberately preserves
`src/tests/fit/test_live_runner.py:511`
(`test_an_agent_that_burned_tokens_without_finishing_is_a_real_failure`) rather
than weakening it — if your change makes that test fail, you have implemented the
wrong half of the ruling.

The remaining unsettled part is the shape of the typed verdict itself and where
per-run logs are written.

---

## ARCH-7 — Give `CandidateSet.warnings` and `TaskSet.warnings` a delivery channel via `ExperimentPlan.warnings` · Open

**What it is.** Six dataclasses in `src/ctx/fit/` carry a `warnings` field:
`FitProfile`, `VerificationInventory`, `CandidateSet`, `TaskSet`,
`ExperimentPlan`, and `ExecutionReport`. The CLI prints exactly two of them —
`profile.warnings` (`src/ctx/cli/fit.py:186`) and `plan.warnings`
(`src/ctx/cli/fit.py:256`). `VerificationInventory.warnings` reaches the user
because `profile.py:243` folds it into the profile's own list.

Two reach no user through any flag: `CandidateSet.warnings` and
`TaskSet.warnings`. `plan_experiment` builds `ExperimentPlan.warnings` only from
its own appends and never receives either. A third,
`ExecutionReport.warnings`, appears under `--json` (as `payload["execution"]`)
but in no human-readable output.

**Why it matters.** These are computed reasons discarded before they reach the
person who needs them. The user-visible symptom is a refusal that names no
cause — see FITBUG-047 in [GOOD_FIRST_ISSUES.md](GOOD_FIRST_ISSUES.md), where a
non-Python repository is told it can be evaluated and then declined with
generic text while `TaskSet.warnings` holds the actual reason.

**Evidence.** Directly checkable:

```bash
grep -rn "warnings: tuple" src/ctx/fit/*.py    # the fields
grep -n "\.warnings" src/ctx/cli/fit.py        # the two that are printed
```

The architecture walk recorded that this unblocks three deferred tracker rows.
FITBUG-047 is the one confirmed to depend on it.

---

## ARCH-4, ARCH-6, ARCH-8, ARCH-9, ARCH-10 — recorded as one batch · Open

Read the caveat before picking one of these up. The source recorded all five IDs
against a **single grouped entry** containing five clauses. The clause order
matches the ID order as written, and that is the basis for the headings below —
but no per-ID reason, evidence, or reproduction was recorded for any of them.
Each is a one-line architectural intent from someone who had just read the whole
package. Treat them as leads to confirm, not as findings.

- **ARCH-4 — A trial carries its authorization.** A trial should be able to say
  what it was authorized to spend, rather than that authorization living only in
  the campaign above it. Compare with ARCH-1, which is the same shape one level
  up: the object that spends should carry the terms it was approved under.
- **ARCH-6 — Validate tasks once, above the trial.** Task validity is a property
  of the task, not of each attempt at it, so it should be established once
  before trials begin instead of re-derived inside each one.
- **ARCH-8 — `recommend` reads its own evidence base.** `src/ctx/fit/recommend.py`
  should reach for the evidence it ranks on rather than being handed a
  pre-shaped view of it.
- **ARCH-9 — A recommendation carries its comparison.** The output should carry
  the comparison that produced it, not just the winner. FITBUG-069, a live
  tracker row, is a concrete instance: the tie-break dimension that decided the
  winner appears in no printed table, so a PR reviewer cannot recompute the
  result.
- **ARCH-10 — A recommendation carries the identity of what it ranks.** The
  ranked rows should be self-identifying, so a row can be traced back to the
  candidate configuration it describes.

If you take one of these on, the most useful first contribution is often not the
refactor but a reproduction: turn the one-line intent into a failing test or a
recorded observation, and post it. That converts a lead into a finding and makes
the item pickable by anyone.

---

## Related, decided, and not started

Not an ARCH item, but recorded in the same ledger and already decided:
**`doctor` should perform a full bounded test run.** `VerificationInventory`
declares `validated: bool = False` at `src/ctx/fit/verification.py:46`, with the
docstring "True only after a cheap non-mutating probe confirmed the command
runs". Nothing in `src/ctx` or `src/tests` ever sets it. The decision recorded
was that `doctor` should run the suite, timeout-bounded so a slow suite reports
rather than hangs. This is FITBUG-032 in
[GOOD_FIRST_ISSUES.md](GOOD_FIRST_ISSUES.md).

## Blocked on an owner decision

Two known problems are not on this list and are not available to pick up,
because they need a product ruling the project owner has not made:

- **FITBUG-016 — trial isolation.** One environment per campaign with
  refuse-as-fallback, versus detect-and-refuse only. This blocks evaluating any
  src-layout editable-install repository, including CTX itself.
- **FITBUG-036 — git behaviour.** Whether `--apply` may run git and `--pr` may
  open the PR via `gh`, versus no git at all. Today `--pr` announces a branch it
  never creates.
