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


# ─────────────────────────────────────────────────────────────────────
# update_manifest: (slug, entity_type) tuple dedup contract
# ─────────────────────────────────────────────────────────────────────
#
# Code-reviewer HIGH (P2.2). Prior impl deduped on slug alone and
# wrote entries without an ``entity_type`` field. A same-slug
# skill + agent collision silently dropped one of them.

import json

class TestUpdateManifestEntityType:

    def test_writes_entity_type_field(self, fake_home):
        loader, home = fake_home
        manifest_path = home / ".claude" / "skill-manifest.json"
        loader.update_manifest("goodskill", entity_type="skill")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = [
            e for e in data["load"]
            if e.get("skill") == "goodskill"
        ]
        assert len(entries) == 1
        assert entries[0].get("entity_type") == "skill"

    def test_same_slug_skill_and_agent_coexist(self, fake_home):
        """The regression: before the fix, adding an agent with the same
        slug as an already-loaded skill was silently a no-op because
        the slug-only dedup thought the agent was already loaded."""
        loader, home = fake_home
        manifest_path = home / ".claude" / "skill-manifest.json"
        loader.update_manifest("code-reviewer", entity_type="skill")
        loader.update_manifest("code-reviewer", entity_type="agent")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        pairs = {
            (e.get("skill"), e.get("entity_type"))
            for e in data["load"]
        }
        assert ("code-reviewer", "skill") in pairs
        assert ("code-reviewer", "agent") in pairs

    def test_idempotent_same_type(self, fake_home):
        """Calling update_manifest twice with the same (slug, type)
        must not append a duplicate entry."""
        loader, home = fake_home
        manifest_path = home / ".claude" / "skill-manifest.json"
        loader.update_manifest("goodskill", entity_type="skill")
        loader.update_manifest("goodskill", entity_type="skill")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = [e for e in data["load"] if e.get("skill") == "goodskill"]
        assert len(entries) == 1

    def test_default_entity_type_is_skill(self, fake_home):
        """Backward compat: call sites that don't pass entity_type
        default to ``skill`` — the pre-fix implicit contract."""
        loader, home = fake_home
        manifest_path = home / ".claude" / "skill-manifest.json"
        loader.update_manifest("legacy-caller")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = next(e for e in data["load"] if e.get("skill") == "legacy-caller")
        assert entry.get("entity_type") == "skill"

    def test_legacy_pre_fix_manifest_entry_is_not_duplicated(self, fake_home):
        """If the manifest already has a pre-fix entry (no ``entity_type``
        key, slug == ``foo``), a new ``update_manifest("foo", "skill")``
        call must recognise it as the same pair — the missing
        ``entity_type`` in the old entry implicitly meant ``skill``."""
        loader, home = fake_home
        manifest_path = home / ".claude" / "skill-manifest.json"
        manifest_path.write_text(json.dumps({
            "load": [{"skill": "foo", "source": "legacy"}],
            "unload": [],
            "warnings": [],
        }), encoding="utf-8")
        loader.update_manifest("foo", entity_type="skill")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        foo_entries = [e for e in data["load"] if e.get("skill") == "foo"]
        assert len(foo_entries) == 1, (
            "legacy entry got duplicated — missing entity_type should "
            "default to 'skill' for dedup purposes"
        )
