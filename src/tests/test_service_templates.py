"""Validate the shipped user-service templates without installing a service."""

from __future__ import annotations

import configparser
from pathlib import Path
import plistlib
import shlex


ROOT = Path(__file__).resolve().parents[2]


def test_systemd_watchdog_is_user_scoped_and_has_valid_restart_limits() -> None:
    path = ROOT / "docs/services/systemd/claude-backup-watchdog.service"
    text = path.read_text(encoding="utf-8")
    unit = configparser.ConfigParser(interpolation=None)
    unit.read_string(text)
    assert shlex.split(unit["Service"]["ExecStart"]) == [
        "/usr/bin/python3",
        "${CTX_REPO}/src/backup_mirror.py",
        "watchdog",
        "--interval",
        "60",
    ]
    assert unit["Service"]["Environment"] == "CTX_REPO=%h/ctx"
    assert unit["Service"]["NoNewPrivileges"] == "yes"
    assert unit["Service"]["ReadWritePaths"] == "%h/.claude/backups"
    assert "User" not in unit["Service"]
    assert unit["Install"]["WantedBy"] == "default.target"
    assert "systemctl --user" in text and "sudo " not in text
    # Rate limits belong to systemd.unit, not systemd.service.
    assert unit["Unit"].get("StartLimitIntervalSec") == "60"
    assert unit["Unit"].get("StartLimitBurst") == "3"
    assert "StartLimitIntervalSec" not in unit["Service"]
    assert "StartLimitBurst" not in unit["Service"]


def test_launchd_watchdog_is_a_user_agent_with_explicit_arguments() -> None:
    path = ROOT / "docs/services/macos/com.claude.backup.watchdog.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == "com.claude.backup.watchdog"
    assert data["ProgramArguments"] == [
        "/usr/bin/python3",
        "/Users/YOUR_USER/ctx/src/backup_mirror.py",
        "watchdog",
        "--interval",
        "60",
    ]
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["ThrottleInterval"] == 30
    assert data["StandardOutPath"] == data["StandardErrorPath"]
    assert "UserName" not in data
    text = path.read_text(encoding="utf-8")
    assert "~/Library/LaunchAgents/" in text and "sudo " not in text
