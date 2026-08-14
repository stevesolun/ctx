# Contributing to ctx

Thank you for your interest in contributing.

## Dev environment setup

```bash
git clone https://github.com/stevesolun/ctx && cd ctx
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,browser,embeddings]" -r requirements-docs.txt build twine
python -m playwright install chromium
git config core.hooksPath .githooks
```

`".[dev]"` alone is not enough to reproduce the pre-PR gate. The `browser`
extra supplies Playwright (plus the `playwright install chromium` browser
download) for the browser lane; `embeddings` supplies sentence-transformers for
the similarity tests and pulls a ~100 MB model on first use; `twine` and the
MkDocs pins in `requirements-docs.txt` are not in any extra, so they must be
installed separately. This is the dependency set CI installs in
`.github/workflows/m5-local-fast.yml` (that workflow installs non-editable;
`-e` is for local work).

`git config core.hooksPath .githooks` enables `.githooks/pre-commit`, which
re-runs `src/update_repo_stats.py` and re-stages the refreshed `README.md`,
`docs/index.md`, `docs/knowledge-graph.md`, and `docs/catalog.md` whenever a
commit touches `src/tests/*.py` or the other stat sources. The `repo stats`
check in `scripts/ci_preflight.py` is unconditional — it runs on every profile
— and it fails when those files are stale. Since the no-test policy pushes
nearly every PR to add or change tests, and changing the test count changes the
README inventory badge, skipping the hook means fixing the badge by hand on
almost every PR.

## Running tests

Use the CI selection as your inner loop:

```bash
pytest -q -m "not browser and not integration" --no-cov \
  -n auto --dist=loadfile --max-worker-restart=0
```

That is the selection CI's unit job and `ci_preflight.py`'s `unit-linux
equivalent` check run, minus coverage — both add
`--cov=src --cov-report=term-missing --cov-fail-under=40`, which you want
before pushing but not on every iteration.

Plain `pytest -q` is **not** a fast suite. There is no `addopts` anywhere in
this repo — not in `pyproject.toml`, and there is no `pytest.ini` or
`setup.cfg` — so it selects everything and runs it in a single process. On one
developer machine, measured back to back on the same checkout:

| Command | Wall time |
| --- | --- |
| the CI selection above | 4m41s |
| `pytest -q` | 11m57s |

The marker filter is not what makes the first one fast: it deselects only 14 of
the 8,524 collected tests. The speedup is `-n auto`, which fans the suite
across cores via pytest-xdist (already in the `dev` extra). `--dist=loadfile`
keeps all tests from one file on one worker.

```bash
pytest -q -m integration              # embedding precision/recall tests
pytest -q -m browser                  # Playwright tests (needs the browser extra)
pytest --cov=src -q                   # with coverage report
pytest -q src/tests/test_package_scaffold.py   # one file, fastest of all
```

Before opening a PR, run the local fast gate on committed branch history:

```bash
scripts/no_mistakes_run.sh fast
```

This selects the same PR checks as CI, groups independent work into lanes, and
runs those lanes in isolated temporary git worktrees so local CPU, graph, docs,
package, and test checks can run in parallel. It writes lane timing evidence to
`.gate/local-fast.json` by default. It is the fast front door; the
serial preflight/no-mistakes gate remains the authoritative final check:

```bash
python scripts/ci_preflight.py --profile pr
```

The preflight uses the same changed-file classifier as CI. Its no-test policy
treats source, workflows, `pyproject.toml`, `scripts/ci_*`, maintainer graph/sync
scripts, and `.no-mistakes.yaml` as contract files; include focused
`src/tests/...` coverage unless the diff is a proven version or stats-only
release metadata update.

## Design decisions

CTX Fit has a decision log. Before proposing a change to how Fit works, read it
— sixteen ADRs (ADR-001 through ADR-016) are `ACCEPTED`, and they are not
open for re-litigation without new evidence.

- [`docs/ctx-fit/DECISIONS.md`](docs/ctx-fit/DECISIONS.md) — the ADRs
  themselves, each recorded once with its evidence.
- [`docs/ctx-fit/MAP.md`](docs/ctx-fit/MAP.md) — a wayfinding index of what is
  settled, what is a precise open question, what is still unknown, and what is
  deliberately out of scope.

If you have new evidence against a settled decision, record the evidence in
`DECISIONS.md` rather than quietly reversing the decision in code.

## Package-layout migration

The legacy flat modules and the `ctx` package intentionally coexist during the
staged migration. `pyproject.toml` is the authoritative inventory of packaged
modules, packages, and console-script targets; `src/tests/test_package_scaffold.py`
pins the import and distribution surface.

When moving a module, preserve its documented CLI and import behavior, update
the relevant `pyproject.toml` entries, and add focused tests for the canonical
path and any compatibility shim. Do not remove a flat shim merely because the
new package path exists: shim removal requires an explicit migration phase plus
clean-install and packaging evidence. Run the package-scaffold tests and the
affected module tests before the repository gates.

Development and CI support CPython 3.11+ on Linux and macOS. Other POSIX
systems are best-effort. Native Windows and PowerShell are unsupported; use a
Linux installation under WSL2 when developing on a Windows machine.

GitHub PRs skip the broad OS/Python `test` matrix. The full Linux/macOS pytest
matrix runs after merge on `main`; PRs use focused required jobs selected from
the changed surface. Local-fast and preflight remain the first pass.

## Documentation changes

Public docs surfaces are release-tracked in the canonical
`qa/feature_status.csv` tracker, with supporting rows in
`docs/qa/feature-user-story-status.csv` and
`docs/qa/dashboard-user-story-status.csv`. If you add, remove, or move a
`.md` entry under `mkdocs.yml` `nav`, or change linked public assets under
`docs/assets/javascripts/`, `docs/services/`, or `docs/toolbox/templates/`,
update the relevant supporting row and canonical row with the exact path in
`entrypoint_or_route` and run:

```bash
python -m pytest -q --no-cov \
  src/tests/test_bug_smoke_tracker.py \
  src/tests/test_feature_user_story_tracker.py \
  src/tests/test_dashboard_user_story_tracker.py \
  src/tests/test_toolbox_cli.py
```

## Code style

Both **ruff** and **mypy** must pass before a PR is merged.

```bash
ruff check src hooks scripts          # linting
ruff format --check src hooks scripts # formatting check
mypy src/                # type checking
```

Fix formatting in one shot:

```bash
ruff format src hooks scripts
ruff check --fix src hooks scripts
```

## No-mistakes runner

Maintainer no-mistakes agents can use `scripts/no_mistakes_codex_env.sh` as
the Codex wrapper for this repo. It prepends the verified project Python venv
when present and owner-only, plus either the configured Codex resource directory
or the resolved executable's directory, without installing or upgrading system
packages. Candidate venvs are checked in this order:
`CTX_NO_MISTAKES_PYTHON_BIN`, `$PWD/.venv/bin`, this repository's `.venv/bin`,
then `/tmp/ctx-verify-venv/bin`; the first owner-only venv containing
`pytest`, `ruff`, and `mypy` wins and is exposed as
`CTX_NO_MISTAKES_PYTHON_BIN_RESOLVED`.

Codex executable discovery first accepts a valid
`CTX_NO_MISTAKES_REAL_CODEX`. When that variable is unset, it checks
`CTX_NO_MISTAKES_CODEX_RESOURCES/codex`, the colon-separated candidates in
`CTX_NO_MISTAKES_CODEX_APP_PATHS`, then `codex` on `PATH`. When the app-path
variable is unset, its candidates default to system `Codex.app`, system
`ChatGPT.app`, user `Codex.app`, then user `ChatGPT.app`. An explicitly empty
app-path value disables those app candidates. An invalid explicit executable
fails closed with exit 127. A resource override is validated as an executable
source only when `CTX_NO_MISTAKES_REAL_CODEX` is unset; when both are set and
the executable is valid, the resource directory is prepended to `PATH` without
separate validation. Invalid app candidates are skipped before the `PATH`
fallback.

The repo disables review-stage no-mistakes auto-fixes (`auto_fix.review: 0`) so
review findings stay human-approved; rebase, test, document, lint, and CI stages
still allow three automated repair attempts.

## Release publishing

PyPI publishes must run from a version tag that matches `pyproject.toml`; manual
PyPI workflow dispatch is disabled. The tagged commit must be the exact current
`main` head and the latest canonical `Tests` workflow for that exact SHA must be
a successful `main` push run. The publish workflow verifies those facts before
building. It resolves the full and runtime graph tarballs from matching GitHub
release cache assets first. If a checked-out Git LFS pointer is newer than the
cache, it performs a targeted `git lfs pull` for that artifact only, enforces the
configured pointer size cap, verifies SHA-256 and byte size, then runs graph
validation before building and publishing the package.

## Commit conventions

This repo uses [Conventional Commits](https://www.conventionalcommits.org/):

```
feat:     new feature
fix:      bug fix
refactor: code restructuring without behaviour change
docs:     documentation only
test:     test additions or corrections
chore:    maintenance (deps, CI, tooling)
perf:     performance improvement
ci:       CI/CD changes
```

Scope is optional but encouraged, e.g. `feat(intake): add fuzzy-match gate`.

## Reporting bugs

Open an issue at <https://github.com/stevesolun/ctx/issues>. Include:

- Python version and OS
- Full traceback
- Minimal reproduction steps

## Pull request process

1. Fork the repo and create a feature branch from `main`.
2. Make your changes. Add or update tests — the CI gate requires the existing suite to pass.
3. Ensure `ruff` and `mypy` pass locally.
4. Open a PR against `main`. Fill in the PR template.
5. A maintainer will review and merge once CI is green.
