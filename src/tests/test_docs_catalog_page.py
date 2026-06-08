from __future__ import annotations

import re
from pathlib import Path


def test_readme_badges_have_public_click_targets() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / "README.md").read_text(encoding="utf-8")
    badge_lines = [line for line in text.splitlines() if line.startswith("[![")]

    assert badge_lines
    assert not any(line.endswith("](#)") for line in badge_lines)
    joined = "\n".join(badge_lines)
    expected = {
        "Tests": r"https://github\.com/stevesolun/ctx/actions/workflows/test\.yml",
        "Skills": r"https://stevesolun\.github\.io/ctx/catalog/\?type=skill",
        "Agents": r"https://stevesolun\.github\.io/ctx/catalog/\?type=agent",
        "MCPs": r"https://stevesolun\.github\.io/ctx/catalog/\?type=mcp-server",
        "Harnesses": r"https://stevesolun\.github\.io/ctx/catalog/\?type=harness",
    }
    for label, target in expected.items():
        assert re.search(rf"^\[!\[{label}\]\([^)]+\.svg\)\]\({target}\)$", joined, re.M)


def test_public_catalog_page_does_not_link_to_local_dashboard() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / "docs" / "catalog.md").read_text(encoding="utf-8")
    js_text = (
        repo_root / "docs" / "assets" / "javascripts" / "catalog.js"
    ).read_text(encoding="utf-8")
    mkdocs_text = (repo_root / "mkdocs.yml").read_text(encoding="utf-8")

    assert "http://127.0.0.1" not in text
    assert "http://localhost" not in text
    assert "ctxLocalWikiUrl" not in text
    assert "ctx-catalog-card" in text
    assert "Code review skills" in text
    assert "data-search=\"code review" in text
    assert "assets/javascripts/catalog.js" in mkdocs_text
    assert "window.document$" in js_text
    assert "queryInput.addEventListener(\"input\"" in js_text
    assert "../dashboard/#catalog-badge-links" in text


def test_docs_pages_workflow_uploads_single_overwritable_pages_artifact() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / ".github" / "workflows" / "docs.yml").read_text(
        encoding="utf-8"
    )

    assert "actions/upload-pages-artifact" not in text
    assert "actions/upload-artifact@v4" in text
    assert "name: github-pages" in text
    assert "artifact.tar" in text
    assert "overwrite: true" in text


def test_public_docs_do_not_render_old_graph_totals() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    paths = [repo_root / "README.md"]
    paths.extend((repo_root / "docs").rglob("*.md"))
    paths.extend((repo_root / "graph").rglob("*.md"))
    public_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in paths
    )

    for stale in (
        "102,696",
        "102,925",
        "91,432",
        "91,463",
        "89,463",
        "2,900,834",
        "2,913,930",
        "2,960,215",
    ):
        assert stale not in public_text

    for current in (
        "102,928",
        "91,464",
        "89,465",
        "2,913,960",
        "10,790",
        "207 harnesses",
    ):
        assert current in public_text
