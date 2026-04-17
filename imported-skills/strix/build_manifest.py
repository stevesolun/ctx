#!/usr/bin/env python3
"""Generate MANIFEST.json for the imported Strix skill set.

Reads every *.md under skills/ and agent-patterns/, parses YAML frontmatter,
emits a deterministic manifest the importer can consume.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
SKILL_GLOBS = ["skills/**/*.md", "agent-patterns/*.md"]

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def build() -> dict:
    entries: list[dict] = []
    for glob in SKILL_GLOBS:
        for path in sorted(ROOT.glob(glob)):
            if path.name.upper() == "README.MD":
                continue
            text = path.read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            rel = path.relative_to(ROOT).as_posix()
            category = rel.split("/")[1] if rel.startswith("skills/") else "agent-patterns"
            entries.append({
                "name": fm.get("name", path.stem),
                "description": fm.get("description", ""),
                "category": category,
                "source_path": rel,
                "lines": len(text.splitlines()),
            })
    return {
        "upstream": "https://github.com/usestrix/strix",
        "upstream_revision": "15c95718e600897a2a532a613a1c8fa6b712b144",
        "upstream_date": "2026-04-13",
        "license": "Apache-2.0",
        "total_skills": sum(1 for e in entries if e["category"] != "agent-patterns"),
        "total_patterns": sum(1 for e in entries if e["category"] == "agent-patterns"),
        "total": len(entries),
        "entries": entries,
    }


def main() -> None:
    manifest = build()
    out_path = ROOT / "MANIFEST.json"
    out_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"Manifest written: {manifest['total']} entries "
        f"({manifest['total_skills']} skills + {manifest['total_patterns']} patterns)"
    )


if __name__ == "__main__":
    main()
