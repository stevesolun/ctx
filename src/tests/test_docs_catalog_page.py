from __future__ import annotations

import re
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "src"))


def test_readme_badges_have_public_click_targets() -> None:
    text = (repo_root / "README.md").read_text(encoding="utf-8")
    badge_lines = [line for line in text.splitlines() if line.startswith("[![")]

    assert badge_lines
    assert not any(line.endswith("](#)") for line in badge_lines)
    joined = "\n".join(badge_lines)
    expected = {
        "CI": r"https://github\.com/stevesolun/ctx/actions/workflows/test\.yml",
        "PyPI": r"https://pypi\.org/project/claude-ctx/",
    }
    assert not any(f"[![{label}]" in joined for label in ("Skills", "Agents", "MCPs", "Harnesses"))
    for label, target in expected.items():
        assert re.search(rf"^\[!\[{label}\]\([^)]+\.svg(?:\?[^)]*)?\)\]\({target}\)$", joined, re.M)


def test_public_catalog_page_does_not_link_to_local_dashboard() -> None:
    text = (repo_root / "docs" / "catalog.md").read_text(encoding="utf-8")
    js_text = (repo_root / "docs" / "assets" / "javascripts" / "catalog.js").read_text(
        encoding="utf-8"
    )
    mkdocs_text = (repo_root / "mkdocs.yml").read_text(encoding="utf-8")

    assert "http://127.0.0.1" not in text
    assert "http://localhost" not in text
    assert "ctxLocalWikiUrl" not in text
    assert "ctx-catalog-card" in text
    assert "Code review skills" in text
    assert 'data-search="code review' in text
    assert "assets/javascripts/catalog.js" in mkdocs_text
    assert "window.document$" in js_text
    assert 'queryInput.addEventListener("input"' in js_text
    assert "../dashboard/#catalog-badge-links" in text


def test_docs_pages_workflow_publishes_branch_without_deploy_pages_queue() -> None:
    text = (repo_root / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8")

    assert "contents: read" in text
    assert "Publish site to gh-pages" in text
    assert "CTX_PAGES_DEPLOY_KEY" in text
    assert "GIT_SSH_COMMAND" in text
    assert "git@github.com:${GITHUB_REPOSITORY}.git" in text
    assert "git ls-remote --exit-code --heads origin gh-pages" in text
    assert "git checkout -B gh-pages origin/gh-pages" in text
    assert "git checkout --orphan gh-pages" in text
    assert 'cp -a "${GITHUB_WORKSPACE}/site/." .' in text
    assert "touch .nojekyll" in text
    assert "git push --force origin gh-pages" in text
    assert "x-access-token" not in text
    assert "secrets.GITHUB_TOKEN" not in text
    assert "actions/configure-pages" not in text
    assert "actions/upload-pages-artifact" not in text
    assert "actions/deploy-pages" not in text
    assert "actions/upload-artifact@v4" not in text
    assert "artifact.tar" not in text
    assert "overwrite: true" not in text
    assert "deployment_queued" not in text


def test_public_catalog_is_schema_not_a_bundled_inventory() -> None:
    text = (repo_root / "docs" / "catalog.md").read_text(encoding="utf-8")
    assert "schema and search examples" in text
    assert "zero bundled skills, agents, MCP servers, or harnesses" in text
    assert "0 bundled entities; add your own records" in text
    assert "ctx-catalog:begin" not in text
    for count in ("68,494", "79,958", "10,790"):
        assert count not in text


def test_public_docs_describe_empty_install_and_explicit_consent() -> None:
    documents = (
        (repo_root / "README.md").read_text(encoding="utf-8").lower(),
        (repo_root / "docs" / "knowledge-graph.md").read_text(encoding="utf-8").lower(),
    )
    for text in documents:
        text = " ".join(text.split())
        assert "empty" in text
        assert "explicit approval" in text
        assert "no" in text and "corpus" in text
        assert "ctx-python-testing" not in text
        assert "pre-built weighted graph" not in text
