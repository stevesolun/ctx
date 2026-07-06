# Contributing to ctx

Thank you for your interest in contributing.

## Dev environment setup

```bash
git clone https://github.com/stevesolun/ctx && cd ctx
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

To also run the similarity/embedding tests (requires ~100 MB model download):

```bash
pip install -e ".[dev,embeddings]"
```

## Running tests

```bash
pytest -q                          # fast suite (skips integration)
pytest -q -m 'not integration'     # same, explicit
pytest -q -m integration           # embedding precision/recall tests
pytest --cov=src -q                # with coverage report
```

Before opening a PR, run the local fast gate on committed branch history:

```bash
scripts/no_mistakes_run.sh fast
```

This selects the same PR checks as CI, groups independent work into lanes, and
runs those lanes in isolated temporary git worktrees so local CPU, graph, docs,
package, and test checks can run in parallel. It is the fast front door; the
serial preflight/no-mistakes gate remains the authoritative final check:

```bash
python scripts/ci_preflight.py --profile pr
```

The preflight uses the same changed-file classifier as CI. Its no-test policy
treats source, workflows, `pyproject.toml`, `scripts/ci_*`, maintainer graph/sync
scripts, and `.no-mistakes.yaml` as contract files; include focused
`src/tests/...` coverage unless the diff is a proven version or stats-only
release metadata update.

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
when present and owner-only, plus Codex-bundled resources, without installing
or upgrading system packages. Candidate venvs are checked in this order:
`CTX_NO_MISTAKES_PYTHON_BIN`, `$PWD/.venv/bin`, this repository's `.venv/bin`,
then `/tmp/ctx-verify-venv/bin`; the first owner-only venv containing
`pytest`, `ruff`, and `mypy` wins and is exposed as
`CTX_NO_MISTAKES_PYTHON_BIN_RESOLVED`. `CTX_NO_MISTAKES_CODEX_RESOURCES` and
`CTX_NO_MISTAKES_REAL_CODEX` override the Codex resource directory or binary.
The repo disables review-stage no-mistakes auto-fixes (`auto_fix.review: 0`) so
review findings stay human-approved; rebase, test, document, lint, and CI stages
still allow three automated repair attempts.

## Release publishing

PyPI publishes must run from a version tag that matches `pyproject.toml`; manual
PyPI workflow dispatch is disabled. The publish workflow resolves the full and
runtime graph tarballs from matching GitHub release cache assets first. If a
checked-out Git LFS pointer is newer than the cache, it performs a targeted
`git lfs pull` for that artifact only, enforces the configured pointer size cap,
verifies SHA-256 and byte size, then runs graph validation before building and
publishing the package.

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
