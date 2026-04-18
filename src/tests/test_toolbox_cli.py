"""
test_toolbox_cli.py -- Regression tests for the toolbox.py CLI.

Uses monkeypatch to redirect global_config_path() to a tmp file so each test
runs in isolation without touching the real ~/.claude/toolboxes.json.

Exercises:
- init seeds starter templates
- init refuses to overwrite without --force
- list shows seeded toolboxes
- show prints JSON for a known toolbox, errors on unknown
- activate / deactivate round-trips
- export / import round-trips via JSON
- validate reports no error on seeded config
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import toolbox as cli
import toolbox_config as tc


@pytest.fixture()
def isolated_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point global_config_path() at a fresh tmp file for the test."""
    target = tmp_path / "toolboxes.json"
    monkeypatch.setattr(tc, "global_config_path", lambda: target)
    monkeypatch.setattr(cli, "global_config_path", lambda: target)
    return target


def _run(argv: list[str]) -> int:
    return cli.main(argv)


def test_init_seeds_starter_templates(isolated_global: Path, capsys):
    rc = _run(["init"])
    assert rc == 0
    assert isolated_global.exists()
    raw = json.loads(isolated_global.read_text(encoding="utf-8"))
    names = set(raw["toolboxes"].keys())
    # All 5 expected starters should be present
    assert {"ship-it", "security-sweep", "refactor-safety",
            "docs-review", "fresh-repo-init"}.issubset(names)


def test_init_refuses_without_force(isolated_global: Path, capsys):
    assert _run(["init"]) == 0
    rc = _run(["init"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--force" in err


def test_init_force_overwrites(isolated_global: Path, capsys):
    assert _run(["init"]) == 0
    assert _run(["init", "--force"]) == 0


def test_list_after_init_shows_all(isolated_global: Path, capsys):
    _run(["init"])
    capsys.readouterr()
    assert _run(["list"]) == 0
    out = capsys.readouterr().out
    for name in ["ship-it", "security-sweep", "refactor-safety",
                 "docs-review", "fresh-repo-init"]:
        assert name in out


def test_show_prints_json(isolated_global: Path, capsys):
    _run(["init"])
    capsys.readouterr()
    assert _run(["show", "ship-it"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "ship-it"
    assert "code-reviewer" in payload["post"]


def test_show_unknown_errors(isolated_global: Path, capsys):
    _run(["init"])
    capsys.readouterr()
    assert _run(["show", "nonexistent"]) == 1


def test_activate_and_deactivate(isolated_global: Path, capsys):
    _run(["init"])
    capsys.readouterr()
    assert _run(["activate", "ship-it"]) == 0
    raw = json.loads(isolated_global.read_text(encoding="utf-8"))
    assert "ship-it" in raw["active"]
    assert _run(["deactivate", "ship-it"]) == 0
    raw = json.loads(isolated_global.read_text(encoding="utf-8"))
    assert "ship-it" not in raw["active"]


def test_activate_unknown_errors(isolated_global: Path, capsys):
    assert _run(["activate", "ghost"]) == 1


def test_export_then_import_roundtrip(isolated_global: Path,
                                      tmp_path: Path,
                                      capsys: pytest.CaptureFixture):
    _run(["init"])
    capsys.readouterr()

    # Export (prefer JSON output so we can round-trip without PyYAML)
    # export writes to stdout \u2014 capture and save
    _run(["export", "ship-it"])
    exported = capsys.readouterr().out
    # If YAML output, we can still write it to a .yaml file; otherwise .json
    ext = ".yaml" if exported.lstrip().startswith("version:") else ".json"
    share = tmp_path / f"shared{ext}"
    share.write_text(exported, encoding="utf-8")

    # Nuke global and re-import
    isolated_global.unlink()
    assert _run(["import", str(share)]) == 0
    raw = json.loads(isolated_global.read_text(encoding="utf-8"))
    assert "ship-it" in raw["toolboxes"]


def test_import_skips_duplicates_without_force(isolated_global: Path,
                                               tmp_path: Path,
                                               capsys: pytest.CaptureFixture):
    _run(["init"])
    capsys.readouterr()

    # Create a simple JSON payload matching ship-it
    share = tmp_path / "dup.json"
    share.write_text(json.dumps({
        "version": 1,
        "toolboxes": {"ship-it": {"description": "imported"}},
    }), encoding="utf-8")

    _run(["import", str(share)])
    err = capsys.readouterr().err
    assert "Skip ship-it" in err

    # With --force, the import should succeed
    _run(["import", str(share), "--force"])
    raw = json.loads(isolated_global.read_text(encoding="utf-8"))
    assert raw["toolboxes"]["ship-it"]["description"] == "imported"


def test_validate_clean_config(isolated_global: Path, capsys):
    _run(["init"])
    capsys.readouterr()
    assert _run(["validate"]) == 0


def test_validate_explicit_bad_file(tmp_path: Path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('{"version": 2}', encoding="utf-8")
    assert cli.main(["validate", str(bad)]) == 2
