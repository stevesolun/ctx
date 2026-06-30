"""ctx.adapters.generic.loop_tools — provision skills for a harness loop.

``ctx.adapters.loopflow.recommend_for_loop`` already recommends the skills a
harness loop needs for a goal (permissioned, loop-aware query building, the
``_is_loadable_skill_row`` filter, even a pre-built ``use skills:`` hint). What
it deliberately does *not* do is install them — it returns read-only context for
a host to inject. A headless, self-correcting ``.loop`` run has no human to act
on that context, so the recommended names never resolve in ``~/.claude/skills``.

``provision_skills`` closes that one gap: run ``recommend_for_loop`` for the
goal, then install the loadable skills it returns via the audited
``install_skill`` path. It adds no new recommendation logic — it is the
write-capable counterpart of ``recommend_for_loop``.

``CtxCoreToolbox`` wires it in as the ``ctx__loop_provision`` /
``ctx__loop_topup`` MCP tools. Kept as a pure function (explicit ``wiki_dir`` /
``skills_dir`` deps) so it unit-tests without the MCP layer.

Return shape (the contract a harness adapter parses):

    {
      "use_skills":    [slug, ...],   # installed or already-present → resolvable now
      "installed":     [slug, ...],   # newly installed this call
      "skipped":       [slug, ...],   # already present, left as-is
      "would_install": [slug, ...],   # dry_run only — what install would add
      "manual":        [{"name", "install_command"}, ...],  # recommended but not locally installable
      "failed":        [{"slug", "status", "message"}, ...],
      "dry_run":       bool
    }
"""

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
    """Recommend skills for ``goal`` (sharpened by ``reflection``) and install the loadable ones.

    ``exclude`` is a set of slugs already loaded (top-up only pulls *new* skills).
    ``reflection`` maps to ``recommend_for_loop``'s ``last_failure`` ranking signal.
    ``dry_run`` evaluates installs without touching disk. A skill recommended but
    not installable from the local wiki (external / skill-index catalog) lands in
    ``manual`` with its install command rather than being silently dropped; a skill
    that fails to install lands in ``failed``.
    """
    exclude_set = {s.strip() for s in exclude if s and s.strip()}
    if not goal.strip():
        return _empty(dry_run, note="goal must be non-empty")

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
        slug = str(row.get("skill_id") or row.get("name") or "").strip()
        if not slug or slug in exclude_set:
            continue
        exclude_set.add(slug)  # de-dupe within this call
        if not _is_loadable_skill_row(row):
            # Recommended but lives in an external catalog — host installs it itself.
            manual.append({
                "name": slug,
                "install_command": str(row.get("install_command") or ""),
            })
            continue
        res = install_skill(
            slug,
            wiki_dir=wiki_dir,
            skills_dir=skills_dir,
            dry_run=dry_run,
            security_scan=security_scan,
        )
        if res.status == "installed":
            installed.append(slug)
            use_skills.append(slug)
        elif res.status == "would-install":
            would_install.append(slug)
            use_skills.append(slug)
        elif res.status == "skipped-existing":
            skipped.append(slug)
            use_skills.append(slug)
        else:  # not-in-wiki | failed
            failed.append({"slug": slug, "status": res.status, "message": res.message})

    return {
        "use_skills": use_skills,
        "installed": installed,
        "would_install": would_install,
        "skipped": skipped,
        "manual": manual,
        "failed": failed,
        "dry_run": dry_run,
    }


def _empty(dry_run: bool, *, note: str) -> dict[str, Any]:
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
