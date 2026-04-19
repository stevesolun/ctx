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
python -m backup_mirror snapshot-if-changed --reason smoke-test --json
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
python -m backup_mirror snapshot-if-changed --reason manual-check

# force an unconditional snapshot with a reason label
python -m backup_mirror create --reason pre-upgrade
```

Both land under `~/.claude/backups/<timestamp>__<reason>/` and write a
`manifest.json` that records the reason alongside every file's SHA-256.

## Watchdog — snapshot on changes outside a Claude session

The PostToolUse hook only fires on `Edit` / `Write` / `MultiEdit` tool
calls *inside* a Claude session. If you edit `~/.claude/settings.json`
in VS Code, or a `git pull` updates an agent file, the hook never
sees it.

For that gap, run the polling watchdog — a simple loop that calls
`snapshot-if-changed` every N seconds:

```bash
python -m backup_mirror watchdog --interval 60
```

Flags:

| Flag | Meaning |
| --- | --- |
| `--interval N` | Seconds between polls. Clamped to `[5, 3600]`. Default 60. |
| `--reason-prefix LBL` | Prefix used for each snapshot's `--reason` label. Default `watchdog`. |
| `--once` | Run exactly one tick and exit. Useful for cron / Task Scheduler. |
| `--json` | Emit run stats as JSON on exit. |

Because change detection is SHA-gated, polling is cheap — a tick with
no real changes does zero disk writes.

### Running it as a background service

Ready-to-use service manifests live under
[`docs/services/`](https://github.com/stevesolun/ctx/tree/main/docs/services).
Each one expects you to edit a handful of paths — there's no installer
that guesses where you keep the checkout.

- **Linux (systemd user unit)** —
  [`docs/services/systemd/claude-backup-watchdog.service`](https://github.com/stevesolun/ctx/blob/main/docs/services/systemd/claude-backup-watchdog.service).
  Copy to `~/.config/systemd/user/`, set `CTX_REPO`, then
  `systemctl --user enable --now claude-backup-watchdog.service`.
- **macOS (launchd agent)** —
  [`docs/services/macos/com.claude.backup.watchdog.plist`](https://github.com/stevesolun/ctx/blob/main/docs/services/macos/com.claude.backup.watchdog.plist).
  Edit the `ProgramArguments` paths, drop into
  `~/Library/LaunchAgents/`, then
  `launchctl load -w ~/Library/LaunchAgents/com.claude.backup.watchdog.plist`.
- **Windows (Task Scheduler installer)** —
  [`docs/services/windows/install-backup-watchdog.ps1`](https://github.com/stevesolun/ctx/blob/main/docs/services/windows/install-backup-watchdog.ps1).
  Run `pwsh -File docs/services/windows/install-backup-watchdog.ps1`
  from the repo root; it detects Python on PATH, registers a
  `ClaudeBackupWatchdog` scheduled task that runs at logon, and kicks
  off the first tick. `-Uninstall` removes it.

All three manifests assume the watchdog runs as an **unprivileged
user** — no admin/root — because it only reads `~/.claude/` and writes
`~/.claude/backups/`.

The watchdog stops cleanly on SIGINT/SIGTERM, flushes its stats line
to stderr, and exits 0. Pair it with the hook: the hook handles
in-session edits in real time; the watchdog catches everything else.

## Retention — how old snapshots get pruned

Auto-pruning runs after every successful `snapshot-if-changed`, so the
hook cannot fill the disk. The active policy comes from
`BackupRetention` in `src/backup_config.py` (or your user override):

| Field | Default | Meaning |
| --- | --- | --- |
| `keep_latest` | `50` | Always keep the N most-recent snapshots. |
| `keep_daily` | `14` | For the M most-recent UTC days that have snapshots, keep the newest snapshot from each. |

A snapshot survives the sweep iff it's in the **union** of those two
sets. Snapshots whose `manifest.json` has a missing or zero
`created_at` are always protected — we never silently delete something
we can't place in time.

To override per user, add a partial config at
`~/.claude/backup-config.json`:

```json
{
  "retention": { "keep_latest": 100, "keep_daily": 30 }
}
```

### Manual prune

```bash
# Dry-run the configured policy — no deletions, JSON report.
python -m backup_mirror prune --policy --dry-run --json

# Apply the configured policy for real.
python -m backup_mirror prune --policy

# Legacy mode (still works): keep only the N newest.
python -m backup_mirror prune --keep 20
```

The policy output tells you which snapshots were kept by `keep_latest`
versus `keep_daily`, so a surprising retention decision is easy to
audit.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Hook never fires | Settings not reloaded, or `matcher` typo. |
| Snapshot folder with no `reason` suffix | Called `create` without `--reason`. |
| Hook fires but no folder appears | Content hash matched — nothing actually changed. |
| Credentials appear in a snapshot | User put them in `top_files`; the `ALWAYS_EXCLUDE` filter would drop them — check you're on the current `backup_config.py`. |
| `ImportError: backup_config` from the hook | Repo moved; update the path in `settings.json`. |
| Snapshots pile up forever | `retention.keep_latest` / `keep_daily` too high. Run `prune --policy --dry-run --json` to see what the current policy would do, then lower the caps in `~/.claude/backup-config.json`. |
| Prune removed too much | Run `prune --policy --dry-run` *before* committing to a new policy. A snapshot with a missing/zero `created_at` is always protected, so if it's getting deleted the manifest is probably fine and the policy is genuinely too aggressive. |
