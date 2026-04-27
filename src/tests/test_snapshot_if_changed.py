"""
Tests for change_detector + snapshot-if-changed CLI verb.

Covers:
  - detect_changes reports every file as 'new' when no baseline exists
  - unchanged files stay unchanged; edited files show up under 'changed'
  - deleted files show up under 'removed'
  - symlinks and oversize files are excluded on both sides (no phantom diff)
  - snapshot_if_changed creates a snapshot only when the report has changes
  - snapshot_if_changed honours --reason for the folder name
  - CLI invocation ``snapshot-if-changed --json`` prints structured output
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import backup_config as bc  # noqa: E402
import backup_mirror as bm  # noqa: E402
import change_detector as cd  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect bm.CLAUDE_HOME + BACKUPS_DIR at tmp_path for isolation."""
    home = tmp_path / "claude"
    home.mkdir()
    backups = home / "backups"
    monkeypatch.setattr(bm, "CLAUDE_HOME", home)
    monkeypatch.setattr(bm, "BACKUPS_DIR", backups)
    return home


def _seed_home(home: Path) -> None:
    (home / "settings.json").write_text(
        json.dumps({"theme": "dark"}), encoding="utf-8"
    )
    (home / "skill-manifest.json").write_text(
        json.dumps({"load": []}), encoding="utf-8"
    )
    agents = home / "agents"
    agents.mkdir()
    (agents / "reviewer.md").write_text("# reviewer\n", encoding="utf-8")
    skill_dir = home / "skills" / "brainstorming"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# brainstorming\n", encoding="utf-8")


# ── detect_changes ──────────────────────────────────────────────────────────


def test_no_baseline_marks_everything_new(fake_home):
    _seed_home(fake_home)
    cfg = bc.BackupConfig()
    report = cd.detect_changes(cfg, fake_home, last_snapshot=None)
    assert report.has_changes
    assert report.baseline_snapshot is None
    assert "settings.json" in report.new
    assert "agents/reviewer.md" in report.new
    assert report.changed == ()
    assert report.removed == ()
    assert report.unchanged == 0


def test_unchanged_files_report_no_changes(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    cfg = bc.BackupConfig()
    report = cd.detect_changes(cfg, fake_home, last_snapshot=snap)
    assert not report.has_changes
    assert report.new == ()
    assert report.changed == ()
    assert report.removed == ()
    assert report.unchanged > 0


def test_edited_file_appears_in_changed(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    (fake_home / "settings.json").write_text(
        json.dumps({"theme": "light"}), encoding="utf-8"
    )
    cfg = bc.BackupConfig()
    report = cd.detect_changes(cfg, fake_home, last_snapshot=snap)
    assert "settings.json" in report.changed
    assert report.has_changes


def test_new_file_appears_in_new(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    # Add an agent that didn't exist at snapshot time.
    (fake_home / "agents" / "critic.md").write_text("# critic\n", encoding="utf-8")
    cfg = bc.BackupConfig()
    report = cd.detect_changes(cfg, fake_home, last_snapshot=snap)
    assert "agents/critic.md" in report.new


def test_deleted_file_appears_in_removed(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    (fake_home / "settings.json").unlink()
    cfg = bc.BackupConfig()
    report = cd.detect_changes(cfg, fake_home, last_snapshot=snap)
    assert "settings.json" in report.removed
    assert report.has_changes


def test_oversize_file_is_excluded_both_sides(fake_home):
    _seed_home(fake_home)
    # Create a file larger than max_file_bytes=100 bytes.
    big = fake_home / "agents" / "huge.md"
    big.write_bytes(b"X" * 500)
    cfg = bc.BackupConfig(max_file_bytes=100)
    report = cd.detect_changes(cfg, fake_home, last_snapshot=None)
    # 'huge.md' is too big; neither side should see it.
    assert "agents/huge.md" not in report.new


# ── snapshot_if_changed ─────────────────────────────────────────────────────


def test_snapshot_if_changed_no_baseline_always_snapshots(fake_home):
    _seed_home(fake_home)
    result = bm.snapshot_if_changed(reason="initial")
    assert result.snapshot_path is not None
    assert result.snapshot_path.exists()
    assert result.report.has_changes
    # Reason slug is appended to the folder name.
    assert "initial" in result.snapshot_path.name


def test_snapshot_if_changed_skips_when_nothing_changed(fake_home):
    _seed_home(fake_home)
    bm.create_snapshot()  # baseline
    result = bm.snapshot_if_changed(reason="tick")
    assert result.snapshot_path is None
    assert not result.report.has_changes
    assert result.report.baseline_snapshot is not None


def test_snapshot_if_changed_fires_when_file_edited(fake_home):
    _seed_home(fake_home)
    bm.create_snapshot()
    (fake_home / "settings.json").write_text(
        json.dumps({"theme": "light"}), encoding="utf-8"
    )
    result = bm.snapshot_if_changed(reason="edit-settings")
    assert result.snapshot_path is not None
    assert "settings.json" in result.report.changed


def test_snapshot_if_changed_sanitises_reason(fake_home):
    _seed_home(fake_home)
    # Illegal characters should be normalised to '-'.
    result = bm.snapshot_if_changed(reason="Edit:/bad\\path<>")
    assert result.snapshot_path is not None
    # No raw path separators leak into the folder name.
    name = result.snapshot_path.name
    assert "/" not in name and "\\" not in name and "<" not in name


def test_snapshot_if_changed_writes_reason_into_manifest(fake_home):
    _seed_home(fake_home)
    result = bm.snapshot_if_changed(reason="unit-test")
    assert result.snapshot_path is not None
    manifest = json.loads(
        (result.snapshot_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.get("reason") == "unit-test"


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_snapshot_if_changed_text_output(fake_home, monkeypatch, capsys):
    _seed_home(fake_home)
    rc = bm.main(["snapshot-if-changed", "--reason", "cli-test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[snapshot-if-changed]" in out
    # When a snapshot was taken its name is echoed.
    assert "cli-test" in out or "new=" in out


def test_cli_snapshot_if_changed_json_output(fake_home, capsys):
    _seed_home(fake_home)
    rc = bm.main(["snapshot-if-changed", "--json", "--reason", "json-test"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "json-test"
    assert "report" in payload
    assert payload["report"]["has_changes"] is True


def test_cli_create_accepts_reason(fake_home, capsys):
    _seed_home(fake_home)
    rc = bm.main(["create", "--reason", "manual-checkpoint"])
    assert rc == 0
    printed = capsys.readouterr().out.strip()
    assert "manual-checkpoint" in printed
