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
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctx.core.wiki.wiki_utils import parse_frontmatter as _parse_fm

# Graph-walk augmentation. Lazy-imported so the module still works when the
# graph artifacts haven't been built yet — resolve_by_seeds() degrades to an
# empty list if the graph has zero nodes.
try:
    from ctx.core.graph.resolve_graph import (
        load_graph as _load_graph,
        resolve_by_seeds as _resolve_by_seeds,
    )
    _GRAPH_AVAILABLE = True
except ImportError:
    _GRAPH_AVAILABLE = False

try:
    from ctx_config import cfg as _cfg
    _WIKI_DEFAULT = str(_cfg.wiki_dir)
    _SKILLS_DEFAULT = str(_cfg.skills_dir)
    _MANIFEST_DEFAULT = str(_cfg.skill_manifest)
    _INTENT_LOG_DEFAULT = str(_cfg.intent_log)
    _REGISTRY_DEFAULT = _cfg.skill_registry
    _MAX_SKILLS_DEFAULT = _cfg.max_skills
except ImportError:
    _WIKI_DEFAULT = os.path.expanduser("~/.claude/skill-wiki")
    _SKILLS_DEFAULT = os.path.expanduser("~/.claude/skills")
    _MANIFEST_DEFAULT = os.path.expanduser("~/.claude/skill-manifest.json")
    _INTENT_LOG_DEFAULT = os.path.expanduser("~/.claude/intent-log.jsonl")
    _REGISTRY_DEFAULT = Path(os.path.expanduser("~/.claude/skill-registry.json"))
    _MAX_SKILLS_DEFAULT = 15


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


# Stack-to-skill mapping lives in ``stack_skill_map`` as the single
# source of truth shared with ``usage_tracker.SIGNAL_SKILL_MAP``.
# Pre-P2.4 each module had its own copy; the usage_tracker one was a
# 20-entry subset that caused use_count to never increment for skills
# in stacks like angular/django/docker/cypress/dbt — lifecycle then
# spuriously flagged them as stale. Code-reviewer HIGH, consolidated.
from ctx.core.resolve.stack_skill_map import STACK_SKILL_MAP  # noqa: E402

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

# Intent-boost tuning: priority bump per matching signal, capped by repeat count.
# Effective max boost = INTENT_BOOST_PER_SIGNAL * INTENT_BOOST_COUNT_CAP.
INTENT_BOOST_PER_SIGNAL = 5
INTENT_BOOST_COUNT_CAP = 3


def resolve(
    profile: dict,
    available: dict,
    overrides: dict,
    max_skills: int = 15,
    intent_signals: dict[str, int] | None = None,
) -> dict:
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

    # ── Fuzzy installed-skill match for unresolved detections ────────
    # STACK_SKILL_MAP lists skills by short canonical names (e.g. "fastapi"),
    # but marketplace skills use suffixed slugs (e.g. "fastapi-pro",
    # "python-fastapi-development"). If a matrix hit is unresolved, pick
    # any installed skill whose name contains the detection id — otherwise
    # the graph walk below has zero seeds and the recommendation engine
    # silently no-ops on a well-known stack.
    detection_ids = {d["name"].lower() for d, _ in all_detections}
    for det_id in list(detection_ids):
        if det_id in available:
            continue  # already a direct hit — nothing to do
        fuzzy_matches = [
            s for s in available
            if det_id in s.lower()
        ]
        for match in fuzzy_matches[:3]:  # cap to avoid flood
            if match not in needed:
                needed[match] = {
                    "reason": f"fuzzy match for detected stack '{det_id}'",
                    "confidence": 0.5,
                    "priority": PRIORITY_BASE.get(match, 5) + 2,
                }

    # ── Graph-walk augmentation ──────────────────────────────────────
    # Seed the graph with skills the (matrix or fuzzy) step matched, then
    # walk 1-2 hops and add high-scoring neighbors that are also installed.
    # This is what makes the 642K-edge knowledge graph load-bearing on the
    # recommendation path — without it, resolve_skills is a static dict.
    #
    # Quiet on empty graph: resolve_by_seeds() returns [] if the graph
    # hasn't been built yet, so this is safe on a fresh install.
    if _GRAPH_AVAILABLE and needed:
        try:
            G = _load_graph()
            if G.number_of_nodes() > 0:
                # top_n=30 gives MCPs (sparse type) room to surface
                # past the denser skill hits at equal scores. Downstream
                # noise floor + availability filter keep the final
                # manifest tight regardless of pool size.
                graph_hits = _resolve_by_seeds(
                    G,
                    list(needed.keys()),
                    max_hops=1,
                    top_n=30,
                    exclude_seeds=True,
                )
                # Percentile noise floor. Pre-P2.5 this used absolute
                # score thresholds (1.5 for skills / 1.0 for MCPs)
                # calibrated to the v0.6 integer-weight graph. On the
                # v0.7 blended float-weight graph, per-edge weight is
                # <=1.0, so absolute thresholds either drop nearly
                # everything (on sparse/fixture graphs) or
                # underweight popular hits (on the real 13k-node
                # graph where ``docker`` accumulates scores of ~300).
                # Normalised [0,1] thresholds are scale-invariant.
                # MCPs keep a slightly lower floor because the type
                # is historically sparser; a single strong link is
                # still signal, not noise.
                _SKILL_NOISE_FLOOR = 0.30     # 30% of top hit's score
                _MCP_NOISE_FLOOR   = 0.20     # 20% (MCPs sparser)
                for hit in graph_hits:
                    name = hit["name"]
                    hit_type = hit.get("type", "skill")
                    # Use normalized_score when present (new schema);
                    # fall through to raw ``score`` for older graphs
                    # that predate the normalised field.
                    score = float(
                        hit.get("normalized_score")
                        if "normalized_score" in hit
                        else hit.get("score", 0.0)
                    )
                    floor = (
                        _MCP_NOISE_FLOOR if hit_type == "mcp-server"
                        else _SKILL_NOISE_FLOOR
                    )
                    if score < floor:
                        continue

                    via = ", ".join(hit.get("via", [])[:2]) or "graph"
                    shared = hit.get("shared_tags", [])[:3]
                    shared_str = f" via shared tags {shared}" if shared else ""
                    reason = f"graph neighbor of {via}{shared_str}"

                    # Phase 5: MCP servers land in their own manifest
                    # bucket. Users don't "load" MCPs the way they load
                    # skills; they see the recommendation and opt into
                    # registering the server in ~/.claude/mcp.json
                    # themselves (ctx-mcp-install will automate this
                    # in Phase 6+). We still surface them here so the
                    # monitor/dashboard has something to show.
                    if hit_type == "mcp-server":
                        if any(m.get("name") == name for m in manifest["mcp_servers"]):
                            continue  # already listed
                        manifest["mcp_servers"].append({
                            "name": name,
                            "reason": reason,
                            "score": score,
                            "via": hit.get("via", [])[:4],
                            "shared_tags": shared,
                        })
                        continue

                    # skill / agent path — existing behaviour unchanged.
                    if name in needed or name not in available:
                        continue
                    priority = 3 + min(int(score), 12)
                    needed[name] = {
                        "reason": reason,
                        "confidence": min(0.6 + score / 20.0, 0.95),
                        "priority": priority,
                    }
        except Exception as exc:  # noqa: BLE001 — graph is advisory
            manifest["warnings"].append(
                f"graph walk skipped: {type(exc).__name__}: {exc}"
            )

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

    # Apply mid-session intent boosts BEFORE conflict resolution and capping,
    # so intent can influence which skill wins a conflict and which skills
    # survive the max_skills cut-off.
    if intent_signals:
        apply_intent_boosts(needed, intent_signals, available, manifest)

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


def apply_intent_boosts(
    needed: dict[str, dict],
    intent_signals: dict[str, int],
    available: dict[str, dict],
    manifest: dict,
) -> None:
    """Apply today's intent signals to the resolve state.

    For each signal, each mapped skill is handled as follows:
      - If already in `needed`: its priority is boosted by
        INTENT_BOOST_PER_SIGNAL * min(count, INTENT_BOOST_COUNT_CAP).
      - Else if installed (in `available`) but not needed: it is appended to
        `manifest["suggestions"]` so downstream tooling can surface it.

    Mutates `needed` and `manifest` in place.
    """
    for signal, count in intent_signals.items():
        skill_names = STACK_SKILL_MAP.get(signal, [])
        boost = INTENT_BOOST_PER_SIGNAL * min(count, INTENT_BOOST_COUNT_CAP)
        for skill_name in skill_names:
            if skill_name in needed:
                needed[skill_name]["priority"] += boost
            elif skill_name in available:
                manifest["suggestions"].append({
                    "skill": skill_name,
                    "reason": f"Intent signal '{signal}' detected {count}x today",
                    "install_from": available[skill_name].get("path", "local"),
                })


def main():
    parser = argparse.ArgumentParser(description="Resolve stack profile to skill manifest")
    parser.add_argument("--profile", required=True, help="Path to stack-profile.json")
    parser.add_argument("--wiki", default=_WIKI_DEFAULT, help="Wiki path")
    parser.add_argument("--available-skills", default=_SKILLS_DEFAULT, help="Skills directory")
    parser.add_argument("--output", default=_MANIFEST_DEFAULT, help="Output manifest path")
    parser.add_argument("--max-skills", type=int, default=_MAX_SKILLS_DEFAULT, help="Max simultaneous skills")
    parser.add_argument("--intent-log", default=_INTENT_LOG_DEFAULT, help="Intent log path for mid-session signal boosts")
    parser.add_argument("--pending-output", default="", help="If set, also write to this path (for mid-session re-runs)")
    args = parser.parse_args()

    # Load profile
    with open(args.profile) as f:
        profile = json.load(f)

    # Discover available skills (from registered dirs if registry exists)
    registry_path = Path(_REGISTRY_DEFAULT)
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

    # Resolve (intent_signals flow through resolve → apply_intent_boosts)
    manifest = resolve(profile, available, overrides, args.max_skills, intent_signals)

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
