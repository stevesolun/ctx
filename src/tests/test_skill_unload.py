"""
test_skill_unload.py -- Regression tests for skill_unload hardening.

Covers:
- Path-traversal (CWE-22): find_entity_page / set_frontmatter_field must reject
  user-controlled names with separators, traversal sequences, or glob metachars.
- ReDoS / regex injection: set_frontmatter_field must escape caller-controlled
  field names before interpolating into a regex.
- YAML injection: multiline values must be collapsed so they cannot inject
  additional YAML keys.
- Atomic writes: updates should survive a crash mid-write (no truncation).
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".claude" / "skill-wiki" / "entities" / "skills").mkdir(parents=True)
    (home / ".claude" / "skill-wiki" / "entities" / "agents").mkdir(parents=True)
    page = home / ".claude" / "skill-wiki" / "entities" / "skills" / "real-skill.md"
    page.write_text(
        "---\nname: real-skill\nstatus: installed\n---\n\n# real-skill\n",
        encoding="utf-8",
    )
    # Sensitive file that path traversal might try to reach
    victim = home / "victim.md"
    victim.write_text("victim content — must not be overwritten", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    from ctx.adapters.claude_code.install import skill_unload
    importlib.reload(skill_unload)
    monkeypatch.setattr(skill_unload, "CLAUDE_DIR", home / ".claude")
    monkeypatch.setattr(
        skill_unload,
        "MANIFEST_PATH",
        home / ".claude" / "skill-manifest.json",
    )
    monkeypatch.setattr(skill_unload, "PENDING_UNLOAD", home / ".claude" / "pending-unload.json")
    monkeypatch.setattr(skill_unload, "WIKI_DIR", home / ".claude" / "skill-wiki")
    monkeypatch.setattr(
        skill_unload,
        "SKILL_ENTITIES",
        home / ".claude" / "skill-wiki" / "entities" / "skills",
    )
    monkeypatch.setattr(
        skill_unload,
        "AGENT_ENTITIES",
        home / ".claude" / "skill-wiki" / "entities" / "agents",
    )
    return skill_unload, home


@pytest.mark.parametrize(
    "evil_name",
    [
        "../../../etc/passwd",
        "..\\..\\victim",
        "/absolute/path",
        "skill/with/slashes",
        "name*with*glob",
        "name with spaces",
        "name\x00null",
    ],
)
def test_find_entity_page_rejects_traversal(fake_home, evil_name):
    unload, _ = fake_home
    assert unload.find_entity_page(evil_name) is None


def test_find_entity_page_accepts_valid(fake_home):
    unload, home = fake_home
    result = unload.find_entity_page("real-skill")
    assert result is not None
    assert result == home / ".claude" / "skill-wiki" / "entities" / "skills" / "real-skill.md"


def test_set_frontmatter_field_escapes_regex_metacharacters(fake_home):
    unload, home = fake_home
    page = home / ".claude" / "skill-wiki" / "entities" / "skills" / "real-skill.md"
    # A field name with regex metacharacters must not blow up — re.escape
    # converts `.+` into a literal string so it is simply appended as a new key.
    unload.set_frontmatter_field(page, "bad.+field", "ok")
    text = page.read_text(encoding="utf-8")
    assert "bad.+field: ok" in text
    # Pre-existing "status" field is unchanged.
    assert "status: installed" in text


def test_set_frontmatter_field_sanitizes_newlines(fake_home):
    unload, home = fake_home
    page = home / ".claude" / "skill-wiki" / "entities" / "skills" / "real-skill.md"
    # A value with embedded newline would inject a rogue YAML key — sanitizer
    # must collapse it onto one line so no new key is created.
    unload.set_frontmatter_field(page, "status", "stale\nmalicious: true")
    text = page.read_text(encoding="utf-8")
    # Verify the injected content stays on the status line (single YAML key),
    # NOT as a standalone "malicious: true" key on its own line.
    lines = text.splitlines()
    standalone_malicious = [ln for ln in lines if ln.strip() == "malicious: true"]
    assert standalone_malicious == [], f"found rogue standalone YAML key: {standalone_malicious}"


def test_atomic_write_preserves_original_on_caller_crash(fake_home, monkeypatch):
    """If the inner write raises, the original file must still be present and intact."""
    unload, home = fake_home
    page = home / ".claude" / "skill-wiki" / "entities" / "skills" / "real-skill.md"
    original = page.read_text(encoding="utf-8")

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        unload.set_frontmatter_field(page, "status", "stale")

    # Original must survive.
    assert page.read_text(encoding="utf-8") == original


def test_unload_from_session_writes_manifest_event_and_audit(
    fake_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unload, home = fake_home
    audit_calls: list[tuple[str, str, dict[str, object]]] = []
    fake_audit = types.SimpleNamespace(
        log_skill_event=lambda event, slug, **kwargs: audit_calls.append(
            (event, slug, kwargs)
        )
    )
    monkeypatch.setitem(sys.modules, "ctx_audit_log", fake_audit)
    monkeypatch.setenv("CTX_SESSION_ID", "session-123")
    manifest_path = home / ".claude" / "skill-manifest.json"
    manifest_path.write_text(
        json.dumps({
            "load": [{
                "skill": "real-skill",
                "entity_type": "skill",
                "source": "test",
            }, {
                "skill": "kept-skill",
                "entity_type": "skill",
                "source": "test",
            }],
            "unload": [],
            "warnings": [],
        }),
        encoding="utf-8",
    )

    removed = unload.unload_from_session(["real-skill"], entity_type="skill")

    assert removed == ["real-skill"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["load"] == [{
        "skill": "kept-skill",
        "entity_type": "skill",
        "source": "test",
    }]
    assert manifest["unload"] == [{
        "skill": "real-skill",
        "entity_type": "skill",
        "source": "test",
    }]
    events = [
        json.loads(line)
        for line in (home / ".claude" / "skill-events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(events) == 1
    assert events[0]["event"] == "unload"
    assert events[0]["skill"] == "real-skill"
    assert events[0]["session_id"] == "session-123"
    assert events[0]["meta"] == {"source": "skill_unload"}
    assert audit_calls == [(
        "skill.unloaded",
        "real-skill",
        {
            "actor": "cli",
            "session_id": "session-123",
            "meta": {"via": "skill_unload"},
        },
    )]


def test_permanent_suppression_updates_graph_node(fake_home):
    unload, home = fake_home
    graph_dir = home / ".claude" / "skill-wiki" / "graphify-out"
    graph_dir.mkdir(parents=True)
    graph_path = graph_dir / "graph.json"
    graph_path.write_text(
        json.dumps({
            "directed": False,
            "multigraph": False,
            "graph": {},
            "nodes": [
                {"id": "skill:real-skill", "label": "real-skill", "type": "skill"},
            ],
            "edges": [],
        }),
        encoding="utf-8",
    )

    assert unload.set_never_load(["real-skill"]) == ["real-skill"]
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    assert payload["nodes"][0]["never_load"] is True

    assert unload.restore_load(["real-skill"]) == ["real-skill"]
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    assert payload["nodes"][0]["never_load"] is False
