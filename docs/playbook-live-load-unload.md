# Live load / unload verification playbook

> Does ctx actually observe skills and agents being loaded and unloaded
> on the fly, in real time, during a live Claude Code session? If yes,
> the telemetry pipeline is truly live. If no, this is a CODE RED — the
> "real-time knowledge graph" claim is broken and we escalate to an
> expert swarm.

## What "live load/unload" means in ctx's model

There is no central controller that "loads" a skill. Claude Code
itself decides when a skill's content is injected into the prompt
based on user intent + the skill's `description` match. ctx is an
**observer**, not a driver. Its claim is:

1. **Observe** — `PostToolUse` hook fires `context_monitor.py` on
   every tool call. When a tool call's content matches a skill-name
   signal, the event is recorded.
2. **Suggest** — unmatched signals accumulate in
   `~/.claude/pending-skills.json`. `skill_suggest.py` surfaces them
   into Claude's context as `hookSpecificOutput.additionalContext`,
   so Claude raises them to the user on next response.
3. **Record** — when Claude actually uses a skill (via its own load
   mechanism), the event lands in `~/.claude/skill-events.jsonl` as
   `{"event": "load", "skill": "<slug>", ...}`.
4. **Score** — the `Stop` hook runs `quality_on_session_end.py`,
   which recomputes the sidecar for every slug with new events. The
   telemetry signal reflects the load within seconds.

The pipeline is **only live if every one of those four links works
with no gap**. This playbook tests each.

## Prerequisites

- `claude-ctx` 0.5.0-rc6 installed (`pip install claude-ctx`).
- `~/.claude/skill-wiki/` present and graph pre-built
  (`graphify-out/graph.json` has 2,211 nodes / 642K edges).
- `~/.claude/settings.json` has the PostToolUse + Stop hooks wired.
- Baseline snapshot of `~/.claude/skill-events.jsonl` (line count).
- Baseline snapshot of 3 sidecars (`python-patterns`, `fastapi-pro`,
  `stripe-integration`) — raw JSON copies under `/tmp/baseline/`.

## Test matrix — each must PASS

### 1. Hook registration
- [ ] `settings.json` `PostToolUse` contains
      `context_monitor.py --from-stdin`.
- [ ] `settings.json` `PostToolUse` contains
      `skill_add_detector.py --from-stdin`.
- [ ] `settings.json` `PostToolUse` contains `skill_suggest.py`.
- [ ] `settings.json` `PostToolUse` contains
      `backup_on_change.py` under an `Edit|Write|MultiEdit` matcher.
- [ ] `settings.json` `Stop` contains `usage_tracker.py --sync`.
- [ ] `settings.json` `Stop` contains `quality_on_session_end.py`.
- [ ] All hook commands use `--from-stdin` (no
      `$CLAUDE_TOOL_INPUT`/`$CLAUDE_TOOL_NAME` argv interpolation).

### 2. Observe — context_monitor detects a known signal
- [ ] Feed `context_monitor.py --from-stdin` a synthetic tool-use
      event whose `tool_input.file_path` contains `fastapi`.
- [ ] Before: read `~/.claude/pending-skills.json` line count (or
      its `unmatched_signals` array length).
- [ ] After: length grew OR `graph_suggestions` changed.
- [ ] Repeat with `stripe`, `postgres`, `pci` — all three now in
      `KEYWORD_SIGNALS` as of rc5.

### 3. Suggest — skill_suggest surfaces pending skills
- [ ] Run `skill_suggest.py` with no args.
- [ ] Stdout is a valid JSON object with
      `hookSpecificOutput.hookEventName == "PostToolUse"` and
      `additionalContext` referencing at least one candidate skill
      from the pending-signals added in step 2.

### 4. Record — skill-events.jsonl grows on a simulated load
- [ ] Before: `wc -l ~/.claude/skill-events.jsonl`.
- [ ] Append three synthetic events:
      `load fastapi-pro`,  `load pci-compliance`,
      `unload fastapi-pro` (distinct `event_id`, realistic
      timestamp, distinct `session_id` from baseline).
- [ ] After: line count grew by exactly 3.

### 5. Score — sidecar refreshes on session end
- [ ] Save sidecar `fastapi-pro.json` copy → `/tmp/baseline/`.
- [ ] Run `python hooks/quality_on_session_end.py`.
- [ ] Stdout should include `fastapi-pro` in the "recomputed" list
      (only slugs with new events should be touched).
- [ ] Compare `~/.claude/skill-quality/fastapi-pro.json` mtime —
      must be newer than baseline by >0 seconds.
- [ ] Telemetry signal `load_count` increased by 2 (the two loads
      we appended); `recent_load_count` increased by 2.
- [ ] Overall `score` changed OR `grade` changed (F→D→C→B→A).

### 6. End-to-end timing
- [ ] From the moment the synthetic load event is written to
      skill-events.jsonl, how many seconds until
      `ctx-skill-quality explain fastapi-pro` reflects it?
      - Required: **< 30 seconds** with `quality_on_session_end.py`
        fired manually.
      - Stretch: **< 5 seconds** if the Stop hook fires on session
        close.
- [ ] Record the measured latency.

### 7. Session attribution — can we tell which session loaded what?
- [ ] Scan `skill-events.jsonl` for the test `session_id`.
- [ ] All 3 test events appear with that session_id.
- [ ] `ctx-skill-quality explain <slug>` doesn't currently surface
      per-session detail — **gap; track as future work**.

## Pass / fail criteria

**PASS** — all of 1–5 pass, and step 6 measured latency is under
30 seconds. This proves the pipeline is live.

**PARTIAL** — 4 of 5 pass; latency acceptable; one gap exists.
Ship the rc, file the gap as a follow-up issue.

**FAIL** — any of steps 2, 4, or 5 fails. The
observe→record→score loop is broken. This is a **CODE RED**:

1. Escalate to an expert swarm (Anthropic, OpenAI, Mistral,
   Microsoft observability + agent-system engineers).
2. Goal: design a minimum change set that makes the pipeline live.
3. Output: a 1-pager plan with the smallest possible code change
   and a test that would have caught the regression.

## What "expert swarm" looks like

Five sub-agents spawned in parallel, each briefed differently:

- **Anthropic engineer** — Claude Code hook model, `stdin` format,
  ordering guarantees of PostToolUse vs Stop. Tells us if what we
  expect is actually guaranteed by Claude Code.
- **OpenAI engineer** — event-driven telemetry design, how
  function-call / tool-call streams are typically observed in
  production ChatGPT-style systems. Tells us if our jsonl approach
  is sound or if we're missing a standard pattern.
- **Mistral engineer** — lightweight on-device observability, how
  to keep the observer cheap when the agent environment is local.
  Tells us whether our hook fan-out is causing latency.
- **Microsoft engineer** — VS Code extension + language-server
  observability. Tells us what hook contract an IDE-hosted agent
  should expose for a pluggable observer like ctx.
- **Consolidator** — takes the four reports and outputs a single
  plan with a "minimum viable fix" ranked by effort/reward.

Each agent gets **the same failing evidence**: the events.jsonl
before/after, the sidecar mtime check, the captured hook output.
Their job is to diagnose, not to re-run the test.

## Next steps after verification

- If PASS: ship the audit-log feature, ship `ctx-monitor serve`
  with per-session view.
- If PARTIAL: ship the gap fix, document the session-attribution
  limitation in the README.
- If FAIL: execute the expert-swarm plan, iterate until PASS.
