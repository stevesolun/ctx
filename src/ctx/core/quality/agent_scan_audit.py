"""Snyk Agent Scan inventory and tracker support for ctx tool catalogs."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

AGENT_SCAN_REPO = "https://github.com/snyk/agent-scan"
TRACKER_SCHEMA_VERSION = 1
DEFAULT_TRACKER = Path("qa/agent_scan_security_tracker.csv")
DEFAULT_STORAGE_FILE = Path(".ctx-agent-scan-state")
CSV_FIELDS = (
    "schema_version",
    "entity_type",
    "slug",
    "source",
    "path",
    "scan_target",
    "agent_scan_supported",
    "scan_status",
    "vulnerable",
    "severity",
    "issue_count",
    "issue_codes",
    "suggested_action",
    "review_status",
    "evidence",
    "scanner",
    "scanner_version",
    "scanned_at",
)
ISSUE_CODE_FIELDS = ("code", "issue_code", "rule_id", "id")


@dataclass(frozen=True)
class AgentScanTarget:
    entity_type: str
    slug: str
    source: str
    path: str
    scan_target: str
    agent_scan_supported: bool


@dataclass(frozen=True)
class AgentScanTrackerRow:
    schema_version: int
    entity_type: str
    slug: str
    source: str
    path: str
    scan_target: str
    agent_scan_supported: bool
    scan_status: str
    vulnerable: bool
    severity: str
    issue_count: int
    issue_codes: str
    suggested_action: str
    review_status: str
    evidence: str
    scanner: str
    scanner_version: str
    scanned_at: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _slug_from_path(path: Path) -> str:
    if path.name == "SKILL.md":
        return _skill_name_from_frontmatter(path) or path.parent.name
    if path.name == "harness-record.json":
        return path.parent.name
    return path.stem


def _skill_name_from_frontmatter(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"(?m)^name:\s*[\"']?([^\"'\n]+)[\"']?\s*$", text[:1000])
    if match is None:
        return None
    return match.group(1).strip() or None


def _posix(path: Path, repo: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return path.as_posix()


def discover_targets(repo: Path) -> list[AgentScanTarget]:
    repo = repo.resolve()
    targets: list[AgentScanTarget] = []
    for path in sorted(repo.glob("skills/*/SKILL.md")):
        targets.append(_target("skill", "repo-skill", path, repo, True))
    docs_skill = repo / "docs" / "SKILL.md"
    if docs_skill.exists():
        targets.append(_target("skill", "repo-doc-skill", docs_skill, repo, True))
    for path in sorted((repo / "imported-skills").glob("**/SKILL.md")):
        targets.append(_target("skill", "imported-skill", path, repo, True))
    for path in sorted((repo / "imported-skills").glob("**/agents/*.md")):
        targets.append(_target("agent", "imported-agent", path, repo, False))
    for path in _mcp_json_files(repo):
        if _is_agent_scan_mcp_config(path):
            targets.append(_target("mcp-config", "repo-mcp-config", path, repo, True))
        else:
            targets.append(_target("mcp-record", "repo-mcp-record", path, repo, False))
    for path in sorted((repo / "imported-skills").glob("**/harness-record.json")):
        targets.append(_target("harness", "imported-harness-record", path, repo, False))
    for path in sorted((repo / "docs" / "harness").glob("*.md")):
        targets.append(_target("harness-doc", "repo-harness-doc", path, repo, False))
    targets.extend(_artifact_scope_targets(repo))
    return targets


def _mcp_json_files(repo: Path) -> list[Path]:
    candidates = {
        *repo.glob("qa/agent_scan_mcp_configs/*.json"),
        *repo.glob("src/tests/fixtures/mcp*.json"),
    }
    return sorted(candidates)


def _is_agent_scan_mcp_config(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("mcpServers"), dict)


def _target(
    entity_type: str,
    source: str,
    path: Path,
    repo: Path,
    agent_scan_supported: bool,
) -> AgentScanTarget:
    rel = _posix(path, repo)
    return AgentScanTarget(
        entity_type=entity_type,
        slug=_slug_from_path(path),
        source=source,
        path=rel,
        scan_target=rel if agent_scan_supported else "",
        agent_scan_supported=agent_scan_supported,
    )


def _artifact_scope_targets(repo: Path) -> list[AgentScanTarget]:
    stats_path = repo / "graph" / "wiki-graph-stats.json"
    if not stats_path.exists():
        return []
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    counts = stats.get("counts") if isinstance(stats, dict) else {}
    if not isinstance(counts, dict):
        return []
    rows: list[AgentScanTarget] = []
    for entity_type, key in (
        ("skill-catalog", "skills"),
        ("agent-catalog", "agents"),
        ("mcp-catalog", "mcps"),
        ("harness-catalog", "harnesses"),
    ):
        count = counts.get(key)
        if count is None:
            continue
        rows.append(
            AgentScanTarget(
                entity_type=entity_type,
                slug=f"shipped-{key}-{count}",
                source="shipped-wiki-graph-artifact",
                path="graph/wiki-graph.tar.gz",
                scan_target="",
                agent_scan_supported=False,
            )
        )
    return rows


def agent_scan_version(
    agent_scan_bin: str,
    *,
    runner: Runner = subprocess.run,
) -> str:
    try:
        completed = runner(
            [agent_scan_bin, "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {exc}"
    text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    match = re.search(r"Agent Scan v([0-9][^\s]+)", text)
    return match.group(1) if match else "installed"


def build_tracker_rows(
    targets: Iterable[AgentScanTarget],
    *,
    repo: Path,
    agent_scan_bin: str = "snyk-agent-scan",
    storage_file: Path = DEFAULT_STORAGE_FILE,
    allow_mcp_execution: bool = False,
    run_scans: bool = True,
    snyk_token_present: bool | None = None,
    runner: Runner = subprocess.run,
    now: str | None = None,
) -> list[AgentScanTrackerRow]:
    repo = repo.resolve()
    scanned_at = now or datetime.now(UTC).isoformat()
    scanner_version = agent_scan_version(agent_scan_bin, runner=runner)
    if snyk_token_present is None:
        snyk_token_present = bool(os.environ.get("SNYK_TOKEN"))
    rows: list[AgentScanTrackerRow] = []
    for target in targets:
        rows.append(
            _row_for_target(
                target,
                repo=repo,
                agent_scan_bin=agent_scan_bin,
                storage_file=storage_file,
                allow_mcp_execution=allow_mcp_execution,
                run_scans=run_scans,
                snyk_token_present=snyk_token_present,
                runner=runner,
                scanned_at=scanned_at,
                scanner_version=scanner_version,
            )
        )
    return rows


def _row_for_target(
    target: AgentScanTarget,
    *,
    repo: Path,
    agent_scan_bin: str,
    storage_file: Path,
    allow_mcp_execution: bool,
    run_scans: bool,
    snyk_token_present: bool,
    runner: Runner,
    scanned_at: str,
    scanner_version: str,
) -> AgentScanTrackerRow:
    if not target.agent_scan_supported:
        return _status_row(
            target,
            scan_status=_unsupported_status(target.entity_type),
            suggested_action=_unsupported_action(target.entity_type),
            review_status="pending-supported-scanner-or-manual-review",
            evidence=(
                "Agent Scan direct path support is limited to MCP configs, "
                "direct MCP specs, skill directories, and SKILL.md files."
            ),
            scanned_at=scanned_at,
            scanner_version=scanner_version,
        )
    if not run_scans:
        return _status_row(
            target,
            scan_status="not_scanned_dry_run",
            suggested_action="Rerun without --dry-run to request Agent Scan review.",
            review_status="pending-scan",
            evidence="Dry-run inventory mode did not invoke Agent Scan.",
            scanned_at=scanned_at,
            scanner_version=scanner_version,
        )
    if target.entity_type == "mcp-config" and not allow_mcp_execution:
        return _status_row(
            target,
            scan_status="not_scanned_requires_mcp_execution_consent",
            suggested_action=(
                "Review the MCP command(s), then rerun with --allow-mcp-execution "
                "inside a sandbox or disposable environment."
            ),
            review_status="blocked-human-consent",
            evidence=(
                "Snyk Agent Scan warns that scanning MCP configs can execute "
                "configured stdio server commands."
            ),
            scanned_at=scanned_at,
            scanner_version=scanner_version,
        )
    if not snyk_token_present:
        return _status_row(
            target,
            scan_status="not_scanned_missing_snyk_token",
            suggested_action=(
                "Set SNYK_TOKEN and rerun ctx-agent-scan-audit so Agent Scan can "
                "produce vulnerability verdicts."
            ),
            review_status="blocked-missing-token",
            evidence="snyk-agent-scan exits before scanning when SNYK_TOKEN is unset.",
            scanned_at=scanned_at,
            scanner_version=scanner_version,
        )
    return _scan_row(
        target,
        repo=repo,
        agent_scan_bin=agent_scan_bin,
        storage_file=storage_file,
        runner=runner,
        scanned_at=scanned_at,
        scanner_version=scanner_version,
    )


def _unsupported_status(entity_type: str) -> str:
    if entity_type.endswith("-catalog"):
        return "not_scanned_catalog_scale_requires_designated_snyk_api"
    return "not_scanned_not_directly_supported_by_agent_scan"


def _unsupported_action(entity_type: str) -> str:
    if entity_type.endswith("-catalog"):
        return (
            "Use Snyk-designated APIs or staged sample batches before attempting "
            "full registry-scale Agent Scan ingestion."
        )
    if entity_type == "mcp-record":
        return (
            "This is catalog metadata, not an executable MCP config. Review the "
            "source record manually or scan a trusted mcpServers config for the server."
        )
    return (
        "Perform manual security review or convert the entity into an Agent "
        "Scan-supported skill/MCP target before scanning."
    )


def _scan_row(
    target: AgentScanTarget,
    *,
    repo: Path,
    agent_scan_bin: str,
    storage_file: Path,
    runner: Runner,
    scanned_at: str,
    scanner_version: str,
) -> AgentScanTrackerRow:
    command = [
        agent_scan_bin,
        "scan",
        "--json",
        "--no-bootstrap",
        "--storage-file",
        str(storage_file),
        target.scan_target,
    ]
    completed = runner(
        command,
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        env=_scan_env(repo),
    )
    payload = _load_agent_scan_json(completed.stdout)
    issues = _collect_issues(payload)
    if completed.returncode != 0 and not issues:
        return _status_row(
            target,
            scan_status="scan_error",
            suggested_action="Inspect Agent Scan stdout/stderr and rerun after fixing scanner/runtime prerequisites.",
            review_status="needs-agent-scan-review",
            evidence=_compact_text(
                completed.stdout or completed.stderr or "Agent Scan returned nonzero."
            ),
            scanned_at=scanned_at,
            scanner_version=scanner_version,
        )
    severity = _max_severity(issue.get("severity") for issue in issues)
    issue_codes = ",".join(
        sorted(
            {
                str(issue[field])
                for issue in issues
                for field in ISSUE_CODE_FIELDS
                if issue.get(field)
            }
        )
    )
    vulnerable = bool(issues)
    return AgentScanTrackerRow(
        schema_version=TRACKER_SCHEMA_VERSION,
        entity_type=target.entity_type,
        slug=target.slug,
        source=target.source,
        path=target.path,
        scan_target=target.scan_target,
        agent_scan_supported=target.agent_scan_supported,
        scan_status="findings" if vulnerable else "passed",
        vulnerable=vulnerable,
        severity=severity if vulnerable else "none",
        issue_count=len(issues),
        issue_codes=issue_codes,
        suggested_action=_finding_action(severity, issue_codes),
        review_status="needs-review" if vulnerable else "done",
        evidence=_compact_text(completed.stdout or "Agent Scan completed."),
        scanner="Snyk Agent Scan",
        scanner_version=scanner_version,
        scanned_at=scanned_at,
    )


def _status_row(
    target: AgentScanTarget,
    *,
    scan_status: str,
    suggested_action: str,
    review_status: str,
    evidence: str,
    scanned_at: str,
    scanner_version: str,
) -> AgentScanTrackerRow:
    return AgentScanTrackerRow(
        schema_version=TRACKER_SCHEMA_VERSION,
        entity_type=target.entity_type,
        slug=target.slug,
        source=target.source,
        path=target.path,
        scan_target=target.scan_target,
        agent_scan_supported=target.agent_scan_supported,
        scan_status=scan_status,
        vulnerable=False,
        severity="unknown",
        issue_count=0,
        issue_codes="",
        suggested_action=suggested_action,
        review_status=review_status,
        evidence=evidence,
        scanner="Snyk Agent Scan",
        scanner_version=scanner_version,
        scanned_at=scanned_at,
    )


def _load_agent_scan_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _collect_issues(payload: Any) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if _looks_like_issue(value):
                issues.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return issues


def _looks_like_issue(value: dict[str, object]) -> bool:
    keys = set(value)
    return bool(keys & {"severity", "issue_code", "code", "rule_id"}) and bool(
        keys & {"message", "title", "description", "name"}
    )


def _max_severity(values: Iterable[object]) -> str:
    rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0, "none": -1}
    best = "unknown"
    best_score = -2
    for value in values:
        normalized = str(value or "").strip().lower()
        score = rank.get(normalized, -2)
        if score > best_score:
            best = normalized
            best_score = score
    return best


def _finding_action(severity: str, issue_codes: str) -> str:
    if severity in {"critical", "high"}:
        return "Quarantine or disable the tool, review Agent Scan details, fix or remove the risky behavior, and expose the risk on the relevant LLM-wiki page until remediated."
    if severity in {"medium", "low", "info"}:
        return "Review Agent Scan details, apply the smallest safe remediation, then rerun Agent Scan and update this tracker."
    if issue_codes:
        return "Review Agent Scan issue codes, remediate, and rerun."
    return "No action required."


def _compact_text(text: str, limit: int = 500) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _scan_env(repo: Path) -> dict[str, str]:
    env = dict(os.environ)
    venv_bin = repo / ".venv" / "bin"
    if venv_bin.is_dir():
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def write_tracker(path: Path, rows: Iterable[AgentScanTrackerRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            payload = asdict(row)
            payload["agent_scan_supported"] = str(row.agent_scan_supported).lower()
            payload["vulnerable"] = str(row.vulnerable).lower()
            writer.writerow(payload)


def summarize_rows(rows: Iterable[AgentScanTrackerRow]) -> dict[str, object]:
    row_list = list(rows)
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for row in row_list:
        by_status[row.scan_status] = by_status.get(row.scan_status, 0) + 1
        by_type[row.entity_type] = by_type.get(row.entity_type, 0) + 1
    return {
        "rows": len(row_list),
        "vulnerable": sum(1 for row in row_list if row.vulnerable),
        "by_status": dict(sorted(by_status.items())),
        "by_type": dict(sorted(by_type.items())),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Build a Snyk Agent Scan tracker for ctx skills, agents, MCPs, and harnesses.")
    )
    parser.add_argument("--repo", default=".", help="Repository root.")
    parser.add_argument("--out", default=str(DEFAULT_TRACKER), help="CSV tracker output path.")
    parser.add_argument("--agent-scan-bin", default="snyk-agent-scan")
    parser.add_argument("--storage-file", default=str(DEFAULT_STORAGE_FILE))
    parser.add_argument("--allow-mcp-execution", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inventory only; do not invoke Agent Scan.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    rows = build_tracker_rows(
        discover_targets(repo),
        repo=repo,
        agent_scan_bin=args.agent_scan_bin,
        storage_file=Path(args.storage_file),
        allow_mcp_execution=args.allow_mcp_execution,
        run_scans=not args.dry_run,
    )
    out = Path(args.out)
    if not out.is_absolute():
        out = repo / out
    write_tracker(out, rows)
    print(json.dumps(summarize_rows(rows), indent=2, sort_keys=True))
    return 1 if any(row.vulnerable for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
