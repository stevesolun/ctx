"""
test_query.py -- Tests for wiki_query (keyword search, tag filter, stats, related).

Every test builds its own minimal wiki structure via tmp_path so the real
~/.claude/skill-wiki is never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is importable regardless of working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ctx.core.wiki import wiki_query as wq  # noqa: E402

from ._wiki_helpers import make_entity_page, make_wiki  # noqa: E402


def _write_entity_page(
    wiki: Path,
    relpath: str,
    *,
    title: str,
    entity_type: str,
    tags: list[str],
    body: str,
    description: str = "",
    status: str = "installed",
) -> Path:
    path = wiki / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    tags_str = "[" + ", ".join(tags) + "]"
    path.write_text(
        "\n".join([
            "---",
            f"title: {title}",
            f"type: {entity_type}",
            *([f"description: {description}"] if description else []),
            f"tags: {tags_str}",
            f"status: {status}",
            "---",
            "",
            body,
        ]),
        encoding="utf-8",
    )
    return path


class TestQueryKeywordMatch:
    """test_query_keyword_match -- searching 'docker' finds skills with docker in name/body."""

    def test_query_keyword_match(self, tmp_path: Path) -> None:
        wiki = make_wiki(tmp_path)
        make_entity_page(wiki, "docker-compose-pro", ["docker"], body="Use docker-compose for local dev.")
        # status="" ensures this page scores exactly 0 for a "docker" query (no installed bonus).
        make_entity_page(wiki, "python-basics", ["python"], body="Learn Python fundamentals.", status="")

        pages = wq.load_all_pages(wiki)
        results = wq.search_by_query(pages, "docker")

        names = [p.name for p in results]
        assert "docker-compose-pro" in names, "docker skill must be returned for 'docker' query"
        assert "python-basics" not in names, "unrelated skill must not appear"

    def test_load_all_pages_includes_agents_and_sharded_mcps(self, tmp_path: Path) -> None:
        wiki = make_wiki(tmp_path)
        make_entity_page(wiki, "python-patterns", ["python"], body="Python skill.")
        _write_entity_page(
            wiki,
            "entities/agents/code-reviewer.md",
            title="Code Reviewer",
            entity_type="agent",
            tags=["review", "quality"],
            body="Reviews code for defects.",
        )
        _write_entity_page(
            wiki,
            "entities/mcp-servers/f/filesystem.md",
            title="Filesystem MCP",
            entity_type="mcp-server",
            tags=["filesystem", "io"],
            body="Filesystem tools for local files.",
        )

        pages = wq.load_all_pages(wiki)

        by_name = {p.name: p for p in pages}
        assert by_name["python-patterns"].entity_type == "skill"
        assert by_name["code-reviewer"].entity_type == "agent"
        assert by_name["filesystem"].entity_type == "mcp-server"
        assert by_name["filesystem"].wikilink == "[[entities/mcp-servers/f/filesystem]]"

    def test_load_all_pages_validates_mcp_shards_and_slugs(self, tmp_path: Path) -> None:
        wiki = make_wiki(tmp_path)
        _write_entity_page(
            wiki,
            "entities/mcp-servers/0-9/1password.md",
            title="1Password MCP",
            entity_type="mcp-server",
            tags=["secrets"],
            body="Secret-management tools.",
        )
        _write_entity_page(
            wiki,
            "entities/mcp-servers/x/2wrong.md",
            title="Mis-sharded MCP",
            entity_type="mcp-server",
            tags=["bad"],
            body="Wrong shard.",
        )
        _write_entity_page(
            wiki,
            "entities/mcp-servers/f/Filesystem.md",
            title="Uppercase MCP",
            entity_type="mcp-server",
            tags=["bad"],
            body="Unsafe slug.",
        )

        pages = wq.load_all_pages(wiki)

        by_name = {p.name: p for p in pages}
        assert by_name["1password"].wikilink == "[[entities/mcp-servers/0-9/1password]]"
        assert "2wrong" not in by_name
        assert "Filesystem" not in by_name

    def test_search_by_query_returns_agent_and_mcp_pages(self, tmp_path: Path) -> None:
        wiki = make_wiki(tmp_path)
        _write_entity_page(
            wiki,
            "entities/agents/code-reviewer.md",
            title="Code Reviewer",
            entity_type="agent",
            tags=["review", "quality"],
            body="Reviews code for defects.",
        )
        _write_entity_page(
            wiki,
            "entities/mcp-servers/f/filesystem.md",
            title="Filesystem MCP",
            entity_type="mcp-server",
            tags=["filesystem", "io"],
            body="Filesystem tools for local files.",
        )

        pages = wq.load_all_pages(wiki)
        results = wq.search_by_query(pages, "filesystem review", top_n=10)

        names = {p.name for p in results}
        assert {"code-reviewer", "filesystem"} <= names
        query_results = {r.name: r for r in map(wq._to_result, results)}
        assert query_results["code-reviewer"].entity_type == "agent"
        assert query_results["code-reviewer"].wikilink == "[[entities/agents/code-reviewer]]"
        assert query_results["filesystem"].entity_type == "mcp-server"
        assert query_results["filesystem"].wikilink == "[[entities/mcp-servers/f/filesystem]]"

    def test_search_by_query_matches_title_and_description(self, tmp_path: Path) -> None:
        wiki = make_wiki(tmp_path)
        _write_entity_page(
            wiki,
            "entities/agents/sre-playbook.md",
            title="Latency Debugger",
            entity_type="agent",
            description="Incident response runbook for production services.",
            tags=[],
            body="Operational checklist.",
            status="",
        )

        pages = wq.load_all_pages(wiki)
        title_hits = {p.name for p in wq.search_by_query(pages, "latency", top_n=10)}
        description_hits = {p.name for p in wq.search_by_query(pages, "incident", top_n=10)}

        assert "sre-playbook" in title_hits
        assert "sre-playbook" in description_hits


class TestQueryTagFilter:
    """test_query_tag_filter -- --tag python returns only python-tagged skills."""

    def test_query_tag_filter(self, tmp_path: Path) -> None:
        wiki = make_wiki(tmp_path)
        make_entity_page(wiki, "fastapi-service", ["python", "fastapi"], body="FastAPI patterns.")
        make_entity_page(wiki, "docker-network", ["docker"], body="Docker networking tips.")
        make_entity_page(wiki, "pytest-patterns", ["python", "testing"], body="Pytest best practices.")

        pages = wq.load_all_pages(wiki)
        results = wq.filter_by_tag(pages, "python")

        names = {p.name for p in results}
        assert "fastapi-service" in names
        assert "pytest-patterns" in names
        assert "docker-network" not in names, "non-python skill must be excluded"


class TestQueryStats:
    """test_query_stats -- compute_stats returns total_entity_pages, top_tags, with_pipeline."""

    def test_query_stats(self, tmp_path: Path) -> None:
        wiki = make_wiki(tmp_path)
        make_entity_page(wiki, "skill-a", ["python"], body="A skill.")
        make_entity_page(wiki, "skill-b", ["python", "fastapi"], body="B skill.", has_pipeline=True)
        make_entity_page(wiki, "skill-c", ["docker"], body="C skill.")
        # Create the converted dir so has_pipeline resolves
        (wiki / "converted" / "skill-b").mkdir(parents=True)

        pages = wq.load_all_pages(wiki)
        stats = wq.compute_stats(wiki, pages)

        assert stats["total_entity_pages"] == 3
        assert stats["with_pipeline"] == 1
        tag_keys = [t for t, _ in stats["top_tags"]]
        assert "python" in tag_keys
        assert "docker" in tag_keys


class TestQueryRelated:
    """test_query_related -- --related fastapi-pro finds python-tagged skills."""

    def test_query_related(self, tmp_path: Path) -> None:
        wiki = make_wiki(tmp_path)
        make_entity_page(wiki, "fastapi-pro", ["python", "fastapi"], body="The target skill.")
        make_entity_page(wiki, "pydantic-models", ["python", "fastapi"], body="Pydantic usage.")
        make_entity_page(wiki, "docker-compose-pro", ["docker"], body="Docker only.")

        pages = wq.load_all_pages(wiki)
        related = wq.find_related(pages, "fastapi-pro")

        names = [p.name for p in related]
        assert "pydantic-models" in names, "pydantic-models shares tags so must appear as related"
        assert "docker-compose-pro" not in names, "no shared tags with fastapi-pro"


class TestQueryNoResults:
    """test_query_no_results -- searching 'xyznonexistent' returns empty list."""

    def test_query_no_results(self, tmp_path: Path) -> None:
        wiki = make_wiki(tmp_path)
        make_entity_page(wiki, "python-basics", ["python"], body="Learn Python.")

        pages = wq.load_all_pages(wiki)
        results = wq.search_by_query(pages, "xyznonexistent")

        assert results == [], f"Expected no results, got {[p.name for p in results]}"
