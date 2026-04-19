# Random load → unload playbook

> Pick a skill the user hasn't touched recently, verify ctx suggests
> loading it, verify the skill actually enters the loaded set, wait
> for staleness, verify ctx suggests unloading it, verify it actually
> leaves. Every claim is backed by a concrete file-system observation,
> not a log-line assumption.

## Why this scenario matters

Previous playbooks verified the *pipeline* (observer → suggest →
record → score). This one verifies the **economic** part: skills
come in and go back out, and the user can observe both halves of
the cycle. Without this, the "nothing rots" claim in the README is
untested.

## What we'll use

- **ctx-monitor** (rc8+) to watch the audit log live via SSE. The
  test agent keeps the dashboard open at
  `http://127.0.0.1:8765/session/<test-session-id>` and screenshots
  the audit timeline before/after.
- **ctx_audit_log** event stream (`~/.claude/ctx-audit.jsonl`).
- **skill-events.jsonl** for the ground-truth load/unload record.
- **pending-unload.json** for the suggest-to-unload signal.
- **skill_quality explain <slug>** to read the sidecar after each
  phase.

## Preconditions

1. `claude-ctx` 0.5.0-rc8 installed from PyPI.
2. `~/.claude/skill-wiki/` pre-built (2,253 nodes, 454K edges).
3. `~/.claude/skills/` has ≥ 1,500 skills installed.
4. `~/.claude/settings.json` has all rc7 hooks wired
   (PostToolUse: context_monitor + skill_add_detector + skill_suggest
   + backup_on_change; Stop: usage_tracker + quality_on_session_end).
5. Stale-threshold override for the test run. Write it into
   `~/.claude/skill-system-config.json` — there is no env var
   shortcut; the threshold only comes from config:
   ```bash
   python -c "
   import json, os
   from pathlib import Path
   p = Path(os.path.expanduser('~/.claude/skill-system-config.json'))
   cfg = json.loads(p.read_text()) if p.exists() else {}
   cfg.setdefault('usage_tracker', {})['stale_threshold_sessions'] = 3
   p.write_text(json.dumps(cfg, indent=2))
   "
   ```
   Default is 30 — too slow to observe in one sitting.
6. `ctx-monitor serve --port 8765` running in a background tab.

## The scenario

### Step 1 — Pick a random candidate

Pick a skill whose sidecar has:
- `hard_floor: "never_loaded_stale"` — these all map to **grade D**
  (grade F is reserved for `intake_fail`), AND
- `intake.score >= 0.8` (structurally valid), AND
- **tag overlap with `context_monitor.KEYWORD_SIGNALS`** — otherwise
  the skill can never surface through the observe→suggest path
  (learned the hard way on rc8's verification run), AND
- not a meta-skill (`skill-router`, `file-reading`, etc.).

```bash
python - <<'PY'
import json, random, re
from pathlib import Path

# Seed: installed KEYWORD_SIGNALS from context_monitor.
# Load dynamically to survive future vocabulary changes.
import importlib.util
src = Path.home() / ".claude" / "skills"  # may differ per install
spec_path = None
for candidate in [
    Path(__file__).resolve().parents[1] / "src" / "context_monitor.py",
    Path.home() / ".local" / "lib" / "python3.11" / "site-packages" / "context_monitor.py",
]:
    if candidate.exists():
        spec_path = candidate; break
spec = importlib.util.spec_from_file_location("_cm", spec_path)
cm = importlib.util.module_from_spec(spec); spec.loader.exec_module(cm)
keywords = set(cm.KEYWORD_SIGNALS.keys())

sidecar_dir = Path.home() / ".claude" / "skill-quality"
candidates: list[str] = []
for p in sidecar_dir.glob("*.json"):
    if p.name.startswith(".") or p.name.endswith(".lifecycle.json"):
        continue
    try:
        sc = json.loads(p.read_text())
    except Exception:
        continue
    if sc.get("hard_floor") != "never_loaded_stale" or sc.get("grade") != "D":
        continue
    intake = (sc.get("signals") or {}).get("intake", {}) or {}
    if intake.get("score", 0) < 0.8:
        continue
    slug = sc["slug"]
    if slug in {"skill-router", "file-reading", "context-monitor"}:
        continue
    # Require ≥2 tag tokens in the slug that also appear as KEYWORD_SIGNALS
    slug_tokens = set(re.split(r"[^a-z0-9]+", slug.lower()))
    if len(slug_tokens & keywords) >= 2:
        candidates.append(slug)

random.shuffle(candidates)
print(candidates[0] if candidates else "NONE_FOUND")
PY
```

Record the picked slug. Call it `$TARGET`.

**Expected**: $TARGET has `grade=D`, `load_count=0`,
`never_loaded=True`, `hard_floor=never_loaded_stale`.

### Step 2 — Inject a stack signal that should surface $TARGET

Look at the target skill's `tags`. Synthesize a PostToolUse payload
whose `tool_input.file_path` or content contains 3+ of those tags
(crossing the `UNMATCHED_SIGNAL_THRESHOLD` in
`context_monitor.py`).

Pipe it into `context_monitor.py --from-stdin` 3 times. On the
third call, ctx should add $TARGET to
`~/.claude/pending-skills.json` under `graph_suggestions`.

```bash
for i in 1 2 3; do
  echo '{"session_id":"random-load-test","tool_name":"Write",
         "tool_input":{"file_path":"app/<tag-heavy-path>.py",
         "content":"<content with target tags>"}}' \
    | python -m context_monitor --from-stdin
done

cat ~/.claude/pending-skills.json | python -m json.tool | head -30
```

**Expected**: `graph_suggestions` array contains $TARGET with a
non-empty `shared_tags` list and `score > 0`.

### Step 3 — Run skill_suggest.py, verify the suggestion surfaces

```bash
python -m skill_suggest
```

**Expected**: stdout is valid JSON with
`hookSpecificOutput.additionalContext` containing $TARGET's slug or
its description. If absent: the graph-walk → suggestion path is
broken.

### Step 4 — Simulate the user accepting the suggestion (skill load)

In a live session, Claude would invoke `skill_loader.py load
$TARGET` here. Simulate by appending the load event:

```bash
python -c "
import json, uuid
from pathlib import Path
from datetime import datetime, timezone
e = Path.home()/'.claude'/'skill-events.jsonl'
line = {
  'event': 'load',
  'event_id': uuid.uuid4().hex,
  'meta': {'source':'random-load-test'},
  'session_id': 'random-load-test',
  'skill': '$TARGET',
  'timestamp': datetime.now(timezone.utc).isoformat(),
}
with e.open('a') as f: f.write(json.dumps(line)+'\n')
"
```

**Expected**:
- `skill-events.jsonl` grew by exactly 1 line with `skill=$TARGET`.
- Audit log (`ctx-audit.jsonl`) will not yet have a `skill.loaded`
  row — that event is written by the hook Claude Code fires when
  it actually injects the skill. For the simulation we record it
  manually too:

```bash
python -c "from ctx_audit_log import log_skill_event; \
  log_skill_event('skill.loaded','$TARGET', \
    session_id='random-load-test', meta={'via':'sim'})"
```

### Step 5 — Verify the skill is loaded

`skill_loader.py` (or its simulation) updates the manifest:

```bash
jq '.load[].skill' ~/.claude/skill-manifest.json | grep -x '"$TARGET"'
```

Plus read it from ctx-monitor's dashboard:

```
curl -s http://127.0.0.1:8765/api/sessions.json | \
  jq '.[] | select(.session_id=="random-load-test")'
```

**Expected**: `skills_loaded` array of the test session includes
$TARGET.

### Step 6 — Force a Stop hook (end-of-session)

The Stop hook reads `session_id` from stdin (Claude Code delivers
the session payload there in production). **Do NOT use `< /dev/null`**
— it strips the session_id and the `skill.score_updated` audit row
gets a synthesized id instead of the real one, so the dashboard's
per-session timeline drops the middle event in the triad.

```bash
SID="random-load-test"
echo "{\"session_id\":\"$SID\"}" | python hooks/quality_on_session_end.py
python -m usage_tracker --sync
```

**Expected**:
- Sidecar for $TARGET now shows `load_count >= 1`,
  `never_loaded=False`, `hard_floor=null`, grade shifted from D
  to something higher (usually C or B).
- Audit log has a new `skill.score_updated` row with
  `session_id=random-load-test` for $TARGET. Confirm via
  `grep score_updated ~/.claude/ctx-audit.jsonl | tail -1`.

### Step 7 — Wait for staleness

Run `usage_tracker --sync` two more times without any
corresponding `used` signal (i.e., no recent intent-log entry for
the tags that originally surfaced $TARGET). With
`CTX_STALE_THRESHOLD_SESSIONS=3` and `session_count` bumped on each
sync, $TARGET should cross the stale threshold on the 3rd sync.

```bash
for i in 1 2 3; do python -m usage_tracker --sync; done
cat ~/.claude/pending-unload.json | python -m json.tool
```

**Expected**: `pending-unload.json` contains $TARGET with
`reason: stale session_count=3 use_count=0`.

### Step 8 — Simulate user approving the unload

```bash
python -m skill_unload --slug $TARGET --session-id random-load-test
```

(If `skill_unload` CLI isn't wired to the same module, append an
`unload` event manually — same shape as step 4 with `event=unload`.)

**Expected**:
- `skill-events.jsonl` has an `unload` line for $TARGET.
- `skill-manifest.json`'s `load[]` no longer contains $TARGET.
- Audit log gains a `skill.unloaded` row.
- ctx-monitor `/session/random-load-test` now shows $TARGET in
  both `skills_loaded` AND `skills_unloaded`.

### Step 9 — Final verification via dashboard

Open `http://127.0.0.1:8765/session/random-load-test` in a browser.

The audit timeline must show, in order:

1. `skill.loaded` $TARGET
2. `skill.score_updated` $TARGET (after Stop hook)
3. `skill.unloaded` $TARGET

Screenshot this. That is the end-to-end proof.

## Pass / fail criteria

- **PASS**: all 9 steps produce the expected on-disk + dashboard
  evidence. The load → stale → unload cycle works.
- **PARTIAL**: suggestion surfaces but `pending-unload.json` never
  gains an entry (step 7 fails). Staleness detection is broken;
  release but file the issue.
- **FAIL**: step 5 fails — the skill can be "suggested" but never
  actually reaches the manifest's `load[]` set. The observer is
  blind to real loads. Release blocker — escalate.

## Known honest limitations

- We can't fully test Claude Code's own load mechanism from a
  simulation — we simulate the load event write. The verification
  is therefore of the ctx half of the contract (suggest → observe →
  queue-for-unload), not the IDE half (inject skill into prompt).
- `context_monitor` only suggests based on KEYWORD_SIGNALS +
  graph walks. If $TARGET has no tags that match any keyword, the
  suggestion won't surface. The candidate picker in step 1 filters
  on tag richness for that reason.
