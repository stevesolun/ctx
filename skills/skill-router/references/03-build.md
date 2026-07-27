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

The default selects one highest-ranked installed skill directly mapped from
detected stack evidence. Resident meta skills and explicit `always_load`
overrides may also appear. `--max-skills N` broadens automatic resolution; it
does not approve named candidates. For exact selection, move only approved
entries from `suggestions[]` to `load[]` in the manifest or set `always_load`.

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
- Read the ranked `pending-skills.json` suggestion list.
- Add only the highest-ranked directly relevant skill that is available on disk.
- Keep all other candidates as suggestions; do not bulk-merge them into `load[]`.
- Add more only after explicit user selection, new task evidence, or an
  `always_load` override. Apply approved names directly to the manifest;
  `--max-skills N` changes only the automatic cap and is not a named approval.
- Skip re-running the full resolver only when the single selected skill can be
  validated from the pending entry.

## On Failure

If resolver fails: use previous manifest if < 24 hours old. Report error as warning.
