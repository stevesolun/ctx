"""
Tests for backup_config: schema validation, defaults, loader, overrides.

Covers:
  - default BackupConfig is valid and uses documented values
  - load_backup_config merges user values over defaults
  - ALWAYS_EXCLUDE names are silently dropped from top_files
  - invalid scope / negative retention / bad name_format raise ValueError
  - from_ctx_config survives a missing / malformed user override file
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


# ── Defaults ────────────────────────────────────────────────────────────────


def test_default_config_is_valid():
    cfg = bc.BackupConfig()
    assert cfg.scope == "full"
    assert cfg.timestamp_format == "%Y%m%dT%H%M%SZ"
    assert cfg.max_file_bytes == 5 * 1024 * 1024
    assert cfg.memory_glob is True
    assert cfg.retention.keep_latest == 50
    assert cfg.retention.keep_daily == 14


def test_default_top_files_include_previously_missing_entries():
    # audit_backup.py flagged these as missing from earlier snapshots.
    # Adding them to defaults is the behavioural change Phase 1 delivers.
    cfg = bc.BackupConfig()
    assert "CLAUDE.md" in cfg.top_files
    assert "AGENTS.md" in cfg.top_files
    assert "skill-system-config.json" in cfg.top_files
    # And the originals are still there.
    assert "settings.json" in cfg.top_files
    assert "skill-manifest.json" in cfg.top_files
    assert "pending-skills.json" in cfg.top_files


def test_default_trees_mirror_current_mirror_behaviour():
    cfg = bc.BackupConfig()
    srcs = {t.src for t in cfg.trees}
    assert srcs == {"agents", "skills"}


def test_snapshot_dir_resolved_expands_tilde():
    cfg = bc.BackupConfig(snapshot_dir="~/.claude/backups")
    resolved = cfg.snapshot_dir_resolved()
    assert "~" not in str(resolved)
    assert resolved.is_absolute()


# ── Validation ──────────────────────────────────────────────────────────────


def test_invalid_scope_rejected():
    with pytest.raises(ValueError, match="scope must be one of"):
        bc.BackupConfig(scope="bogus")


def test_negative_max_file_bytes_rejected():
    with pytest.raises(ValueError, match="max_file_bytes must be >= 0"):
        bc.BackupConfig(max_file_bytes=-1)


def test_negative_retention_keep_latest_rejected():
    with pytest.raises(ValueError, match="keep_latest must be >= 0"):
        bc.BackupConfig(retention=bc.BackupRetention(keep_latest=-5))


def test_negative_retention_keep_daily_rejected():
    with pytest.raises(ValueError, match="keep_daily must be >= 0"):
        bc.BackupConfig(retention=bc.BackupRetention(keep_daily=-1))


def test_name_format_without_timestamp_placeholder_rejected():
    # Without ``{timestamp}`` snapshot names would collide.
    with pytest.raises(ValueError, match="must contain"):
        bc.BackupConfig(name_format="backup_{reason}")


# ── ALWAYS_EXCLUDE safety net ───────────────────────────────────────────────


def test_credentials_dropped_from_top_files_even_if_listed():
    # A user could copy-paste the ~/.claude directory listing and
    # accidentally include .credentials.json. The config silently drops
    # it — this is the hard-exclude guarantee.
    cfg = bc.BackupConfig(top_files=(
        "settings.json",
        ".credentials.json",
        "mcp-needs-auth-cache.json",
        "CLAUDE.md",
    ))
    assert ".credentials.json" not in cfg.top_files
    assert "mcp-needs-auth-cache.json" not in cfg.top_files
    assert "settings.json" in cfg.top_files
    assert "CLAUDE.md" in cfg.top_files


def test_is_excluded_covers_always_exclude_and_user_excludes():
    cfg = bc.BackupConfig(excludes=("secrets/prod.env",))
    assert cfg.is_excluded(".credentials.json") is True
    assert cfg.is_excluded("claude.json") is True
    assert cfg.is_excluded("secrets/prod.env") is True
    assert cfg.is_excluded("settings.json") is False


# ── load_backup_config ──────────────────────────────────────────────────────


def test_load_returns_defaults_when_no_raw():
    cfg = bc.load_backup_config(None)
    assert cfg == bc.BackupConfig()
    cfg2 = bc.load_backup_config({})
    assert cfg2 == bc.BackupConfig()


def test_load_overrides_scalar_fields():
    cfg = bc.load_backup_config({
        "scope": "incremental",
        "max_file_bytes": 1024,
        "memory_glob": False,
    })
    assert cfg.scope == "incremental"
    assert cfg.max_file_bytes == 1024
    assert cfg.memory_glob is False


def test_load_overrides_top_files_and_trees():
    cfg = bc.load_backup_config({
        "top_files": ["only-this.json"],
        "trees": [{"src": "my-skills", "dest": "skills"}],
    })
    assert cfg.top_files == ("only-this.json",)
    assert cfg.trees == (bc.BackupTree(src="my-skills", dest="skills"),)


def test_load_tree_defaults_dest_to_src_when_missing():
    cfg = bc.load_backup_config({
        "trees": [{"src": "agents"}],
    })
    assert cfg.trees == (bc.BackupTree(src="agents", dest="agents"),)


def test_load_ignores_malformed_trees():
    cfg = bc.load_backup_config({
        "trees": [{"src": ""}, "not-a-dict", {"src": "skills"}],
    })
    assert cfg.trees == (bc.BackupTree(src="skills", dest="skills"),)


def test_load_retention_partial_override():
    cfg = bc.load_backup_config({"retention": {"keep_latest": 100}})
    assert cfg.retention.keep_latest == 100
    assert cfg.retention.keep_daily == 14  # default


# ── from_ctx_config ─────────────────────────────────────────────────────────


def test_from_ctx_config_survives_missing_user_override(monkeypatch, tmp_path):
    # Point HOME at tmp_path so ~/.claude/backup-config.json is absent.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    cfg = bc.from_ctx_config()
    # Baseline: we at least get the defaults back plus whatever lives
    # in the repo config.json — never a crash.
    assert cfg.scope in bc._ALLOWED_SCOPES


def test_from_ctx_config_survives_malformed_user_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "backup-config.json").write_text(
        "{ not valid json", encoding="utf-8"
    )
    cfg = bc.from_ctx_config()
    # Malformed JSON is swallowed rather than raised so a broken user
    # file never blocks a backup from happening.
    assert isinstance(cfg, bc.BackupConfig)


def test_from_ctx_config_applies_user_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "backup-config.json").write_text(
        json.dumps({
            "scope": "hybrid",
            "retention": {"keep_latest": 77},
        }),
        encoding="utf-8",
    )
    cfg = bc.from_ctx_config()
    assert cfg.scope == "hybrid"
    assert cfg.retention.keep_latest == 77
