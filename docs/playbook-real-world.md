# Real-world ctx playbook

> Canonical end-to-end scenario exercising every ctx surface: scan → graph
> suggest → skill load/unload → toolbox council → skill_add → lifecycle
> archive → KPI. Assumes the user's environment already has the graph
> (`~/.claude/skill-wiki/graphify-out/graph.json`) and wiki
> (`~/.claude/skill-wiki/`) **pre-built** — we do NOT rebuild in this
> playbook.

## Persona

**Maya** — senior backend engineer at a B2C fintech. Installing `claude-ctx`
fresh. First project on ctx: a **PCI-compliant checkout microservice**
(FastAPI + SQLAlchemy + PostgreSQL + Stripe + pytest). Target: working
endpoint + council-signed commit by end of day.

She has `~/.claude/skills/` with 1,789 skills and `~/.claude/agents/`
with 464 agents pre-installed (extracted from `graph/wiki-graph.tar.gz`).
Graph is pre-built (2,253 nodes / 454K edges / 93 communities). She has
never run ctx before.

## Environment precondition

```
~/.claude/skill-wiki/graphify-out/graph.json   # pre-built, 454K edges
~/.claude/skill-wiki/entities/                 # 2,253 entity pages
~/.claude/skill-wiki/converted/                # 952 compressed skills
~/.claude/skill-quality/                       # 1,891 sidecars
```

`pip install claude-ctx` is done. The four console scripts are on PATH.

---

## Phase 1 — Project bootstrap + first stack scan

```bash
mkdir ~/work/checkout-svc && cd ~/work/checkout-svc
git init
cat > requirements.txt <<EOF
fastapi
sqlalchemy
psycopg2-binary
pydantic
stripe
pytest
EOF
echo "app" > .gitignore
mkdir -p app tests
```

Maya runs:

```bash
ctx-scan-repo --repo . --output .ctx/stack.json
```

**Expected ctx behavior**
1. `scan_repo` detects: python / fastapi / sqlalchemy / pytest /
   stripe / postgres (from `psycopg2-binary` signature).
2. `resolve_skills` walks the **pre-built graph** from those stack tags
   and recommends ~10–15 skills:
   - direct/fuzzy matches: `fastapi-pro`, `python-patterns`,
     `python-fastapi-development`, `pydantic-models-py`,
     `stripe-integration`, `payment-integration`, `pci-compliance`,
     `postgresql-optimization`
   - graph neighbors (edge-weight ≥ 1.5): `test-automator`,
     `python-pro`, `async-python-patterns`, `backend-security-coder`,
     `api-security-best-practices`

**Verification**
- Manifest has 10–15 `load` entries with mixed `reason` fields:
  `fuzzy match for detected stack 'fastapi'`, `graph neighbor of ...`.
- No warnings for "not installed" when fuzzy fallback found an alternative.

---

## Phase 2 — Claude session starts; context monitor observes

Maya launches Claude Code in `~/work/checkout-svc/`. `PostToolUse` hooks
fire on every tool call; `Stop` fires at session end.

Maya asks Claude: *"scaffold the checkout endpoint with Stripe payment
intents".*

**Expected ctx behavior during the session**
1. **PostToolUse** fires `context_monitor.py --from-stdin` on every
   tool call. The monitor reads the tool input, detects signals:
   - file path `app/api/checkout.py` → stack signal `python`, `fastapi`
   - content containing `stripe.PaymentIntent` → new signal `stripe`
2. When an unmatched signal accumulates past threshold (3 by default),
   `context_monitor` writes to `~/.claude/pending-skills.json` and
   `skill_suggest.py` surfaces it to Claude's context as a
   `hookSpecificOutput.additionalContext` blob.
3. Claude reads the suggestion ("You may want to load `stripe-integration`
   and `pci-compliance`") and asks Maya to confirm.
4. Maya says *"yes, load pci-compliance"*. Claude loads it via its
   normal skill-load mechanism; an event lands in `skill-events.jsonl`:
   `{"event": "load", "skill": "pci-compliance", ...}`.
5. The quality sidecar for `pci-compliance` increments `load_count`;
   `telemetry` score rises on next recompute.

**Verification**
- `tail -1 ~/.claude/skill-events.jsonl` shows the new load event.
- `ctx-skill-quality explain pci-compliance` shows `load_count ≥ 1`,
  `never_loaded: False`, floor cleared, grade improved.

---

## Phase 3 — First real feature + live suggestion cycle

Maya writes `app/api/checkout.py`:

```python
from fastapi import APIRouter, HTTPException
import stripe
from app.schemas import CheckoutRequest, CheckoutResponse

router = APIRouter()

@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(req: CheckoutRequest) -> CheckoutResponse:
    intent = stripe.PaymentIntent.create(
        amount=req.amount_cents, currency="usd",
        metadata={"user_id": req.user_id},
    )
    return CheckoutResponse(client_secret=intent.client_secret)
```

She runs `pytest` (no tests yet) → exits 0 (no-op). She realizes she
needs test coverage.

**Expected ctx behavior**
1. Editing files under `tests/` triggers `context_monitor` to detect
   the `testing` signal.
2. `skill_suggest` surfaces `test-driven-development`,
   `python-testing`, `pytest-patterns` from the graph.
3. Maya loads `python-testing`. Sidecar updates.

---

## Phase 4 — Pre-commit council (toolbox run)

Before committing, Maya wants a security review. She has `toolbox init`
output 5 starter toolboxes. She activates `security-sweep`:

```bash
ctx-toolbox activate security-sweep
ctx-toolbox run --event pre-commit
```

**Expected ctx behavior**
1. `toolbox.py run` reads the active toolbox config.
2. Council is assembled: `security-reviewer`, `security-auditor`,
   `penetration-tester`, `compliance-auditor`,
   `threat-detection-engineer`.
3. `council_runner.py` builds a `RunPlan` with scope=`diff` (files
   changed since last commit), budget=300K tokens, guardrail=True.
4. Claude Code picks up the plan, dispatches each agent, collects
   findings.
5. `toolbox_verdict.py` merges findings by stable id, escalates level.
6. If any HIGH/CRITICAL finding exists, `run --event pre-commit`
   exits 2 → blocks `git commit`.

**Expected finding** (realistic): `security-reviewer` flags the
checkout endpoint lacks webhook signature verification and Stripe key
is read from env but never validated as non-empty → MEDIUM. No block.

---

## Phase 5 — Custom skill for the domain (skill_add)

Maya realizes her team reuses the same `PaymentIntent` error-mapping
pattern across 3 services. She wants to capture it as a custom skill.

```bash
mkdir -p .skills/stripe-error-mapping
cat > .skills/stripe-error-mapping/SKILL.md <<EOF
---
name: stripe-error-mapping
description: Canonical Stripe API error → domain exception mapping for fintech backends
tags: [stripe, payments, error-handling, pci]
---

# Stripe error mapping

## When to use
Whenever a service integrates with Stripe's PaymentIntent, Charge, or
Setup Intent APIs. This skill ensures every Stripe error (rate-limit,
card-declined, authentication, API connection, idempotency) maps to
a stable domain exception with an actionable error code.

## Mapping table

| Stripe error class | Domain exception | HTTP status |
| ... |

EOF

ctx-skill-quality recompute stripe-error-mapping  # will fail — not yet installed
# Install path:
python -m skill_add --skill-path .skills/stripe-error-mapping/SKILL.md
```

**Expected ctx behavior**
1. `skill_add` runs the intake gate (`intake_gate.py`):
   frontmatter-present, has-description, body-long-enough, has-H2.
2. Similarity check against existing skills via embedding backend
   (falls back to structural-only if sentence-transformers missing).
3. If novel: writes `~/.claude/skills/stripe-error-mapping/SKILL.md`,
   adds entity page at
   `~/.claude/skill-wiki/entities/skills/stripe-error-mapping.md`
   with frontmatter (tags, use_count=0, last_used=null, status=installed).
4. Returns success: manifest counter `1,579 → 1,580`.

**Post-add follow-ups**
- Graph rebuild required to include the new skill as a node with edges
  to `stripe`, `payments`, `pci` tag communities.
- `ctx-skill-quality recompute stripe-error-mapping` seeds the sidecar.

---

## Phase 6 — Session end + lifecycle pruning

End of day. Claude's `Stop` hook fires:
1. `usage_tracker.py --sync` updates skill usage stats from
   `skill-events.jsonl`.
2. `hooks/quality_on_session_end.py` recomputes sidecars for only
   the slugs touched this session (incremental).
3. `ctx_lifecycle` reviews sidecars; any skill that sat in `_demoted`
   past the 14-day archive threshold is moved to `_archive`.

Maya runs:

```bash
ctx-toolbox status
python -m kpi_dashboard render
open ~/.claude/skill-quality/kpi.md
```

She sees:
- Grade distribution (A: 2, B: 18, C: 240, D: 1320, F: 311).
- Hard-floor reasons.
- Top demotion candidates. Skills she never used in 30 days get
  auto-archived next tick.

Optionally she launches the claude-mem web monitor:

```bash
npx claude-mem start
# browser opens http://localhost:37777
```

---

## Success criteria (what "good" looks like at end of day)

| Check | Pass when |
|---|---|
| Phase 1 manifest | 10–15 loads, mixed fuzzy + graph-neighbor reasons |
| Phase 2 events log | Every Claude tool call produced a PostToolUse invocation |
| Phase 3 suggest | `pending-skills.json` gained ≥ 1 new entry for Stripe |
| Phase 4 verdict | Council runs, findings file written, exits 0 or 2 cleanly |
| Phase 5 skill_add | New skill appears in skills dir + wiki; dedup fired |
| Phase 6 KPI | `kpi.md` reflects today's sessions; archived skills moved |

## Known gaps this playbook will surface

- `wiki_sync` has no drift detection (skill edits don't re-sync wiki).
- `batch_convert` hash-gated idempotency looks like no-op on re-run.
- `scan_repo` doesn't detect pytest from pyproject `[dev]` deps.
- `skill_add` CLI raises false `BODY_MISSING_H2` — in-process API works.

Each gap is tracked as a separate issue and will be fixed post-playbook.
