"""
test_ctx_monitor_3type.py -- pins the dashboard entity contract.

The dashboard audit (Phase 5c) surfaced that ctx_monitor.py was MCP-
unaware across every route. This file pins the fixes so a future
refactor can't silently regress back to the pre-v0.7 skill+agent-only
framing.

Scope:
  - _wiki_stats counts MCPs and harnesses alongside skills + agents
  - _wiki_index_entries includes mcp-server and harness entries
  - _render_home shows the entity-type breakdown in the Wiki entities card
  - _render_wiki_index has a type filter that includes mcp-server and harness
  - _render_loaded renders a Skills / Agents / MCP servers section
    layout and carries entity_type on every unload button
  - _perform_unload routes by entity_type (skill+agent -> skill_unload,
    mcp-server -> mcp_install.uninstall_mcp)
  - _render_graph has a left-sidebar filter panel (type checkboxes
    + tag filter) and cytoscape styles for MCP and harness nodes in
    distinct colours.
"""

from __future__ import annotations

import sys
from pathlib import Path

import networkx as nx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import ctx_monitor as _cm


# ────────────────────────────────────────────────────────────────────
# Fixture — synthetic wiki with all dashboard entity types
# ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def wiki_3type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a minimal wiki and point ctx_monitor at it via ``_wiki_dir``."""
    wiki = tmp_path / "skill-wiki"
    for sub in ("skills", "agents", "harnesses"):
        (wiki / "entities" / sub).mkdir(parents=True)
    # MCP dirs are sharded — put one under 'a/', one under 'p/'.
    (wiki / "entities" / "mcp-servers" / "a").mkdir(parents=True)
    (wiki / "entities" / "mcp-servers" / "p").mkdir(parents=True)

    # 2 skills
    for slug in ("python-patterns", "security-basics"):
        (wiki / "entities" / "skills" / f"{slug}.md").write_text(
            f"---\ntitle: {slug}\ntype: skill\ntags: [python, testing]\n---\n# {slug}\n",
            encoding="utf-8",
        )
    # 1 agent
    (wiki / "entities" / "agents" / "code-reviewer.md").write_text(
        "---\ntitle: code-reviewer\ntype: agent\ntags: [python, review]\n---\n# code-reviewer\n",
        encoding="utf-8",
    )
    # 3 MCPs across shards
    (wiki / "entities" / "mcp-servers" / "a" / "anthropic-python-sdk.md").write_text(
        "---\ntype: mcp-server\nslug: anthropic-python-sdk\ntags: [python, sdk]\n---\n# anthropic-python-sdk\n",
        encoding="utf-8",
    )
    (wiki / "entities" / "mcp-servers" / "a" / "atlassian-cloud.md").write_text(
        "---\ntype: mcp-server\nslug: atlassian-cloud\ntags: [saas]\n---\n# atlassian-cloud\n",
        encoding="utf-8",
    )
    (wiki / "entities" / "mcp-servers" / "p" / "pulsemcp-meta.md").write_text(
        "---\ntype: mcp-server\nslug: pulsemcp-meta\ntags: [meta]\n---\n# pulsemcp-meta\n",
        encoding="utf-8",
    )
    # 1 harness
    (wiki / "entities" / "harnesses" / "langgraph.md").write_text(
        "---\ntype: harness\nslug: langgraph\ntags: [agent, orchestration]\n---\n# langgraph\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(_cm, "_wiki_dir", lambda: wiki)
    return wiki


# ────────────────────────────────────────────────────────────────────
# _wiki_stats
# ────────────────────────────────────────────────────────────────────


class TestWikiStats:
    def test_counts_include_mcps(self, wiki_3type):
        s = _cm._wiki_stats()
        assert s["skills"] == 2
        assert s["agents"] == 1
        assert s["mcps"] == 3
        assert s["harnesses"] == 1
        assert s["total"] == 7

    def test_mcps_sharded_dirs_scanned_recursively(self, wiki_3type):
        """MCPs live under entities/mcp-servers/<first-char>/<slug>.md —
        the scan must walk the shard dirs, not just the top level."""
        s = _cm._wiki_stats()
        assert s["mcps"] == 3  # 2 under a/, 1 under p/

    def test_no_mcp_dir_gracefully_zero(self, tmp_path, monkeypatch):
        """Missing entities/mcp-servers/ must not crash — common for
        users who have only ingested skills."""
        wiki = tmp_path / "wiki"
        (wiki / "entities" / "skills").mkdir(parents=True)
        monkeypatch.setattr(_cm, "_wiki_dir", lambda: wiki)
        s = _cm._wiki_stats()
        assert s["mcps"] == 0
        assert s["harnesses"] == 0
        assert "mcps" in s  # key present even at zero


# ────────────────────────────────────────────────────────────────────
# _wiki_index_entries
# ────────────────────────────────────────────────────────────────────


class TestWikiIndexEntries:
    def test_entries_include_mcp_servers(self, wiki_3type):
        entries = _cm._wiki_index_entries()
        types = {e["type"] for e in entries}
        assert "mcp-server" in types
        assert "harness" in types
        assert "skill" in types
        assert "agent" in types

    def test_mcp_slugs_surfaced_from_both_shards(self, wiki_3type):
        entries = _cm._wiki_index_entries()
        slugs = {e["slug"] for e in entries if e["type"] == "mcp-server"}
        assert slugs == {"anthropic-python-sdk", "atlassian-cloud", "pulsemcp-meta"}

    def test_wiki_entity_path_resolves_sharded_mcp_pages(self, wiki_3type):
        path = _cm._wiki_entity_path("anthropic-python-sdk")
        assert path == (
            wiki_3type / "entities" / "mcp-servers" / "a" / "anthropic-python-sdk.md"
        )

    def test_wiki_entity_path_resolves_harness_pages(self, wiki_3type):
        path = _cm._wiki_entity_path("langgraph", entity_type="harness")
        assert path == wiki_3type / "entities" / "harnesses" / "langgraph.md"


# ────────────────────────────────────────────────────────────────────
# _render_home — entity-type card
# ────────────────────────────────────────────────────────────────────


class TestRenderHome3Type:
    def test_wiki_card_shows_mcp_count(self, wiki_3type, monkeypatch):
        # Other dependencies of _render_home need shims that don't touch
        # the real user dir. Empty manifests + empty audit log are fine.
        monkeypatch.setattr(_cm, "_read_manifest", lambda: {"load": [], "unload": []})
        monkeypatch.setattr(_cm, "_summarize_sessions", lambda: [])
        monkeypatch.setattr(_cm, "_grade_distribution", lambda: {})
        monkeypatch.setattr(_cm, "_graph_stats", lambda: {"nodes": 0, "edges": 0, "available": False})
        monkeypatch.setattr(_cm, "_audit_log_path", lambda: wiki_3type / "no-audit.log")
        monkeypatch.setattr(_cm, "_read_jsonl", lambda *a, **k: [])

        html = _cm._render_home()
        # The detail line inside the Wiki entities card must name MCPs.
        assert "MCPs" in html
        assert "2 skills" in html or "2 skill" in html
        assert "1 agent" in html
        assert "3 MCPs" in html
        assert "1 harness" in html


# ────────────────────────────────────────────────────────────────────
# _render_wiki_index — type filter includes mcp-server
# ────────────────────────────────────────────────────────────────────


class TestRenderWikiIndex3Type:
    def test_mcp_server_checkbox_present(self, wiki_3type, monkeypatch):
        monkeypatch.setattr(_cm, "_all_sidecars", lambda: [])
        html = _cm._render_wiki_index()
        assert "value='mcp-server' checked" in html
        assert "value='harness' checked" in html
        # Count for the mcp-server bucket should render.
        assert ">3</span>" in html  # 3 MCPs in fixture
        assert ">1</span>" in html  # 1 harness in fixture


# ────────────────────────────────────────────────────────────────────
# _render_loaded — 3-section layout + entity_type on unload buttons
# ────────────────────────────────────────────────────────────────────


class TestRenderLoaded3Type:
    def test_heading_names_all_three_types(self, monkeypatch):
        monkeypatch.setattr(_cm, "_read_manifest",
                            lambda: {"load": [], "unload": []})
        html = _cm._render_loaded()
        assert "skills, agents, MCPs &amp; harnesses" in html or (
            "skills, agents, MCPs & harnesses" in html
        )

    def test_loaded_entries_split_by_entity_type(self, monkeypatch):
        manifest = {
            "load": [
                {"skill": "python-patterns", "entity_type": "skill",
                 "source": "ctx-skill-install"},
                {"skill": "code-reviewer", "entity_type": "agent",
                 "source": "ctx-agent-install"},
                {"skill": "anthropic-python-sdk", "entity_type": "mcp-server",
                 "source": "ctx-mcp-install", "command": "npx -y @anthropic/sdk"},
                {"skill": "langgraph", "entity_type": "harness",
                 "source": "ctx-harness-install"},
            ],
            "unload": [],
        }
        monkeypatch.setattr(_cm, "_read_manifest", lambda: manifest)
        html = _cm._render_loaded()
        # Four section headers render — one per type.
        assert "<h3" in html
        assert "Skills " in html or "Skills</h3" in html or "Skills " in html
        assert "Agents " in html or "Agents</h3" in html
        assert "MCP servers" in html
        assert "Harnesses" in html
        assert "langgraph" in html

    def test_unload_buttons_carry_entity_type(self, monkeypatch):
        """Unload buttons must carry data-etype for live-action entity types.

        Harnesses are managed by ctx-harness-install because they own target
        directories and setup commands, so the dashboard shows the CLI handoff
        instead of a misleading live unload button.
        """
        manifest = {
            "load": [
                {"skill": "foo-skill", "entity_type": "skill"},
                {"skill": "bar-agent", "entity_type": "agent"},
                {"skill": "baz-mcp", "entity_type": "mcp-server"},
                {"skill": "langgraph", "entity_type": "harness"},
            ],
            "unload": [],
        }
        monkeypatch.setattr(_cm, "_read_manifest", lambda: manifest)
        html = _cm._render_loaded()
        assert "data-etype='skill'" in html
        assert "data-etype='agent'" in html
        assert "data-etype='mcp-server'" in html
        assert "data-etype='harness'" not in html
        assert "ctx-harness-install langgraph --uninstall --dry-run" in html

    def test_legacy_manifest_entry_defaults_to_skill(self, monkeypatch):
        """Pre-install_utils manifest entries had no entity_type field.
        Those must default to skill (what the implicit contract was)
        and still render in the Skills section without crashing."""
        monkeypatch.setattr(_cm, "_read_manifest",
                            lambda: {"load": [{"skill": "legacy"}], "unload": []})
        html = _cm._render_loaded()
        assert "legacy" in html
        assert "data-etype='skill'" in html


# ────────────────────────────────────────────────────────────────────
# _perform_unload — routes by entity_type
# ────────────────────────────────────────────────────────────────────


class TestPerformUnloadByEntityType:
    def test_skill_type_calls_skill_unload(self, monkeypatch):
        calls = {}

        def fake_unload_from_session(slugs, *, entity_type=None):
            calls["slugs"] = slugs
            calls["entity_type"] = entity_type
            return slugs

        from ctx.adapters.claude_code.install import skill_unload
        monkeypatch.setattr(skill_unload, "unload_from_session",
                            fake_unload_from_session)
        ok, msg = _cm._perform_unload("python-patterns", entity_type="skill")
        assert ok
        assert calls["slugs"] == ["python-patterns"]
        assert calls["entity_type"] == "skill"

    def test_mcp_type_calls_mcp_install_uninstall(self, monkeypatch):
        """The MCP path must go through mcp_install.uninstall_mcp,
        NOT skill_unload. That's the core routing contract."""
        from dataclasses import dataclass

        @dataclass
        class _FakeResult:
            slug: str
            status: str
            message: str = ""

        calls = {}

        def fake_uninstall(slug, **kw):
            calls["slug"] = slug
            calls["kw"] = kw
            return _FakeResult(slug=slug, status="uninstalled", message="ok")

        from ctx.adapters.claude_code.install import mcp_install
        monkeypatch.setattr(mcp_install, "uninstall_mcp", fake_uninstall)

        ok, msg = _cm._perform_unload("anthropic-python-sdk",
                                       entity_type="mcp-server")
        assert ok
        assert calls["slug"] == "anthropic-python-sdk"
        # force=True so a drifted local state (entity status != installed)
        # doesn't block the dashboard unload.
        assert calls["kw"].get("force") is True

    def test_mcp_uninstall_failure_surfaces(self, monkeypatch):
        from dataclasses import dataclass

        @dataclass
        class _FakeResult:
            slug: str
            status: str
            message: str = ""

        from ctx.adapters.claude_code.install import mcp_install
        monkeypatch.setattr(
            mcp_install, "uninstall_mcp",
            lambda slug, **kw: _FakeResult(slug=slug, status="claude-cli-failed",
                                           message="claude mcp remove failed"),
        )

        ok, msg = _cm._perform_unload("bad-mcp", entity_type="mcp-server")
        assert not ok
        assert "claude mcp remove failed" in msg

    def test_invalid_slug_rejected(self):
        ok, msg = _cm._perform_unload("../etc/passwd", entity_type="skill")
        assert not ok
        assert "invalid slug" in msg


# ────────────────────────────────────────────────────────────────────
# _render_graph — left-sidebar filter panel + MCP node style
# ────────────────────────────────────────────────────────────────────


class TestRenderGraphSidebar:
    def test_sidebar_type_checkboxes_include_mcp_server(self, monkeypatch):
        monkeypatch.setattr(_cm, "_graph_stats",
                            lambda: {"nodes": 10, "edges": 20, "available": True})
        monkeypatch.setattr(_cm, "_top_degree_seeds", lambda: [])
        html = _cm._render_graph()
        # Four type checkboxes: skill, agent, mcp-server, harness.
        assert "class='graph-type-filter' value='skill'" in html
        assert "class='graph-type-filter' value='agent'" in html
        assert "class='graph-type-filter' value='mcp-server'" in html
        assert "class='graph-type-filter' value='harness'" in html

    def test_sidebar_has_tag_filter(self, monkeypatch):
        monkeypatch.setattr(_cm, "_graph_stats",
                            lambda: {"nodes": 0, "edges": 0, "available": False})
        monkeypatch.setattr(_cm, "_top_degree_seeds", lambda: [])
        html = _cm._render_graph()
        assert "id='tag-filter'" in html

    def test_cytoscape_styles_mcp_and_harness_nodes_distinctly(self, monkeypatch):
        """The graph view shows four types; each must be visually
        distinct or users can't tell them apart. Before the fix, MCPs
        rendered in the default skill color because no CSS rule
        matched node[type="mcp-server"]."""
        monkeypatch.setattr(_cm, "_graph_stats",
                            lambda: {"nodes": 0, "edges": 0, "available": False})
        monkeypatch.setattr(_cm, "_top_degree_seeds", lambda: [])
        html = _cm._render_graph()
        assert 'node[type = "mcp-server"]' in html
        assert 'node[type = "harness"]' in html
        # The current design uses red for MCPs to distinguish from
        # indigo (skill) and amber (agent). A future restyle can
        # loosen this — just require SOME style block exists.
        assert "shape" in html.split('node[type = "mcp-server"]')[1][:300]
        assert "shape" in html.split('node[type = "harness"]')[1][:300]

    def test_tap_handler_strips_mcp_server_prefix(self, monkeypatch):
        """Clicking an MCP node must route to /wiki/<slug> — the
        prefix-stripping regex has to know all four types."""
        monkeypatch.setattr(_cm, "_graph_stats",
                            lambda: {"nodes": 0, "edges": 0, "available": False})
        monkeypatch.setattr(_cm, "_top_degree_seeds", lambda: [])
        html = _cm._render_graph()
        # Regex should include mcp-server and harness in the prefix alternation.
        assert "(skill|agent|mcp-server|harness)" in html

    def test_graph_neighborhood_can_disambiguate_harness_slug(self, monkeypatch):
        graph = nx.Graph()
        graph.add_node("skill:langgraph", label="langgraph", type="skill", tags=["python"])
        graph.add_node("harness:langgraph", label="langgraph", type="harness", tags=["agent"])
        graph.add_node("skill:agent-patterns", label="agent-patterns", type="skill", tags=["agent"])
        graph.add_edge("harness:langgraph", "skill:agent-patterns", weight=2, shared_tags=["agent"])
        monkeypatch.setattr(_cm, "_load_dashboard_graph", lambda: graph)

        out = _cm._graph_neighborhood("langgraph", entity_type="harness")

        assert out["center"] == "harness:langgraph"
        assert any(n["data"]["type"] == "harness" for n in out["nodes"])


class TestMonitorRoutesPreserveEntityType:
    def _run_get(self, path: str, *, html_fn=None, json_fn=None):
        handler = type("FakeHandler", (), {})()
        handler.path = path
        sent: dict[str, object] = {}
        handler._send_html = lambda body: sent.setdefault("html", body)
        handler._send_json = lambda body: sent.setdefault("json", body)
        handler._send_404 = lambda detail: sent.setdefault("404", detail)
        handler._send_500 = lambda exc: sent.setdefault("500", exc)
        if html_fn is not None:
            handler._send_html = html_fn
        if json_fn is not None:
            handler._send_json = json_fn
        _cm._MonitorHandler.do_GET(handler)
        return sent

    def test_graph_route_passes_type_query(self, monkeypatch):
        calls = {}

        def fake_render_graph(slug, focus_type=None):
            calls["slug"] = slug
            calls["focus_type"] = focus_type
            return "graph"

        monkeypatch.setattr(_cm, "_render_graph", fake_render_graph)

        sent = self._run_get("/graph?slug=langgraph&type=harness")

        assert sent["html"] == "graph"
        assert calls == {"slug": "langgraph", "focus_type": "harness"}

    def test_wiki_route_passes_type_query(self, monkeypatch):
        calls = {}

        def fake_render_wiki_entity(slug, entity_type=None):
            calls["slug"] = slug
            calls["entity_type"] = entity_type
            return "wiki"

        monkeypatch.setattr(_cm, "_render_wiki_entity", fake_render_wiki_entity)

        sent = self._run_get("/wiki/langgraph?type=harness")

        assert sent["html"] == "wiki"
        assert calls == {"slug": "langgraph", "entity_type": "harness"}

    def test_graph_api_route_passes_type_query(self, monkeypatch):
        calls = {}

        def fake_graph_neighborhood(slug, hops=1, limit=40, entity_type=None):
            calls["slug"] = slug
            calls["hops"] = hops
            calls["limit"] = limit
            calls["entity_type"] = entity_type
            return {"center": "harness:langgraph", "nodes": [], "edges": []}

        monkeypatch.setattr(_cm, "_graph_neighborhood", fake_graph_neighborhood)

        sent = self._run_get("/api/graph/langgraph.json?type=harness&hops=2&limit=55")

        assert sent["json"] == {"center": "harness:langgraph", "nodes": [], "edges": []}
        assert calls == {
            "slug": "langgraph",
            "hops": 2,
            "limit": 55,
            "entity_type": "harness",
        }
