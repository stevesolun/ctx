"""Dashboard documentation rendering for ``ctx-monitor``.

This module owns the local docs index, MkDocs-flavored Markdown rendering,
sanitization, link rewriting, and docs HTML cache.  ``ctx_monitor`` supplies
page chrome and asset readers; the docs implementation stays isolated here.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence
from urllib.parse import quote, unquote

from ctx.utils._fs_utils import atomic_write_text as _atomic_write_text

PUBLIC_DOCS_URL = "https://stevesolun.github.io/ctx/"

LayoutRenderer = Callable[[str, str], str]
AssetReader = Callable[[str], str]
InlineScriptRenderer = Callable[[str], str]
MarkdownRenderer = Callable[[str, str], str]
FallbackMarkdownRenderer = Callable[[str], str]

_DOCS_RENDER_CACHE_KEY: tuple[Any, ...] | None = None
_DOCS_RENDER_CACHE_VALUE: str | None = None


def reset_docs_render_cache() -> None:
    """Clear the in-process docs render cache."""
    global _DOCS_RENDER_CACHE_KEY, _DOCS_RENDER_CACHE_VALUE
    _DOCS_RENDER_CACHE_KEY = None
    _DOCS_RENDER_CACHE_VALUE = None


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(value) <= limit:
        return value, False
    if limit <= 3:
        return value[:limit], True
    return value[: limit - 3].rstrip() + "...", True


def _slugish(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def docs_roots(current_dir: Path, package_root: Path) -> list[Path]:
    roots: list[Path] = []
    for root in (current_dir, package_root):
        if root not in roots and (root / "docs").is_dir():
            roots.append(root)
    return roots


def docs_cache_files(roots: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        candidates = [root / "README.md", root / "graph" / "README.md", root / "mkdocs.yml"]
        docs_dir = root / "docs"
        if docs_dir.is_dir():
            candidates.extend(sorted(docs_dir.rglob("*.md")))
        for path in candidates:
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)
    return files


def docs_render_cache_key(
    roots: Sequence[Path],
    *,
    asset_text: AssetReader,
    cache_salt_paths: Sequence[Path] = (),
    cache_extra: Sequence[Any] = (),
) -> tuple[Any, ...]:
    parts: list[Any] = []
    for path in (Path(__file__), *cache_salt_paths):
        try:
            stat = path.stat()
            parts.append(("source", str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        except OSError:
            parts.append(("source", str(path), None, None))
    for item in cache_extra:
        parts.append(("extra", repr(item)))
    for asset_name in ("monitor.css", "monitor-docs.js"):
        try:
            asset_hash = hashlib.sha256(asset_text(asset_name).encode("utf-8")).hexdigest()
        except Exception:
            asset_hash = ""
        parts.append(("asset", asset_name, asset_hash))
    for path in docs_cache_files(roots):
        try:
            stat = path.stat()
            path_name = str(path.resolve())
            parts.append((path_name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    return tuple(parts)


def docs_render_disk_cache_path(claude_dir: Path) -> Path:
    return claude_dir / ".ctx-monitor-docs-cache.json"


def _disk_cache_token(cache_key: tuple[Any, ...]) -> str:
    return json.dumps(cache_key, separators=(",", ":"), sort_keys=True)


def _read_disk_cache_payload(path: Path, cache_token: str) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != 1 or data.get("cache_token") != cache_token:
        return None
    return data


def _write_disk_cache_payload(
    path: Path,
    cache_token: str,
    payload: dict[str, Any],
    *,
    sort_keys: bool = False,
) -> None:
    try:
        _atomic_write_text(
            path,
            json.dumps(
                {
                    "schema_version": 1,
                    "cache_token": cache_token,
                    **payload,
                },
                ensure_ascii=False,
                sort_keys=sort_keys,
            )
            + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError):
        return


def _read_html_disk_cache(path: Path, cache_token: str) -> str | None:
    data = _read_disk_cache_payload(path, cache_token)
    if data is None:
        return None
    html_text = data.get("html")
    return html_text if isinstance(html_text, str) else None


def _write_html_disk_cache(path: Path, cache_token: str, html_text: str) -> None:
    _write_disk_cache_payload(path, cache_token, {"html": html_text})


def doc_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return fallback


def doc_summary(text: str) -> str:
    in_frontmatter = text.startswith("---\n")
    for block in re.split(r"\n\s*\n", text):
        chunk = block.strip()
        if not chunk:
            continue
        if in_frontmatter:
            if chunk == "---" or chunk.endswith("\n---"):
                in_frontmatter = False
            continue
        if chunk.startswith("#") or chunk.startswith("```") or chunk.startswith("<!--"):
            continue
        summary = re.sub(r"\s+", " ", chunk)
        summary, _truncated = _truncate_text(summary, 180)
        return summary
    return ""


def strip_doc_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    parts = text.split("---", 2)
    return parts[2].lstrip() if len(parts) == 3 else text


def doc_anchor(value: str) -> str:
    return _slugish(value) or "docs"


def docs_index_entries(roots: Sequence[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        candidates = [root / "README.md", root / "graph" / "README.md"]
        docs_dir = root / "docs"
        if docs_dir.is_dir():
            candidates.extend(sorted(docs_dir.rglob("*.md")))
        for path in candidates:
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel == "docs/SKILL.md":
                continue
            if rel in seen:
                continue
            seen.add(rel)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            text = strip_doc_frontmatter(text)
            title = doc_title(text, path.stem.replace("-", " ").title())
            entries.append(
                {
                    "title": title,
                    "path": rel,
                    "summary": doc_summary(text),
                    "body": text,
                }
            )
    return sorted(entries, key=lambda row: str(row["path"]))


def docs_nav_from_mkdocs(
    root: Path,
    entries_by_path: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    mkdocs_path = root / "mkdocs.yml"
    if not mkdocs_path.is_file():
        return []
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.load(mkdocs_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader) or {}
    except Exception:
        return []
    raw_nav = data.get("nav")
    if not isinstance(raw_nav, list):
        return []

    def pages_from_nav(value: Any) -> list[tuple[str, str]]:
        if isinstance(value, str):
            return [(Path(value).stem.replace("-", " ").title(), f"docs/{value}")]
        if isinstance(value, list):
            pages: list[tuple[str, str]] = []
            for child in value:
                pages.extend(pages_from_nav(child))
            return pages
        if isinstance(value, dict):
            pages = []
            for label, child in value.items():
                if isinstance(child, str):
                    pages.append((str(label), f"docs/{child}"))
                else:
                    pages.extend(pages_from_nav(child))
            return pages
        return []

    tabs: list[dict[str, Any]] = []
    for item in raw_nav:
        if not isinstance(item, dict):
            continue
        for label, value in item.items():
            pages = [
                {**entries_by_path[path], "nav_title": page_label}
                for page_label, path in pages_from_nav(value)
                if path in entries_by_path
            ]
            if pages:
                tabs.append(
                    {
                        "label": str(label),
                        "slug": doc_anchor(str(label)),
                        "pages": pages,
                    }
                )
    return tabs


def docs_tabs(entries: list[dict[str, Any]], roots: Sequence[Path]) -> list[dict[str, Any]]:
    entries_by_path = {str(entry["path"]): entry for entry in entries}
    tabs: list[dict[str, Any]] = []
    used: set[str] = set()
    for root in roots:
        tabs = docs_nav_from_mkdocs(root, entries_by_path)
        if tabs:
            break
    for tab in tabs:
        for page in tab["pages"]:
            used.add(str(page["path"]))

    repo_pages = [
        entries_by_path[path]
        for path in ("README.md", "graph/README.md")
        if path in entries_by_path
    ]
    if repo_pages:
        tabs.append({"label": "Repo", "slug": "repo", "pages": repo_pages})
        used.update(str(page["path"]) for page in repo_pages)

    other_pages = [
        entry for entry in entries if entry["path"] not in used and entry["path"] != "docs/SKILL.md"
    ]
    if other_pages:
        tabs.append({"label": "Other", "slug": "other", "pages": other_pages})
    if not tabs and entries:
        tabs.append({"label": "Docs", "slug": "docs", "pages": entries})
    return tabs


def docs_heading_text(raw: str) -> str:
    raw = re.sub(r"\s+\{#[^}]+\}\s*$", "", raw.strip())
    raw = re.sub(r"`([^`]+)`", r"\1", raw)
    raw = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", raw)
    raw = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw)
    raw = re.sub(r"[*_~]+", "", raw)
    return re.sub(r"\s+", " ", raw).strip()


def docs_heading_id(page_anchor: str, title: str, seen: dict[str, int]) -> str:
    base = f"{page_anchor}-{doc_anchor(title)}"
    count = seen.get(base, 0)
    seen[base] = count + 1
    return base if count == 0 else f"{base}_{count}"


def docs_heading_items(markdown_text: str, page_anchor: str) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    in_fence = False
    for line in markdown_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,4})\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        level = len(match.group(1))
        title = docs_heading_text(match.group(2))
        if not title:
            continue
        heading_id = docs_heading_id(page_anchor, title, seen)
        if level >= 2:
            headings.append({"level": level, "title": title, "id": heading_id})
    return headings


def docs_page_anchor(tab_slug: str, path: str) -> str:
    return f"doc-{tab_slug}-{doc_anchor(path)}"


def normalise_doc_path(path: str) -> str:
    parts: list[str] = []
    for part in PurePosixPath(path).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts).as_posix() if parts else ""


def resolve_docs_link_path(
    source_path: str,
    href_path: str,
    page_anchors: dict[str, tuple[str, str]],
) -> str | None:
    base = PurePosixPath(source_path).parent
    resolved = normalise_doc_path((base / href_path).as_posix())
    candidates = [resolved]
    if not resolved.endswith(".md"):
        stripped = resolved.rstrip("/")
        candidates.extend([f"{stripped}.md", f"{stripped}/index.md"])
    for candidate in candidates:
        if candidate in page_anchors:
            return candidate
    return None


def docs_dashboard_link(
    source_path: str,
    source_tab: str,
    source_anchor: str,
    href: str,
    page_anchors: dict[str, tuple[str, str]],
) -> tuple[str, str, str] | None:
    href = html.unescape(href).strip()
    lowered = href.lower()
    if (
        not href
        or lowered.startswith(("http://", "https://", "mailto:", "tel:", "javascript:"))
        or href.startswith("/")
    ):
        return None
    href_path, sep, fragment = href.partition("#")
    if not href_path:
        target_tab = source_tab
        target_anchor = source_anchor
    else:
        resolved_path = resolve_docs_link_path(source_path, href_path, page_anchors)
        if resolved_path is None:
            return None
        target_tab, target_anchor = page_anchors[resolved_path]
    if sep and fragment:
        fragment_anchor = doc_anchor(unquote(fragment))
        if not fragment_anchor.startswith(target_anchor):
            fragment_anchor = f"{target_anchor}-{fragment_anchor}"
        return f"#{fragment_anchor}", target_tab, fragment_anchor
    return f"#{target_anchor}", target_tab, target_anchor


def rewrite_docs_links(
    rendered_html: str,
    source_path: str,
    source_tab: str,
    source_anchor: str,
    page_anchors: dict[str, tuple[str, str]],
) -> str:
    def replace_link(match: re.Match[str]) -> str:
        before = match.group(1)
        href = match.group(2)
        after = match.group(3)
        resolved = docs_dashboard_link(source_path, source_tab, source_anchor, href, page_anchors)
        if resolved is None:
            return match.group(0)
        dashboard_href, target_tab, target_anchor = resolved
        return (
            f'<a{before}href="{html.escape(dashboard_href, quote=True)}"'
            f' data-doc-tab="{html.escape(target_tab, quote=True)}"'
            f' data-doc-target="{html.escape(target_anchor, quote=True)}"{after}>'
        )

    return re.sub(r"<a([^>]*?)href=\"([^\"]+)\"([^>]*)>", replace_link, rendered_html)


def render_docs_markdown(
    markdown_text: str,
    page_anchor: str,
    *,
    fallback_renderer: FallbackMarkdownRenderer,
) -> str:
    """Render repo docs with MkDocs-like Markdown support when available."""
    markdown_text = re.sub(r":octicons-arrow-right-24:", "->", markdown_text)
    markdown_text = re.sub(r":octicons-[a-z0-9-]+:", "", markdown_text)
    try:
        import markdown as markdown_lib  # type: ignore[import-untyped]

        rendered = str(
            markdown_lib.markdown(
                markdown_text,
                extensions=[
                    "admonition",
                    "attr_list",
                    "def_list",
                    "fenced_code",
                    "footnotes",
                    "md_in_html",
                    "tables",
                    "toc",
                    "pymdownx.details",
                    "pymdownx.superfences",
                    "pymdownx.tabbed",
                    "pymdownx.tasklist",
                    "pymdownx.inlinehilite",
                ],
                extension_configs={
                    "toc": {
                        "permalink": True,
                        "slugify": lambda value, separator: f"{page_anchor}-{doc_anchor(value)}",
                    },
                    "pymdownx.tabbed": {"alternate_style": True},
                    "pymdownx.tasklist": {"custom_checkbox": True},
                },
                output_format="html5",
            )
        )
        return sanitize_docs_html(rendered)
    except Exception:
        return fallback_renderer(markdown_text)


def sanitize_docs_html(rendered_html: str) -> str:
    """Remove active HTML from local docs before embedding in the dashboard."""
    dangerous_blocks = (
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "form",
        "textarea",
        "select",
    )
    dangerous_tags = (
        "base",
        "button",
        "input",
        "link",
        "meta",
    )

    def escape_match(match: re.Match[str]) -> str:
        return html.escape(match.group(0))

    def parse_attrs(tag_html: str) -> dict[str, str | None] | None:
        attrs: dict[str, str | None] = {}
        for match in re.finditer(
            r"([a-zA-Z_:][\w:.-]*)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?",
            tag_html,
        ):
            name = match.group(1).lower()
            if name == "input":
                continue
            if name in attrs:
                return None
            raw_value = match.group(2)
            if raw_value is None:
                attrs[name] = None
                continue
            value = raw_value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            attrs[name] = html.unescape(value)
        return attrs

    def is_safe_tabbed_input(tag_html: str) -> bool:
        attrs = parse_attrs(tag_html)
        if attrs is None:
            return False
        if set(attrs) - {"checked", "id", "name", "type"}:
            return False
        input_type = (attrs.get("type") or "").lower()
        input_id = attrs.get("id") or ""
        input_name = attrs.get("name") or ""
        if input_type != "radio":
            return False
        if not re.fullmatch(r"__tabbed_\d+_\d+", input_id):
            return False
        if not re.fullmatch(r"__tabbed_\d+", input_name):
            return False
        if not input_id.startswith(f"{input_name}_"):
            return False
        checked = attrs.get("checked")
        return checked is None or checked.lower() == "checked"

    def escape_input_unless_safe(match: re.Match[str]) -> str:
        tag_html = match.group(0)
        if is_safe_tabbed_input(tag_html):
            return tag_html
        return html.escape(tag_html)

    for tag in dangerous_blocks:
        rendered_html = re.sub(
            rf"<\s*{tag}\b[^>]*>.*?<\s*/\s*{tag}\s*>",
            escape_match,
            rendered_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        rendered_html = re.sub(
            rf"<\s*/?\s*{tag}\b[^>]*>",
            escape_match,
            rendered_html,
            flags=re.IGNORECASE,
        )
    for tag in dangerous_tags:
        if tag == "input":
            rendered_html = re.sub(
                rf"<\s*/?\s*{tag}\b[^>]*>",
                escape_input_unless_safe,
                rendered_html,
                flags=re.IGNORECASE,
            )
            continue
        rendered_html = re.sub(
            rf"<\s*/?\s*{tag}\b[^>]*>",
            escape_match,
            rendered_html,
            flags=re.IGNORECASE,
        )

    rendered_html = re.sub(
        r"\s+on[a-zA-Z0-9_-]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)",
        "",
        rendered_html,
        flags=re.IGNORECASE,
    )
    rendered_html = re.sub(
        r"\s+(href|src)\s*=\s*(?:\"\s*(?:javascript:|data:text/html)[^\"]*\"|'\s*(?:javascript:|data:text/html)[^']*'|(?:javascript:|data:text/html)[^\s>]*)",
        lambda match: f' {match.group(1)}="#"',
        rendered_html,
        flags=re.IGNORECASE,
    )
    return rendered_html


def docs_search_text(entry: dict[str, Any]) -> str:
    text = f"{entry['title']} {entry['path']} {entry['summary']} {entry['body']}"
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"!!!\s+\w+(?:\s+\"[^\"]+\")?", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[*_`#>\[\]().!:-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def render_docs_sidebar_page(
    entry: dict[str, Any],
    tab_slug: str,
    page_anchors: dict[str, tuple[str, str]],
) -> str:
    page_anchor = page_anchors.get(
        str(entry["path"]),
        (tab_slug, docs_page_anchor(tab_slug, str(entry["path"]))),
    )[1]
    title = str(entry.get("nav_title") or entry["title"])
    page_search = docs_search_text(entry)
    heading_links = "".join(
        "<a class='docs-heading-link "
        f"docs-heading-level-{int(heading['level'])}' "
        f"href='#{html.escape(str(heading['id']))}' "
        f"data-doc-link data-doc-tab='{html.escape(tab_slug)}' "
        f"data-doc-target='{html.escape(str(heading['id']))}' "
        f"data-doc-search='{html.escape(str(heading['title']).lower())}' "
        f"data-doc-label='{html.escape(title)} / {html.escape(str(heading['title']))}'>"
        f"{html.escape(str(heading['title']))}</a>"
        for heading in docs_heading_items(str(entry["body"]), page_anchor)
    )
    headings = f"<div class='docs-heading-list'>{heading_links}</div>" if heading_links else ""
    return (
        "<div class='docs-toc-page'>"
        f"<a class='docs-page-link' href='#{html.escape(page_anchor)}' "
        f"data-doc-link data-doc-tab='{html.escape(tab_slug)}' "
        f"data-doc-target='{html.escape(page_anchor)}' "
        f"data-doc-search='{html.escape(page_search)}' "
        f"data-doc-label='{html.escape(title)}'>{html.escape(title)}</a>"
        f"{headings}"
        "</div>"
    )


def render_docs_page(
    entry: dict[str, Any],
    tab_slug: str,
    page_anchors: dict[str, tuple[str, str]],
    *,
    render_markdown_func: MarkdownRenderer,
) -> str:
    page_anchor = page_anchors.get(
        str(entry["path"]),
        (tab_slug, docs_page_anchor(tab_slug, str(entry["path"]))),
    )[1]
    source_url = f"https://github.com/stevesolun/ctx/blob/main/{quote(str(entry['path']))}"
    body_html = render_markdown_func(str(entry["body"]), page_anchor)
    body_html = rewrite_docs_links(
        body_html, str(entry["path"]), tab_slug, page_anchor, page_anchors
    )
    return (
        f"<article id='{html.escape(page_anchor)}' class='docs-page wiki-body' "
        f"data-doc-page='{html.escape(docs_search_text(entry))}'>"
        "<div class='docs-page-source'>"
        f"<code>{html.escape(str(entry['path']))}</code>"
        f"<a href='{html.escape(source_url)}'>source -></a>"
        "</div>"
        f"{body_html}"
        "</article>"
    )


def render_docs(
    *,
    roots: Sequence[Path],
    layout: LayoutRenderer,
    asset_text: AssetReader,
    inline_script: InlineScriptRenderer,
    cache_path: Path,
    fallback_markdown: FallbackMarkdownRenderer,
    cache_salt_paths: Sequence[Path] = (),
    cache_extra: Sequence[Any] = (),
    index_entries: Callable[[], list[dict[str, Any]]] | None = None,
    tabs_for_entries: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    render_markdown_func: MarkdownRenderer | None = None,
    public_docs_url: str = PUBLIC_DOCS_URL,
) -> str:
    cache_key = docs_render_cache_key(
        roots,
        asset_text=asset_text,
        cache_salt_paths=cache_salt_paths,
        cache_extra=cache_extra,
    )
    global _DOCS_RENDER_CACHE_KEY, _DOCS_RENDER_CACHE_VALUE
    if _DOCS_RENDER_CACHE_KEY == cache_key and _DOCS_RENDER_CACHE_VALUE is not None:
        return _DOCS_RENDER_CACHE_VALUE
    cache_token = _disk_cache_token(cache_key)
    cached = _read_html_disk_cache(cache_path, cache_token)
    if cached is not None:
        _DOCS_RENDER_CACHE_KEY = cache_key
        _DOCS_RENDER_CACHE_VALUE = cached
        return cached

    entries = index_entries() if index_entries is not None else docs_index_entries(roots)
    tabs = tabs_for_entries(entries) if tabs_for_entries is not None else docs_tabs(entries, roots)
    if render_markdown_func is None:

        def default_render_markdown(text: str, anchor: str) -> str:
            return render_docs_markdown(
                text,
                anchor,
                fallback_renderer=fallback_markdown,
            )

        render_markdown_func = default_render_markdown
    if not tabs:
        body = (
            "<h1>Docs</h1>"
            "<div class='card'><strong>No local docs found.</strong>"
            f"<p class='muted'>Open the public docs at "
            f"<a href='{public_docs_url}'>{public_docs_url}</a>.</p></div>"
        )
        html_out = layout("Docs", body)
        _write_html_disk_cache(cache_path, cache_token, html_out)
        _DOCS_RENDER_CACHE_KEY = cache_key
        _DOCS_RENDER_CACHE_VALUE = html_out
        return html_out

    tab_buttons = "".join(
        f"<button class='docs-tab-button{' active' if idx == 0 else ''}' "
        f"type='button' data-doc-tab='{html.escape(str(tab['slug']))}'>"
        f"{html.escape(str(tab['label']))}</button>"
        for idx, tab in enumerate(tabs)
    )
    panels: list[str] = []
    page_count = sum(len(list(tab["pages"])) for tab in tabs)
    page_anchors: dict[str, tuple[str, str]] = {}
    for tab in tabs:
        tab_slug = str(tab["slug"])
        for page in list(tab["pages"]):
            page_anchors[str(page["path"])] = (
                tab_slug,
                docs_page_anchor(tab_slug, str(page["path"])),
            )
    for idx, tab in enumerate(tabs):
        tab_slug = str(tab["slug"])
        pages = list(tab["pages"])
        page_links = "".join(
            render_docs_sidebar_page(page, tab_slug, page_anchors) for page in pages
        )
        page_bodies = "".join(
            render_docs_page(
                page,
                tab_slug,
                page_anchors,
                render_markdown_func=render_markdown_func,
            )
            for page in pages
        )
        hidden = " hidden" if idx else ""
        panels.append(
            f"<section class='docs-tab-panel' data-doc-panel='{html.escape(tab_slug)}'{hidden}>"
            "<div class='docs-reader'>"
            f"<aside class='docs-page-list'>{page_links}</aside>"
            f"<div>{page_bodies}</div>"
            "</div>"
            "</section>"
        )

    body = (
        "<div class='docs-shell'>"
        "<section class='docs-hero'>"
        "<div class='docs-hero-grid'>"
        "<div>"
        "<div class='docs-eyebrow'>Repo documentation</div>"
        "<h1>Docs</h1>"
        "<p>Read the same Markdown tree and MkDocs nav shipped with the repo. "
        "Use tabs for sections, search across local docs, and jump to source when you need the exact file.</p>"
        "</div>"
        "<div class='docs-hero-meta'>"
        f"<span class='docs-stat'>{len(tabs)} sections</span>"
        f"<span class='docs-stat'>{page_count} pages</span>"
        "</div>"
        "</div>"
        "<div class='docs-actions'>"
        "<div class='docs-search-wrap'>"
        "<input id='docs-search' type='text' placeholder='Search local docs...'>"
        "</div>"
        f"<a class='docs-public-link' href='{public_docs_url}'>public docs -></a>"
        "</div>"
        "<div id='docs-search-results' class='docs-search-results' hidden></div>"
        "</section>"
        f"<div class='docs-tabs' role='tablist'>{tab_buttons}</div>"
        + "".join(panels)
        + inline_script("monitor-docs.js")
        + "</div>"
    )
    html_out = layout("Docs", body)
    _write_html_disk_cache(cache_path, cache_token, html_out)
    _DOCS_RENDER_CACHE_KEY = cache_key
    _DOCS_RENDER_CACHE_VALUE = html_out
    return html_out
