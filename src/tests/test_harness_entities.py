"""Harness entity support across wiki query, graphify, and ctx-core tools."""

from __future__ import annotations

import json
from pathlib import Path

from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox
from ctx.adapters.generic.providers import ToolCall
from ctx.core.wiki import wiki_graphify as wg
from ctx.core.wiki import wiki_query as wq


def _write_harness(path: Path, *, tags: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tags = tags or ["cad", "urdf", "viewer", "validation"]
    path.write_text(
        "\n".join([
            "---",
            "title: text-to-cad",
            "type: harness",
            "status: cataloged",
            "harness_kind: domain-workbench",
            "model_modes: [api, local]",
            "tools: [filesystem, python, viewer]",
            "verification: [cad-snapshot, urdf-validation]",
            "install_risk: medium",
            "execution_policy: user-confirmed",
            "tags:",
            *[f"  - {tag}" for tag in tags],
            "---",
            "",
            "# text-to-cad",
            "",
            "CAD and URDF generation harness with local viewer verification.",
        ]),
        encoding="utf-8",
    )


def test_wiki_query_loads_harness_pages(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _write_harness(wiki / "entities" / "harnesses" / "text-to-cad.md")

    pages = wq.load_all_pages(wiki)

    by_name = {page.name: page for page in pages}
    assert by_name["text-to-cad"].entity_type == "harness"
    assert by_name["text-to-cad"].wikilink == "[[entities/harnesses/text-to-cad]]"


def test_ctx_core_wiki_get_disambiguates_harness(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _write_harness(wiki / "entities" / "harnesses" / "text-to-cad.md")
    toolbox = CtxCoreToolbox(wiki_dir=wiki, graph_path=tmp_path / "missing.json")

    result = json.loads(toolbox.dispatch(ToolCall(
        id="c1",
        name="ctx__wiki_get",
        arguments={"slug": "text-to-cad", "entity_type": "harness"},
    )))

    assert result["entity_type"] == "harness"
    assert result["wikilink"] == "[[entities/harnesses/text-to-cad]]"
    assert result["frontmatter"]["harness_kind"] == "domain-workbench"


def test_wiki_graphify_includes_harness_nodes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    entities = tmp_path / "entities"
    skills_dir = entities / "skills"
    agents_dir = entities / "agents"
    mcp_dir = entities / "mcp-servers"
    harness_dir = entities / "harnesses"
    for directory in (skills_dir, agents_dir, mcp_dir, harness_dir):
        directory.mkdir(parents=True)

    monkeypatch.setattr(wg, "SKILL_ENTITIES", skills_dir)
    monkeypatch.setattr(wg, "AGENT_ENTITIES", agents_dir)
    monkeypatch.setattr(wg, "MCP_ENTITIES", mcp_dir)
    monkeypatch.setattr(wg, "HARNESS_ENTITIES", harness_dir)
    monkeypatch.setattr(wg, "QUALITY_SIDECAR_DIR", tmp_path / "quality")

    _write_harness(harness_dir / "text-to-cad.md")

    graph, _ = wg.build_graph(incremental=False)

    assert graph.nodes["harness:text-to-cad"]["type"] == "harness"
    assert "cad" in graph.nodes["harness:text-to-cad"]["tags"]
    assert wg._entity_page_path("harness", "text-to-cad") == (
        harness_dir / "text-to-cad.md"
    )
    assert wg._entity_wikilink("harness", "text-to-cad") == (
        "[[entities/harnesses/text-to-cad]]"
    )
    assert wg._related_section_header("harness") == "## Related Harnesses"
