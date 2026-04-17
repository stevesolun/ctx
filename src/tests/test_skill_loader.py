"""
test_skill_loader.py -- Regression tests for path-traversal hardening in skill_loader.

Covers Strix vuln-0001 (CWE-22): find_skill() must reject user-controlled names that
contain path separators, traversal sequences, glob metacharacters, or absolute paths,
and must confine resolved paths to SKILLS_DIR / AGENTS_DIR.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reload skill_loader with a throwaway HOME so module-level paths re-resolve."""
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "goodskill").mkdir(parents=True)
    (home / ".claude" / "skills" / "goodskill" / "SKILL.md").write_text("# good", encoding="utf-8")
    (home / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "outside-skill").mkdir(parents=True)
    (home / ".claude" / "outside-skill" / "SKILL.md").write_text("# outside skill", encoding="utf-8")
    (home / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "agents" / "goodagent.md").write_text("# good agent", encoding="utf-8")
    (home / ".claude" / "outside-agent.md").write_text("# outside agent", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows

    import skill_loader
    importlib.reload(skill_loader)
    return skill_loader, home


def test_valid_skill_name_resolves(fake_home):
    loader, _ = fake_home
    result = loader.find_skill("goodskill")
    assert result is not None
    assert result["type"] == "skill"
    assert result["name"] == "goodskill"


def test_valid_agent_name_resolves(fake_home):
    loader, _ = fake_home
    result = loader.find_skill("goodagent")
    assert result is not None
    assert result["type"] == "agent"


@pytest.mark.parametrize(
    "bad_name",
    [
        "../outside-skill",
        "../outside-agent",
        "..",
        "../..",
        "../../etc/passwd",
        "foo/../bar",
        "/absolute/path",
        "C:/Windows/System32",
        "**/outside-agent",
        "*",
        "**",
        "?*",
        "name\x00.md",
        "name with space",
        "name\nwith\nnewline",
        "",
        "name/",
        "name\\windows",
    ],
)
def test_traversal_and_metachars_rejected(fake_home, bad_name):
    """Every traversal, glob metacharacter, separator, or absolute path must return None."""
    loader, _ = fake_home
    assert loader.find_skill(bad_name) is None, f"expected None for {bad_name!r}"


def test_rglob_pattern_cannot_escape_agents_dir(fake_home):
    """Strix's original rglob PoC: AGENTS_DIR.rglob('../outside-agent.md') used to match."""
    loader, _ = fake_home
    assert loader.find_skill("../outside-agent") is None


def test_validate_skill_name_accepts_common_names(fake_home):
    loader, _ = fake_home
    from wiki_utils import validate_skill_name
    for name in ("fastapi-pro", "docker_expert", "py3.11", "a", "Aa0._-"):
        assert validate_skill_name(name) == name


def test_validate_skill_name_rejects_bad(fake_home):
    from wiki_utils import validate_skill_name
    for bad in ("../x", "x/y", "*", "", "_leading", ".leading", "-leading"):
        with pytest.raises(ValueError):
            validate_skill_name(bad)
