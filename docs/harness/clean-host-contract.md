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
- `ctx-scan-repo --recommend` can scan a tiny FastAPI-like repo from the wheel.
- `ctx run` can start a session with a process-local fake LiteLLM provider.
- `ctx resume` can continue that session from the same isolated session store.
- `--deny-tool` blocks a model-requested ctx tool call before dispatch.

## What It Skips

- It does not run `ctx-init --graph`; graph builds are intentionally slow.
- It does not execute real Claude Code hooks inside a live Claude Code process.
- It does not connect to a real third-party MCP server.
- It does not browser-test the monitor dashboard.
- It does not simulate process kills or power loss during writes.

Those checks are tracked as later hardening phases in
`docs/plans/2026-04-27-current-next-steps-hardening.md`.

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

## CI Usage

The `.github/workflows/clean-host-contract.yml` workflow runs this contract
manually via `workflow_dispatch` and weekly on a schedule. It is not part of
the fast PR matrix yet; keep it that way until runtime and flake rate are
measured.

## Failure Triage

- Wheel build failure: inspect package metadata and `pyproject.toml`.
- Install failure: inspect dependency constraints and `pip check` output.
- `ctx-init` failure: inspect packaged entrypoints and hook module paths.
- `ctx-scan-repo` failure: inspect installed flat-module entrypoints and
  resolver imports.
- `ctx run` or `ctx resume` failure: inspect LiteLLM provider import behavior,
  session store paths, and CLI metadata replay.
- Tool denial failure: inspect `--allow-tool`/`--deny-tool` policy handling in
  `src/ctx/cli/run.py` and `src/ctx/adapters/generic/loop.py`.
