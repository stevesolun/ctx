#!/usr/bin/env python3
"""Generate MANIFEST.json for the imported mattpocock/skills set.

Each top-level directory under imported-skills/mattpocock/ is one skill.
SKILL.md is the entry point; sibling .md/.sh files travel with the skill
and are deployed into the same target directory.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
UPSTREAM = "https://github.com/mattpocock/skills"
LICENSE = "MIT"


def parse_frontmatter(text: str) -> dict[str, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    pending_key: str | None = None
    for raw in m.group(1).splitlines():
        if pending_key and raw.startswith((" ", "\t")):
            out[pending_key] = (out[pending_key] + " " + raw.strip()).strip()
            continue
        pending_key = None
        if ":" not in raw:
            continue
        k, _, v = raw.partition(":")
        v = v.strip()
        if v in {"", ">", "|"}:
            pending_key = k.strip()
            out[pending_key] = ""
        else:
            out[k.strip()] = v.strip('"').strip("'")
    return out


def support_files(skill_dir: Path) -> list[str]:
    out: list[str] = []
    for p in sorted(skill_dir.rglob("*")):
        if not p.is_file() or p.name == "SKILL.md":
            continue
        out.append(p.relative_to(skill_dir).as_posix())
    return out


def upstream_revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def build() -> dict:
    entries: list[dict] = []
    for skill_dir in sorted(p for p in ROOT.iterdir() if p.is_dir() and (p / "SKILL.md").exists()):
        skill_md = skill_dir / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        slug = skill_dir.name
        entries.append({
            "name": fm.get("name", slug),
            "description": fm.get("description", "").strip(),
            "slug": slug,
            "source_path": (skill_dir.relative_to(ROOT) / "SKILL.md").as_posix(),
            "support_files": support_files(skill_dir),
            "lines": len(text.splitlines()),
        })
    rev = upstream_revision()
    return {
        "upstream": UPSTREAM,
        "upstream_revision": rev,
        "license": LICENSE,
        "namespace": "mattpocock",
        "total": len(entries),
        "entries": entries,
    }


def main() -> None:
    manifest = build()
    out = ROOT / "MANIFEST.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest written: {manifest['total']} skills @ {manifest['upstream_revision'][:12]}")


if __name__ == "__main__":
    main()
