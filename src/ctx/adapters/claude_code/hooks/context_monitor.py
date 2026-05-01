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
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctx.utils._fs_utils import atomic_write_text as _atomic_write_text

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
    # Payment / fintech
    "stripe": "stripe",
    "paymentintent": "stripe",
    "setupintent": "stripe",
    "paypal": "paypal",
    "braintree": "braintree",
    "plaid": "plaid",
    "pci": "pci-compliance",
    "payment": "payment-integration",
    # Postgres signals beyond just "postgres"
    "postgres": "postgres",
    "postgresql": "postgres",
    "psycopg": "postgres",
    "psycopg2": "postgres",
    "asyncpg": "postgres",
    # MongoDB
    "mongo": "mongodb",
    "mongodb": "mongodb",
    "pymongo": "mongodb",
    "motor": "mongodb",
    # Data validation commonly paired with FastAPI
    "pydantic": "pydantic",
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


def graph_suggest(
    unmatched_tags: list[str], *, top_k: int | None = None,
) -> list[dict]:
    """Walk the knowledge graph for a BUNDLE recommendation across all 3 types.

    Returns up to ``top_k`` results ranked by relevance, mixed across
    skills, agents, and MCP servers — whatever scores highest wins,
    regardless of entity type. A bundle can therefore be:
      - all three types (e.g. python-pro skill + code-reviewer agent +
        anthropic-python-sdk MCP)
      - two types
      - just skills, or just agents, or just MCPs
    It's dynamic, driven by graph affinity to the current signals.

    Scoring: name match (50pts) > tag overlap (10pts/tag) > degree
    (tiebreak). 'fastapi-pro' ranks above 'prompt-optimizer' for a
    'fastapi' signal.

    ``top_k`` defaults to ``cfg.recommendation_top_k`` (config default 5)
    so the user doesn't get overwhelmed. The full ranked list beyond
    top_k is dropped — we don't store it anywhere.
    """
    if top_k is None:
        # Local import: the config module imports os / sys / pathlib
        # lazily and we want context_monitor to stay importable in a
        # minimal-env test where the full config chain isn't wired.
        try:
            from ctx_config import cfg  # noqa: PLC0415
            top_k = int(cfg.recommendation_top_k)
        except Exception:
            top_k = 5  # matches the config default
    if top_k < 1:
        top_k = 1
    graph_path = CLAUDE_DIR / "skill-wiki" / "graphify-out" / "graph.json"
    if not graph_path.exists():
        return []
    try:
        from ctx.core.graph.resolve_graph import load_graph  # noqa: PLC0415
        from ctx.core.resolve.recommendations import recommend_by_tags  # noqa: PLC0415
        graph = load_graph(graph_path)
        if graph.number_of_nodes() == 0:
            return []
        return recommend_by_tags(
            graph,
            unmatched_tags,
            top_n=top_k,
            entity_types=("skill", "agent", "mcp-server"),
        )
    except Exception as exc:
        print(f"Warning: graph suggest error: {exc}", file=sys.stderr)
        return []


def write_pending_skills(unmatched: list[str]) -> None:
    """Write pending bundle suggestions enriched with graph-based discovery.

    The bundle may span all three entity types (skill/agent/mcp-server)
    or any subset — the graph score is what ranks, not the type.
    """
    graph_suggestions = graph_suggest(unmatched)  # already top-K capped
    suggestion_names = [s["name"] for s in graph_suggestions]
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


def _parse_stdin_payload() -> tuple[str, dict[str, Any]]:
    """Read the PostToolUse JSON payload from stdin.

    Claude Code delivers the payload on stdin when a hook is invoked without
    $CLAUDE_TOOL_NAME / $CLAUDE_TOOL_INPUT interpolation in the command string.
    Returns (tool_name, tool_input).
    """
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return "unknown", {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return "unknown", {}
        tool_name = str(data.get("tool_name") or "unknown")
        tool_input = data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        return tool_name, tool_input
    except (json.JSONDecodeError, OSError):
        return "unknown", {}


def main() -> None:
    parser = argparse.ArgumentParser(description="PostToolUse intent signal extractor")
    parser.add_argument("--tool", default="unknown", help="Tool name from hook")
    parser.add_argument("--input", default="{}", help="JSON-encoded tool input")
    parser.add_argument(
        "--from-stdin",
        action="store_true",
        help="Read tool_name and tool_input from the JSON payload on stdin "
             "(safe alternative to --tool/--input that avoids shell injection)",
    )
    args = parser.parse_args()

    if args.from_stdin:
        tool_name, tool_input = _parse_stdin_payload()
    else:
        tool_name = args.tool
        # Parse tool input safely
        try:
            tool_input = json.loads(args.input)
        except json.JSONDecodeError:
            tool_input = {"raw": args.input}

    signals = extract_signals(tool_name, tool_input)
    if not signals:
        sys.exit(0)  # Nothing to log

    loaded_skills = load_manifest_skills()
    unmatched = count_recent_unmatched(signals, loaded_skills)

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "tool": tool_name,
        "signals": signals,
        "unmatched": unmatched,
    }
    append_intent_log(entry)

    # Cumulative check: collect every unique unmatched signal in today's
    # intent log (already written above, so this invocation's unmatched
    # is included) and gate on THAT count, not on the single-invocation
    # ``unmatched`` list.
    #
    # Prior impl checked ``len(unmatched) >= THRESHOLD`` — which required
    # a single tool call to surface THRESHOLD=3 unmatched signals by
    # itself. In practice Read/Edit tool calls surface 1 signal each,
    # so the gate never fired and pending-skills.json was almost never
    # written. That silently killed the suggestion arm of the alive
    # loop: context_monitor → skill_suggest → skill_loader never
    # triggered. Code-reviewer BLOCKER, fixed here.
    THRESHOLD = _THRESHOLD
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

    if len(all_unmatched) >= THRESHOLD:
        write_pending_skills(sorted(all_unmatched))


if __name__ == "__main__":
    main()
