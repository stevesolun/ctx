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

## Documentation changes

Public docs surfaces are release-tracked in
`docs/qa/feature-user-story-status.csv`. If you add, remove, or move a `.md`
entry under `mkdocs.yml` `nav`, or change linked public assets under
`docs/assets/javascripts/`, `docs/services/`, or `docs/toolbox/templates/`,
update the canonical feature tracker with the exact path in
`entrypoint_or_route` and run:

```bash
python -m pytest -q --no-cov \
  src/tests/test_feature_user_story_tracker.py \
  src/tests/test_toolbox_cli.py
```

## Code style

Both **ruff** and **mypy** must pass before a PR is merged.

```bash
ruff check src/          # linting
ruff format --check src/ # formatting check
mypy src/                # type checking
```

Fix formatting in one shot:

```bash
ruff format src/
ruff check --fix src/
```

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
