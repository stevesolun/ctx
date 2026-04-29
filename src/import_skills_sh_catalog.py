#!/usr/bin/env python3
"""Import Skills.sh search metadata into ctx's shipped graph artifacts.

The Skills.sh API exposes search, not a single full-catalog export. This
script supports both:

* ``--fetch``: build a best-effort full catalog by querying all safe
  two-character alphanumeric terms plus a few high-yield domain terms.
* ``--from-api-union``: normalize a previously fetched union JSON.

It writes ``graph/skills-sh-catalog.json.gz`` and can inject the catalog into
``graph/wiki-graph.tar.gz`` as:

* ``external-catalogs/skills-sh/catalog.json`` for machine reads.
* ``entities/external-skills/<shard>/<slug>.md`` metadata pages for browsing.
* ``external-skill`` graph nodes, connected sparsely to curated entities by
  exact duplicate metadata or meaningful shared tags.

These records are graph-visible but not curated local skills: the canonical
SKILL.md bodies remain upstream until a human promotes a candidate.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import gzip
import hashlib
import json
import math
import re
import string
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG_OUT = REPO_ROOT / "graph" / "skills-sh-catalog.json.gz"
DEFAULT_WIKI_TAR = REPO_ROOT / "graph" / "wiki-graph.tar.gz"
SKILLS_SH_API = "https://skills.sh/api/search"
SKILLS_SH_HOME = "https://skills.sh/"
SKILLS_SH_SITEMAP = "https://skills.sh/sitemap.xml"
USER_AGENT = "ctx-skills-sh-import/0.1 (+https://github.com/stevesolun/ctx)"
EXTERNAL_NODE_PREFIX = "external-skill:skills-sh:"
MAX_EXTERNAL_EDGES_PER_NODE = 2
EXTERNAL_ENTITY_ROOT = "entities/external-skills"

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")
_NOISY_EXTERNAL_EDGE_TAGS = {
    "skills-sh", "skill", "skills", "ai", "agent", "agents", "api", "web", "code",
}
_COMMON_TAGS = {
    "ai", "api", "agent", "agents", "anthropic", "automation", "aws", "azure",
    "claude", "cloud", "code", "codex", "css", "data", "database", "deploy",
    "design", "devops", "docker", "docs", "fastapi", "frontend", "github",
    "google", "javascript", "kubernetes", "llm", "mcp", "microsoft", "nextjs",
    "node", "openai", "performance", "playwright", "postgres", "python",
    "react", "security", "skill", "testing", "typescript", "vercel", "web",
}
_TAG_ALIASES = {
    "doc": "docs",
    "next": "nextjs",
    "next.js": "nextjs",
    "skills": "skill",
    "js": "javascript",
    "ts": "typescript",
    "k8s": "kubernetes",
    "postgresql": "postgres",
}


@dataclass(frozen=True)
class ExistingWikiIndex:
    skill_slugs: set[str]
    skill_ids: set[str]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _slugify(value: str, *, max_len: int = 140) -> str:
    slug = _SAFE_SLUG_RE.sub("-", value.lower()).strip("-")
    return slug[:max_len].strip("-") or "unknown"


def _ctx_slug(source: str, skill_id: str) -> str:
    return "skills-sh-" + _slugify(f"{source}-{skill_id}", max_len=128)


def _unique_ctx_slug(base_slug: str, full_id: str, seen_slugs: set[str]) -> tuple[str, bool]:
    if base_slug not in seen_slugs:
        seen_slugs.add(base_slug)
        return base_slug, False
    for salt in range(1000):
        digest = hashlib.sha1(f"{full_id}:{salt}".encode("utf-8")).hexdigest()[:10]
        stem = base_slug[: 140 - len(digest) - 1].rstrip("-") or "skills-sh"
        candidate = f"{stem}-{digest}"
        if candidate not in seen_slugs:
            seen_slugs.add(candidate)
            return candidate, True
    raise ValueError(f"could not allocate unique ctx slug for {full_id!r}")


def _is_site_source(source: str) -> bool:
    return "/" not in source and "." in source


def _detail_url(source: str, skill_id: str) -> str:
    if _is_site_source(source):
        return "https://skills.sh/site/" + "/".join(
            urllib.parse.quote(p, safe="") for p in (source, skill_id)
        )
    return "https://skills.sh/" + "/".join(
        urllib.parse.quote(p, safe="") for p in (*source.split("/"), skill_id)
    )


def _install_command(source: str, skill_id: str) -> str:
    if _is_site_source(source):
        return f"npx skills add https://{source}"
    if "/" in source and not source.startswith(("http://", "https://")):
        return f"npx skills add https://github.com/{source} --skill {skill_id}"
    return f"npx skills add {source} --skill {skill_id}"


def _infer_tags(*parts: str) -> list[str]:
    raw_tokens: list[str] = []
    for part in parts:
        raw_tokens.extend(_TOKEN_RE.findall(part.lower()))
    tags: list[str] = []
    seen: set[str] = set()
    for token in raw_tokens:
        tag = _TAG_ALIASES.get(token, token)
        if tag in _COMMON_TAGS and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags or ["skills-sh"]


def _read_site_reported_total() -> int | None:
    req = urllib.request.Request(SKILLS_SH_HOME, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            text = response.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'\\"totalSkills\\":(\d+)', text)
    if not match:
        match = re.search(r'"totalSkills":(\d+)', text)
    return int(match.group(1)) if match else None


def _read_sitemap_records() -> list[dict[str, Any]]:
    req = urllib.request.Request(SKILLS_SH_SITEMAP, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            xml_text = response.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    records: list[dict[str, Any]] = []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = root.findall(".//sm:loc", ns) or root.findall(".//loc")
    for loc in locs:
        url = (loc.text or "").strip()
        if not url.startswith("https://skills.sh/"):
            continue
        raw_parts = url.removeprefix("https://skills.sh/").strip("/").split("/")
        parts = [urllib.parse.unquote(p) for p in raw_parts]
        if len(parts) == 3 and parts[0] == "site":
            source, skill_id = parts[1], parts[2]
            full_id = f"{source}/{skill_id}"
        elif len(parts) == 3 and parts[0] not in {"picks", "site"}:
            source, skill_id = f"{parts[0]}/{parts[1]}", parts[2]
            full_id = f"{source}/{skill_id}"
        else:
            continue
        records.append({
            "id": full_id,
            "source": source,
            "skillId": skill_id,
            "name": skill_id,
            "installs": 0,
            "_from_sitemap": True,
        })
    return records


def _fetch_query(
    query: str, *, limit: int, delay_seconds: float = 0.0,
) -> tuple[str, list[dict[str, Any]], str | None]:
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    url = SKILLS_SH_API + "?" + urllib.parse.urlencode({"q": query, "limit": str(limit)})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - preserve query-level failures in metadata.
        return query, [], f"{type(exc).__name__}: {exc}"
    skills = payload.get("skills") or []
    if not isinstance(skills, list):
        return query, [], "response.skills was not a list"
    return query, [s for s in skills if isinstance(s, dict)], None


def fetch_api_union(*, limit: int, workers: int, delay_seconds: float = 0.0) -> dict[str, Any]:
    chars = string.ascii_lowercase + string.digits
    queries = [a + b for a in chars for b in chars]
    queries += [
        "skill", "skills", "agent", "claude", "code", "ai", "dev", "test",
        "data", "api", "web", "app", "github", "mcp", "llm", "react",
        "python", "typescript", "openai", "vercel", "google", "microsoft",
        "anthropic",
    ]
    queries = list(dict.fromkeys(queries))

    by_id: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    query_counts: dict[str, int] = {}
    started = time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_fetch_query, q, limit=limit, delay_seconds=delay_seconds): q
            for q in queries
        }
        for i, future in enumerate(cf.as_completed(futures), 1):
            query, skills, error = future.result()
            query_counts[query] = len(skills)
            if error:
                errors.append({"query": query, "error": error})
            for item in skills:
                skill_id = item.get("id")
                if not isinstance(skill_id, str) or not skill_id:
                    continue
                existing = by_id.get(skill_id)
                if existing is None or int(item.get("installs") or 0) > int(
                    existing.get("installs") or 0
                ):
                    by_id[skill_id] = item
            if i % 25 == 0 or i == len(queries):
                print(
                    f"fetch progress: {i}/{len(queries)} queries, "
                    f"{len(by_id):,} unique, {len(errors)} errors, "
                    f"{time.time() - started:.1f}s",
                    flush=True,
                )

    return {
        "fetched_at": _utc_now(),
        "source": SKILLS_SH_API,
        "query_count": len(queries),
        "query_limit": limit,
        "query_errors": errors,
        "query_counts_top": sorted(query_counts.items(), key=lambda kv: kv[1], reverse=True)[:100],
        "skills": sorted(
            by_id.values(),
            key=lambda s: (-int(s.get("installs") or 0), str(s.get("id") or "")),
        ),
    }


def _safe_tar_name(name: str) -> str | None:
    normalized = name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/")
    if not normalized:
        return None
    parts = normalized.split("/")
    first = parts[0]
    if (
        normalized.startswith("/")
        or (len(first) == 2 and first[1] == ":")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None
    return normalized


def read_existing_wiki_index(tarball: Path) -> ExistingWikiIndex:
    skill_slugs: set[str] = set()
    skill_ids: set[str] = set()
    if not tarball.exists():
        return ExistingWikiIndex(skill_slugs=skill_slugs, skill_ids=skill_ids)
    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf.getmembers():
            name = _safe_tar_name(member.name)
            if not name or not member.isfile() or not name.startswith("entities/skills/"):
                continue
            if not name.endswith(".md"):
                continue
            slug = Path(name).stem
            skill_slugs.add(slug)
            skill_ids.add(slug.lower())
    return ExistingWikiIndex(skill_slugs=skill_slugs, skill_ids=skill_ids)


def normalize_catalog(raw: dict[str, Any], existing: ExistingWikiIndex) -> dict[str, Any]:
    site_total = _read_site_reported_total()
    skills_in = raw.get("skills") or []
    if not isinstance(skills_in, list):
        raise ValueError("input JSON must contain a list at key 'skills'")
    sitemap_records = _read_sitemap_records()
    api_ids = {
        str(item.get("id"))
        for item in skills_in
        if isinstance(item, dict) and item.get("id")
    }
    sitemap_merged = [item for item in sitemap_records if str(item.get("id")) not in api_ids]
    skills_in = [*skills_in, *sitemap_merged]

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_ctx_slugs: set[str] = set()
    overlap_skill_id = 0
    overlap_ctx_slug = 0
    ctx_slug_collisions = 0
    for item in skills_in:
        if not isinstance(item, dict):
            continue
        full_id = str(item.get("id") or "").strip()
        source = str(item.get("source") or "").strip()
        skill_id = str(item.get("skillId") or item.get("name") or "").strip()
        name = str(item.get("name") or skill_id).strip()
        if not full_id or not source or not skill_id or full_id in seen:
            continue
        seen.add(full_id)
        installs = int(item.get("installs") or 0)
        base_ctx_slug = _ctx_slug(source, skill_id)
        ctx_slug, ctx_slug_collision = _unique_ctx_slug(base_ctx_slug, full_id, seen_ctx_slugs)
        ctx_slug_collisions += int(ctx_slug_collision)
        tags = _infer_tags(full_id, source, skill_id, name)
        skill_id_overlap = skill_id.lower() in existing.skill_ids
        ctx_slug_overlap = ctx_slug in existing.skill_slugs
        overlap_skill_id += int(skill_id_overlap)
        overlap_ctx_slug += int(ctx_slug_overlap)
        normalized.append({
            "id": full_id,
            "ctx_slug": ctx_slug,
            "base_ctx_slug": base_ctx_slug,
            "source": source,
            "skill_id": skill_id,
            "name": name,
            "type": "external-skill",
            "external_catalog": "skills.sh",
            "installs": installs,
            "tags": tags,
            "detail_url": _detail_url(source, skill_id),
            "install_command": _install_command(source, skill_id),
            "overlap": {
                "skill_id_in_existing_wiki": skill_id_overlap,
                "ctx_slug_in_existing_wiki": ctx_slug_overlap,
                "ctx_slug_collision_resolved": ctx_slug_collision,
            },
        })

    normalized.sort(key=lambda s: (-int(s["installs"]), str(s["id"])))
    observed = len(normalized)
    query_errors_raw = raw.get("query_errors")
    if not isinstance(query_errors_raw, list):
        errors_raw = raw.get("errors")
        query_errors_raw = errors_raw if isinstance(errors_raw, list) else []
    query_errors: list[Any] = query_errors_raw
    return {
        "schema_version": 1,
        "source": "skills.sh",
        "api": SKILLS_SH_API,
        "fetched_at": raw.get("fetched_at") or _utc_now(),
        "site_reported_total": site_total,
        "observed_unique_skills": observed,
        "coverage_vs_site_reported_total": (
            round(observed / site_total, 6) if site_total else None
        ),
        "query_count": raw.get("query_count"),
        "query_limit": raw.get("query_limit"),
        "query_error_count": int(raw.get("error_count") or len(query_errors)),
        "query_errors_sample": query_errors[:20],
        "sitemap_records_merged": len(sitemap_merged),
        "ctx_slug_collisions_resolved": ctx_slug_collisions,
        "overlap": {
            "existing_wiki_skill_pages": len(existing.skill_slugs),
            "skill_id_matches_existing_wiki": overlap_skill_id,
            "ctx_slug_matches_existing_wiki": overlap_ctx_slug,
        },
        "notes": [
            "Skills.sh exposes search, not a documented full export endpoint.",
            "Catalog was recovered by unioning high-limit search API responses.",
            "Entries are external recommendations; full SKILL.md bodies stay upstream.",
        ],
        "skills": normalized,
    }


def write_gzip_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def read_gzip_json(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return data


def _fmt_count(value: Any) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    return "unknown"


def render_external_readme(catalog: dict[str, Any]) -> str:
    return f"""# Skills.sh External Catalog

This directory is generated by `src/import_skills_sh_catalog.py`.

- Source: https://skills.sh/
- API surface used: `{SKILLS_SH_API}`
- Observed unique skills: {_fmt_count(catalog.get("observed_unique_skills"))}
- Site-reported total at fetch time: {_fmt_count(catalog.get("site_reported_total"))}
- Existing wiki skill-id overlaps: {_fmt_count(catalog.get("overlap", {}).get("skill_id_matches_existing_wiki"))}
- Resolved ctx slug collisions: {_fmt_count(catalog.get("ctx_slug_collisions_resolved"))}
- External graph nodes: {_fmt_count(catalog.get("graph_external_nodes"))}

The catalog is intentionally stored as external metadata and external graph
nodes rather than 90K first-class curated wiki entity pages. ctx can rank these
entries as external recommendations, but the canonical skill body and install
flow remain upstream through the `npx skills` CLI.
"""


def _node_label(node: dict[str, Any]) -> str:
    node_id = str(node.get("id") or "")
    return str(node.get("label") or node_id.split(":", 1)[-1])


def _meaningful_tags(raw_tags: Any) -> list[str]:
    if not isinstance(raw_tags, list):
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for tag_raw in raw_tags:
        tag = str(tag_raw).strip().lower()
        if not tag or tag in _NOISY_EXTERNAL_EDGE_TAGS or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tags


def _source_reputation(source: str) -> int:
    trusted_prefixes = (
        "anthropics/", "vercel-labs/", "vercel/", "microsoft/", "google/",
        "aws-samples/", "openai/", "github/",
    )
    if source.startswith(trusted_prefixes):
        return 25
    if _is_site_source(source):
        return 10
    return 0


def _quality_signals(item: dict[str, Any], duplicate_targets: list[str]) -> dict[str, Any]:
    installs = int(item.get("installs") or 0)
    source = str(item.get("source") or "")
    install_score = min(40, int(math.log10(installs + 1) * 10)) if installs > 0 else 0
    reputation_score = _source_reputation(source)
    duplicate_score = 20 if duplicate_targets else 0
    tag_score = min(15, len(_meaningful_tags(item.get("tags"))) * 5)
    score = install_score + reputation_score + duplicate_score + tag_score
    return {
        "score": score,
        "install_score": install_score,
        "source_reputation_score": reputation_score,
        "duplicate_score": duplicate_score,
        "tag_score": tag_score,
        "body_available": False,
        "security_review": "metadata-only",
        "promotion_state": "alias" if duplicate_targets else "candidate",
    }


def _external_entity_path(ctx_slug: str) -> str:
    shard = (ctx_slug.removeprefix("skills-sh-")[:1] or "_").lower()
    if not shard.isalnum():
        shard = "_"
    return f"{EXTERNAL_ENTITY_ROOT}/{shard}/{ctx_slug}.md"


def _md_list(values: list[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in values) + "]"


def _render_external_entity_page(
    item: dict[str, Any],
    *,
    external_id: str,
    duplicate_targets: list[str],
    quality: dict[str, Any],
) -> str:
    ctx_slug = str(item.get("ctx_slug") or "")
    label = str(item.get("id") or item.get("name") or ctx_slug)
    merge_state = "alias-of-curated" if duplicate_targets else "catalog-only"
    duplicate_target = duplicate_targets[0] if duplicate_targets else "null"
    tags = [str(tag) for tag in item.get("tags", []) if tag]
    body = f"""---
title: {json.dumps(label, ensure_ascii=False)}
type: external-skill
status: catalog-only
external: true
external_catalog: skills.sh
ctx_slug: {ctx_slug}
node_id: {external_id}
source: {json.dumps(str(item.get("source") or ""), ensure_ascii=False)}
skill_id: {json.dumps(str(item.get("skill_id") or ""), ensure_ascii=False)}
installs: {int(item.get("installs") or 0)}
tags: {_md_list(tags)}
detail_url: {json.dumps(str(item.get("detail_url") or ""), ensure_ascii=False)}
install_command: {json.dumps(str(item.get("install_command") or ""), ensure_ascii=False)}
merge_state: {merge_state}
duplicate_of: {duplicate_target}
quality_score: {int(quality.get("score") or 0)}
security_review: metadata-only
---

# {label}

Catalog-only Skills.sh entry.

## Install

```bash
{item.get("install_command") or ""}
```

## Provenance

- Source: `{item.get("source") or ""}`
- Skill ID: `{item.get("skill_id") or ""}`
- Detail URL: {item.get("detail_url") or ""}
- Installs: {int(item.get("installs") or 0):,}
- Merge state: `{merge_state}`
"""
    if duplicate_targets:
        body += "\n## Duplicate / Merge\n\n"
        body += "This upstream record appears to overlap an existing curated ctx entity:\n"
        body += "".join(f"- `{target}`\n" for target in duplicate_targets)
    else:
        body += "\n## Duplicate / Merge\n\nNo exact curated duplicate was detected from available Skills.sh metadata.\n"
    body += f"""

## Quality Signals

- Quality score: {int(quality.get("score") or 0)}
- Install score: {quality.get("install_score")}
- Source reputation score: {quality.get("source_reputation_score")}
- Duplicate score: {quality.get("duplicate_score")}
- Tag score: {quality.get("tag_score")}
- Body availability: metadata-only; canonical body remains upstream.
- Security review: metadata-only. Fetch and inspect the body before promotion to curated `skill`.
"""
    return body


def _augment_graph_with_external_nodes(graph: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    nodes = graph.get("nodes")
    edges = graph.get("edges") if "edges" in graph else graph.get("links")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return graph

    skills = catalog.get("skills")
    if not isinstance(skills, list):
        return graph

    external_ids = {
        str(node.get("id"))
        for node in nodes
        if str(node.get("id") or "").startswith(EXTERNAL_NODE_PREFIX)
        or (
            node.get("external_catalog") == "skills.sh"
            and node.get("type") == "external-skill"
        )
    }
    if external_ids:
        nodes = [node for node in nodes if str(node.get("id")) not in external_ids]
        edges = [
            edge for edge in edges
            if str(edge.get("source")) not in external_ids
            and str(edge.get("target")) not in external_ids
        ]

    degree: dict[str, int] = {}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source:
            degree[source] = degree.get(source, 0) + 1
        if target:
            degree[target] = degree.get(target, 0) + 1

    label_index: dict[str, list[str]] = {}
    tag_index: dict[str, list[str]] = {}
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        if node.get("type") == "external-skill" or node.get("external"):
            continue
        label_index.setdefault(_node_label(node).lower(), []).append(node_id)
        for tag in _meaningful_tags(node.get("tags")):
            tag_index.setdefault(tag, []).append(node_id)

    for bucket in label_index.values():
        bucket.sort(key=lambda node_id: degree.get(node_id, 0), reverse=True)
    for bucket in tag_index.values():
        bucket.sort(key=lambda node_id: degree.get(node_id, 0), reverse=True)

    added_edges = 0
    for item in skills:
        if not isinstance(item, dict):
            continue
        ctx_slug = str(item.get("ctx_slug") or "").strip()
        if not ctx_slug:
            continue
        external_id = EXTERNAL_NODE_PREFIX + ctx_slug
        tags = [str(tag) for tag in item.get("tags", []) if tag]
        skill_id = str(item.get("skill_id") or "").lower()
        duplicate_targets = label_index.get(skill_id, [])[:MAX_EXTERNAL_EDGES_PER_NODE]
        quality = _quality_signals(item, duplicate_targets)
        item["graph_node_id"] = external_id
        item["entity_path"] = _external_entity_path(ctx_slug)
        item["merge_state"] = "alias-of-curated" if duplicate_targets else "catalog-only"
        item["duplicate_of"] = duplicate_targets[0] if duplicate_targets else None
        item["duplicate_targets"] = duplicate_targets
        item["quality_score"] = quality["score"]
        item["quality_signals"] = quality
        nodes.append({
            "id": external_id,
            "label": str(item.get("id") or item.get("name") or ctx_slug),
            "type": "external-skill",
            "external": True,
            "external_catalog": "skills.sh",
            "ctx_slug": ctx_slug,
            "source": item.get("source"),
            "skill_id": item.get("skill_id"),
            "installs": item.get("installs"),
            "tags": tags,
            "detail_url": item.get("detail_url"),
            "install_command": item.get("install_command"),
            "status": "catalog-only",
            "merge_state": item["merge_state"],
            "duplicate_of": item["duplicate_of"],
            "quality_score": item["quality_score"],
            "quality_signals": item["quality_signals"],
            "entity_path": item["entity_path"],
        })

        targets: list[tuple[str, list[str], list[str]]] = []
        for target in duplicate_targets:
            targets.append((target, [], [skill_id]))
        for tag in _meaningful_tags(item.get("tags")):
            for target in tag_index.get(tag, [])[:MAX_EXTERNAL_EDGES_PER_NODE]:
                targets.append((target, [tag], []))

        seen_targets: set[str] = set()
        for target, shared_tags, shared_tokens in targets:
            if target in seen_targets:
                continue
            seen_targets.add(target)
            edges.append({
                "source": target,
                "target": external_id,
                "semantic_sim": 0.0,
                "tag_sim": 0.2 if shared_tags else 0.0,
                "token_sim": 0.2 if shared_tokens else 0.0,
                "final_weight": 0.03,
                "weight": 0.03,
                "shared_tags": shared_tags,
                "shared_tokens": shared_tokens,
                "external_catalog": "skills.sh",
            })
            added_edges += 1
            if len(seen_targets) >= MAX_EXTERNAL_EDGES_PER_NODE:
                break

    graph["nodes"] = nodes
    edge_key = "edges" if "edges" in graph else "links"
    graph[edge_key] = edges
    metadata = graph.setdefault("graph", {})
    if isinstance(metadata, dict):
        metadata.setdefault("external_catalog_nodes", {})
        metadata["external_catalog_nodes"]["skills.sh"] = len(skills)
        metadata.setdefault("external_catalog_edges", {})
        metadata["external_catalog_edges"]["skills.sh"] = added_edges
    catalog["graph_external_nodes"] = len(skills)
    catalog["graph_external_edges"] = added_edges
    return graph


def _add_bytes(
    dst: tarfile.TarFile,
    *,
    name: str,
    payload: bytes,
    mode: int = 0o644,
    mtime: int | None = None,
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mtime = int(time.time()) if mtime is None else mtime
    info.mode = mode
    dst.addfile(info, BytesIO(payload))


def update_wiki_tarball(tarball: Path, catalog: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp_file:
        tmp_path = Path(tmp_file.name)
    try:
        with tarfile.open(tarball, "r:gz") as src, tarfile.open(tmp_path, "w:gz") as dst:
            for member in src.getmembers():
                safe_name = _safe_tar_name(member.name)
                if safe_name is None:
                    continue
                if (
                    safe_name.startswith("external-catalogs/skills-sh/")
                    or safe_name.startswith(f"{EXTERNAL_ENTITY_ROOT}/")
                ):
                    continue
                if member.isfile():
                    f = src.extractfile(member)
                    if f is None:
                        continue
                    if safe_name == "graphify-out/graph.json":
                        graph = json.loads(f.read().decode("utf-8"))
                        graph = _augment_graph_with_external_nodes(graph, catalog)
                        payload = json.dumps(
                            graph,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        _add_bytes(
                            dst,
                            name=member.name,
                            payload=payload,
                            mode=member.mode,
                            mtime=int(member.mtime),
                        )
                        continue
                    dst.addfile(member, f)
                else:
                    dst.addfile(member)

            catalog_bytes = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            readme_bytes = render_external_readme(catalog).encode("utf-8")
            summary = {
                k: catalog.get(k)
                for k in (
                    "schema_version", "source", "api", "fetched_at", "site_reported_total",
                    "observed_unique_skills", "coverage_vs_site_reported_total",
                    "query_count", "query_error_count", "ctx_slug_collisions_resolved", "overlap",
                    "graph_external_nodes", "graph_external_edges",
                )
            }
            summary_bytes = json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")
            raw_skills = catalog.get("skills")
            skills = raw_skills if isinstance(raw_skills, list) else []
            for item in skills:
                if not isinstance(item, dict):
                    continue
                external_id = str(item.get("graph_node_id") or "")
                entity_path = str(item.get("entity_path") or "")
                quality = item.get("quality_signals")
                if not external_id or not entity_path or not isinstance(quality, dict):
                    continue
                duplicate_raw = item.get("duplicate_targets")
                duplicate_targets = [
                    str(target) for target in duplicate_raw
                    if target
                ] if isinstance(duplicate_raw, list) else []
                page = _render_external_entity_page(
                    item,
                    external_id=external_id,
                    duplicate_targets=duplicate_targets,
                    quality=quality,
                )
                _add_bytes(
                    dst,
                    name=f"./{entity_path}",
                    payload=page.encode("utf-8"),
                )
            for name, payload in (
                ("external-catalogs/skills-sh/catalog.json", catalog_bytes),
                ("external-catalogs/skills-sh/summary.json", summary_bytes),
                ("external-catalogs/skills-sh/README.md", readme_bytes),
            ):
                _add_bytes(dst, name=f"./{name}", payload=payload)
        tmp_path.replace(tarball)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _load_raw_input(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return data


def _load_catalog_input(path: Path) -> dict[str, Any]:
    if path.suffix == ".gz":
        return read_gzip_json(path)
    return _load_raw_input(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fetch", action="store_true", help="Fetch a Skills.sh API union")
    source.add_argument("--from-api-union", type=Path, help="Use a previously fetched API union JSON")
    source.add_argument("--from-catalog", type=Path, help="Use a normalized Skills.sh catalog JSON or JSON.gz")
    parser.add_argument("--query-limit", type=int, default=100_000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--catalog-out", type=Path, default=DEFAULT_CATALOG_OUT)
    parser.add_argument("--wiki-tar", type=Path, default=DEFAULT_WIKI_TAR)
    parser.add_argument("--update-wiki-tar", action="store_true")
    args = parser.parse_args()

    if args.fetch:
        raw = fetch_api_union(
            limit=args.query_limit,
            workers=args.workers,
            delay_seconds=max(args.delay_ms, 0) / 1000.0,
        )
        existing = read_existing_wiki_index(args.wiki_tar)
        catalog = normalize_catalog(raw, existing)
    elif args.from_api_union is not None:
        raw = _load_raw_input(args.from_api_union)
        existing = read_existing_wiki_index(args.wiki_tar)
        catalog = normalize_catalog(raw, existing)
    else:
        catalog = _load_catalog_input(args.from_catalog)
    if args.update_wiki_tar:
        update_wiki_tarball(args.wiki_tar, catalog)
    write_gzip_json(args.catalog_out, catalog)
    print(
        f"skills.sh catalog: {catalog['observed_unique_skills']:,} observed "
        f"(site total={catalog.get('site_reported_total')}); "
        f"wrote {args.catalog_out}"
    )
    if args.update_wiki_tar:
        print(f"updated {args.wiki_tar}")


if __name__ == "__main__":
    main()
