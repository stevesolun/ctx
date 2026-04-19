# Change-triggered backup — hook install

One page on wiring the `backup_on_change.py` PostToolUse hook into Claude
Code so a new snapshot fires automatically whenever you edit a tracked
config file (`~/.claude/settings.json`, agents, skills, top-level
manifests, etc.).

## What it does

On every `Edit` / `Write` / `MultiEdit` tool call, the hook:

1. Reads the tool payload from stdin.
2. Resolves `tool_input.file_path` and checks if it sits under
   `~/.claude` in a file/tree/memory path tracked by `BackupConfig`.
3. If tracked, shells out to
   `python <repo>/src/backup_mirror.py snapshot-if-changed --reason <tool>:<basename>`.
4. `snapshot-if-changed` hashes every tracked file, compares against the
   most recent snapshot's `manifest.json`, and only creates a new folder
   when at least one SHA differs.

No-op edits don't create folders. The hook always exits 0 so a bug in
the backup layer cannot stall a Claude session.

## Register the hook

Edit `~/.claude/settings.json` and add the following under `hooks` (keep
any existing entries alongside it). Replace `<REPO>` with the absolute
path to this checkout — on Windows this is a path like
`C:/Steves_Files/Work/Research_and_Papers/ctx`.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python <REPO>/hooks/backup_on_change.py"
          }
        ]
      }
    ]
  }
}
```

Notes:

- The `matcher` is a regex against the tool name — the three names above
  are the only tools that touch files.
- Use forward slashes in the path even on Windows.
- If `python` on your PATH is not the interpreter you want, give the
  absolute path instead (e.g.
  `C:/Users/you/.pyenv/pyenv-win/versions/3.13.2/python.exe`).

## Verify it works

1. Reload Claude Code (the hook registration is read at session start).
2. Edit a tracked file, e.g. `~/.claude/CLAUDE.md`.
3. Watch `~/.claude/backups/` — a new folder named
   `<timestamp>__edit-claude-md` should appear within a second.
4. Edit the same file again with identical content — no new folder
   appears (SHA is unchanged).

If nothing shows up, run the verb manually to isolate the failure:

```bash
python src/backup_mirror.py snapshot-if-changed --reason smoke-test --json
```

The JSON output tells you which files the detector considered new,
changed, or removed.

## What gets backed up

See `src/backup_config.py` and the `backup` section of
`src/config.json` for the current defaults:

- **top_files** — `settings.json`, `skill-manifest.json`,
  `pending-skills.json`, `CLAUDE.md`, `AGENTS.md`, `user-profile.json`,
  `skill-system-config.json`, `skill-registry.json`.
- **trees** — `agents/`, `skills/`.
- **memory** — `projects/*/memory/**` when `memory_glob` is true.
- **always excluded** — `.credentials.json`, `claude.json`, token
  caches; these are dropped even if a user config lists them.

To override per user, drop a partial config at
`~/.claude/backup-config.json`. Fields you omit fall back to the repo
default. Example:

```json
{
  "retention": { "keep_latest": 100 },
  "top_files": ["settings.json", "CLAUDE.md"]
}
```

## Manual CLI

The same verb is available as a one-shot command:

```bash
# snapshot only when something changed
python src/backup_mirror.py snapshot-if-changed --reason manual-check

# force an unconditional snapshot with a reason label
python src/backup_mirror.py create --reason pre-upgrade
```

Both land under `~/.claude/backups/<timestamp>__<reason>/` and write a
`manifest.json` that records the reason alongside every file's SHA-256.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Hook never fires | Settings not reloaded, or `matcher` typo. |
| Snapshot folder with no `reason` suffix | Called `create` without `--reason`. |
| Hook fires but no folder appears | Content hash matched — nothing actually changed. |
| Credentials appear in a snapshot | User put them in `top_files`; the `ALWAYS_EXCLUDE` filter would drop them — check you're on the current `backup_config.py`. |
| `ImportError: backup_config` from the hook | Repo moved; update the path in `settings.json`. |
