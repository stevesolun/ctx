# Verdicts & guardrails

[`src/toolbox_verdict.py`](https://github.com/stevesolun/ctx/blob/main/src/toolbox_verdict.py)
owns the council's finding ledger. A `RunPlan` says *what should run*; a
`Verdict` says *what was found* and, if the level escalates high enough,
blocks `git commit`.

## Data model

```python
@dataclass(frozen=True)
class Evidence:
    file: str
    line: int | None = None
    note: str = ""

@dataclass(frozen=True)
class Finding:
    id: str                    # stable, hash(level|agent|title)
    level: str                 # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
    title: str
    agent: str = ""
    evidence: tuple[Evidence, ...] = ()
    rationale: str = ""
    created_at: float = 0.0

@dataclass(frozen=True)
class Verdict:
    plan_hash: str
    level: str                 # max(findings)
    summary: str
    findings: tuple[Finding, ...]
    created_at: float
    updated_at: float
```

Level escalation is always `max(findings)`. Clearing a finding
re-escalates from whatever remains.

## Storage

```
~/.claude/toolbox-runs/
  abc123.json                # the RunPlan
  abc123.verdict.json        # the Verdict (sibling)
```

Same directory, same hash. A single `history` sweep covers both.

## Merge-by-id

Agents can refine a previous finding by recording with the same id.
The new record replaces the old one (rationale, evidence, level all
update). This is how a `security-reviewer` can start with a MEDIUM
finding, then bump it to CRITICAL after deeper analysis — without
leaving duplicate entries.

The default id is `sha256(level|agent|title)[:12]`, so the same
agent recording the same titled issue naturally dedups. Pass
`--id custom-value` if you need a different stable key.

## Blocking

`toolbox_hooks` reads `<plan>.verdict.json` after a `pre-commit`
council runs. If `level in {"HIGH", "CRITICAL"}` and the toolbox has
`guardrail: true`, it returns exit `2`, which stops the commit.

`LOW` and `MEDIUM` findings are logged but never block.

## CLI

```bash
# Record a finding
python src/toolbox_verdict.py record \
  --plan-hash abc123 \
  --level HIGH \
  --title "SQL injection in users.py" \
  --agent security-reviewer \
  --evidence src/users.py:42:unescaped input \
  --rationale "req.form values flow into raw SQL"

# Show the verdict
python src/toolbox_verdict.py show --plan-hash abc123

# JSON payload for piping
python src/toolbox_verdict.py show --plan-hash abc123 --json

# Recent verdicts (retrospective)
python src/toolbox_verdict.py retro --limit 10

# Only HIGH/CRITICAL
python src/toolbox_verdict.py retro --min-level HIGH

# Pretty-print the evidence chain
python src/toolbox_verdict.py explain --plan-hash abc123

# Remove a single finding
python src/toolbox_verdict.py clear --plan-hash abc123 --id <id>
```

## Evidence parsing

`parse_evidence()` accepts three forms, parsed right-to-left so Windows
drive-letter paths don't trip the delimiter:

- `src/foo.py` → `Evidence(file="src/foo.py")`
- `src/foo.py:42` → `Evidence(file="src/foo.py", line=42)`
- `src/foo.py:42:race on counter` → `Evidence(file=..., line=42, note=...)`
- `C:/Users/me/foo.py:17` → `Evidence(file="C:/Users/me/foo.py", line=17)`

Empty specs yield an `Evidence` with an empty file and are filtered out
at `build_finding()` time.

## Explain output

```
[verdict] plan=abc123  level=HIGH  2 finding(s): 1 high, 1 low
  - [HIGH] SQL injection in users.py  (agent: security-reviewer)
      why: req.form values flow into raw SQL
      evidence: src/users.py:42 — unescaped input
  - [LOW] style: trailing whitespace  (agent: code-reviewer)
      evidence: src/users.py:57
```

Findings render in severity-desc order so the blocking issue always
appears first.

## Retrospective

`recent_verdicts()` returns verdicts sorted by `updated_at` desc:

```
[retro] 3 recent verdict(s):
  - plan-crit   CRITICAL  BLOCK  1 finding(s): 1 critical
  - plan-hi     HIGH      BLOCK  2 finding(s): 1 high, 1 low
  - plan-ok     LOW       ok     1 finding(s): 1 low
```

## Related

- [Hooks & triggers](hooks.md) — where the `2` exit blocks the commit.
- [Council runner](council-runner.md) — where the plan hash comes from.
