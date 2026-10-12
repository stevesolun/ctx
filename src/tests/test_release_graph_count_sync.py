"""Release verification covers graph infrastructure, not a vendor tool corpus."""

from pathlib import Path

import yaml

import update_repo_stats
from scripts.ci_preflight import GRAPH_CONTRACT_TESTS


def test_graph_contract_is_identical_in_ci_and_release() -> None:
    for path in (".github/workflows/test.yml", ".github/workflows/publish.yml"):
        workflow = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
        step = next(
            item
            for item in steps
            if item.get("name") == "Validate graph infrastructure with synthetic fixtures"
        )
        argv = step["run"].replace("\\\n", " ").split()
        assert argv[:5] == ["python", "-m", "pytest", "-q", "--no-cov"]
        assert tuple(argv[5:]) == GRAPH_CONTRACT_TESTS


def test_graph_contract_checks_schema_and_archive_safety_offline() -> None:
    assert "src/tests/test_validate_graph_artifacts.py" in GRAPH_CONTRACT_TESTS
    assert "src/tests/test_graph_packs.py" in GRAPH_CONTRACT_TESTS
    assert "src/tests/test_graph_json_integrity.py" in GRAPH_CONTRACT_TESTS
    assert all(Path(path).is_file() for path in GRAPH_CONTRACT_TESTS)


def test_public_inventory_is_empty_not_the_historical_catalog() -> None:
    counts = update_repo_stats.read_graph_stats()
    assert all(value == 0 for value in counts.values())
    assert counts["nodes"] == counts["edges"] == counts["skills"] == 0


def test_software_release_has_no_graph_asset_precondition() -> None:
    text = Path(".github/workflows/publish.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert "graph_assets_available" not in text
    assert "graph-release-assets" not in text
    assert workflow["jobs"]["release-assets"]["if"] == "github.event_name == 'push'"
    assert "needs.release-assets.result == 'success'" in workflow["jobs"]["publish"]["if"]
