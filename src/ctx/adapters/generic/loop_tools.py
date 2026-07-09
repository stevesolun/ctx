"""Loop provisioning helpers for opt-in harness tool surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ctx.adapters.claude_code.install.skill_install import install_skill
from ctx.adapters.loopflow import _is_loadable_skill_row, recommend_for_loop


def provision_skills(
    *,
    wiki_dir: Path,
    skills_dir: Path,
    goal: str,
    reflection: str = "",
    top_k: int = 5,
    dry_run: bool = False,
    exclude: Iterable[str] = (),
    security_scan: bool = False,
) -> dict[str, Any]:
    """Recommend loop skills and install only locally loadable wiki rows."""
    exclude_set = {str(slug).strip() for slug in exclude if str(slug).strip()}
    if not goal.strip():
        return _empty_result(dry_run, note="goal must be non-empty")

    rec = recommend_for_loop(
        goal=goal,
        last_failure=reflection,
        permissions={"skills"},
        top_k=top_k,
    )
    rows = rec.get("capabilities", {}).get("skills", [])

    use_skills: list[str] = []
    installed: list[str] = []
    would_install: list[str] = []
    skipped: list[str] = []
    manual: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("skill_id") or row.get("name") or "").strip()
        if not slug or slug in exclude_set:
            continue
        exclude_set.add(slug)

        if not _is_loadable_skill_row(row):
            manual.append(
                {
                    "name": slug,
                    "install_command": str(row.get("install_command") or ""),
                }
            )
            continue

        result = install_skill(
            slug,
            wiki_dir=wiki_dir,
            skills_dir=skills_dir,
            dry_run=dry_run,
            security_scan=security_scan,
        )
        if result.status == "installed":
            installed.append(slug)
            use_skills.append(slug)
        elif result.status == "would-install":
            would_install.append(slug)
            use_skills.append(slug)
        elif result.status == "skipped-existing":
            skipped.append(slug)
            use_skills.append(slug)
        else:
            failed.append({"slug": slug, "status": result.status, "message": result.message})

    return {
        "use_skills": use_skills,
        "installed": installed,
        "would_install": would_install,
        "skipped": skipped,
        "manual": manual,
        "failed": failed,
        "dry_run": dry_run,
    }


def _empty_result(dry_run: bool, *, note: str) -> dict[str, Any]:
    return {
        "use_skills": [],
        "installed": [],
        "would_install": [],
        "skipped": [],
        "manual": [],
        "failed": [],
        "dry_run": dry_run,
        "note": note,
    }
