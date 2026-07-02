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
import os
import sqlite3
import stat
import sys
import types
from pathlib import Path

import networkx as nx
import pytest

from ctx.core.graph.graph_packs import load_merged_pack_graph, write_base_pack
from ctx.core.graph.graph_store import graph_store_stats
from ctx.core.wiki import wiki_queue, wiki_queue_worker
from ctx.core.wiki.wiki_packs import load_merged_wiki_pages, write_wiki_base_pack


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
        log_skill_event=lambda event, slug, **kwargs: audit_calls.append((event, slug, kwargs))
    )
    monkeypatch.setitem(sys.modules, "ctx_audit_log", fake_audit)
    monkeypatch.setenv("CTX_SESSION_ID", "session-123")
    manifest_path = home / ".claude" / "skill-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "load": [
                    {
                        "skill": "real-skill",
                        "entity_type": "skill",
                        "source": "test",
                    },
                    {
                        "skill": "kept-skill",
                        "entity_type": "skill",
                        "source": "test",
                    },
                ],
                "unload": [],
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )

    removed = unload.unload_from_session(["real-skill"], entity_type="skill")

    assert removed == ["real-skill"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["load"] == [
        {
            "skill": "kept-skill",
            "entity_type": "skill",
            "source": "test",
        }
    ]
    assert manifest["unload"] == [
        {
            "skill": "real-skill",
            "entity_type": "skill",
            "source": "test",
        }
    ]
    events = [
        json.loads(line)
        for line in (home / ".claude" / "skill-events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(events) == 1
    assert events[0]["event"] == "unload"
    assert events[0]["schema_version"] == "ctx.skill_telemetry.v1"
    assert events[0]["skill"] == "real-skill"
    assert events[0]["entity_type"] == "skill"
    assert events[0]["session_id"] == "session-123"
    assert events[0]["skill_hash"].startswith("sha256:")
    assert events[0]["session_hash"].startswith("sha256:")
    assert events[0]["meta"] == {"source": "skill_unload"}
    if os.name != "nt":
        assert stat.S_IMODE((home / ".claude" / "skill-events.jsonl").stat().st_mode) == 0o600
    assert audit_calls == [
        (
            "skill.unloaded",
            "real-skill",
            {
                "actor": "cli",
                "session_id": "session-123",
                "meta": {"via": "skill_unload"},
            },
        )
    ]


def test_agent_unload_entrypoint_only_removes_agent_when_slug_matches_skill(
    fake_home,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unload, home = fake_home
    agent_page = home / ".claude" / "skill-wiki" / "entities" / "agents" / "real-skill.md"
    agent_page.write_text(
        "---\nname: real-skill\nstatus: installed\n---\n\n# agent real-skill\n",
        encoding="utf-8",
    )
    audit_calls: list[tuple[str, str, dict[str, object]]] = []
    fake_audit = types.SimpleNamespace(
        log_skill_event=lambda event, slug, **kwargs: audit_calls.append((event, slug, kwargs))
    )
    monkeypatch.setitem(sys.modules, "ctx_audit_log", fake_audit)
    manifest_path = home / ".claude" / "skill-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "load": [
                    {
                        "skill": "real-skill",
                        "entity_type": "skill",
                        "source": "test",
                    },
                    {
                        "skill": "real-skill",
                        "entity_type": "agent",
                        "source": "test",
                    },
                ],
                "unload": [],
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )

    unload.main(["--name", "real-skill"], default_entity_type="agent")

    assert "Unloaded from session: real-skill" in capsys.readouterr().out
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["load"] == [
        {
            "skill": "real-skill",
            "entity_type": "skill",
            "source": "test",
        }
    ]
    assert manifest["unload"] == [
        {
            "skill": "real-skill",
            "entity_type": "agent",
            "source": "test",
        }
    ]
    assert audit_calls[0][0] == "agent.unloaded"


def test_permanent_suppression_updates_graph_node(fake_home):
    unload, home = fake_home
    graph_dir = home / ".claude" / "skill-wiki" / "graphify-out"
    graph_dir.mkdir(parents=True)
    graph_path = graph_dir / "graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "directed": False,
                "multigraph": False,
                "graph": {},
                "nodes": [
                    {"id": "skill:real-skill", "label": "real-skill", "type": "skill"},
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )

    assert unload.set_never_load(["real-skill"]) == ["real-skill"]
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    assert payload["nodes"][0]["never_load"] is True

    assert unload.restore_load(["real-skill"]) == ["real-skill"]
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    assert payload["nodes"][0]["never_load"] is False


def test_permanent_suppression_updates_graph_pack_node(fake_home):
    unload, home = fake_home
    packs_dir = home / ".claude" / "skill-wiki" / "graphify-out" / "packs"
    graph = nx.Graph()
    graph.add_node("skill:real-skill", label="real-skill", type="skill")
    write_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        config_hash="config-sha",
        model_id="test-model",
        graph=graph,
    )

    assert unload.set_never_load(["real-skill"]) == ["real-skill"]
    merged = load_merged_pack_graph(packs_dir)
    assert merged.nodes["skill:real-skill"]["never_load"] is True

    assert unload.restore_load(["real-skill"]) == ["real-skill"]
    merged = load_merged_pack_graph(packs_dir)
    assert merged.nodes["skill:real-skill"]["never_load"] is False


def test_permanent_suppression_updates_pack_only_wiki_page(fake_home):
    unload, home = fake_home
    wiki = home / ".claude" / "skill-wiki"
    legacy_page = wiki / "entities" / "skills" / "real-skill.md"
    legacy_page.unlink()
    write_wiki_base_pack(
        pack_dir=wiki / "wiki-packs" / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        pages={
            "entities/skills/real-skill.md": (
                "---\nname: real-skill\nstatus: stale\nnever_load: false\n---\n\n# real-skill\n"
            ),
        },
    )
    packs_dir = wiki / "graphify-out" / "packs"
    graph = nx.Graph()
    graph.add_node("skill:real-skill", label="real-skill", type="skill")
    write_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        config_hash="config-sha",
        model_id="test-model",
        graph=graph,
    )

    assert unload.get_stale_skills(entity_type="skill") == ["real-skill"]
    assert unload.set_never_load(["real-skill"], entity_type="skill") == ["real-skill"]

    merged_pages = load_merged_wiki_pages(wiki / "wiki-packs")
    assert "never_load: true" in merged_pages["entities/skills/real-skill.md"]
    merged_graph = load_merged_pack_graph(packs_dir)
    assert merged_graph.nodes["skill:real-skill"]["never_load"] is True


def test_permanent_suppression_refreshes_graph_store_from_pack_overlay(fake_home):
    unload, home = fake_home
    wiki = home / ".claude" / "skill-wiki"
    graph_dir = wiki / "graphify-out"
    packs_dir = graph_dir / "packs"
    graph = nx.Graph()
    graph.add_node("skill:real-skill", label="real-skill", type="skill")
    write_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        config_hash="config-sha",
        model_id="test-model",
        graph=graph,
    )

    assert unload.set_never_load(["real-skill"]) == ["real-skill"]
    jobs = wiki_queue.list_jobs(wiki_queue.queue_db_path(wiki))
    assert [job.kind for job in jobs] == [wiki_queue.GRAPH_STORE_REFRESH_JOB]

    result = wiki_queue_worker.process_next(wiki, worker_id="test-worker")

    assert result is not None
    assert result.kind == wiki_queue.GRAPH_STORE_REFRESH_JOB
    assert result.status == wiki_queue.STATUS_SUCCEEDED
    store = graph_dir / "graph-store.sqlite3"
    assert graph_store_stats(store) == {"nodes": 1, "edges": 0}
    with sqlite3.connect(store) as conn:
        row = conn.execute(
            "SELECT attrs_json FROM nodes WHERE id = ?",
            ("skill:real-skill",),
        ).fetchone()
    assert row is not None
    assert json.loads(row[0])["never_load"] is True
