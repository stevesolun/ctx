#!/usr/bin/env python3
"""
context_monitor.py -- PostToolUse hook: extract intent signals, update intent log.

Called by Claude Code PostToolUse hook:
    python context_monitor.py --tool <tool_name> [--input <json_string>]

Runs in <200ms. Appends to ~/.claude/intent-log.jsonl.
If >=3 unmatched signals detected, writes ~/.claude/pending-skills.json.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically via temp file + os.replace(). Safe under concurrent hook invocations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

try:
    from ctx_config import cfg as _cfg
    CLAUDE_DIR = _cfg.claude_dir
    INTENT_LOG = _cfg.intent_log
    PENDING_SKILLS = _cfg.pending_skills
    MANIFEST_PATH = _cfg.skill_manifest
    _THRESHOLD = _cfg.unmatched_signal_threshold
except ImportError:
    CLAUDE_DIR = Path(os.path.expanduser("~/.claude"))
    INTENT_LOG = CLAUDE_DIR / "intent-log.jsonl"
    PENDING_SKILLS = CLAUDE_DIR / "pending-skills.json"
    MANIFEST_PATH = CLAUDE_DIR / "skill-manifest.json"
    _THRESHOLD = 3

# Keyword → stack signal mapping
KEYWORD_SIGNALS: dict[str, str] = {
    "docker": "docker",
    "dockerfile": "docker",
    "docker-compose": "docker",
    "kubernetes": "kubernetes",
    "kubectl": "kubernetes",
    "k8s": "kubernetes",
    "helm": "kubernetes",
    "terraform": "terraform",
    ".tf": "terraform",
    "react": "react",
    "jsx": "react",
    "tsx": "react",
    "vue": "vue",
    "angular": "angular",
    "svelte": "svelte",
    "nextjs": "nextjs",
    "next.js": "nextjs",
    "nuxt": "nuxt",
    "fastapi": "fastapi",
    "django": "django",
    "flask": "flask",
    "express": "express",
    "nestjs": "nestjs",
    "rails": "rails",
    "langchain": "langchain",
    "llamaindex": "llamaindex",
    "crewai": "crewai",
    "pytorch": "pytorch",
    "tensorflow": "tensorflow",
    "huggingface": "huggingface",
    "anthropic": "anthropic-sdk",
    "openai": "openai-sdk",
    "mcp": "mcp",
    "pytest": "pytest",
    "jest": "jest",
    "vitest": "vitest",
    "playwright": "playwright",
    "cypress": "cypress",
    "prisma": "prisma",
    "sqlalchemy": "sqlalchemy",
    "typeorm": "typeorm",
    "drizzle": "drizzle",
    "redis": "redis",
    "dbt": "dbt",
    "airflow": "airflow",
    "kafka": "kafka",
    "graphql": "graphql",
    "openapi": "openapi",
    "swagger": "openapi",
    "turborepo": "turborepo",
    "vite": "vite",
    "webpack": "webpack",
    "github actions": "github-actions",
    "github-actions": "github-actions",
    "gitlab-ci": "gitlab-ci",
    "vercel": "vercel",
}

# File extension → stack signal
EXTENSION_SIGNALS: dict[str, str] = {
    ".tsx": "react",
    ".jsx": "react",
    ".vue": "vue",
    ".tf": "terraform",
    ".go": "golang",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "rails",
    ".ex": "elixir",
    ".exs": "elixir",
}


def extract_signals(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """Extract stack signal names from tool name and input. Fast, no I/O."""
    signals: set[str] = set()

    # Serialize input to searchable text
    raw = json.dumps(tool_input).lower()

    for keyword, stack in KEYWORD_SIGNALS.items():
        if keyword.lower() in raw:
            signals.add(stack)

    # Check file extensions in any string values
    for ext, stack in EXTENSION_SIGNALS.items():
        if ext in raw:
            signals.add(stack)

    # Tool-level signals
    if tool_name in ("Bash",):
        # Bash commands reveal intent directly
        cmd = raw
        if "pip install" in cmd or "poetry add" in cmd or "uv add" in cmd:
            signals.add("python")
        if "npm install" in cmd or "yarn add" in cmd or "pnpm add" in cmd:
            signals.add("javascript")

    return sorted(signals)


def load_manifest_skills() -> set[str]:
    """Return set of currently loaded skill names from manifest."""
    if not MANIFEST_PATH.exists():
        return set()
    try:
        with open(MANIFEST_PATH) as f:
            manifest = json.load(f)
        return {entry["skill"] for entry in manifest.get("load", [])}
    except Exception as exc:
        print(f"Warning: failed to load manifest skills: {exc}", file=sys.stderr)
        return set()


def append_intent_log(entry: dict[str, Any]) -> None:
    """Append a single JSON line to the intent log."""
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    with open(INTENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def count_recent_unmatched(signals: list[str], loaded_skills: set[str]) -> list[str]:
    """
    Compare today's signals against loaded skills.
    Returns list of unmatched signal stacks.
    """
    # Simple heuristic: signal name often matches skill name directly
    unmatched = [s for s in signals if s not in loaded_skills]
    return unmatched


def graph_suggest(unmatched_tags: list[str]) -> list[dict]:
    """Use the knowledge graph to suggest skills/agents for unmatched signals.

    Scoring: name match (50pts) > tag overlap (10pts/tag) > degree (tiebreak).
    This ensures 'fastapi-pro' ranks above 'prompt-optimizer' for a 'fastapi' signal.
    """
    graph_path = Path(os.path.expanduser("~/.claude/skill-wiki/graphify-out/graph.json"))
    if not graph_path.exists():
        return []
    try:
        from networkx.readwrite import node_link_graph
        import math
        with open(graph_path, encoding="utf-8") as f:
            G = node_link_graph(json.load(f))
        tag_set = set(unmatched_tags)
        scores: dict[str, float] = {}
        for nid, data in G.nodes(data=True):
            label = data.get("label", nid.split(":", 1)[-1]).lower()
            node_tags = set(data.get("tags", []))
            tag_overlap = tag_set & node_tags

            score = 0.0
            # Name-match bonus: if signal appears in the skill/agent name
            for signal in unmatched_tags:
                if signal.lower() in label:
                    score += 50.0
            # Tag overlap
            score += len(tag_overlap) * 10.0
            # Degree tiebreak (small)
            if score > 0:
                score += math.log1p(G.degree(nid))
                scores[nid] = score

        ranked = sorted(scores.items(), key=lambda x: -x[1])[:8]
        return [
            {
                "name": G.nodes[nid].get("label", nid.split(":", 1)[-1]),
                "type": G.nodes[nid].get("type", "skill"),
                "score": round(sc, 1),
                "matching_tags": sorted(tag_set & set(G.nodes[nid].get("tags", []))),
            }
            for nid, sc in ranked
        ]
    except Exception as exc:
        print(f"Warning: graph suggest error: {exc}", file=sys.stderr)
        return []


def write_pending_skills(unmatched: list[str]) -> None:
    """Write pending skill suggestions enriched with graph-based discovery."""
    graph_suggestions = graph_suggest(unmatched)
    suggestion_names = [s["name"] for s in graph_suggestions[:5]]
    suggestion_text = (
        f"Detected {len(unmatched)} stack signals not covered by loaded skills: {', '.join(unmatched)}"
    )
    if suggestion_names:
        suggestion_text += f". Graph suggests: {', '.join(suggestion_names)}"

    pending = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "context-monitor",
        "unmatched_signals": unmatched,
        "suggestion": suggestion_text,
        "graph_suggestions": graph_suggestions,
    }
    _atomic_write_text(PENDING_SKILLS, json.dumps(pending, indent=2))


def load_recent_unmatched_count() -> int:
    """Count distinct unmatched signals in today's intent log."""
    if not INTENT_LOG.exists():
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    unmatched: set[str] = set()
    try:
        with open(INTENT_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("date", "")[:10] == today:
                        unmatched.update(entry.get("unmatched", []))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        print(f"Warning: failed to load recent unmatched: {exc}", file=sys.stderr)
    return len(unmatched)


def main() -> None:
    parser = argparse.ArgumentParser(description="PostToolUse intent signal extractor")
    parser.add_argument("--tool", default="unknown", help="Tool name from hook")
    parser.add_argument("--input", default="{}", help="JSON-encoded tool input")
    args = parser.parse_args()

    # Parse tool input safely
    try:
        tool_input = json.loads(args.input)
    except json.JSONDecodeError:
        tool_input = {"raw": args.input}

    signals = extract_signals(args.tool, tool_input)
    if not signals:
        sys.exit(0)  # Nothing to log

    loaded_skills = load_manifest_skills()
    unmatched = count_recent_unmatched(signals, loaded_skills)

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "tool": args.tool,
        "signals": signals,
        "unmatched": unmatched,
    }
    append_intent_log(entry)

    # If cumulative unmatched today hits threshold → write pending-skills
    THRESHOLD = _THRESHOLD
    if len(unmatched) >= THRESHOLD:
        # Collect all today's unmatched for a richer suggestion
        all_unmatched: set[str] = set(unmatched)
        if INTENT_LOG.exists():
            today = entry["date"]
            try:
                with open(INTENT_LOG, encoding="utf-8") as f:
                    for line in f:
                        try:
                            e = json.loads(line.strip())
                            if e.get("date", "") == today:
                                all_unmatched.update(e.get("unmatched", []))
                        except json.JSONDecodeError:
                            continue
            except Exception as exc:
                print(f"Warning: failed to collect today's unmatched: {exc}", file=sys.stderr)
        write_pending_skills(sorted(all_unmatched))


if __name__ == "__main__":
    main()
