#!/usr/bin/env python3
"""Generate MANIFEST.json for the imported designdotmd.directory set.

Each entry pairs a markdown file under ``designs/<id>.md`` (fetched
directly from the upstream API) with the listing-API metadata
(``id``, ``name``, ``author``, ``tags``, ``tagline``).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

ROOT = Path(__file__).parent
DESIGNS_DIR = ROOT / "designs"
LISTING_PATH = ROOT / "designs-listing.json"
UPSTREAM = "https://designdotmd.directory"
UPSTREAM_API = f"{UPSTREAM}/api/designs"


def build() -> dict:
    listing = json.loads(LISTING_PATH.read_text(encoding="utf-8"))
    entries: list[dict] = []
    for d in listing:
        slug = d["id"]
        md_path = DESIGNS_DIR / f"{slug}.md"
        if not md_path.is_file():
            continue
        text = md_path.read_text(encoding="utf-8")
        entries.append({
            "name": d.get("name") or slug,
            "tagline": d.get("tagline", "").strip(),
            "author": d.get("author", "").strip(),
            "tags": [str(t).strip().lower() for t in d.get("tags", []) if str(t).strip()],
            "slug": slug,
            "source_path": (md_path.relative_to(ROOT)).as_posix(),
            "lines": len(text.splitlines()),
        })
    entries.sort(key=lambda e: e["slug"])
    return {
        "upstream": UPSTREAM,
        "upstream_api": UPSTREAM_API,
        "fetched_on": datetime.date.today().isoformat(),
        "license": "unknown (see ATTRIBUTION.md)",
        "namespace": "designdotmd",
        "total": len(entries),
        "entries": entries,
    }


def main() -> None:
    manifest = build()
    out = ROOT / "MANIFEST.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest written: {manifest['total']} designs")


if __name__ == "__main__":
    main()
