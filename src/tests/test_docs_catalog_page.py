from __future__ import annotations

from collections.abc import Mapping
import json
import re
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "src"))

import update_repo_stats as urs  # noqa: E402


def _required_stat(stats: Mapping[str, int | None], key: str) -> int:
    value = stats[key]
    assert value is not None, f"missing graph stat: {key}"
    return value


def test_readme_badges_have_public_click_targets() -> None:
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


def test_public_docs_render_current_graph_contract_totals() -> None:
    stats = urs._read_graph_contract_stats()
    assert stats is not None
    knowledge_text = (repo_root / "docs" / "knowledge-graph.md").read_text(
        encoding="utf-8",
    )
    graph_text = (repo_root / "graph" / "README.md").read_text(encoding="utf-8")
    public_text = knowledge_text + "\n" + graph_text

    expected_rows = {
        "Total nodes": _required_stat(stats, "nodes"),
        "Total edges": _required_stat(stats, "edges"),
        "Harness edges": _required_stat(stats, "harness_edges"),
    }
    for label, value in expected_rows.items():
        assert f"| {label} | **{value:,}** |" in public_text

    assert f"semantic {_required_stat(stats, 'semantic_edges'):,}" in public_text
    assert f"{_required_stat(stats, 'skills'):,} skill" in public_text
    assert f"{_required_stat(stats, 'agents'):,} agent" in public_text
    assert f"{_required_stat(stats, 'mcps'):,} MCP" in public_text
    assert f"{_required_stat(stats, 'harnesses'):,} harness" in public_text


def test_public_docs_describe_clean_install_fallback_contract() -> None:
    manifest = json.loads(
        (repo_root / "src" / "ctx" / "assets" / "runtime-availability.json").read_text(
            encoding="utf-8"
        )
    )
    nodes = manifest["overlay"]["nodes"]
    node_ids = {node["id"] for node in nodes}
    entry_ids = {entry["id"] for entry in manifest["entries"]}

    assert len(node_ids) == 6
    assert node_ids == entry_ids
    assert all(node["project_owned"] is True for node in nodes)
    assert all(node["license"] == "MIT" for node in nodes)
    assert all(node["requires_api_keys"] is False for node in nodes)

    documents = (
        (repo_root / "README.md").read_text(encoding="utf-8").lower(),
        (repo_root / "docs" / "knowledge-graph.md").read_text(encoding="utf-8").lower(),
    )
    for text in documents:
        for entity_id in node_ids:
            assert entity_id.split(":", 1)[-1] in text
        for boundary in (
            "project-owned",
            "no-key",
            "preserves unrelated skill",
            "runtime-managed harness pages",
            "fails closed",
        ):
            assert boundary in text
