"""Plan remediation/removal from ctx SkillSpector audit records.

This module is intentionally non-destructive. It converts the persisted
SkillSpector audit into a reviewable action plan so the later graph/wiki rewrite
can remove exactly the intended skill entities with provenance.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from ctx.core.quality.skillspector_audit import (
    SKILLSPECTOR_REPO_URL,
    SkillSpectorAuditRecord,
    load_audit_records,
)
from ctx.utils._fs_utils import atomic_write_json, atomic_write_text

PLAN_SCHEMA_VERSION = 1

REMOVE_STATUSES = frozenset({"blocked", "not_scanned_no_body"})
REVIEW_STATUSES = frozenset({"findings"})
KEEP_STATUSES = frozenset({"passed"})


@dataclass(frozen=True)
class RemediationDecision:
    slug: str
    action: str
    reason: str
    status: str
    risk_severity: str
    risk_score: int | None
    issues: int
    issue_rules: tuple[str, ...]
    recommendation: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "action": self.action,
            "reason": self.reason,
            "status": self.status,
            "risk_severity": self.risk_severity,
            "risk_score": self.risk_score,
            "issues": self.issues,
            "issue_rules": list(self.issue_rules),
            "recommendation": self.recommendation,
        }


def decide_record(record: SkillSpectorAuditRecord) -> RemediationDecision:
    """Return the deterministic first-pass action for one audit record."""
    severity = record.risk_severity or "UNKNOWN"
    if record.status in REMOVE_STATUSES:
        if record.status == "not_scanned_no_body":
            action = "remove"
            reason = "skill entity has no converted SKILL.md body to scan or install"
        else:
            action = "remove"
            reason = f"SkillSpector blocked the skill with {severity} risk"
    elif record.status in REVIEW_STATUSES:
        action = "remove"
        reason = (
            "SkillSpector finding remains unresolved; remove until remediated and rescanned cleanly"
        )
    elif record.status in KEEP_STATUSES:
        action = "keep"
        reason = "SkillSpector passed"
    else:
        action = "review_unknown"
        reason = f"unrecognized SkillSpector status: {record.status}"

    return RemediationDecision(
        slug=record.slug,
        action=action,
        reason=reason,
        status=record.status,
        risk_severity=severity,
        risk_score=record.risk_score,
        issues=record.issues,
        issue_rules=record.issue_rules,
        recommendation=record.recommendation,
    )


def build_remediation_plan(
    records: dict[str, SkillSpectorAuditRecord],
    *,
    audit_path: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a stable JSON remediation plan from loaded audit records."""
    decisions = [decide_record(record) for record in records.values()]
    decisions.sort(key=lambda decision: (decision.action, decision.slug))

    status_counts = Counter(record.status for record in records.values())
    severity_counts = Counter(record.risk_severity or "UNKNOWN" for record in records.values())
    action_counts = Counter(decision.action for decision in decisions)
    rule_counts = Counter(rule for record in records.values() for rule in record.issue_rules)

    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "audit_path": str(audit_path) if audit_path is not None else None,
        "scanner_repo": SKILLSPECTOR_REPO_URL,
        "summary": {
            "total": len(records),
            "actions": dict(sorted(action_counts.items())),
            "statuses": dict(sorted(status_counts.items())),
            "severities": dict(sorted(severity_counts.items())),
            "top_issue_rules": [
                {"rule": rule, "count": count} for rule, count in rule_counts.most_common(25)
            ],
        },
        "remove_slugs": [decision.slug for decision in decisions if decision.action == "remove"],
        "review_slugs": [
            decision.slug
            for decision in decisions
            if decision.action in {"review_remediate", "review_unknown"}
        ],
        "decisions": [decision.to_json() for decision in decisions],
    }


def render_markdown_plan(plan: dict[str, Any]) -> str:
    """Render a compact human-readable remediation report."""
    summary = plan["summary"]
    lines = [
        "# SkillSpector Remediation Plan",
        "",
        f"- Generated: `{plan['generated_at']}`",
        f"- Audit: `{plan.get('audit_path') or 'unknown'}`",
        f"- Total records: **{summary['total']:,}**",
        "",
        "## Actions",
        "",
    ]
    for action, count in summary["actions"].items():
        lines.append(f"- `{action}`: **{count:,}**")
    lines.extend(["", "## Statuses", ""])
    for status, count in summary["statuses"].items():
        lines.append(f"- `{status}`: **{count:,}**")
    lines.extend(["", "## Top Issue Rules", ""])
    for item in summary["top_issue_rules"][:15]:
        lines.append(f"- `{item['rule']}`: **{item['count']:,}**")
    lines.extend(["", "## Removal Scope", ""])
    lines.append(
        "Remove actions include records SkillSpector blocked, records without a "
        "converted `SKILL.md` body, and every non-passing finding record. A "
        "finding can return only after the skill is remediated and rescanned "
        "cleanly.",
    )
    return "\n".join(lines) + "\n"


def _write_plan(path: Path, plan: dict[str, Any], *, output_format: str) -> None:
    if output_format == "json":
        atomic_write_json(path, plan, indent=2)
    elif output_format == "md":
        atomic_write_text(path, render_markdown_plan(plan), encoding="utf-8")
    else:
        raise ValueError(f"unsupported output format: {output_format}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a non-destructive SkillSpector remediation/removal plan.",
    )
    parser.add_argument(
        "--audit",
        type=Path,
        default=Path("graph/skillspector-audit.jsonl.gz"),
        help="SkillSpector audit JSONL gzip path",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional output path. Defaults to stdout.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "md"),
        default="json",
        help="Plan output format",
    )
    args = parser.parse_args(argv)

    records = load_audit_records(args.audit)
    plan = build_remediation_plan(records, audit_path=args.audit)

    if args.out is None:
        if args.format == "json":
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print(render_markdown_plan(plan), end="")
        return 0

    _write_plan(args.out, plan, output_format=args.format)
    print(f"wrote SkillSpector remediation plan: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
