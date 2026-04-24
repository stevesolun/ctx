#!/usr/bin/env python3
"""
agent_add.py -- Add new agents with intake gate + wiki ingestion.

Mirror of ``skill_add`` for the agent subject type. Agents live as flat
``.md`` files under ``~/.claude/agents/`` (unlike skills, which each get
their own directory). The flow is therefore shorter:

    validate name -> read content -> intake gate -> copy file ->
    record embedding -> write wiki entity page -> log

Usage
-----
    # Single agent
    python agent_add.py --agent-path /path/to/agent.md --name my-agent \
        --wiki ~/.claude/skill-wiki --agents-dir ~/.claude/agents

    # Batch from directory (every *.md at depth 1 is treated as an agent)
    python agent_add.py --scan-dir /path/to/new-agents/ \
        --wiki ~/.claude/skill-wiki --agents-dir ~/.claude/agents
"""

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from ctx_config import cfg
from intake_pipeline import IntakeRejected, check_intake, record_embedding
from wiki_batch_entities import generate_agent_page
from ctx.core.wiki.wiki_sync import append_log, ensure_wiki, update_index
from ctx.core.wiki.wiki_utils import validate_skill_name

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Match skill_add's ceiling — agents are prose, 1 MB is absurdly generous.
_MAX_AGENT_BYTES = 1_048_576


def install_agent(source: Path, agents_dir: Path, name: str) -> Path:
    """Copy agent file into ``agents_dir/<name>.md``.

    Unlike :func:`skill_add.install_skill`, agents are flat single-file
    entries — no per-agent subdirectory.
    """
    agents_dir.mkdir(parents=True, exist_ok=True)
    dest = agents_dir / f"{name}.md"
    shutil.copy2(source, dest)
    return dest


def write_entity_page(wiki_path: Path, name: str, content: str) -> bool:
    """Write agent entity page. Returns True if newly created."""
    entities_dir = wiki_path / "entities" / "agents"
    entities_dir.mkdir(parents=True, exist_ok=True)
    page = entities_dir / f"{name}.md"
    is_new = not page.exists()
    page.write_text(content, encoding="utf-8")
    return is_new


def add_agent(
    *,
    source_path: Path,
    name: str,
    wiki_path: Path,
    agents_dir: Path,
) -> dict:
    """Add a single agent: validate, gate, install, ingest, log.

    Returns a result dict with keys: name, installed, is_new_page.
    """
    validate_skill_name(name)

    file_size = source_path.stat().st_size
    if file_size > _MAX_AGENT_BYTES:
        raise ValueError(
            f"agent file too large ({file_size:,} bytes). Max "
            f"{_MAX_AGENT_BYTES:,}. Trim before ingestion."
        )

    content = source_path.read_text(encoding="utf-8", errors="replace")
    line_count = len(content.splitlines())

    # Intake gate: reject broken/duplicate agents before we install.
    decision = check_intake(content, "agents")
    if not decision.allow:
        raise IntakeRejected(decision)

    # 1. Install into agents-dir.
    installed_path = install_agent(source_path, agents_dir, name)

    # 2. Record embedding. Non-fatal on failure — install already
    # succeeded and a missing vector only weakens the next check.
    try:
        record_embedding(subject_id=name, raw_md=content, subject_type="agents")
    except Exception as exc:  # noqa: BLE001 — cache failure must not break install
        print(
            f"Warning: failed to record intake embedding for {name}: {exc}",
            file=sys.stderr,
        )

    # 3. Write wiki entity page via the shared generator so the agent
    # page layout stays consistent with wiki_batch_entities output.
    page_content = generate_agent_page(name, installed_path)
    is_new = write_entity_page(wiki_path, name, page_content)

    # 4. Index + log.
    if is_new:
        update_index(str(wiki_path), [name], subject_type="agents")

    log_details = [
        f"Source: {source_path}",
        f"Installed: {installed_path}",
        f"Lines: {line_count}",
    ]
    warnings = decision.warnings
    if warnings:
        log_details.append(
            "Warnings: " + "; ".join(f"{w.code}:{w.message}" for w in warnings)
        )
    append_log(str(wiki_path), "add-agent", name, log_details)

    return {
        "name": name,
        "installed": str(installed_path),
        "is_new_page": is_new,
    }


# ── CLI ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Add new agents with wiki ingestion")
    parser.add_argument("--agent-path", help="Path to a single agent .md to add")
    parser.add_argument("--name", help="Agent name (required with --agent-path)")
    parser.add_argument(
        "--scan-dir",
        help="Directory of agent .md files to batch-add (non-recursive)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip agents already installed (prevents overwrites)",
    )
    parser.add_argument("--wiki", default=str(cfg.wiki_dir), help="Wiki path")
    parser.add_argument(
        "--agents-dir",
        default=str(cfg.agents_dir),
        help="Agents install path",
    )
    args = parser.parse_args()

    wiki_path = Path(os.path.expanduser(args.wiki))
    agents_dir = Path(os.path.expanduser(args.agents_dir))

    ensure_wiki(str(wiki_path))
    agents_dir.mkdir(parents=True, exist_ok=True)

    if args.agent_path and args.scan_dir:
        print("Error: use --agent-path or --scan-dir, not both.", file=sys.stderr)
        sys.exit(1)

    if not args.agent_path and not args.scan_dir:
        print("Error: --agent-path or --scan-dir is required.", file=sys.stderr)
        sys.exit(1)

    candidates: list[tuple[Path, str]] = []

    if args.agent_path:
        if not args.name:
            print("Error: --name is required with --agent-path.", file=sys.stderr)
            sys.exit(1)
        source = Path(os.path.expanduser(args.agent_path))
        if not source.exists():
            print(f"Error: {source} does not exist.", file=sys.stderr)
            sys.exit(1)
        candidates.append((source, args.name))

    if args.scan_dir:
        scan_root = Path(os.path.expanduser(args.scan_dir))
        if not scan_root.exists():
            print(f"Error: {scan_root} does not exist.", file=sys.stderr)
            sys.exit(1)
        # Non-recursive: only top-level *.md files. Nested agents are
        # rare enough that forcing an explicit path is safer than
        # surprise recursion sweeping up unrelated markdown.
        for agent_md in sorted(scan_root.glob("*.md")):
            candidates.append((agent_md, agent_md.stem))

        if not candidates:
            print(f"No agent .md files found under {scan_root}.", file=sys.stderr)
            sys.exit(0)

    added = skipped = rejected = errors = 0
    total = len(candidates)
    for i, (source_path, name) in enumerate(candidates, 1):
        if args.skip_existing and (agents_dir / f"{name}.md").exists():
            skipped += 1
            if skipped <= 5 or skipped % 100 == 0:
                print(f"  [{i}/{total}] [skipped] {name}")
            continue
        try:
            add_agent(
                source_path=source_path,
                name=name,
                wiki_path=wiki_path,
                agents_dir=agents_dir,
            )
            added += 1
            print(f"  [{i}/{total}] [installed] {name}")
        except IntakeRejected as exc:
            rejected += 1
            codes = ", ".join(f.code for f in exc.decision.failures) or "unknown"
            print(f"  [{i}/{total}] [rejected] {name}: {codes}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — batch CLI must continue past one failure
            errors += 1
            print(f"  [{i}/{total}] ERROR: {name}: {exc}", file=sys.stderr)

    print(
        f"\nDone: {added} added, {skipped} skipped, "
        f"{rejected} rejected, {errors} errors"
    )


if __name__ == "__main__":
    main()
