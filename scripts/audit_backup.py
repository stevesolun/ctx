"""audit_backup.py -- Summarize a backup snapshot and flag what is missing."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
BACKUPS = CLAUDE_HOME / "backups"


def latest_snapshot() -> Path:
    snaps = sorted(p for p in BACKUPS.iterdir() if p.is_dir())
    return snaps[-1]


def main() -> int:
    snap = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_snapshot()
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))

    print(f"snapshot:  {snap}")
    print(f"entries:   {len(manifest['entries'])}")
    total = sum((e.get("size") or 0) for e in manifest["entries"])
    print(f"bytes:     {total:,}")

    print("\n--- Top-level files ---")
    for e in manifest["entries"]:
        d = e["dest"]
        if "/" not in d and "\\" not in d:
            print(f"  {d:<32} {e['size']:>8}B")

    print("\n--- Tree coverage ---")
    roots: Counter[str] = Counter()
    for e in manifest["entries"]:
        head = e["dest"].replace("\\", "/").split("/", 1)[0]
        roots[head] += 1
    for r, c in roots.most_common():
        print(f"  {r:<32} {c:>5} files")

    # Audit what's in ~/.claude but NOT in the backup.
    print("\n--- Candidate files in ~/.claude NOT backed up ---")
    backed_up_sources = {Path(e["source"]).resolve() for e in manifest["entries"]}
    candidates = [
        CLAUDE_HOME / "skill-system-config.json",
        CLAUDE_HOME / "intent-log.jsonl",
        CLAUDE_HOME / "skill-registry.json",
        CLAUDE_HOME / "CLAUDE.md",
        CLAUDE_HOME / "claude.json",
        CLAUDE_HOME / ".claude.json",
    ]
    for c in candidates:
        if c.is_file() and c.resolve() not in backed_up_sources:
            print(f"  MISSING: {c} ({c.stat().st_size}B)")
        elif c.is_file():
            print(f"  OK:      {c}")
        else:
            print(f"  absent:  {c}")

    # Also check common config locations.
    cfg_glob = list((CLAUDE_HOME).glob("*.json")) + list((CLAUDE_HOME).glob("*.md"))
    print("\n--- All top-level files in ~/.claude ---")
    for p in sorted(cfg_glob):
        in_backup = p.resolve() in backed_up_sources
        tag = "OK" if in_backup else "MISSING"
        print(f"  [{tag:<7}] {p.name} ({p.stat().st_size}B)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
