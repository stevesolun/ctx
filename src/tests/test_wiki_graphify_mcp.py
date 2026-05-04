"""
tests/test_wiki_graphify_mcp.py -- pytest suite for MCP-server entity
support in wiki_graphify.

Covers:
  - The three pure helpers (_mcp_shard, _entity_page_path,
    _entity_wikilink, _related_section_header).
  - build_graph() picks up MCP entities under their sharded layout.
  - inject_wikilinks_in_pages writes the correct ## Related MCP Servers
    section header and points neighbor links at the sharded path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from ctx.core.wiki import wiki_graphify as wg  # noqa: E402


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestMcpShard:
    def test_letter_slug_uses_first_char(self):
        assert wg._mcp_shard("github") == "g"

    def test_digit_leading_slug_uses_0_9(self):
        assert wg._mcp_shard("007-server") == "0-9"

    def test_empty_string_uses_0_9(self):
        # Empty slug: not isalpha() -> "0-9". Not a real codepath but
        # the helper must not crash on it.
        assert wg._mcp_shard("") == "0-9"


class TestEntityPagePath:
    def test_skill_returns_flat_path(self):
        path = wg._entity_page_path("skill", "python-patterns")
        assert path is not None
        assert path == wg.SKILL_ENTITIES / "python-patterns.md"

    def test_agent_returns_flat_path(self):
        path = wg._entity_page_path("agent", "code-reviewer")
        assert path is not None
        assert path == wg.AGENT_ENTITIES / "code-reviewer.md"

    def test_mcp_returns_sharded_path(self):
        path = wg._entity_page_path("mcp-server", "github")
        assert path is not None
        assert path == wg.MCP_ENTITIES / "g" / "github.md"

    def test_mcp_digit_uses_0_9_shard(self):
        path = wg._entity_page_path("mcp-server", "007-mcp")
        assert path is not None
        assert path == wg.MCP_ENTITIES / "0-9" / "007-mcp.md"

    def test_unknown_type_returns_none(self):
        assert wg._entity_page_path("widget", "anything") is None


class TestEntityWikilink:
    def test_skill_link_format(self):
        assert wg._entity_wikilink("skill", "react") == "[[entities/skills/react]]"

    def test_agent_link_format(self):
        assert wg._entity_wikilink("agent", "planner") == "[[entities/agents/planner]]"

    def test_mcp_link_includes_shard(self):
        assert wg._entity_wikilink("mcp-server", "fetch") == "[[entities/mcp-servers/f/fetch]]"

    def test_mcp_digit_link_uses_0_9(self):
        assert wg._entity_wikilink("mcp-server", "9-svc") == "[[entities/mcp-servers/0-9/9-svc]]"

    def test_unknown_type_returns_none(self):
        assert wg._entity_wikilink("widget", "x") is None


class TestRelatedSectionHeader:
    def test_skill_header(self):
        assert wg._related_section_header("skill") == "## Related Skills"

    def test_agent_header(self):
        assert wg._related_section_header("agent") == "## Related Agents"

    def test_mcp_header(self):
        assert wg._related_section_header("mcp-server") == "## Related MCP Servers"

    def test_unknown_falls_back_to_generic(self):
        assert wg._related_section_header("widget") == "## Related"


# ---------------------------------------------------------------------------
# build_graph integration: MCP entities flow through the graph build
# ---------------------------------------------------------------------------


def _make_entity_md(path: Path, slug: str, etype: str, tags: list[str]) -> None:
    """Write a minimal entity markdown file with parseable frontmatter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"---\ntitle: {slug}\ntype: {etype}\ntags:\n"
    for tag in tags:
        body += f"  - {tag}\n"
    body += "---\n\n# " + slug + "\n"
    path.write_text(body, encoding="utf-8")


class TestBuildGraphIncludesMcp:
    @pytest.fixture()
    def tmp_wiki(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        import ctx_config

        skills_dir = tmp_path / "entities" / "skills"
        agents_dir = tmp_path / "entities" / "agents"
        mcp_dir = tmp_path / "entities" / "mcp-servers"
        harness_dir = tmp_path / "entities" / "harnesses"
        skills_dir.mkdir(parents=True)
        agents_dir.mkdir(parents=True)
        mcp_dir.mkdir(parents=True)

        # Repoint every graphify path at the tiny fixture. Leaving WIKI_DIR,
        # GRAPH_OUT, HARNESS_ENTITIES, or QUALITY_SIDECAR_DIR on the live
        # ~/.claude tree makes these tests parse or score the real catalog.
        monkeypatch.setattr(wg, "WIKI_DIR", tmp_path)
        monkeypatch.setattr(wg, "SKILL_ENTITIES", skills_dir)
        monkeypatch.setattr(wg, "AGENT_ENTITIES", agents_dir)
        monkeypatch.setattr(wg, "MCP_ENTITIES", mcp_dir)
        monkeypatch.setattr(wg, "HARNESS_ENTITIES", harness_dir)
        monkeypatch.setattr(wg, "GRAPH_OUT", tmp_path / "graphify-out")
        monkeypatch.setattr(wg, "QUALITY_SIDECAR_DIR", tmp_path / "skill-quality")
        monkeypatch.setattr(ctx_config.cfg, "graph_edge_weight_semantic", 0.0)

        return tmp_path

    def test_mcp_entity_creates_node(self, tmp_wiki: Path) -> None:
        _make_entity_md(
            tmp_wiki / "entities" / "mcp-servers" / "g" / "github.md",
            slug="github", etype="mcp-server", tags=["git", "github"],
        )
        graph, entities = wg.build_graph(incremental=False)
        assert "mcp-server:github" in graph.nodes
        assert graph.nodes["mcp-server:github"]["type"] == "mcp-server"
        assert "git" in graph.nodes["mcp-server:github"]["tags"]

    def test_mcp_node_picked_up_from_digit_shard(self, tmp_wiki: Path) -> None:
        _make_entity_md(
            tmp_wiki / "entities" / "mcp-servers" / "0-9" / "007-mcp.md",
            slug="007-mcp", etype="mcp-server", tags=["security"],
        )
        graph, _ = wg.build_graph(incremental=False)
        assert "mcp-server:007-mcp" in graph.nodes

    def test_mcp_and_skill_share_edges_via_tags(self, tmp_wiki: Path) -> None:
        # Skill and MCP both tagged 'github' — should connect.
        _make_entity_md(
            tmp_wiki / "entities" / "skills" / "github-ops.md",
            slug="github-ops", etype="skill", tags=["github"],
        )
        _make_entity_md(
            tmp_wiki / "entities" / "mcp-servers" / "g" / "github.md",
            slug="github", etype="mcp-server", tags=["github"],
        )
        graph, _ = wg.build_graph(incremental=False)
        assert graph.has_edge("skill:github-ops", "mcp-server:github")

    def test_existing_skill_agent_layouts_still_work(self, tmp_wiki: Path) -> None:
        # No MCP file present — should not error on missing dir or
        # produce spurious nodes.
        _make_entity_md(
            tmp_wiki / "entities" / "skills" / "react.md",
            slug="react", etype="skill", tags=["frontend"],
        )
        _make_entity_md(
            tmp_wiki / "entities" / "agents" / "planner.md",
            slug="planner", etype="agent", tags=["planning"],
        )
        graph, _ = wg.build_graph(incremental=False)
        assert "skill:react" in graph.nodes
        assert "agent:planner" in graph.nodes
        # No MCP entities written — confirm no MCP nodes appear.
        mcp_nodes = [n for n in graph.nodes() if str(n).startswith("mcp-server:")]
        assert mcp_nodes == []

    def test_missing_mcp_dir_is_skipped_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ctx_config

        # Wikis that predate MCP support won't have entities/mcp-servers/
        # — build_graph must not raise.
        skills_dir = tmp_path / "entities" / "skills"
        skills_dir.mkdir(parents=True)
        monkeypatch.setattr(wg, "WIKI_DIR", tmp_path)
        monkeypatch.setattr(wg, "SKILL_ENTITIES", skills_dir)
        monkeypatch.setattr(wg, "AGENT_ENTITIES", tmp_path / "entities" / "agents")
        monkeypatch.setattr(wg, "MCP_ENTITIES", tmp_path / "entities" / "mcp-servers")
        monkeypatch.setattr(
            wg, "HARNESS_ENTITIES", tmp_path / "entities" / "harnesses",
        )
        monkeypatch.setattr(wg, "GRAPH_OUT", tmp_path / "graphify-out")
        monkeypatch.setattr(wg, "QUALITY_SIDECAR_DIR", tmp_path / "skill-quality")
        monkeypatch.setattr(ctx_config.cfg, "graph_edge_weight_semantic", 0.0)

        _make_entity_md(
            skills_dir / "lonely.md",
            slug="lonely", etype="skill", tags=["solo"],
        )
        graph, _ = wg.build_graph(incremental=False)
        assert "skill:lonely" in graph.nodes
