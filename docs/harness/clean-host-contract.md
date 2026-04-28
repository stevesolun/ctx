# Clean Host Contract

The clean-host contract is a release-hardening check for ctx. It builds the
current source tree into a wheel, installs that wheel into a fresh virtualenv,
redirects user-state environment variables into a temporary directory, and then
drives real console scripts.

It is intentionally implemented as `scripts/clean_host_contract.py`, not as a
public `ctx-*` command. The runner is infrastructure for maintainers until the
contract stabilizes.

## What It Proves

- The source tree can build a wheel.
- The built wheel installs into a clean virtualenv.
- Console-script entrypoints execute from the installed wheel.
- `ctx-init --hooks` writes Claude settings only under an isolated temp home.
- A deterministic fake Claude host reads the generated settings and executes
  the installed PostToolUse and Stop hook commands without calling Anthropic
  APIs.
- With `--run-live-claude`, a real Claude Code host can be exercised behind an
  explicit quota acknowledgement, non-spending preflights, and a hard budget
  cap. Hook execution is verified through a JSONL sentinel written by injected
  PostToolUse and Stop hooks under the temp root.
- `ctx-scan-repo --recommend` can scan a tiny FastAPI-like repo from the wheel.
- `ctx run` can start a session with a process-local fake LiteLLM provider.
- `ctx resume` can continue that session from the same isolated session store.
- `--deny-tool` blocks a model-requested ctx tool call before dispatch.
- Caller `PYTHONPATH` is stripped so the contract cannot accidentally import
  source-tree modules instead of the installed wheel.

## What It Skips

- It does not run `ctx-init --graph`; graph builds are intentionally slow.
- It does not execute hooks inside a live Claude Code process by default. The
  live host path is opt-in because it can consume Anthropic or provider quota.
- It does not connect to a real third-party MCP server.
- It does not browser-test the monitor dashboard.
- It does not simulate process kills or power loss during writes.

Those checks stay intentionally manual or opt-in until they are stable enough
for the default CI path.

## Local Usage

Run from the repository root:

```bash
python scripts/clean_host_contract.py --fast
```

For debugging, keep the temp directory:

```bash
python scripts/clean_host_contract.py --fast --keep-temp
```

To force a specific temp root:

```bash
python scripts/clean_host_contract.py --fast --temp-root /tmp/ctx-clean-host-debug
```

To run the real Claude Code host gate, use a shell with explicit non-file auth
available, acknowledge quota, and keep the budget small:

```bash
CTX_LIVE_CLAUDE_ACK=uses_quota \
python scripts/clean_host_contract.py --fast --run-live-claude --live-claude-max-budget-usd 0.05
```

Use `--claude-bin /path/to/claude` if `claude` is not on `PATH`. The live gate
runs `claude --version` and `claude auth status` before the budgeted prompt,
then appends sentinel hooks to the isolated `settings.json` and requires both
PostToolUse and Stop records in `live-claude-hooks.jsonl`. It intentionally
does not read OAuth or keychain state from the real user home; use explicit
environment/provider auth for this check.

## CI Usage

The main `.github/workflows/test.yml` workflow runs this contract on pushes and
pull requests. The standalone `.github/workflows/clean-host-contract.yml`
workflow remains available for manual runs and weekly scheduled drift checks.
CI uses the default fake-host path and does not spend model quota.

## Failure Triage

- Wheel build failure: inspect package metadata and `pyproject.toml`.
- Install failure: inspect dependency constraints and `pip check` output.
- `ctx-init` failure: inspect packaged entrypoints and hook module paths.
- Fake Claude hook-smoke failure: inspect generated `settings.json`, packaged
  hook module paths, and whether PostToolUse/Stop hook schemas changed.
- Live Claude gate failure: inspect explicit auth env/provider credentials,
  `claude auth status`, the budget cap, the injected sentinel hook entries, and
  `live-claude-hooks.jsonl` under the temp root.
- `ctx-scan-repo` failure: inspect installed flat-module entrypoints and
  resolver imports.
- `ctx run` or `ctx resume` failure: inspect LiteLLM provider import behavior,
  session store paths, and CLI metadata replay.
- Tool denial failure: inspect `--allow-tool`/`--deny-tool` policy handling in
  `src/ctx/cli/run.py` and `src/ctx/adapters/generic/loop.py`.
