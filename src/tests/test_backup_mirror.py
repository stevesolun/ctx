"""
Tests for backup_mirror: create, list, verify, restore, prune, and CLI.

Uses monkeypatch to redirect CLAUDE_HOME + BACKUPS_DIR at a ``tmp_path`` so
nothing in the real ~/.claude is touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import backup_mirror as bm  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point bm.CLAUDE_HOME + BACKUPS_DIR at a tmp_path layout."""
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
        json.dumps({"load": [{"skill": "alpha"}]}), encoding="utf-8"
    )
    (home / "pending-skills.json").write_text(
        json.dumps({"graph_suggestions": []}), encoding="utf-8"
    )

    agents = home / "agents"
    agents.mkdir()
    (agents / "reviewer.md").write_text("# reviewer\n", encoding="utf-8")

    skill_dir = home / "skills" / "brainstorming"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# brainstorming\n", encoding="utf-8")

    mem = home / "projects" / "demo-slug" / "memory"
    mem.mkdir(parents=True)
    (mem / "user_role.md").write_text("role: dev\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text("- [role](user_role.md)\n", encoding="utf-8")


# ── create_snapshot ─────────────────────────────────────────────────────────


def test_create_captures_all_expected_files(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    assert snap.exists()
    manifest = json.loads((snap / "manifest.json").read_text())
    dests = {e["dest"] for e in manifest["entries"]}
    assert "settings.json" in dests
    assert "skill-manifest.json" in dests
    assert "pending-skills.json" in dests
    assert "agents/reviewer.md" in dests
    assert "skills/brainstorming/SKILL.md" in dests
    assert "memory/demo-slug/user_role.md" in dests
    assert "memory/demo-slug/MEMORY.md" in dests


def test_create_hashes_every_non_skipped_file(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    manifest = json.loads((snap / "manifest.json").read_text())
    for e in manifest["entries"]:
        if e["skipped"] is None:
            assert e["sha256"] is not None
            assert len(e["sha256"]) == 64  # hex sha256
            assert e["size"] > 0


def test_create_skips_files_over_cap(fake_home, monkeypatch):
    _seed_home(fake_home)
    # Force the cap down and make one file exceed it.
    monkeypatch.setattr(bm, "MAX_FILE_BYTES", 5)
    (fake_home / "settings.json").write_text("x" * 20, encoding="utf-8")
    snap = bm.create_snapshot()
    manifest = json.loads((snap / "manifest.json").read_text())
    entries = {e["dest"]: e for e in manifest["entries"]}
    assert entries["settings.json"]["skipped"] == "too_large"
    assert entries["settings.json"]["sha256"] is None


def test_create_handles_missing_directories(fake_home):
    # No seeded files -- should still produce a snapshot with empty entries.
    snap = bm.create_snapshot()
    manifest = json.loads((snap / "manifest.json").read_text())
    assert manifest["entries"] == []


def test_snapshot_ids_sort_chronologically(fake_home):
    _seed_home(fake_home)
    a = bm.create_snapshot(now=1700000000.0)
    b = bm.create_snapshot(now=1700000000.0 + 3600)
    assert a.name < b.name


# ── list_snapshots ──────────────────────────────────────────────────────────


def test_list_returns_newest_first(fake_home):
    _seed_home(fake_home)
    a = bm.create_snapshot(now=1700000000.0)
    b = bm.create_snapshot(now=1700000000.0 + 3600)
    infos = bm.list_snapshots()
    assert [i.snapshot_id for i in infos] == [b.name, a.name]


def test_list_empty_when_no_snapshots(fake_home):
    assert bm.list_snapshots() == []


def test_list_skips_corrupt_manifest(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    (snap / "manifest.json").write_text("not json", encoding="utf-8")
    assert bm.list_snapshots() == []


# ── verify_snapshot ─────────────────────────────────────────────────────────


def test_verify_clean_snapshot_is_ok(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    report = bm.verify_snapshot(snap)
    assert report.ok
    assert report.checked > 0
    assert report.missing == ()
    assert report.hash_mismatch == ()


def test_verify_detects_missing_file(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    (snap / "settings.json").unlink()
    report = bm.verify_snapshot(snap)
    assert not report.ok
    assert "settings.json" in report.missing


def test_verify_detects_hash_mismatch(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    (snap / "settings.json").write_text("tampered", encoding="utf-8")
    report = bm.verify_snapshot(snap)
    assert not report.ok
    assert "settings.json" in report.hash_mismatch


# ── restore_snapshot ────────────────────────────────────────────────────────


def test_restore_overwrites_modified_files(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    # Corrupt a live file.
    (fake_home / "settings.json").write_text("corrupted", encoding="utf-8")
    bm.restore_snapshot(snap, claude_home=fake_home)
    assert json.loads((fake_home / "settings.json").read_text()) == {"theme": "dark"}


def test_restore_dry_run_does_not_change_files(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    (fake_home / "settings.json").write_text("corrupted", encoding="utf-8")
    bm.restore_snapshot(snap, claude_home=fake_home, dry_run=True)
    assert (fake_home / "settings.json").read_text() == "corrupted"


def test_restore_preserves_memory_subdirs(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    # Delete memory file on live side.
    (fake_home / "projects" / "demo-slug" / "memory" / "user_role.md").unlink()
    bm.restore_snapshot(snap, claude_home=fake_home)
    live = fake_home / "projects" / "demo-slug" / "memory" / "user_role.md"
    assert live.exists()
    assert live.read_text() == "role: dev\n"


def test_restore_refuses_tampered_snapshot(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    (snap / "settings.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError, match="failed verification"):
        bm.restore_snapshot(snap, claude_home=fake_home)


def test_restore_target_resolution_rejects_unknown_layout():
    with pytest.raises(ValueError):
        bm._resolve_restore_target("random/garbage/path.txt", Path("/home"))


# ── prune_snapshots ─────────────────────────────────────────────────────────


def test_prune_keeps_only_newest(fake_home):
    _seed_home(fake_home)
    bm.create_snapshot(now=1.0)
    bm.create_snapshot(now=2.0)
    newest = bm.create_snapshot(now=3.0)
    removed = bm.prune_snapshots(keep=1)
    assert len(removed) == 2
    remaining = bm.list_snapshots()
    assert len(remaining) == 1
    assert remaining[0].snapshot_id == newest.name


def test_prune_with_keep_zero_removes_all(fake_home):
    _seed_home(fake_home)
    bm.create_snapshot(now=1.0)
    bm.create_snapshot(now=2.0)
    removed = bm.prune_snapshots(keep=0)
    assert len(removed) == 2
    assert bm.list_snapshots() == []


def test_prune_negative_raises(fake_home):
    with pytest.raises(ValueError):
        bm.prune_snapshots(keep=-1)


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_create_prints_snapshot_path(fake_home, capsys):
    _seed_home(fake_home)
    rc = bm.main(["create"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert Path(out).exists()
    assert (Path(out) / "manifest.json").is_file()


def test_cli_list_json_returns_list(fake_home, capsys):
    _seed_home(fake_home)
    bm.create_snapshot()
    rc = bm.main(["list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert "snapshot_id" in payload[0]


def test_cli_verify_clean_exits_zero(fake_home):
    _seed_home(fake_home)
    bm.create_snapshot()
    rc = bm.main(["verify"])
    assert rc == 0


def test_cli_verify_tampered_exits_two(fake_home):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    (snap / "settings.json").write_text("tampered", encoding="utf-8")
    rc = bm.main(["verify"])
    assert rc == 2


def test_cli_restore_dry_run(fake_home, capsys):
    _seed_home(fake_home)
    bm.create_snapshot()
    (fake_home / "settings.json").write_text("corrupted", encoding="utf-8")
    rc = bm.main(["restore", "--dry-run"])
    assert rc == 0
    # Not actually restored.
    assert (fake_home / "settings.json").read_text() == "corrupted"


def test_cli_restore_nonexistent_snapshot(fake_home, capsys):
    rc = bm.main(["restore", "--snapshot", "does-not-exist"])
    assert rc == 1


def test_cli_restore_refuses_corrupted_snapshot(fake_home, capsys):
    _seed_home(fake_home)
    snap = bm.create_snapshot()
    (snap / "settings.json").write_text("tampered", encoding="utf-8")
    rc = bm.main(["restore"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "verify" in err.lower()


def test_cli_prune_keeps_newest(fake_home):
    _seed_home(fake_home)
    bm.create_snapshot(now=1.0)
    bm.create_snapshot(now=2.0)
    rc = bm.main(["prune", "--keep", "1"])
    assert rc == 0
    assert len(bm.list_snapshots()) == 1
