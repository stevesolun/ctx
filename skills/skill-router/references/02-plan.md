# Stage 2: Plan — Run the Scanner

Only reached if Stage 1 determined `full_scan`.

## Steps

1. **Identify current repo root**
   - Use current working directory from the session
   - Confirm it looks like a project (has package.json, pyproject.toml, Cargo.toml, go.mod, etc.)
   - If it's the home dir or system dir → warn and use limited scan

2. **Run the scanner**

```bash
python3 ~/.claude/ctx/scan_repo.py \
  --repo <cwd> \
  --output /tmp/skill-stack-profile.json
```

(Replace `~/.claude/ctx` with the actual ctx_dir from `~/.claude/skill-registry.json`)

3. **Read the output** (`/tmp/skill-stack-profile.json`)
   - Report top 5 detected stacks with confidence scores
   - Note any monorepo packages detected

## Expected Duration

< 10 seconds for repos up to 10K files. If it takes longer, something is wrong.

## On Failure

If scanner exits non-zero or output file is missing:
- Fallback: use `use_cached` action if manifest exists
- Otherwise: proceed to Stage 3 with an empty profile (will load only meta-skills)
