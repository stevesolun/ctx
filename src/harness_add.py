#!/usr/bin/env python3
"""Add harness records to the ctx wiki catalog.

Harness records are catalog-only. They describe the runtime machinery around
a model: where the agent runs, which tools and files it can access, how model
credentials are supplied, and how work is verified. Adding a harness does not
install or execute the upstream project.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml  # type: ignore[import-untyped]

from ctx.core.entity_update import build_update_review, render_update_review
from ctx.core.wiki.wiki_sync import append_log, ensure_wiki, update_index
from ctx.utils._fs_utils import safe_atomic_write_text
from ctx_config import cfg
from mcp_entity import canonicalize_github_url, normalize_slug

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

_HARNESS_ENTITY_SUBDIR = "entities/harnesses"


def _split_values(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = []
        for item in raw:
            values.extend(str(item).split(","))
    else:
        return ()
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if item and item not in seen:
            cleaned.append(item)
            seen.add(item)
    return tuple(cleaned)


def _normalize_tag_values(raw: object) -> tuple[str, ...]:
    tags = {
        normalize_slug(tag)
        for tag in _split_values(raw)
        if tag.strip()
    }
    return tuple(sorted(tags)) or ("harness", "llm")


def _normalize_repo_url(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("harness record requires a non-empty repo_url")
    candidate = raw.strip().rstrip("/")
    github = canonicalize_github_url(candidate)
    if github is not None:
        return github
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"repo_url must be an http(s) URL: {raw!r}")
    return candidate


def _repo_name(repo_url: str) -> str:
    parsed = urlparse(repo_url)
    path = parsed.path.rstrip("/")
    last_segment = path.rsplit("/", 1)[-1].removesuffix(".git")
    return last_segment or parsed.netloc


def _display_name_from_repo(repo_url: str) -> str:
    return _repo_name(repo_url).replace("-", " ").replace("_", " ").title()


def _description(raw: object, repo_url: str) -> str:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return f"Harness catalog entry for {repo_url}."


def _optional_url(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = raw.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"docs_url must be an http(s) URL: {raw!r}")
    return candidate


@dataclass(frozen=True)
class HarnessRecord:
    slug: str
    name: str
    description: str
    repo_url: str
    docs_url: str | None = None
    tags: tuple[str, ...] = ("harness", "llm")
    model_providers: tuple[str, ...] = ()
    runtimes: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    setup_commands: tuple[str, ...] = ()
    verify_commands: tuple[str, ...] = ()
    sources: tuple[str, ...] = ("manual",)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HarnessRecord:
        if not isinstance(data, dict):
            raise ValueError(f"from_dict expected dict, got {type(data).__name__}")

        repo_url = _normalize_repo_url(data.get("repo_url") or data.get("repo"))
        name_value = data.get("name")
        name = (
            name_value.strip()
            if isinstance(name_value, str) and name_value.strip()
            else _display_name_from_repo(repo_url)
        )
        slug_source = data.get("slug") or name or _repo_name(repo_url)
        slug = normalize_slug(str(slug_source))

        return cls(
            slug=slug,
            name=name,
            description=_description(data.get("description"), repo_url),
            repo_url=repo_url,
            docs_url=_optional_url(data.get("docs_url")),
            tags=_normalize_tag_values(data.get("tags")),
            model_providers=_split_values(data.get("model_providers")),
            runtimes=_split_values(data.get("runtimes")),
            capabilities=_split_values(data.get("capabilities")),
            setup_commands=_split_values(data.get("setup_commands")),
            verify_commands=_split_values(data.get("verify_commands")),
            sources=_split_values(data.get("sources")) or ("manual",),
            raw=dict(data),
        )

    def to_frontmatter(self, *, created: str | None = None) -> dict[str, Any]:
        fm: dict[str, Any] = {
            "title": self.name,
            "created": created or TODAY,
            "updated": TODAY,
            "type": "harness",
            "status": "cataloged",
            "tags": list(self.tags),
            "repo_url": self.repo_url,
            "sources": list(self.sources),
            "model_providers": list(self.model_providers),
            "runtimes": list(self.runtimes),
            "capabilities": list(self.capabilities),
            "setup_commands": list(self.setup_commands),
            "verify_commands": list(self.verify_commands),
        }
        if self.docs_url:
            fm["docs_url"] = self.docs_url
        return fm


def _parse_frontmatter(text: str) -> dict[str, Any]:
    match = re.match(r"^---\n(.*?\n)---\n", text, re.DOTALL)
    if not match:
        return {}
    try:
        parsed = yaml.safe_load(match.group(1))
        return parsed if isinstance(parsed, dict) else {}
    except yaml.YAMLError:
        return {}


def _markdown_list(items: tuple[str, ...], empty: str) -> str:
    return "\n".join(f"- {item}" for item in items) if items else f"- {empty}"


def _command_block(commands: tuple[str, ...], empty: str) -> str:
    if not commands:
        return empty
    body = "\n".join(commands)
    return f"```bash\n{body}\n```"


def generate_harness_page(record: HarnessRecord, *, created: str | None = None) -> str:
    frontmatter = yaml.safe_dump(
        record.to_frontmatter(created=created),
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    docs_line = f"- [Documentation]({record.docs_url})\n" if record.docs_url else ""
    source_lines = f"- [Repository]({record.repo_url})\n{docs_line}".rstrip()

    return f"""---
{frontmatter}---

# {record.name}

## Overview

{record.description}

## When to Recommend

{_markdown_list(record.capabilities, "Use when the project needs this harness profile.")}

## Model Providers

{_markdown_list(record.model_providers, "No explicit provider constraints cataloged.")}

## Runtimes

{_markdown_list(record.runtimes, "No explicit runtime constraints cataloged.")}

## Setup

{_command_block(record.setup_commands, "Read the upstream repository before installing.")}

## Verification

{_command_block(record.verify_commands, "No verification command cataloged.")}

## Sources

{source_lines}

## Related Harnesses

<!-- backlinks added later by graph build -->
"""


def _merge_sources(
    existing_fm: dict[str, Any],
    new_sources: tuple[str, ...],
) -> tuple[str, ...]:
    existing = existing_fm.get("sources", []) or []
    return tuple(sorted(set(str(source) for source in existing) | set(new_sources)))


def add_harness(
    *,
    record: HarnessRecord,
    wiki_path: Path,
    dry_run: bool = False,
    skip_existing: bool = False,
    review_existing: bool = False,
    update_existing: bool = False,
) -> dict[str, Any]:
    target_path = wiki_path / _HARNESS_ENTITY_SUBDIR / f"{record.slug}.md"
    is_new_page = not target_path.exists()

    if skip_existing and not is_new_page:
        return {
            "slug": record.slug,
            "is_new_page": False,
            "skipped": True,
            "path": str(target_path),
            "sources": list(record.sources),
        }

    existing_fm: dict[str, Any] = {}
    existing_text = ""
    created = TODAY
    merged_sources = record.sources
    if target_path.exists():
        existing_text = target_path.read_text(encoding="utf-8", errors="replace")
        existing_fm = _parse_frontmatter(existing_text)
        created = str(existing_fm.get("created") or TODAY)
        merged_sources = _merge_sources(existing_fm, record.sources)

    final_record = replace(record, sources=merged_sources)
    proposed_text = generate_harness_page(final_record, created=created)

    if review_existing and not is_new_page and not update_existing:
        review = build_update_review(
            entity_type="harness",
            slug=record.slug,
            existing_text=existing_text,
            proposed_text=proposed_text,
        )
        return {
            "slug": record.slug,
            "is_new_page": False,
            "skipped": True,
            "update_required": True,
            "update_review": render_update_review(review),
            "path": str(target_path),
            "sources": list(merged_sources),
        }

    if not dry_run:
        ensure_wiki(str(wiki_path))
        safe_atomic_write_text(target_path, proposed_text, encoding="utf-8")
        if is_new_page:
            update_index(str(wiki_path), [record.slug], subject_type="harnesses")
        append_log(
            str(wiki_path),
            "add-harness",
            record.slug,
            [
                f"Repository: {record.repo_url}",
                f"Sources: {', '.join(merged_sources)}",
                f"Tags: {', '.join(record.tags)}",
            ],
        )

    return {
        "slug": record.slug,
        "is_new_page": is_new_page,
        "skipped": False,
        "update_required": False,
        "path": str(target_path),
        "sources": list(merged_sources),
    }


def _record_from_args(args: argparse.Namespace) -> HarnessRecord:
    return HarnessRecord.from_dict(
        {
            "repo_url": args.repo,
            "slug": args.slug,
            "name": args.name,
            "description": args.description,
            "docs_url": args.docs_url,
            "tags": args.tag,
            "model_providers": args.model_provider,
            "runtimes": args.runtime,
            "capabilities": args.capability,
            "setup_commands": args.setup_command,
            "verify_commands": args.verify_command,
            "sources": args.source,
        }
    )


def _load_json_record(path: Path) -> HarnessRecord:
    data = json.loads(path.read_text(encoding="utf-8"))
    return HarnessRecord.from_dict(data)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Add a harness wiki catalog entry")
    parser.add_argument("--repo", help="Harness repository URL")
    parser.add_argument("--from-json", help="Load one harness record from JSON")
    parser.add_argument("--slug", help="Explicit harness slug")
    parser.add_argument("--name", help="Display name")
    parser.add_argument("--description", help="Short recommendation description")
    parser.add_argument("--docs-url", help="Documentation URL")
    parser.add_argument("--tag", action="append", help="Tag, repeatable or comma-separated")
    parser.add_argument("--model-provider", action="append", help="Supported provider")
    parser.add_argument("--runtime", action="append", help="Runtime, repeatable")
    parser.add_argument("--capability", action="append", help="When to recommend it")
    parser.add_argument("--setup-command", action="append", help="Setup command to document")
    parser.add_argument("--verify-command", action="append", help="Verification command")
    parser.add_argument("--source", action="append", default=["manual"], help="Catalog source")
    parser.add_argument("--wiki", default=str(cfg.wiki_dir), help="Wiki path")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--skip-existing", action="store_true", help="Do not rewrite existing page")
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Apply the reviewed replacement when the harness already exists",
    )
    args = parser.parse_args(argv)

    if bool(args.repo) == bool(args.from_json):
        parser.error("use exactly one of --repo or --from-json")

    try:
        record = (
            _load_json_record(Path(os.path.expanduser(args.from_json)))
            if args.from_json
            else _record_from_args(args)
        )
        result = add_harness(
            record=record,
            wiki_path=Path(os.path.expanduser(args.wiki)),
            dry_run=args.dry_run,
            skip_existing=args.skip_existing,
            review_existing=True,
            update_existing=args.update_existing,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    action = "would add" if args.dry_run else "added"
    if result["skipped"]:
        action = "skipped"
    if result.get("update_review"):
        print(result["update_review"])
    print(f"{action}: {result['slug']} -> {result['path']}")


if __name__ == "__main__":
    main()
