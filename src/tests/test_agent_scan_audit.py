from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path
from typing import Any

from ctx.core.quality.agent_scan_audit import AgentScanTarget
from ctx.core.quality.agent_scan_audit import build_tracker_rows
from ctx.core.quality.agent_scan_audit import discover_targets
from ctx.core.quality.agent_scan_audit import summarize_rows
from ctx.core.quality.agent_scan_audit import write_tracker


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _write(repo / "skills" / "router" / "SKILL.md", "# Router\n")
    _write(repo / "docs" / "SKILL.md", "---\nname: docs-router\n---\n# Docs Router\n")
    _write(repo / "imported-skills" / "pkg" / "demo" / "SKILL.md", "# Demo\n")
    _write(repo / "imported-skills" / "pkg" / "agents" / "reviewer.md", "# Reviewer\n")
    _write(
        repo / "imported-skills" / "pkg" / "harness-record.json",
        '{"name":"pkg"}\n',
    )
    _write(repo / "docs" / "harness" / "loopflow.md", "# LoopFlow\n")
    _write(
        repo / "qa" / "agent_scan_mcp_configs" / "ctx-mcp-server.json",
        '{"mcpServers":{"ctx-wiki":{"command":"ctx-mcp-server","args":[]}}}\n',
    )
    _write(
        repo / "src" / "tests" / "fixtures" / "mcp_record.json",
        '{"name":"github","description":"Catalog metadata only"}\n',
    )
    _write(
        repo / "graph" / "wiki-graph-stats.json",
        json.dumps(
            {
                "counts": {
                    "skills": 10,
                    "agents": 2,
                    "mcps": 3,
                    "harnesses": 4,
                }
            }
        ),
    )
    return repo


def _runner(stdout_by_command: dict[str, str] | None = None):
    outputs = stdout_by_command or {}

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(command)
        if command[-1] == "--help":
            return subprocess.CompletedProcess(command, 0, stdout="Snyk Agent Scan v0.5.12")
        for needle, stdout in outputs.items():
            if needle in joined:
                return subprocess.CompletedProcess(command, 0, stdout=stdout)
        return subprocess.CompletedProcess(command, 0, stdout='{"scan_results":[]}')

    return run


def test_discover_targets_covers_repo_sources_and_artifact_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    targets = discover_targets(repo)
    keys = {(target.entity_type, target.slug, target.source) for target in targets}

    assert ("skill", "router", "repo-skill") in keys
    assert ("skill", "docs-router", "repo-doc-skill") in keys
    assert ("skill", "demo", "imported-skill") in keys
    assert ("agent", "reviewer", "imported-agent") in keys
    assert ("mcp-config", "ctx-mcp-server", "repo-mcp-config") in keys
    assert ("mcp-record", "mcp_record", "repo-mcp-record") in keys
    assert ("harness", "pkg", "imported-harness-record") in keys
    assert ("harness-doc", "loopflow", "repo-harness-doc") in keys
    assert ("skill-catalog", "shipped-skills-10", "shipped-wiki-graph-artifact") in keys
    assert ("mcp-catalog", "shipped-mcps-3", "shipped-wiki-graph-artifact") in keys


def test_tracker_rows_record_missing_token_and_mcp_consent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    targets = [
        AgentScanTarget(
            "skill",
            "router",
            "repo-skill",
            "skills/router/SKILL.md",
            "skills/router/SKILL.md",
            True,
        ),
        AgentScanTarget(
            "mcp-config",
            "ctx-mcp-server",
            "repo-mcp-config",
            "qa/agent_scan_mcp_configs/ctx-mcp-server.json",
            "qa/agent_scan_mcp_configs/ctx-mcp-server.json",
            True,
        ),
        AgentScanTarget(
            "mcp-record",
            "github",
            "repo-mcp-record",
            "src/tests/fixtures/mcp_github.json",
            "",
            False,
        ),
        AgentScanTarget("agent", "reviewer", "imported-agent", "agents/reviewer.md", "", False),
    ]

    rows = build_tracker_rows(
        targets,
        repo=repo,
        snyk_token_present=False,
        runner=_runner(),
        now="2026-07-02T00:00:00+00:00",
    )

    by_slug = {row.slug: row for row in rows}
    assert by_slug["router"].scan_status == "not_scanned_missing_snyk_token"
    assert by_slug["router"].review_status == "blocked-missing-token"
    assert by_slug["ctx-mcp-server"].scan_status == "not_scanned_requires_mcp_execution_consent"
    assert by_slug["ctx-mcp-server"].review_status == "blocked-human-consent"
    assert by_slug["github"].scan_status == "not_scanned_not_directly_supported_by_agent_scan"
    assert by_slug["reviewer"].scan_status == "not_scanned_not_directly_supported_by_agent_scan"

    dry_rows = build_tracker_rows(
        targets[:2],
        repo=repo,
        run_scans=False,
        snyk_token_present=False,
        runner=_runner(),
        now="2026-07-02T00:00:00+00:00",
    )
    assert {row.scan_status for row in dry_rows} == {"not_scanned_dry_run"}


def test_tracker_rows_normalize_agent_scan_findings(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = AgentScanTarget(
        "skill",
        "router",
        "repo-skill",
        "skills/router/SKILL.md",
        "skills/router/SKILL.md",
        True,
    )
    output = json.dumps(
        {
            "results": [
                {
                    "issues": [
                        {
                            "issue_code": "W001",
                            "severity": "high",
                            "title": "Prompt injection",
                        }
                    ]
                }
            ]
        }
    )

    rows = build_tracker_rows(
        [target],
        repo=repo,
        snyk_token_present=True,
        runner=_runner({"skills/router/SKILL.md": output}),
        now="2026-07-02T00:00:00+00:00",
    )

    row = rows[0]
    assert row.scan_status == "findings"
    assert row.vulnerable is True
    assert row.severity == "high"
    assert row.issue_count == 1
    assert row.issue_codes == "W001"
    assert "expose the risk" in row.suggested_action


def test_write_tracker_outputs_stable_csv_booleans(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    rows = build_tracker_rows(
        [
            AgentScanTarget(
                "skill",
                "router",
                "repo-skill",
                "skills/router/SKILL.md",
                "skills/router/SKILL.md",
                True,
            )
        ],
        repo=repo,
        snyk_token_present=False,
        runner=_runner(),
        now="2026-07-02T00:00:00+00:00",
    )
    out = tmp_path / "tracker.csv"

    write_tracker(out, rows)

    text = out.read_text(encoding="utf-8")
    assert "\r\n" not in text
    with out.open(encoding="utf-8", newline="") as f:
        record = next(csv.DictReader(f))
    assert record["agent_scan_supported"] == "true"
    assert record["vulnerable"] == "false"
    assert summarize_rows(rows)["by_status"] == {"not_scanned_missing_snyk_token": 1}
