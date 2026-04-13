#!/usr/bin/env python3
"""
resolve_skills.py -- Map a stack profile to a skill manifest.

Usage:
    python resolve_skills.py \
      --profile /tmp/stack-profile.json \
      --wiki ~/skill-wiki \
      --available-skills /mnt/skills/ \
      --output /tmp/skill-manifest.json

Reads the stack profile, checks what skills are available, applies wiki
overrides, resolves conflicts, and produces a load/unload manifest.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from wiki_utils import parse_frontmatter as _parse_fm  # noqa: E402


def discover_available_skills(skills_dir: str) -> dict[str, dict]:
    """Find all SKILL.md files and extract metadata."""
    skills = {}
    skills_path = Path(skills_dir)

    if not skills_path.exists():
        return skills

    for skill_md in skills_path.rglob("SKILL.md"):
        skill_dir = skill_md.parent
        skill_name = skill_dir.name

        # Read frontmatter
        try:
            content = skill_md.read_text(encoding="utf-8", errors="replace")
            meta = {"name": skill_name, "path": str(skill_md)}
            meta.update(_parse_fm(content))
            skills[skill_name] = meta
        except Exception as exc:
            print(f"Warning: skill metadata parse error for {skill_name}: {exc}", file=sys.stderr)
            skills[skill_name] = {"name": skill_name, "path": str(skill_md)}

    return skills


def read_wiki_overrides(wiki_path: str) -> dict[str, dict]:
    """Read entity pages from the wiki for always_load/never_load overrides."""
    overrides = {}
    entities_dir = Path(wiki_path) / "entities" / "skills"

    if not entities_dir.exists():
        return overrides

    for page in entities_dir.glob("*.md"):
        try:
            content = page.read_text(encoding="utf-8", errors="replace")
            meta = _parse_fm(content)
            if not meta:
                continue

            skill_name = page.stem
            use_count_val = int(str(meta.get("use_count", "0")))
            overrides[skill_name] = {
                "always_load": str(meta.get("always_load", "false")).lower() == "true",
                "never_load": str(meta.get("never_load", "false")).lower() == "true",
                "last_used": str(meta.get("last_used", "")),
                "use_count": use_count_val,
                "status": str(meta.get("status", "unknown")),
            }
        except Exception as exc:
            print(f"Warning: wiki override parse error for {page.stem}: {exc}", file=sys.stderr)
            continue

    return overrides


# Stack-to-skill mapping (simplified version of skill-stack-matrix.md)
STACK_SKILL_MAP = {
    # Web frameworks
    "fastapi": ["fastapi"],
    "django": ["django"],
    "flask": ["flask"],
    "express": ["express"],
    "nestjs": ["nestjs"],
    "react": ["react", "frontend-design"],
    "nextjs": ["nextjs", "react", "frontend-design"],
    "vue": ["vue", "frontend-design"],
    "nuxt": ["nuxt", "vue", "frontend-design"],
    "angular": ["angular", "frontend-design"],
    "svelte": ["svelte", "frontend-design"],
    # AI/ML
    "langchain": ["langchain"],
    "llamaindex": ["llamaindex"],
    "crewai": ["crewai"],
    "pytorch": ["pytorch"],
    "tensorflow": ["tensorflow"],
    "huggingface": ["huggingface"],
    "openai-sdk": ["openai-sdk"],
    "anthropic-sdk": ["anthropic-sdk"],
    "mcp": ["mcp-dev"],
    # Infra
    "docker": ["docker"],
    "docker-compose": ["docker"],
    "kubernetes": ["kubernetes"],
    "terraform": ["terraform"],
    "github-actions": ["github-actions"],
    "gitlab-ci": ["gitlab-ci"],
    "aws-cdk": ["aws"],
    "vercel": ["vercel"],
    # Data
    "sqlalchemy": ["sqlalchemy"],
    "prisma": ["prisma"],
    "typeorm": ["typeorm"],
    "drizzle": ["drizzle"],
    "redis": ["redis"],
    "dbt": ["dbt"],
    # Testing
    "pytest": ["pytest"],
    "jest": ["jest"],
    "vitest": ["vitest"],
    "playwright": ["playwright"],
    "cypress": ["cypress"],
    # Docs
    "openapi": ["openapi"],
    "mkdocs": ["mkdocs"],
    "docusaurus": ["docusaurus"],
    # Build
    "vite": ["vite"],
    "webpack": ["webpack"],
    "turborepo": ["turborepo"],
}

# Skills that conflict
CONFLICTS = [
    ({"flask", "fastapi"}, "web framework"),
    ({"flask", "django"}, "web framework"),
    ({"jest", "vitest"}, "test runner"),
    ({"webpack", "vite"}, "bundler"),
]

# Priority base scores
PRIORITY_BASE = {
    "frontend-design": 8,
    "react": 7, "vue": 7, "angular": 7, "svelte": 7,
    "nextjs": 8, "nuxt": 8,
    "fastapi": 8, "django": 8, "flask": 7, "express": 8,
    "docker": 6, "kubernetes": 6, "terraform": 7,
    "langchain": 7, "llamaindex": 7,
    "pytest": 5, "jest": 5,
}


def resolve(profile: dict, available: dict, overrides: dict, max_skills: int = 15) -> dict:
    """Resolve stack profile to skill manifest."""
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_path": profile["repo_path"],
        "load": [],
        "unload": [],
        "mcp_servers": [],
        "plugins": [],
        "warnings": [],
        "suggestions": [],
    }

    # Collect all needed skill names from stack detections
    needed: dict[str, dict] = {}  # skill_name -> {reason, confidence, priority}

    all_detections = (
        [(d, "language") for d in profile.get("languages", [])] +
        [(d, "framework") for d in profile.get("frameworks", [])] +
        [(d, "infrastructure") for d in profile.get("infrastructure", [])] +
        [(d, "data_store") for d in profile.get("data_stores", [])] +
        [(d, "testing") for d in profile.get("testing", [])] +
        [(d, "ai_tooling") for d in profile.get("ai_tooling", [])] +
        [(d, "build") for d in profile.get("build_system", [])] +
        [(d, "docs") for d in profile.get("docs", [])]
    )

    for detection, category in all_detections:
        stack_id = detection["name"]
        confidence = detection.get("confidence", 0.5)
        evidence = detection.get("evidence", [])

        skill_names = STACK_SKILL_MAP.get(stack_id, [])
        for skill_name in skill_names:
            priority = PRIORITY_BASE.get(skill_name, 5)

            # Boost for high confidence
            if confidence >= 0.9:
                priority += 10

            # Boost if recently used (from wiki)
            if skill_name in overrides and overrides[skill_name].get("use_count", 0) > 0:
                priority += 5

            if skill_name not in needed or needed[skill_name]["priority"] < priority:
                needed[skill_name] = {
                    "reason": f"{stack_id} detected ({', '.join(evidence[:2])})",
                    "confidence": confidence,
                    "priority": priority,
                }

    # Apply wiki overrides
    for skill_name, override in overrides.items():
        if override.get("always_load"):
            if skill_name not in needed:
                needed[skill_name] = {
                    "reason": "User override: always_load",
                    "confidence": 1.0,
                    "priority": 20,
                }
            else:
                needed[skill_name]["priority"] += 20

        if override.get("never_load") and skill_name in needed:
            del needed[skill_name]

    # Resolve conflicts
    for conflict_set, conflict_type in CONFLICTS:
        in_needed = conflict_set & set(needed.keys())
        if len(in_needed) > 1:
            # Keep highest priority
            best = max(in_needed, key=lambda s: needed[s]["priority"])
            for s in in_needed:
                if s != best:
                    manifest["warnings"].append(
                        f"Conflict ({conflict_type}): {s} removed in favor of {best}"
                    )
                    del needed[s]

    # Check availability
    for skill_name, info in list(needed.items()):
        if skill_name not in available:
            manifest["suggestions"].append({
                "skill": skill_name,
                "reason": info["reason"],
                "install_from": f"marketplace:search/{skill_name}",
            })
            manifest["warnings"].append(
                f"{skill_name} needed but not installed ({info['reason']})"
            )
            del needed[skill_name]

    # Cap at max_skills
    sorted_needed = sorted(needed.items(), key=lambda x: -x[1]["priority"])
    if len(sorted_needed) > max_skills:
        manifest["warnings"].append(
            f"Capped at {max_skills} skills. {len(sorted_needed) - max_skills} lower-priority skills excluded."
        )
        sorted_needed = sorted_needed[:max_skills]

    # Build load list
    loaded_names = set()
    for skill_name, info in sorted_needed:
        skill_meta = available.get(skill_name, {})
        manifest["load"].append({
            "skill": skill_name,
            "path": skill_meta.get("path", f"/mnt/skills/unknown/{skill_name}/SKILL.md"),
            "reason": info["reason"],
            "priority": info["priority"],
        })
        loaded_names.add(skill_name)

    # Meta skills always loaded
    meta_skills = {"skill-router", "file-reading"}
    for ms in meta_skills:
        if ms not in loaded_names and ms in available:
            manifest["load"].append({
                "skill": ms,
                "path": available[ms].get("path", ""),
                "reason": "Meta skill (always loaded)",
                "priority": 99 if ms == "skill-router" else 50,
            })
            loaded_names.add(ms)

    # Build unload list
    for skill_name in available:
        if skill_name not in loaded_names:
            manifest["unload"].append({
                "skill": skill_name,
                "reason": "Not needed for detected stack",
            })

    # Sort load by priority descending
    manifest["load"].sort(key=lambda x: -x["priority"])

    return manifest


def read_intent_signals(intent_log_path: str) -> dict[str, int]:
    """Read today's intent signals from the intent log. Returns {signal: count}."""
    from datetime import datetime, timezone
    counts: dict[str, int] = {}
    log_path = Path(intent_log_path)
    if not log_path.exists():
        return counts

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("date", "") == today:
                        for sig in entry.get("signals", []):
                            counts[sig] = counts.get(sig, 0) + 1
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        print(f"Warning: failed to read intent signals: {exc}", file=sys.stderr)
    return counts


def apply_intent_boosts(needed: dict[str, dict], intent_signals: dict[str, int]) -> None:
    """Boost priority of skills that match today's intent signals (+5 per matching signal)."""
    for signal, count in intent_signals.items():
        # Map signal → skill names (reuse STACK_SKILL_MAP)
        skill_names = STACK_SKILL_MAP.get(signal, [signal])
        for skill_name in skill_names:
            if skill_name in needed:
                needed[skill_name]["priority"] += 5 * min(count, 3)  # cap boost at 15


def main():
    parser = argparse.ArgumentParser(description="Resolve stack profile to skill manifest")
    parser.add_argument("--profile", required=True, help="Path to stack-profile.json")
    parser.add_argument("--wiki", default=os.path.expanduser("~/.claude/skill-wiki"), help="Wiki path")
    parser.add_argument("--available-skills", default=os.path.expanduser("~/.claude/skills"), help="Skills directory")
    parser.add_argument("--output", default=os.path.expanduser("~/.claude/skill-manifest.json"), help="Output manifest path")
    parser.add_argument("--max-skills", type=int, default=15, help="Max simultaneous skills")
    parser.add_argument("--intent-log", default=os.path.expanduser("~/.claude/intent-log.jsonl"), help="Intent log path for mid-session signal boosts")
    parser.add_argument("--pending-output", default="", help="If set, also write to this path (for mid-session re-runs)")
    args = parser.parse_args()

    # Load profile
    with open(args.profile) as f:
        profile = json.load(f)

    # Discover available skills (from registered dirs if registry exists)
    registry_path = Path(os.path.expanduser("~/.claude/skill-registry.json"))
    skill_dirs = [args.available_skills]
    if registry_path.exists():
        try:
            registry = json.loads(registry_path.read_text())
            skill_dirs = registry.get("skill_dirs", skill_dirs)
        except Exception as exc:
            print(f"Warning: failed to read skill registry: {exc}", file=sys.stderr)

    available: dict[str, Any] = {}
    for d in skill_dirs:
        available.update(discover_available_skills(d))
    print(f"Found {len(available)} available skills across {len(skill_dirs)} dirs")

    # Read wiki overrides
    overrides = read_wiki_overrides(args.wiki)
    print(f"Found {len(overrides)} wiki overrides")

    # Read intent signals (mid-session boosts)
    intent_signals = read_intent_signals(args.intent_log)
    if intent_signals:
        print(f"Intent signals today: {dict(list(intent_signals.items())[:5])} ...")

    # Resolve
    manifest = resolve(profile, available, overrides, args.max_skills)

    # Apply intent boosts post-resolve (reopen needed dict isn't accessible — re-resolve with boosts)
    # Simple approach: if intent signals point to skills NOT in load list, add suggestions
    loaded_names = {e["skill"] for e in manifest["load"]}
    for signal, count in intent_signals.items():
        skill_names = STACK_SKILL_MAP.get(signal, [])
        for skill_name in skill_names:
            if skill_name not in loaded_names and skill_name in available:
                manifest["suggestions"].append({
                    "skill": skill_name,
                    "reason": f"Intent signal '{signal}' detected {count}x today",
                    "install_from": available[skill_name].get("path", "local"),
                })

    # Write manifest
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # Also write to pending-output if requested (mid-session re-run)
    if args.pending_output:
        Path(args.pending_output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.pending_output, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

    print(f"\nManifest: {len(manifest['load'])} to load, {len(manifest['unload'])} to unload")
    print(f"Warnings: {len(manifest['warnings'])}")
    print(f"Suggestions: {len(manifest['suggestions'])}")
    print(f"Saved to {args.output}")


if __name__ == "__main__":
    main()
