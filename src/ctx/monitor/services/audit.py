"""Audit logging helpers for the ctx dashboard."""

from __future__ import annotations

from pathlib import Path


def log_dashboard_entity_event(
    audit_log_path: Path,
    entity_type: str,
    action: str,
    slug: str,
) -> None:
    """Append a dashboard-visible audit row for a load/unload action."""
    try:
        from ctx_audit_log import log

        if entity_type == "skill":
            log(
                f"skill.{action}",
                subject_type="skill",
                subject=slug,
                actor="user",
                meta={"via": "ctx-monitor"},
                path=audit_log_path,
            )
        elif entity_type == "agent":
            log(
                f"agent.{action}",
                subject_type="agent",
                subject=slug,
                actor="user",
                meta={"via": "ctx-monitor"},
                path=audit_log_path,
            )
        elif entity_type == "mcp-server":
            log(
                "toolbox.triggered",
                subject_type="toolbox",
                subject=slug,
                actor="user",
                meta={
                    "via": "ctx-monitor",
                    "entity_type": "mcp-server",
                    "action": action,
                },
                path=audit_log_path,
            )
    except Exception:  # noqa: BLE001
        pass
