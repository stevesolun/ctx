# Stage 3: Build — Resolve the Manifest

Convert the stack profile into a concrete load/unload list.

## Steps

1. **Run the resolver**

```bash
python3 ~/.claude/ctx/resolve_skills.py \
  --profile /tmp/skill-stack-profile.json \
  --wiki ~/.claude/skill-wiki \
  --output ~/.claude/skill-manifest.json \
  --intent-log ~/.claude/intent-log.jsonl
```

2. **Read the manifest** (`~/.claude/skill-manifest.json`)
   - Extract: `load[]`, `unload[]`, `warnings[]`, `suggestions[]`

3. **Sync to wiki**

```bash
python3 ~/.claude/ctx/wiki_sync.py \
  --profile /tmp/skill-stack-profile.json \
  --manifest ~/.claude/skill-manifest.json \
  --wiki ~/.claude/skill-wiki
```

## apply_pending Fast Path

If Stage 1 returned `apply_pending`:
- Read `pending-skills.json` suggestion list
- Add each suggested skill to the manifest load list (if available on disk)
- Skip re-running the full resolver

## On Failure

If resolver fails: use previous manifest if < 24 hours old. Report error as warning.
