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
* ``entities/skills/<slug>.md`` remote-cataloged skill pages for browsing.
* first-class ``skill`` graph nodes with Skills.sh provenance, connected
  sparsely to curated entities by exact duplicate metadata or meaningful
  shared tags.

Hydrated upstream SKILL.md bodies are shipped in the wiki under
``converted/skills-sh-*/SKILL.md``. These records are graph-visible but not
curated local skills until a human security-reviews and promotes a candidate.
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
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any, TextIO

from ctx.core.wiki.artifact_promotion import promote_staged_artifact

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG_OUT = REPO_ROOT / "graph" / "skills-sh-catalog.json.gz"
DEFAULT_WIKI_TAR = REPO_ROOT / "graph" / "wiki-graph.tar.gz"
SKILLS_SH_API = "https://skills.sh/api/search"
SKILLS_SH_HOME = "https://skills.sh/"
SKILLS_SH_SITEMAP = "https://skills.sh/sitemap.xml"
USER_AGENT = "ctx-skills-sh-import/0.1 (+https://github.com/stevesolun/ctx)"
SKILLS_SH_NODE_PREFIX = "skill:"
LEGACY_EXTERNAL_NODE_PREFIX = "external-skill:skills-sh:"
MAX_EXTERNAL_EDGES_PER_NODE = 2
EXTERNAL_ENTITY_ROOT = "entities/external-skills"
SKILLS_SH_ENTITY_ROOT = "entities/skills"
CONVERTED_SKILL_ROOT = "converted"
DEFAULT_DETAIL_MAX_BYTES = 2_000_000
DEFAULT_SKILL_BODY_MAX_CHARS = 120_000
GITHUB_RAW_HOST = "raw.githubusercontent.com"
_HYDRATION_CHECKPOINT_FIELDS = (
    "id",
    "ctx_slug",
    "skill_body",
    "body_available",
    "body_source_url",
    "body_hydrated_at",
    "body_truncated",
    "body_error",
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")
_VOID_HTML_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
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


class ConvertedSkillPackagingError(RuntimeError):
    """Raised when a hydrated Skills.sh body cannot be packaged safely."""


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


class _SkillBodyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._capture_depth = 0
        self._pre_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._capture_depth == 0:
            if tag == "div" and self._has_prose_class(attrs):
                self._capture_depth = 1
            return
        if tag in _VOID_HTML_TAGS:
            if tag in {"br", "hr"}:
                self._push("\n")
            return
        self._capture_depth += 1
        if tag in {"h1", "h2", "h3"}:
            self._push("\n" + {"h1": "# ", "h2": "## ", "h3": "### "}[tag])
        elif tag in {"h4", "h5", "h6"}:
            self._push("\n#### ")
        elif tag in {"p", "pre", "blockquote", "ul", "ol", "div", "section", "article"}:
            self._push("\n")
        elif tag == "li":
            self._push("\n- ")
        elif tag == "br":
            self._push("\n")
        if tag == "pre":
            self._pre_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture_depth > 0 and tag.lower() == "br":
            self._push("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._capture_depth == 0:
            return
        if tag in {
            "h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "blockquote",
            "li", "ul", "ol", "div", "section", "article",
        }:
            self._push("\n")
        if tag == "pre" and self._pre_depth > 0:
            self._pre_depth -= 1
        self._capture_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture_depth == 0:
            return
        text = data.strip("\r\n") if self._pre_depth else re.sub(r"\s+", " ", data)
        if text.strip():
            self._push(text.strip() if not self._pre_depth else text)

    @staticmethod
    def _has_prose_class(attrs: list[tuple[str, str | None]]) -> bool:
        for name, value in attrs:
            if name.lower() != "class" or not value:
                continue
            if "prose" in value.split():
                return True
        return False

    def _push(self, value: str) -> None:
        if (
            self.parts
            and value
            and not value.startswith(("\n", " ", ".", ",", ":", ";", ")", "]"))
            and not self.parts[-1].endswith(("\n", " ", "(", "[", "# ", "## ", "### ", "#### "))
        ):
            self.parts.append(" ")
        self.parts.append(value)


def _normalize_skill_body_text(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    normalized = "\n".join(lines).strip()
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized


def _extract_skill_body_from_detail_html(html_text: str) -> str:
    marker_index = html_text.find(">SKILL.md<")
    if marker_index >= 0:
        body_parser = _SkillBodyParser()
        body_parser.feed(html_text[marker_index:])
        body_parser.close()
        body = _normalize_skill_body_text("".join(body_parser.parts))
        if body:
            return body

    parser = _SkillBodyParser()
    parser.feed(html_text)
    parser.close()
    return _normalize_skill_body_text("".join(parser.parts))


def _is_skills_sh_detail_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() == "skills.sh"
        and bool(parsed.path.strip("/"))
    )


def _fetch_detail_html(
    url: str,
    timeout: int = 30,
    max_bytes: int = DEFAULT_DETAIL_MAX_BYTES,
) -> tuple[str | None, str | None]:
    if not _is_skills_sh_detail_url(url):
        return None, "refused non-skills.sh detail URL"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read(max_bytes + 1)
    except Exception as exc:  # noqa: BLE001 - store per-skill failures in catalog metadata.
        return None, f"{type(exc).__name__}: {exc}"
    if len(payload) > max_bytes:
        return None, f"detail response exceeded {max_bytes} bytes"
    return payload.decode("utf-8", errors="replace"), None


def _github_raw_skill_urls(source: str, skill_id: str) -> list[str]:
    if (
        "/" not in source
        or source.startswith(("http://", "https://"))
        or not skill_id.strip()
    ):
        return []
    owner, repo = source.split("/", 1)
    if not owner or not repo:
        return []
    owner_q = urllib.parse.quote(owner, safe="")
    repo_q = urllib.parse.quote(repo, safe=".-_")
    skill_q = urllib.parse.quote(skill_id.strip("/"), safe="")
    filenames = ("SKILL.md", "Skill.md", "skill.md")
    candidate_paths = [
        f"{base}/{filename}"
        for filename in filenames
        for base in (
            skill_q,
            f"skills/{skill_q}",
            f".claude/skills/{skill_q}",
            f"claude/skills/{skill_q}",
            f"agent-skills/{skill_q}",
        )
    ]
    if _slugify(repo) == _slugify(skill_id):
        candidate_paths.extend(filenames)
    urls: list[str] = []
    for branch in ("main", "master"):
        for path in candidate_paths:
            urls.append(
                f"https://{GITHUB_RAW_HOST}/{owner_q}/{repo_q}/{branch}/{path}",
            )
    return urls


def _fetch_github_raw_skill_body(
    item: dict[str, Any],
    *,
    timeout: int = 30,
    max_bytes: int = DEFAULT_DETAIL_MAX_BYTES,
) -> tuple[str | None, str | None, str | None]:
    source = str(item.get("source") or "")
    skill_id = str(item.get("skill_id") or item.get("skillId") or "")
    raw_timeout = min(timeout, 12)
    for url in _github_raw_skill_urls(source, skill_id):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=raw_timeout) as response:
                payload = response.read(max_bytes + 1)
        except Exception:
            continue
        if len(payload) > max_bytes:
            return None, None, f"GitHub raw SKILL.md exceeded {max_bytes} bytes"
        body = _normalize_skill_body_text(payload.decode("utf-8", errors="replace"))
        if body:
            return body, url, None
    return None, None, "GitHub raw fallback found no SKILL.md candidates"


def _refresh_body_summary(catalog: dict[str, Any]) -> dict[str, Any]:
    raw_skills = catalog.get("skills")
    skills = raw_skills if isinstance(raw_skills, list) else []
    body_available_count = sum(
        1 for item in skills
        if isinstance(item, dict) and (item.get("body_available") or item.get("skill_body"))
    )
    summary = {
        "body_available_count": body_available_count,
        "body_hydration_checkpoint_applied_count": catalog.get(
            "body_hydration_checkpoint_applied_count", 0,
        ),
        "body_hydration_attempted_count": catalog.get("body_hydration_attempted_count", 0),
        "body_hydrated_count": catalog.get("body_hydrated_count", body_available_count),
        "body_hydration_error_count": catalog.get("body_hydration_error_count", 0),
        "body_hydration_errors_sample": catalog.get("body_hydration_errors_sample", []),
    }
    catalog.update(summary)
    return summary


def _catalog_skills(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    raw_skills = catalog.get("skills")
    return [item for item in raw_skills if isinstance(item, dict)] if isinstance(raw_skills, list) else []


def _has_packaged_or_inline_body(item: dict[str, Any]) -> bool:
    return bool(
        item.get("body_available")
        or str(item.get("skill_body") or "").strip()
        or str(item.get("converted_path") or "").strip()
    )


def drop_body_unavailable_skills(catalog: dict[str, Any]) -> dict[str, int]:
    """Remove Skills.sh records that cannot ship as installable skill bodies."""
    skills = _catalog_skills(catalog)
    kept = [item for item in skills if _has_packaged_or_inline_body(item)]
    original_count = int(
        catalog.get("observed_unique_skills_before_body_prune")
        or catalog.get("observed_unique_skills")
        or len(skills)
    )
    pruned_count = max(original_count - len(kept), 0)

    catalog["skills"] = kept
    catalog["observed_unique_skills_before_body_prune"] = original_count
    catalog["observed_unique_skills"] = len(kept)
    catalog["body_unavailable_pruned_count"] = pruned_count
    catalog["body_unavailable_pruned_at"] = _utc_now() if pruned_count else catalog.get(
        "body_unavailable_pruned_at",
    )
    site_total = catalog.get("site_reported_total")
    catalog["coverage_vs_site_reported_total"] = (
        round(len(kept) / site_total, 6) if isinstance(site_total, int) and site_total else None
    )
    overlap = catalog.setdefault("overlap", {})
    if isinstance(overlap, dict):
        overlap["skill_id_matches_existing_wiki"] = sum(
            1 for item in kept
            if isinstance(item.get("overlap"), dict)
            and item["overlap"].get("skill_id_in_existing_wiki")
        )
        overlap["ctx_slug_matches_existing_wiki"] = sum(
            1 for item in kept
            if isinstance(item.get("overlap"), dict)
            and item["overlap"].get("ctx_slug_in_existing_wiki")
        )
    catalog["ctx_slug_collisions_resolved"] = sum(
        1 for item in kept
        if isinstance(item.get("overlap"), dict)
        and item["overlap"].get("ctx_slug_collision_resolved")
    )
    catalog["body_hydration_error_count"] = 0
    catalog["body_hydration_errors_sample"] = []
    notes = catalog.setdefault("notes", [])
    note = "Body-unavailable Skills.sh records are pruned from shipped graph/wiki artifacts."
    if isinstance(notes, list) and note not in notes:
        notes.append(note)
    _refresh_body_summary(catalog)
    return {"kept": len(kept), "pruned": pruned_count, "original": original_count}


def hydrate_catalog_bodies(
    catalog: dict[str, Any],
    *,
    workers: int,
    limit: int | None = None,
    delay_seconds: float = 0.0,
    timeout: int = 30,
    max_response_bytes: int = DEFAULT_DETAIL_MAX_BYTES,
    max_body_chars: int = DEFAULT_SKILL_BODY_MAX_CHARS,
    checkpoint_path: Path | None = None,
    progress_every: int = 0,
    status_path: Path | None = None,
) -> dict[str, Any]:
    raw_skills = catalog.get("skills")
    skills = raw_skills if isinstance(raw_skills, list) else []
    total = len(skills)
    checkpoint_applied = _apply_hydration_checkpoint(catalog, checkpoint_path)
    def has_available_body(item: dict[str, Any]) -> bool:
        return bool(item.get("body_available") or item.get("skill_body"))

    already_available = sum(
        1 for item in skills
        if isinstance(item, dict) and has_available_body(item)
    )
    pending = [
        item for item in skills
        if isinstance(item, dict)
        and not has_available_body(item)
    ]
    fetchable = [
        item for item in pending
        if str(item.get("detail_url") or "").strip()
    ]
    fetchable_count = len(fetchable)
    not_fetchable = len(pending) - fetchable_count
    candidates = fetchable
    if limit is not None:
        candidates = candidates[: max(limit, 0)]
    deferred_by_limit = fetchable_count - len(candidates)

    errors: list[dict[str, str]] = []
    hydrated = 0
    hydration_time = _utc_now()
    started = time.time()
    completed = 0

    def emit_status(*, final: bool = False) -> None:
        overall_done = min(total, already_available + completed)
        remaining_unhydrated = max(total - (already_available + hydrated), 0)
        elapsed = max(time.time() - started, 0.001)
        payload = {
            "status": "completed" if final else "running",
            "updated_at": _utc_now(),
            "total": total,
            "checkpoint_applied": checkpoint_applied,
            "skipped_by_checkpoint": checkpoint_applied,
            "already_available": already_available,
            "pending_unhydrated": len(pending),
            "fetchable_unhydrated": fetchable_count,
            "not_fetchable_unhydrated": not_fetchable,
            "deferred_by_limit": deferred_by_limit,
            "attempted_new": len(candidates),
            "completed_new": completed,
            "hydrated_new": hydrated,
            "errors_new": len(errors),
            "overall_completed": overall_done,
            "overall_remaining": max(total - overall_done, 0),
            "remaining_unhydrated": remaining_unhydrated,
            "percent": round((overall_done / total * 100.0) if total else 100.0, 4),
            "rate_per_second": round(completed / elapsed, 4),
            "elapsed_seconds": round(elapsed, 3),
        }
        if status_path is not None:
            _write_json_atomic(status_path, payload)

    def hydrate_one(
        item: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None, str | None, str | None]:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        url = str(item.get("detail_url") or "")
        if not _is_skills_sh_detail_url(url):
            return item, None, None, "refused non-skills.sh detail URL"
        html_text, error = _fetch_detail_html(
            url,
            timeout=timeout,
            max_bytes=max_response_bytes,
        )
        if error:
            return item, None, None, error
        body = _extract_skill_body_from_detail_html(html_text or "")
        if not body:
            raw_body, raw_url, raw_error = _fetch_github_raw_skill_body(
                item,
                timeout=timeout,
                max_bytes=max_response_bytes,
            )
            if raw_body:
                return item, raw_body, raw_url, None
            error = "detail page did not contain a parseable Skills.sh prose body"
            if raw_error:
                error = f"{error}; {raw_error}"
            return item, None, None, error
        return item, body, url, None

    if candidates:
        checkpoint_writer = (
            _open_checkpoint_writer(checkpoint_path)
            if checkpoint_path is not None else None
        )
        try:
            with cf.ThreadPoolExecutor(max_workers=max(workers, 1)) as executor:
                future_map = {executor.submit(hydrate_one, item): item for item in candidates}
                for future in cf.as_completed(future_map):
                    item, body, body_source_url, error = future.result()
                    completed += 1
                    if body:
                        item["body_truncated"] = len(body) > max_body_chars
                        if item["body_truncated"]:
                            body = body[:max_body_chars].rstrip()
                        item["skill_body"] = body
                        item["body_available"] = True
                        item["body_source_url"] = body_source_url or str(item.get("detail_url") or "")
                        item["body_hydrated_at"] = hydration_time
                        item.pop("body_error", None)
                        hydrated += 1
                    else:
                        item["body_available"] = False
                        if error:
                            item["body_error"] = error
                            errors.append({
                                "id": str(item.get("id") or ""),
                                "detail_url": str(item.get("detail_url") or ""),
                                "error": error,
                            })
                    if checkpoint_writer is not None:
                        json.dump(
                            _checkpoint_record(item),
                            checkpoint_writer,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        checkpoint_writer.write("\n")
                        checkpoint_writer.flush()
                    if progress_every > 0 and (
                        completed % progress_every == 0
                        or completed == len(candidates)
                    ):
                        elapsed = max(time.time() - started, 0.001)
                        rate = completed / elapsed
                        overall_done = min(total, already_available + completed)
                        pct = (overall_done / total * 100.0) if total else 100.0
                        print(
                            "hydrate progress: "
                            f"{overall_done:,}/{total:,} ({pct:.2f}%) "
                            f"new={completed:,}/{len(candidates):,} "
                            f"hydrated_new={hydrated:,} errors_new={len(errors):,} "
                            f"rate={rate:.1f}/s",
                            flush=True,
                        )
                        emit_status()
        finally:
            if checkpoint_writer is not None:
                checkpoint_writer.close()
    emit_status(final=True)

    summary = {
        "body_hydration_checkpoint_applied_count": checkpoint_applied,
        "body_hydration_attempted_count": len(candidates),
        "body_hydrated_count": hydrated,
        "body_hydration_error_count": len(errors),
        "body_hydration_errors_sample": errors[:20],
    }
    catalog.update(summary)
    return _refresh_body_summary(catalog)


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
            "type": "skill",
            "status": "remote-cataloged",
            "source_catalog": "skills.sh",
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
            "Entries are remote-cataloged skills until their full SKILL.md bodies are hydrated and reviewed.",
        ],
        "skills": normalized,
    }


def write_gzip_json(path: Path, data: dict[str, Any]) -> None:
    data = _catalog_for_storage(data)
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


def _catalog_for_storage(catalog: dict[str, Any]) -> dict[str, Any]:
    """Return a catalog copy suitable for checked-in JSON artifacts.

    Hydrated SKILL.md bodies are shipped in the wiki under
    ``converted/<skills-sh-slug>/SKILL.md``. Keeping the same body text inside
    the catalog JSON would duplicate tens of thousands of markdown files and
    make both GitHub and Hugging Face snapshots unnecessarily large.
    """
    raw_skills = catalog.get("skills")
    if not isinstance(raw_skills, list):
        return catalog
    stored = dict(catalog)
    stored_skills: list[Any] = []
    for item in raw_skills:
        if not isinstance(item, dict):
            stored_skills.append(item)
            continue
        out = dict(item)
        body = str(out.pop("skill_body", "") or "")
        if body:
            out["body_char_count"] = len(body)
            out["body_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        stored_skills.append(out)
    stored["skills"] = stored_skills
    return stored


def _fmt_count(value: Any) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    return "unknown"


def render_external_readme(catalog: dict[str, Any]) -> str:
    return f"""# Skills.sh Catalog

This directory is generated by `src/import_skills_sh_catalog.py`.

- Source: https://skills.sh/
- API surface used: `{SKILLS_SH_API}`
- Observed unique skills: {_fmt_count(catalog.get("observed_unique_skills"))}
- Site-reported total at fetch time: {_fmt_count(catalog.get("site_reported_total"))}
- Existing wiki skill-id overlaps: {_fmt_count(catalog.get("overlap", {}).get("skill_id_matches_existing_wiki"))}
- Resolved ctx slug collisions: {_fmt_count(catalog.get("ctx_slug_collisions_resolved"))}
- Remote-cataloged skill graph nodes: {_fmt_count(catalog.get("graph_skill_nodes"))}
- Hydrated upstream bodies: {_fmt_count(catalog.get("body_available_count"))}

The catalog is stored as first-class remote-cataloged `skill` entities. Hydrated
upstream bodies are shipped as `converted/skills-sh-*/SKILL.md` wiki pages and
referenced from each entity's `converted_path`. Skills.sh provenance, install
commands, and metadata-only security status remain attached until a candidate is
reviewed and promoted.
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
        "body_available": bool(item.get("body_available") or item.get("skill_body")),
        "security_review": "metadata-only",
        "promotion_state": "alias" if duplicate_targets else "remote-cataloged",
    }


def _skills_sh_entity_path(ctx_slug: str) -> str:
    return f"{SKILLS_SH_ENTITY_ROOT}/{ctx_slug}.md"


def _skills_sh_converted_path(ctx_slug: str) -> str:
    return f"{CONVERTED_SKILL_ROOT}/{ctx_slug}/SKILL.md"


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
    merge_state = "alias-of-curated" if duplicate_targets else "remote-cataloged"
    duplicate_target = duplicate_targets[0] if duplicate_targets else "null"
    tags = [str(tag) for tag in item.get("tags", []) if tag]
    skill_body = str(item.get("skill_body") or "").strip()
    body_available = bool(quality.get("body_available") or skill_body)
    body_source_url = str(item.get("body_source_url") or "")
    body_status = (
        "hydrated from Skills.sh detail page."
        if body_available else
        "metadata-only; canonical body remains upstream."
    )
    body = f"""---
title: {json.dumps(label, ensure_ascii=False)}
type: skill
status: remote-cataloged
source_catalog: skills.sh
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
body_available: {str(body_available).lower()}
body_source_url: {json.dumps(body_source_url, ensure_ascii=False)}
converted_path: {json.dumps(str(item.get("converted_path") or ""), ensure_ascii=False)}
security_review: metadata-only
---

# {label}

Remote-cataloged Skills.sh skill.

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
- Body availability: {body_status}
- Hydrated SKILL.md path: `{item.get("converted_path") or ""}`.
- Security review: metadata-only. Fetch and inspect the body before promotion to curated `skill`.
"""
    return body


def _skills_sh_node_ids(catalog: dict[str, Any]) -> set[str]:
    return {
        SKILLS_SH_NODE_PREFIX + str(item.get("ctx_slug") or "")
        for item in _catalog_skills(catalog)
        if str(item.get("ctx_slug") or "")
    }


def _is_skills_sh_graph_node(node: dict[str, Any]) -> bool:
    node_id = str(node.get("id") or "")
    return node_id.startswith(SKILLS_SH_NODE_PREFIX + "skills-sh-") or (
        node.get("source_catalog") == "skills.sh"
        and node.get("type") == "skill"
    )


def _filter_skills_sh_communities(
    communities: dict[str, Any],
    valid_node_ids: set[str],
) -> dict[str, Any]:
    raw_communities = communities.get("communities")
    if not isinstance(raw_communities, dict):
        return communities
    filtered: dict[str, Any] = {}
    for community_id, raw_community in raw_communities.items():
        if not isinstance(raw_community, dict):
            continue
        raw_members = raw_community.get("members")
        if not isinstance(raw_members, list):
            filtered[str(community_id)] = raw_community
            continue
        members = [
            str(member) for member in raw_members
            if not (
                str(member).startswith(SKILLS_SH_NODE_PREFIX + "skills-sh-")
                and str(member) not in valid_node_ids
            )
        ]
        if not members:
            continue
        community = dict(raw_community)
        community["members"] = members
        filtered[str(community_id)] = community
    out = dict(communities)
    out["communities"] = filtered
    out["total_communities"] = len(filtered)
    out["generated"] = _utc_now()
    return out


def _render_graph_report(graph: dict[str, Any], communities: dict[str, Any] | None) -> str:
    nodes = graph.get("nodes")
    edges = graph.get("edges") if "edges" in graph else graph.get("links")
    if not isinstance(nodes, list):
        nodes = []
    if not isinstance(edges, list):
        edges = []
    degree: dict[str, int] = {}
    node_by_id = {
        str(node.get("id") or ""): node
        for node in nodes
        if isinstance(node, dict) and str(node.get("id") or "")
    }
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source:
            degree[source] = degree.get(source, 0) + 1
        if target:
            degree[target] = degree.get(target, 0) + 1
    top_nodes = sorted(degree.items(), key=lambda item: (-item[1], item[0]))[:20]
    community_count = 0
    if isinstance(communities, dict):
        raw_communities = communities.get("communities")
        if isinstance(raw_communities, dict):
            community_count = len(raw_communities)
    lines = [
        "# Graph Report",
        "",
        f"> Generated: {_utc_now()}",
        f"> Nodes: {len(nodes):,} | Edges: {len(edges):,} | Communities: {community_count:,}",
        "",
        "## Most Connected Nodes",
        "",
    ]
    for node_id, count in top_nodes:
        node = node_by_id.get(node_id, {})
        label = node.get("label") or node_id
        node_type = node.get("type") or "unknown"
        lines.append(f"- **{label}** ({count:,} connections) - {node_type}")
    lines.append("")
    return "\n".join(lines)


def _augment_graph_with_external_nodes(graph: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    nodes = graph.get("nodes")
    edges = graph.get("edges") if "edges" in graph else graph.get("links")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return graph

    skills = catalog.get("skills")
    if not isinstance(skills, list):
        return graph
    valid_skills_sh_ids = _skills_sh_node_ids(catalog)

    legacy_skills_sh_ids = {
        str(node.get("id"))
        for node in nodes
        if str(node.get("id") or "").startswith(LEGACY_EXTERNAL_NODE_PREFIX)
        or (
            node.get("external_catalog") == "skills.sh"
            and node.get("type") == "external-skill"
        )
    }
    if legacy_skills_sh_ids:
        nodes = [node for node in nodes if str(node.get("id")) not in legacy_skills_sh_ids]
        edges = [
            edge for edge in edges
            if str(edge.get("source")) not in legacy_skills_sh_ids
            and str(edge.get("target")) not in legacy_skills_sh_ids
        ]
    stale_skills_sh_ids = {
        str(node.get("id") or "")
        for node in nodes
        if isinstance(node, dict)
        and _is_skills_sh_graph_node(node)
        and str(node.get("id") or "") not in valid_skills_sh_ids
    }
    if stale_skills_sh_ids:
        nodes = [
            node for node in nodes
            if str(node.get("id") or "") not in stale_skills_sh_ids
        ]
        edges = [
            edge for edge in edges
            if str(edge.get("source") or "") not in stale_skills_sh_ids
            and str(edge.get("target") or "") not in stale_skills_sh_ids
        ]
    edges = [edge for edge in edges if edge.get("source_catalog") != "skills.sh"]
    node_by_id = {
        str(node.get("id")): node
        for node in nodes
        if str(node.get("id") or "")
    }
    existing_skills_sh_incident_edges: dict[str, int] = {}
    for edge in edges:
        for endpoint in (str(edge.get("source") or ""), str(edge.get("target") or "")):
            if endpoint.startswith(SKILLS_SH_NODE_PREFIX + "skills-sh-"):
                existing_skills_sh_incident_edges[endpoint] = (
                    existing_skills_sh_incident_edges.get(endpoint, 0) + 1
                )

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
        if (
            node_id.startswith(SKILLS_SH_NODE_PREFIX + "skills-sh-")
            or node.get("source_catalog") == "skills.sh"
        ):
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
        external_id = SKILLS_SH_NODE_PREFIX + ctx_slug
        tags = [str(tag) for tag in item.get("tags", []) if tag]
        skill_id = str(item.get("skill_id") or "").lower()
        duplicate_targets = label_index.get(skill_id, [])[:MAX_EXTERNAL_EDGES_PER_NODE]
        quality = _quality_signals(item, duplicate_targets)
        item["type"] = "skill"
        item["status"] = "remote-cataloged"
        item["source_catalog"] = "skills.sh"
        item.pop("external_catalog", None)
        item["graph_node_id"] = external_id
        item["entity_path"] = _skills_sh_entity_path(ctx_slug)
        item["converted_path"] = (
            _skills_sh_converted_path(ctx_slug)
            if quality.get("body_available") else None
        )
        item["merge_state"] = "alias-of-curated" if duplicate_targets else "remote-cataloged"
        item["duplicate_of"] = duplicate_targets[0] if duplicate_targets else None
        item["duplicate_targets"] = duplicate_targets
        item["quality_score"] = quality["score"]
        item["quality_signals"] = quality
        node_payload = {
            "id": external_id,
            "label": ctx_slug,
            "type": "skill",
            "status": "remote-cataloged",
            "source_catalog": "skills.sh",
            "ctx_slug": ctx_slug,
            "source": item.get("source"),
            "skill_id": item.get("skill_id"),
            "installs": item.get("installs"),
            "tags": tags,
            "detail_url": item.get("detail_url"),
            "install_command": item.get("install_command"),
            "merge_state": item["merge_state"],
            "duplicate_of": item["duplicate_of"],
            "quality_score": item["quality_score"],
            "quality_signals": item["quality_signals"],
            "entity_path": item["entity_path"],
            "converted_path": item["converted_path"],
        }
        existing_node = node_by_id.get(external_id)
        if existing_node is None:
            nodes.append(node_payload)
            node_by_id[external_id] = node_payload
        else:
            existing_node.update(node_payload)

        # Full graphify can already emit semantic/tag/token edges for hydrated
        # Skills.sh pages. Preserve those and only fall back to sparse metadata
        # edges for catalog-only nodes with no graphify-produced connectivity.
        if existing_skills_sh_incident_edges.get(external_id, 0) > 0:
            continue

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
                "source_catalog": "skills.sh",
            })
            added_edges += 1
            if len(seen_targets) >= MAX_EXTERNAL_EDGES_PER_NODE:
                break

    graph["nodes"] = nodes
    edge_key = "edges" if "edges" in graph else "links"
    graph[edge_key] = edges
    metadata = graph.setdefault("graph", {})
    if isinstance(metadata, dict):
        external_nodes_meta = metadata.get("external_catalog_nodes")
        if isinstance(external_nodes_meta, dict):
            external_nodes_meta.pop("skills.sh", None)
            if not external_nodes_meta:
                metadata.pop("external_catalog_nodes", None)
        external_edges_meta = metadata.get("external_catalog_edges")
        if isinstance(external_edges_meta, dict):
            external_edges_meta.pop("skills.sh", None)
            if not external_edges_meta:
                metadata.pop("external_catalog_edges", None)
        metadata.setdefault("source_catalog_nodes", {})
        metadata["source_catalog_nodes"]["skills.sh"] = len(skills)
        metadata.setdefault("source_catalog_edges", {})
        metadata["source_catalog_edges"]["skills.sh"] = added_edges
    catalog["graph_skill_nodes"] = len(skills)
    catalog["graph_skill_edges"] = added_edges
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
    info.mtime = 0 if mtime is None else mtime
    info.mode = mode
    dst.addfile(info, BytesIO(payload))


def _converted_skill_files_from_body(
    *,
    converted_path: str,
    skill_body: str,
) -> dict[str, bytes]:
    body = skill_body.rstrip() + "\n"
    root = converted_path.removesuffix("/SKILL.md")
    if not root.startswith(f"{CONVERTED_SKILL_ROOT}/skills-sh-"):
        return {converted_path: body.encode("utf-8")}
    from ctx_config import cfg as _cfg
    line_threshold = _cfg.line_threshold
    if len(body.splitlines()) <= line_threshold:
        return {converted_path: body.encode("utf-8")}

    try:
        import batch_convert
    except Exception as exc:
        raise ConvertedSkillPackagingError(f"micro-skill converter unavailable: {exc}") from exc

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "converted" / Path(root).name
        output_dir.mkdir(parents=True, exist_ok=True)
        virtual_source = tmp_path / "source" / Path(root).name / "SKILL.md"
        result = batch_convert.convert_skill(
            virtual_source,
            output_dir=output_dir,
            source_content=body,
            skill_name=Path(root).name,
            preserve_original=False,
        )
        if result.get("status") != "converted":
            raise ConvertedSkillPackagingError(
                f"micro-skill converter returned {result.get('status')!r}",
            )
        files: dict[str, bytes] = {}
        for path in sorted(output_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(output_dir).as_posix()
            if rel.endswith(".original"):
                continue
            try:
                files[f"{root}/{rel}"] = _read_bytes_with_retry(path)
            except OSError as exc:
                raise ConvertedSkillPackagingError(
                    f"generated file {rel!r} could not be read: {exc}",
                ) from exc
        if converted_path not in files:
            raise ConvertedSkillPackagingError("micro-skill converter did not emit SKILL.md")
        return files


def _read_bytes_with_retry(
    path: Path,
    *,
    attempts: int = 50,
    delay_seconds: float = 0.2,
) -> bytes:
    """Read a generated file despite transient Windows scanner locks."""
    last_error: OSError | None = None
    for attempt in range(max(attempts, 1)):
        try:
            return path.read_bytes()
        except OSError as exc:
            last_error = exc
            if attempt + 1 >= max(attempts, 1):
                break
            time.sleep(max(delay_seconds, 0.0))
    assert last_error is not None
    raise last_error


def _stage_inline_skill_bodies_for_packaging(
    catalog: dict[str, Any],
) -> dict[str, dict[str, bytes]]:
    """Precompute converted files and downgrade bodies that cannot ship safely."""
    raw_skills = catalog.get("skills")
    skills = raw_skills if isinstance(raw_skills, list) else []
    staged: dict[str, dict[str, bytes]] = {}
    for item in skills:
        if not isinstance(item, dict):
            continue
        ctx_slug = str(item.get("ctx_slug") or "")
        skill_body = str(item.get("skill_body") or "").strip()
        converted_path = str(item.get("converted_path") or "")
        if not ctx_slug or not skill_body or not converted_path:
            continue
        try:
            staged[ctx_slug] = _converted_skill_files_from_body(
                converted_path=converted_path,
                skill_body=skill_body,
            )
        except ConvertedSkillPackagingError as exc:
            item["body_available"] = False
            item["converted_path"] = None
            item["body_error"] = f"local micro-skill packaging failed: {exc}"
            item.pop("skill_body", None)
            item.pop("body_char_count", None)
            item.pop("body_sha256", None)
            quality = item.get("quality_signals")
            if isinstance(quality, dict):
                quality["body_available"] = False
    _refresh_body_summary(catalog)
    return staged


def update_wiki_tarball(tarball: Path, catalog: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp_file:
        tmp_path = Path(tmp_file.name)
    try:
        with tarfile.open(tarball, "r:gz") as src, tarfile.open(tmp_path, "w:gz") as dst:
            members = src.getmembers()
            valid_skills_sh_ids = _skills_sh_node_ids(catalog)
            existing_converted_paths = {
                safe_name
                for member in members
                if (safe_name := _safe_tar_name(member.name)) is not None
                and member.isfile()
                and safe_name.startswith(f"{CONVERTED_SKILL_ROOT}/skills-sh-")
                and safe_name.endswith("/SKILL.md")
            }
            _reconcile_body_availability_with_tar(catalog, existing_converted_paths)
            staged_converted_files = _stage_inline_skill_bodies_for_packaging(catalog)
            replacement_slugs = set(staged_converted_files)
            graph_for_report: dict[str, Any] | None = None
            communities_for_report: dict[str, Any] | None = None

            for member in members:
                safe_name = _safe_tar_name(member.name)
                if safe_name is None:
                    continue
                parts = safe_name.split("/", 2)
                is_skills_sh_converted = (
                    len(parts) >= 2
                    and parts[0] == CONVERTED_SKILL_ROOT
                    and parts[1].startswith("skills-sh-")
                )
                if (
                    safe_name.startswith("external-catalogs/skills-sh/")
                    or safe_name.startswith(f"{EXTERNAL_ENTITY_ROOT}/")
                    or (
                        safe_name.startswith(f"{SKILLS_SH_ENTITY_ROOT}/skills-sh-")
                        and safe_name.endswith(".md")
                    )
                    or (
                        is_skills_sh_converted
                        and len(parts) >= 2
                        and parts[1] in replacement_slugs
                    )
                    or safe_name.endswith(".original")
                ):
                    continue
                if safe_name == "graphify-out/graph-report.md":
                    continue
                if member.isfile():
                    f = src.extractfile(member)
                    if f is None:
                        continue
                    if safe_name == "graphify-out/graph.json":
                        graph = json.loads(f.read().decode("utf-8"))
                        graph = _augment_graph_with_external_nodes(graph, catalog)
                        graph_for_report = graph
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
                    if safe_name == "graphify-out/communities.json":
                        communities = json.loads(f.read().decode("utf-8"))
                        if isinstance(communities, dict):
                            communities = _filter_skills_sh_communities(
                                communities,
                                valid_skills_sh_ids,
                            )
                            communities_for_report = communities
                        payload = json.dumps(
                            communities,
                            ensure_ascii=False,
                            indent=2,
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
                elif member.isdir():
                    dst.addfile(member)

            stored_catalog = _catalog_for_storage(catalog)
            catalog_bytes = json.dumps(
                stored_catalog,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            readme_bytes = render_external_readme(stored_catalog).encode("utf-8")
            summary = {
                k: catalog.get(k)
                for k in (
                    "schema_version", "source", "api", "fetched_at", "site_reported_total",
                    "observed_unique_skills", "coverage_vs_site_reported_total",
                    "query_count", "query_error_count", "ctx_slug_collisions_resolved", "overlap",
                    "graph_skill_nodes", "graph_skill_edges", "body_available_count",
                    "body_hydration_attempted_count", "body_hydrated_count",
                    "body_hydration_error_count",
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
                skill_body = str(item.get("skill_body") or "").strip()
                converted_path = str(item.get("converted_path") or "")
                if skill_body and converted_path:
                    ctx_slug = str(item.get("ctx_slug") or "")
                    converted_files = staged_converted_files.get(ctx_slug, {})
                    for path, payload in converted_files.items():
                        _add_bytes(dst, name=f"./{path}", payload=payload)
            for name, payload in (
                ("external-catalogs/skills-sh/catalog.json", catalog_bytes),
                ("external-catalogs/skills-sh/summary.json", summary_bytes),
                ("external-catalogs/skills-sh/README.md", readme_bytes),
            ):
                _add_bytes(dst, name=f"./{name}", payload=payload)
            if graph_for_report is not None:
                _add_bytes(
                    dst,
                    name="./graphify-out/graph-report.md",
                    payload=_render_graph_report(
                        graph_for_report,
                        communities_for_report,
                    ).encode("utf-8"),
                )
        promote_staged_artifact(
            tmp_path,
            tarball,
            validate=_validate_wiki_tarball_candidate,
        )
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _validate_wiki_tarball_candidate(candidate: Path) -> None:
    required = {
        "graphify-out/graph.json",
        "external-catalogs/skills-sh/catalog.json",
    }
    seen: set[str] = set()
    with tarfile.open(candidate, "r:gz") as tf:
        for member in tf.getmembers():
            safe_name = _safe_tar_name(member.name)
            if safe_name is None:
                raise ValueError(f"unsafe tar member in candidate: {member.name!r}")
            if safe_name.endswith(".original"):
                raise ValueError(f"raw backup member leaked into candidate: {safe_name}")
            seen.add(safe_name)
            if safe_name == "graphify-out/graph.json":
                graph = _read_tar_json(tf, member, "graph.json")
                if not isinstance(graph.get("nodes"), list) or not isinstance(
                    graph.get("edges"), list
                ):
                    raise ValueError("candidate graph.json must contain nodes and edges")
            elif safe_name == "external-catalogs/skills-sh/catalog.json":
                catalog = _read_tar_json(tf, member, "Skills.sh catalog")
                if not isinstance(catalog.get("skills"), list):
                    raise ValueError("candidate Skills.sh catalog must contain skills")
    missing = sorted(required - seen)
    if missing:
        raise ValueError(f"candidate wiki tarball is missing required members: {missing}")


def _read_tar_json(tf: tarfile.TarFile, member: tarfile.TarInfo, label: str) -> dict[str, Any]:
    f = tf.extractfile(member)
    if f is None:
        raise ValueError(f"candidate {label} member is not readable")
    data = json.loads(f.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"candidate {label} must be a JSON object")
    return data


def _reconcile_body_availability_with_tar(
    catalog: dict[str, Any],
    existing_converted_paths: set[str],
) -> None:
    """Align catalog hydration metadata with bodies that will ship.

    ``graph/skills-sh-catalog.json.gz`` intentionally strips full
    ``skill_body`` text. A tarball refresh from that checked-in catalog must
    therefore preserve already-converted bodies instead of deleting them and
    falsely leaving ``body_available: true`` records behind.
    """
    raw_skills = catalog.get("skills")
    skills = raw_skills if isinstance(raw_skills, list) else []
    for item in skills:
        if not isinstance(item, dict):
            continue
        ctx_slug = str(item.get("ctx_slug") or "")
        if not ctx_slug:
            continue
        converted_path = str(item.get("converted_path") or _skills_sh_converted_path(ctx_slug))
        has_inline_body = bool(str(item.get("skill_body") or "").strip())
        has_existing_body = converted_path in existing_converted_paths
        if has_inline_body or has_existing_body:
            item["body_available"] = True
            item["converted_path"] = converted_path
            if has_existing_body and not has_inline_body:
                item.pop("body_error", None)
        else:
            item["body_available"] = False
            item["converted_path"] = None


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


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as tmp:
        tmp.write(payload)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _open_checkpoint_reader(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("rt", encoding="utf-8")


def _open_checkpoint_writer(path: Path) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".gz":
        return gzip.open(path, "at", encoding="utf-8")
    return path.open("a", encoding="utf-8")


def _checkpoint_record(item: dict[str, Any]) -> dict[str, Any]:
    return {field: item[field] for field in _HYDRATION_CHECKPOINT_FIELDS if field in item}


def _iter_gzip_checkpoint_lines(path: Path) -> list[str]:
    lines: list[str] = []
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    pending = b""
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            data = chunk
            while data:
                try:
                    pending += decoder.decompress(data)
                except zlib.error:
                    return lines
                if decoder.unused_data:
                    data = decoder.unused_data
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    continue
                break
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                lines.append(line.decode("utf-8", errors="replace"))
    return lines


def _iter_checkpoint_lines(path: Path) -> list[str]:
    if path.suffix == ".gz":
        return _iter_gzip_checkpoint_lines(path)
    with _open_checkpoint_reader(path) as f:
        return list(f)


def _apply_hydration_checkpoint(catalog: dict[str, Any], path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    records: dict[str, dict[str, Any]] = {}
    for line in _iter_checkpoint_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("id"):
            records[str(record["id"])] = record
    if not records:
        return 0
    raw_skills = catalog.get("skills")
    skills = raw_skills if isinstance(raw_skills, list) else []
    applied = 0
    for item in skills:
        if not isinstance(item, dict):
            continue
        record = records.get(str(item.get("id") or ""))
        if not record:
            continue
        for field in _HYDRATION_CHECKPOINT_FIELDS:
            if field in {"id", "ctx_slug"}:
                continue
            if field in record:
                item[field] = record[field]
        applied += 1
    return applied


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
    parser.add_argument(
        "--drop-body-unavailable",
        action="store_true",
        help="Delete Skills.sh records that have no packaged SKILL.md/prose body",
    )
    parser.add_argument("--hydrate-bodies", action="store_true")
    parser.add_argument("--hydrate-limit", type=int, default=None)
    parser.add_argument("--hydrate-workers", type=int, default=4)
    parser.add_argument("--hydrate-delay-ms", type=int, default=0)
    parser.add_argument("--hydrate-timeout", type=int, default=30)
    parser.add_argument("--hydrate-max-response-bytes", type=int, default=DEFAULT_DETAIL_MAX_BYTES)
    parser.add_argument("--hydrate-max-body-chars", type=int, default=DEFAULT_SKILL_BODY_MAX_CHARS)
    parser.add_argument("--hydrate-progress-every", type=int, default=1000)
    parser.add_argument(
        "--hydrate-status",
        type=Path,
        default=None,
        help="Atomically updated JSON status file for long Skills.sh hydration runs",
    )
    parser.add_argument(
        "--hydrate-checkpoint",
        type=Path,
        default=None,
        help="JSONL or JSONL.gz checkpoint for resumable Skills.sh body hydration",
    )
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
    if args.hydrate_bodies:
        summary = hydrate_catalog_bodies(
            catalog,
            workers=args.hydrate_workers,
            limit=args.hydrate_limit,
            delay_seconds=max(args.hydrate_delay_ms, 0) / 1000.0,
            timeout=args.hydrate_timeout,
            max_response_bytes=args.hydrate_max_response_bytes,
            max_body_chars=args.hydrate_max_body_chars,
            checkpoint_path=args.hydrate_checkpoint,
            progress_every=max(args.hydrate_progress_every, 0),
            status_path=args.hydrate_status,
        )
        print(
            "hydrated Skills.sh bodies: "
            f"{summary['body_hydrated_count']:,}/"
            f"{summary['body_hydration_attempted_count']:,} attempted "
            f"({summary['body_hydration_error_count']:,} errors)"
        )
    if args.drop_body_unavailable:
        summary = drop_body_unavailable_skills(catalog)
        print(
            "dropped body-unavailable Skills.sh records: "
            f"{summary['pruned']:,} pruned; {summary['kept']:,} kept "
            f"(original={summary['original']:,})"
        )
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
