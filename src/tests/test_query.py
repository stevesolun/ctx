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

import wiki_query as wq  # noqa: E402

from ._wiki_helpers import make_entity_page, make_wiki  # noqa: E402


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
        # Use status="" so the installed bonus (+0.5) does not leak into an
        # otherwise zero-scoring page when the query has no keyword match.
        make_entity_page(wiki, "python-basics", ["python"], body="Learn Python.", status="")

        pages = wq.load_all_pages(wiki)
        results = wq.search_by_query(pages, "xyznonexistent")

        assert results == [], f"Expected no results, got {[p.name for p in results]}"
