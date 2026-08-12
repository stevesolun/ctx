# CTX Fit — Good First Issues

Seven open problems, each one already found, already reproduced, and deferred
for a stated reason rather than forgotten. They come from the QA tracker at
`qa/ctx_fit_bug_tracker.csv` in this repository: 92 rows, of which 83 are fixed
and 9 are deferred. These are the deferred ones that are actually available to
work on.

They are not evenly sized. Most were deferred because the remaining half of the
fix crossed into a file another reviewer was editing at the time — that
constraint is gone, and what is left is a contained change. Two are larger and
say so.

## Before you start

Set up per `CONTRIBUTING.md` at the repository root, then run the fast suite
once so you know what green looks like:

```bash
pytest -q -m "not browser and not integration" --no-cov
```

Every command below is written to run **from the repository root** with the
project's virtualenv at `.venv/`. If your interpreter lives elsewhere, replace
`.venv/bin/python` throughout. Scratch repositories go in `mktemp -d` so nothing
touches your checkout.

Line numbers in this document were re-checked against the tree when it was
written, but they move. Grep for the symbol.

## The list

| # | Row | What is wrong | Size |
|---|---|---|---|
| 1 | FITBUG-069 | The ranked table hides the dimension that picked the winner | Small |
| 2 | FITBUG-011 | `--apply` previews a filename, never the bytes it will write | Small |
| 3 | FITBUG-047 | A repository is refused with no cause, while the cause sits in a discarded field | Small |
| 4 | FITBUG-054 | "Could not analyze" and "analyzed, declined" share exit code 1 | Medium |
| 5 | FITBUG-073 | An unreadable subtree warns, but the exit code says nothing | Medium |
| 6 | FITBUG-032 | `doctor` never runs the test command it reports on | Medium |
| 7 | FITBUG-040 | A trial CTX cut short is blamed on the candidate | Large |

---

## 1. FITBUG-069 — the ranked table hides the dimension that picked the winner

**The problem.** The winner is chosen by a lexicographic rule: reliability
floor, then cheapest, then fewest capabilities as the tie-break
([ADR-014](DECISIONS.md)). On a cost tie the tie-break decides — and
`capability_count` appears in neither printed table. A PR reviewer sees two rows
with identical numbers and no way to recompute why one won, against the
auditability promise the module states at the top of
`src/ctx/fit/recommend.py`.

**Where it stands.** The sort half is already fixed: both sorts now share one
key and the winner is printed first. Only the presentation half is left.

**Evidence.** Run this from the repository root:

```bash
cat > /tmp/ord_check.py <<'PY'
from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.execution import CandidateOutcome, ExecutionReport, TrialResult
from ctx.fit.recommend import recommend
def cand(cid, n):
    return CandidateConfiguration(cid, "baseline" if cid == "baseline" else "recommended",
        tuple(f"skill:c{i}" for i in range(n)), None, (), "why")
def out(cid, rows):
    return CandidateOutcome(cid, tuple(TrialResult(cid, f"t{i}", 0, o, cost_usd=c)
        for i, (o, c) in enumerate(rows)), reliability_floor=1.0)
r = recommend(ExecutionReport(outcomes=(
        out("recommended", [("verified", 0.09), ("verified", 0.09)]),
        out("lean",        [("verified", 0.09), ("verified", 0.09)]),
        out("baseline",    [("verified", 0.45), ("verified", 0.45)])), simulated=False),
    (cand("recommended", 5), cand("lean", 1), cand("baseline", 0)), task_count=3, trials_per_task=2)
print("winner:", r.winner_id, " table order:", [x.candidate_id for x in r.ranked])
print("capability_count on the rows:", [x.capability_count for x in r.ranked])
PY
.venv/bin/python /tmp/ord_check.py
```

Prints `winner: lean  table order: ['lean', 'recommended', 'baseline']` and
`capability_count on the rows: [1, 5, 0]`. The number that decided it is on the
row and never rendered.

**Where to look.** `src/ctx/fit/apply.py:251` builds the PR-body table header;
`src/ctx/cli/fit.py:313` builds the CLI one. `capability_count` is already a
field on `RankedCandidate` (`src/ctx/fit/recommend.py:59`).

**Done when.** Both tables carry a capabilities column and mark the winning row,
and a test asserts that on a cost tie a reader can tell from the table alone
which row won and why. Note that `src/tests/fit/test_apply.py:159` asserts the
current header string exactly, so it will need updating — that is expected here,
not a sign you have gone wrong.

---

## 2. FITBUG-011 — `--apply` previews a filename, never the bytes it will write

**The problem.** `apply.py` opens by promising "Nothing is written without being
shown first", and `--apply`'s help text says "Shows every change before
writing." What the user is actually shown is one line naming the file and a
reason. Not one byte of the content, and no diff.

**Where it stands.** The data-loss half is fixed: the generated document now
lives inside a delimited `<!-- BEGIN CTX FIT -->` block and the user's own prose
survives. `Artifact.preserved_bytes` was added so the CLI could report what it
kept. The CLI never reads it — `grep -rn preserved_bytes src/ctx/cli/` returns
nothing.

**Evidence.** From the repository root:

```bash
LAB=$(mktemp -d); mkdir -p "$LAB/repo"
printf '# My instructions\nnever touch migrations/\n' > "$LAB/repo/AGENTS.md"
cat > "$LAB/mini_apply.py" <<'PY'
import sys
from pathlib import Path
from ctx.fit.apply import apply_plan, plan_apply
from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.recommend import RankedCandidate, Recommendation
repo = Path(sys.argv[1])
winner = CandidateConfiguration("lean", "recommended", ("skill:x",), None, (), "why")
rec = Recommendation(schema="ctx.fit.recommendation-v1", verdict="recommend-change",
    winner_id="lean", ranked=(RankedCandidate("lean", 1.0, 9, 9, 0.18, 1, True),),
    reasoning=("lean verified 9/9",), limitations=(), confidence="medium", simulated=False)
plan = plan_apply(rec, (winner,), repo_path=repo)
print("everything the user is shown:")
for a in plan.artifacts:
    print(f"  {a.action}: {a.path} ({a.reason})")
print("plan.to_dict artifacts ->", plan.to_dict()["artifacts"])
print("wrote:", apply_plan(plan, repo))
PY
.venv/bin/python "$LAB/mini_apply.py" "$LAB/repo"
echo '--- AGENTS.md is now:'; cat "$LAB/repo/AGENTS.md"
rm -rf "$LAB"
```

The preview is `modify: AGENTS.md (records the winning capability set and the
evidence behind it)`. The plan dict reports `bytes: 368, preserved_bytes: 41`.
The user's two lines survive — and the user was still never shown the 368 bytes
about to be written into their repository.

**Where to look.** `src/ctx/cli/fit.py:504-506` is the preview loop.
`_handle_apply` gates on `--yes` a few lines below; the content has to appear
before that gate to be worth anything.

**Done when.** `--apply` prints the proposed content — or a unified diff against
the current file — before the `--yes` gate, and says how many of the user's own
bytes it is preserving. A test asserts that a distinctive string from the
generated document appears in stdout before any write occurs.

---

## 3. FITBUG-047 — a repository is refused with no cause, while the cause sits in a discarded field

**The problem.** Task derivation only considers `.py` files, but nothing
upstream restricts evaluability to Python. A Jest repository with a declared
test command and test files passes `has_deterministic_verification`, is told
"This repository can be evaluated", and is then refused with `blocked-no-tasks`
and canned text that names no cause. The cause exists: `TaskSet.warnings` holds
it. Nothing forwards it.

**Where it stands.** `derive_tasks` now emits a warning naming the Python
restriction, with a regression test. The forwarding half is untouched.

**Evidence.**

```bash
grep -rn "warnings: tuple" src/ctx/fit/*.py   # six warnings fields
grep -n "\.warnings" src/ctx/cli/fit.py       # only profile.warnings and plan.warnings
```

`ExperimentPlan.warnings` is built inside `plan_experiment` from its own appends
only; it never receives `TaskSet.warnings` or `CandidateSet.warnings`, and the
CLI prints nothing else.

**Where to look.** `src/ctx/fit/tasks.py:100` (the field),
`src/ctx/fit/experiment.py:286-361` (where the plan's warnings are assembled),
`src/ctx/cli/fit.py:186` and `:256` (the two things printed). This is the
narrow, concrete instance of ARCH-7 in
[ARCHITECTURE_CANDIDATES.md](ARCHITECTURE_CANDIDATES.md) — fixing it well fixes
`CandidateSet.warnings` at the same time.

**Done when.** A non-Python repository with tests, run end to end through the
CLI, is refused with the actual reason printed. An end-to-end test asserts the
warning text reaches stdout, not merely that the field is populated. Leave
`plan.task_count or 'not yet derived'` alone unless you also fix the wording —
it currently implies derivation has not run when it ran and found nothing.

---

## 4. FITBUG-054 — "could not analyze" and "analyzed, declined" share exit code 1

**The problem.** Exit 1 means two different things. It is what a well-formed
blocked-plan run returns, and it is also what an unhandled exception returns. A
CI job cannot tell them apart by status and has to guess from whether stdout
happens to parse.

**Where it stands.** The crashes that motivated the row are fixed —
`detect_stack` type-guards its dependency sections, and all four malformed
`package.json` shapes now produce a valid profile. What remains is the exit-code
contract itself: `cmd_fit` still catches only `NotADirectoryError`
(`src/ctx/cli/fit.py:400`), so any other analysis failure escapes as a traceback
and exits 1 — indistinguishable from a deliberate refusal.

**Evidence.** Both halves, from the repository root:

```bash
R=$(mktemp -d); printf 'x\n' > "$R/a.js"
for shape in '{"dependencies": null}' '["dependencies"]' '{"dependencies": [1,2]}' '5'; do
  printf '%s' "$shape" > "$R/package.json"
  .venv/bin/python -m ctx fit "$R" --json >/dev/null 2>&1; echo "shape=$shape exit=$?"
done
# now the collision partner: a well-formed run whose plan is blocked
R2=$(mktemp -d); printf 'print(1)\n' > "$R2/a.py"
.venv/bin/python -m ctx fit "$R2" --test --budget 5 --json --yes >/dev/null 2>&1
echo "blocked plan exit=$?"
rm -rf "$R" "$R2"
```

All four shapes exit 0. The blocked plan exits 1, with 11KB of valid JSON on
stdout.

**Where to look.** `src/ctx/cli/fit.py:395-400` (the guard) and the `return 1`
sites around `:424-490`. Note that `2` is already taken by the bad-invocation
guards just above the profile call.

**Done when.** There is a documented exit-code contract — a reasonable shape is
`0` analyzed, `1` analyzed but declined, `2` bad invocation, `3` could not
analyze — the guard around `build_fit_profile` reports the failure on stderr
instead of a traceback, and a test pins each code to its meaning. Say what the
contract is in the CLI's own help or docs; an undocumented exit code is not a
contract.

---

## 5. FITBUG-073 — an unreadable subtree warns, but the exit code says nothing

**The problem.** The unreadable subtree is reported but still unscanned, so a
directory the process cannot read is skipped in silence. Nothing below it
reaches the file signals, so no language, framework, or test is detected there.

**Where it stands.** The headline is closed. `discover_verification` now appends
the unreadable subtree to its warnings and `profile.py` folds those into the
profile, so the omission is announced. What is left is whether an incomplete
scan should still be exit 0.

**Evidence.**

```bash
R=$(mktemp -d); mkdir -p "$R/hidden"
printf 'x\n' > "$R/hidden/t.py"; printf 'print(1)\n' > "$R/a.py"
chmod 000 "$R/hidden"
.venv/bin/python -m ctx fit "$R" --json 2>/dev/null | .venv/bin/python -c \
  "import json,sys; print(json.load(sys.stdin)['warnings'])"
chmod 755 "$R/hidden"; rm -rf "$R"
```

Prints `['could not read hidden; any tests below are invisible to this scan',
...]` — and the process exits 0, the same as a complete scan.

**Where to look.** `src/scan_repo.py:160` for the walk,
`src/ctx/fit/profile.py:243` for where warnings are folded in, and the exit
paths in `src/ctx/cli/fit.py`.

**Done when.** The project has decided whether an incomplete scan is a distinct
outcome, that decision is recorded, and the code and a test match it. This is
the smaller sibling of FITBUG-054 and it is worth doing them together — the
honest first contribution here is a short proposal on the issue, not a patch.

---

## 6. FITBUG-032 — `doctor` never runs the test command it reports on

**The problem.** `VerificationInventory` declares
`validated: bool = False` at `src/ctx/fit/verification.py:46`, documented as
"True only after a cheap non-mutating probe confirmed the command runs". The
probe was never written:

```bash
grep -rn "validated=" src/ctx src/tests    # no hits
```

Nothing ever sets it, and nothing consults it. Separately, the discovered
command is the hardcoded tuple `("python", "-m", "pytest", "-q")` at
`src/ctx/fit/verification.py:310` and `:320`, rather than `sys.executable` — so
on a stock macOS or a `python3`-only Linux it names an interpreter that does not
exist.

**Where it stands.** `doctor`'s wording was fixed first: it now says "test
command discovered", with a comment stating it must not claim the command was
observed to work. So `doctor` is no longer *false* — it is honestly reporting a
weaker fact than it could. The probe itself, and the hardcoded interpreter, are
both still open.

**Where to look.** `src/ctx/cli/doctor.py:80-116` for the repo checks,
`src/ctx/fit/verification.py:46` for the flag, `:310` and `:320` for the
hardcoded command. Tests live in `src/tests/fit/test_doctor.py`.

**Done when.** Either the probe exists — `validated` is set by a real,
timeout-bounded run, `doctor` reports it, and a repository whose declared test
command does not actually run is reported as such — or, if the project decides a
support command must never execute repository code, `validated` is deleted
rather than left as a field that documents a promise nothing keeps.

The interpreter fix is separable and smaller: replacing the hardcoded `"python"`
with `sys.executable` is a clean standalone contribution. It needs a test that
would fail on a host without a bare `python` on `PATH`.

**Note on scope.** The deferral reason was that the probe needs a design
decision — which probe (`--collect-only`, or a full run?), what timeout, and
whether a support tool may execute repository code at all. The direction
recorded in the architecture ledger was a full bounded test run, timeout-bounded
so a slow suite reports rather than hangs. That is a direction, not a ruling;
propose your design on the issue before writing much code.

---

## 7. FITBUG-040 — a trial CTX cut short is blamed on the candidate

**The problem.** The verdict branch in `src/ctx/fit/live_runner.py` reads only
the test exit code. Anything non-zero becomes `outcome="failed"`, which counts
toward reliability. The harness's `stop_reason` — set on `AgentOutcome.detail`
at `src/ctx/fit/providers.py:338-339` — is never read there. So a trial CTX
itself stopped at the $2.00 per-trial budget cap or at 25 iterations is recorded
as the user's repository defeating the candidate. The 900-second subprocess timeout and a genuine provider `OSError` are
**not** in that set: both spend nothing, so `live_runner`'s spent-nothing
guard already records `infrastructure-failure` with
`counts_toward_reliability False`. Only the two caps reach the `failed`
branch. With the default reliability floor of 1.0, one such trial
disqualifies the candidate and adaptive stopping abandons the rest of its
trials.

**Evidence.** Confirmed by execution during QA: a stand-in harness returned
`stop_reason="cost_budget"` having spent the full $2.00 without finishing, and
the trial was recorded `outcome="failed"`, `cost 2.0`,
`counts_toward_reliability True`. A transient `OSError` is *not* billed to
the candidate — it spends nothing and is already excluded. You can see the
cause by reading the final `else` branch of the verdict block in
`src/ctx/fit/live_runner.py` — `agent.detail` appears nowhere in it.

**Where it stands, and why this one is now unblocked.** It was deferred because
it asks for the opposite of a rule a previous change deliberately wrote in, and
reversing that is a product decision. That decision has since been made and
recorded as [ADR-015](DECISIONS.md):

- budget caps and timeouts produce `inconclusive`, not `failed`;
- the iteration cap stays a real candidate failure;
- every outcome keeps its `stop_reason` and its logs.

So the ruling you need already exists. Read ADR-015 first.

**Two traps.** `src/tests/fit/test_live_runner.py:511`
(`test_an_agent_that_burned_tokens_without_finishing_is_a_real_failure`) is
*preserved* by the ruling — if your change makes it fail, you have implemented
the wrong half. And a `cost_budget` stop arrives as `completed=True` with the
reason smuggled into `detail`, so branching on `completed` gets that case
backwards.

**Done when.** `stop_reason` is a structured field on the outcome rather than a
string in `detail`; each stop reason maps to the verdict ADR-015 assigns it;
tests cover budget stop, iteration stop, timeout, and provider error separately;
and an inconclusive trial is still visible in the recorded evidence even though
it no longer counts toward reliability.

**Size warning.** This is the largest item here and it is really the first half
of ARCH-5 in [ARCHITECTURE_CANDIDATES.md](ARCHITECTURE_CANDIDATES.md). Do not
pick it as a first contribution to the codebase; pick it as a second.

---

## Not available: blocked on an owner decision

Two deferred rows are deliberately not offered above. Both need a product ruling
the project owner has not yet made, so a patch cannot be reviewed against
anything. Discussion is welcome; code is premature.

- **FITBUG-016 — trial isolation** (`src/ctx/fit/live_runner.py`, the workspace
  and subprocess setup). With a normal editable install, the workspace's tests
  exercise the user's original source tree rather than the isolated copy. The
  open question is one environment per campaign with refuse-as-fallback, versus
  detect-and-refuse only. This blocks evaluating any src-layout
  editable-install repository — including CTX itself, which makes it the
  highest-impact item on this page.
- **FITBUG-036 — git behaviour** (`_handle_apply` in `src/ctx/cli/fit.py`).
  Today `--pr` announces a branch it never creates, and `--apply` writes to whatever branch
  you are standing on. The open question is whether `--apply` may run git and
  `--pr` may open the PR via `gh`, or whether CTX Fit should touch git at all.
  The current output is at least honest about this — it prints "Suggested branch
  (not created)" and states that changes land in your working tree — so the bug
  is the missing capability, not a false claim.
