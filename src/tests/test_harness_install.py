"""Tests for ctx-harness-install."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import harness_install


def _write_harness_page(
    wiki: Path,
    slug: str = "text-to-cad",
    **frontmatter: Any,
) -> Path:
    data: dict[str, Any] = {
        "title": "Text to CAD",
        "type": "harness",
        "status": "cataloged",
        "repo_url": "https://github.com/earthtojake/text-to-cad",
        "tags": ["cad", "3d"],
        "runtimes": ["python", "node"],
        "setup_commands": ["python -m pip install -e ."],
        "verify_commands": ["python -m pytest"],
    }
    data.update(frontmatter)
    page = wiki / "entities" / "harnesses" / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.extend(["---", "", "# Harness"])
    page.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return page


@dataclass
class _FakeRun:
    returncode: int = 0
    stdout: str = "ok"
    stderr: str = ""


def test_dry_run_prints_plan_without_writing(tmp_path: Path, capsys: Any) -> None:
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki)
    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
        dry_run=True,
    )

    assert result.status == "dry-run"
    assert not (tmp_path / "installs").exists()
    assert "Text to CAD" in capsys.readouterr().out


def test_install_copies_local_source_and_writes_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("harness", encoding="utf-8")
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))

    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
    )

    assert result.status == "installed"
    assert (tmp_path / "installs" / "text-to-cad" / "README.md").exists()
    manifest = json.loads(
        (tmp_path / "manifests" / "text-to-cad.json").read_text(encoding="utf-8")
    )
    assert manifest["slug"] == "text-to-cad"
    assert manifest["status"] == "installed"
    assert manifest["setup_commands_run"] == []
    assert manifest["verify_commands_run"] == []


def test_setup_and_verify_commands_require_explicit_flags(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeRun:
        calls.append(cmd)
        return _FakeRun()

    monkeypatch.setattr(harness_install.subprocess, "run", fake_run)

    harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs-a",
        manifest_dir=tmp_path / "manifests-a",
    )
    assert calls == []

    harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs-b",
        manifest_dir=tmp_path / "manifests-b",
        approve_commands=True,
        run_verify=True,
    )
    assert calls == [
        ["python", "-m", "pip", "install", "-e", "."],
        ["python", "-m", "pytest"],
    ]


def test_target_must_stay_inside_installs_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))

    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
        target=tmp_path / "outside",
    )

    assert result.status == "invalid-target"
    assert not (tmp_path / "outside").exists()


def test_repo_url_identifier_resolves_matching_page(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url="https://github.com/earthtojake/text-to-cad")

    record = harness_install.resolve_harness(
        "https://github.com/earthtojake/text-to-cad",
        wiki_path=wiki,
    )

    assert record.slug == "text-to-cad"


def test_missing_harness_fails_clearly(tmp_path: Path) -> None:
    result = harness_install.install_harness(
        "missing",
        wiki_path=tmp_path / "wiki",
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
    )

    assert result.status == "not-found"
    assert "missing" in result.message
