# Stage 1: Scope — Is a Scan Needed?

Determine which action to take before spending tokens on a full scan.

## Check Order

1. **Read** `~/.claude/pending-skills.json`
   - If exists and `generated_at` is < 2 hours old → action = `apply_pending`
   - Delete the file after reading (it is one-shot)

2. **Read** `~/.claude/skill-manifest.json`
   - Extract `repo_path` and `generated_at`
   - If `repo_path` ≠ current working directory → action = `full_scan`
   - If `generated_at` > 1 hour ago → action = `full_scan`
   - Otherwise → action = `use_cached`

3. **If no manifest exists** → action = `full_scan`

## Output

Emit one of:
```
ACTION: full_scan      — proceed to Stage 2
ACTION: apply_pending  — skip to Stage 3 (use pending-skills.json as manifest delta)
ACTION: use_cached     — skip to Stage 5 (just display current manifest)
```

## Fast Path

If `use_cached`: read current manifest load list and jump to Stage 5 directly.
If `apply_pending`: merge pending into current manifest, jump to Stage 4.
