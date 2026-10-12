"""Fixtures shared by the Fit tests.

A repository with derivable history is needed by both the experiment tests and
the CLI tests, and it is fiddly enough — the scaffolding commit, the paired
source and test change — that two copies would drift into testing two different
things.
"""

from __future__ import annotations

import subprocess
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.release_catalog_fixture import RELEASE_CATALOG_FIXTURE_ROOT


@pytest.fixture
def historical_fit_catalog(monkeypatch):
    """Opt-in candidate/material fixture for the secondary evaluator, not production."""
    from ctx.fit import candidates, release_catalog

    payload = json.loads((RELEASE_CATALOG_FIXTURE_ROOT / "runtime-availability.json").read_text())
    entries = tuple(payload["entries"])
    signals = {
        "skill:ctx-python-testing": ("python", "pytest", "test"),
        "skill:ctx-python-state-protocols": ("python",),
        "skill:ctx-python-input-boundaries": ("python",),
        "skill:ctx-python-api-compatibility": ("python",),
        "skill:ctx-javascript-testing": ("javascript", "jest", "vitest", "test"),
        "skill:ctx-rust-patterns": ("rust", "cargo"),
        "skill:ctx-typescript": ("typescript", "javascript"),
        "agent:ctx-python-reviewer": ("python", "lint", "typecheck"),
    }
    material = {}
    for entry in entries:
        if entry["type"] == "skill":
            body = "\n\n".join(row["content"].strip() for row in entry["files"]).strip()
            material[entry["id"]] = (body, candidates._canonical_catalog_entry_digest(entry))
    monkeypatch.setattr(
        release_catalog,
        "_load_catalog",
        lambda: tuple({**entry, "signals": signals.get(entry["id"], ())} for entry in entries),
    )
    monkeypatch.setattr(candidates, "_packaged_capability_content", lambda: material)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(("git", "-C", str(repo), *args), check=True, capture_output=True, text=True)


@pytest.fixture
def repo_with_history(tmp_path: Path) -> Callable[..., Path]:
    """Build a repository ``derive_tasks`` accepts: paired source and test changes.

    ``commits`` counts the derivable ones. The scaffolding commit that lands the
    module is extra: a task reverts to the commit before its own, so the source
    file has to already exist there.
    """

    def build(*, commits: int = 1) -> Path:
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "tests").mkdir(parents=True)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")

        source = repo / "src" / "calc.py"
        source.write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "chore: scaffold the calc module")

        source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (repo / "tests" / "test_calc.py").write_text(
            "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            encoding="utf-8",
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "feat: add addition helper")

        for index in range(1, commits):
            with source.open("a", encoding="utf-8") as handle:
                handle.write(f"\n\ndef op{index}(a, b):\n    return a + b + {index}\n")
            (repo / "tests" / f"test_op{index}.py").write_text(
                f"from src.calc import op{index}\n\n\n"
                f"def test_op{index}():\n    assert op{index}(1, 1) == {2 + index}\n",
                encoding="utf-8",
            )
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", f"feat: add op{index}")
        return repo

    return build
