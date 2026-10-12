"""ADR-017: shipping the engine must never restore the retired tool corpus."""

from pathlib import Path

import update_repo_stats
from scripts.ci_preflight import select_checks


ROOT = Path(__file__).resolve().parents[2]


def test_default_checks_do_not_hydrate_tool_catalogs() -> None:
    checks, _ = select_checks(
        base_ref="main", files=["src/ctx/api.py", "graph/SCHEMA.md"], profile="pr", python="python"
    )
    commands = "\n".join(" ".join(check.argv) for check in checks)
    assert "hydrate" not in commands
    assert "test_validate_graph_artifacts.py" in commands
    assert "test_graph_packs.py" in commands


def test_default_workflows_do_not_download_or_publish_a_tool_catalog() -> None:
    assert not (ROOT / ".github/workflows/huggingface-sync.yml").exists()
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = path.read_text(encoding="utf-8")
        assert "graph_release_manifest.py hydrate" not in workflow, path
        assert "graph-release-assets" not in workflow, path
        assert "sync_huggingface.py" not in workflow, path


def test_public_counts_never_read_private_or_retired_catalogs(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("public stats read a private or retired catalog")

    monkeypatch.setattr(Path, "home", forbidden)
    monkeypatch.setattr(update_repo_stats, "_read_graph_artifact_stats", forbidden)
    monkeypatch.setattr(update_repo_stats, "_read_graph_from_tarball", forbidden)
    monkeypatch.setattr(update_repo_stats, "_read_graph_contract_stats", forbidden)
    counts = update_repo_stats.read_graph_stats()
    assert counts and all(value == 0 for value in counts.values())
    assert update_repo_stats.read_converted_count() == 0


def test_stats_check_does_not_collect_tests_without_inventory_markers(
    tmp_path, monkeypatch
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# CTX\nBring your own knowledge.\n", encoding="utf-8")
    monkeypatch.setattr(update_repo_stats, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(update_repo_stats, "README", readme)
    for name in ("DOCS_INDEX", "DOCS_KNOWLEDGE_GRAPH", "DOCS_CATALOG", "DOCS_SKILL_ROUTER"):
        monkeypatch.setattr(update_repo_stats, name, tmp_path / "missing.md")

    def forbidden(**kwargs):
        raise AssertionError("collected tests for a document without an inventory")

    monkeypatch.setattr(update_repo_stats, "read_test_count", forbidden)
    assert update_repo_stats.patch_readme(check_only=True) == 0
