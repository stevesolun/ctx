#!/usr/bin/env python3
"""
mcp_install.py -- Install and uninstall MCP servers from the wiki catalog.

Wraps Claude Code's native ``claude mcp add`` / ``claude mcp remove``
with the wiki's entity metadata, so a user can approve a
recommendation and have the MCP registered in one command:

    ctx-mcp-install filesystem --cmd "npx -y @modelcontextprotocol/server-filesystem /data"

Flow on install:

  1. Resolve the entity page under ``<wiki>/entities/mcp-servers/``.
     Fails fast when the slug isn't cataloged (no wiki card => we
     have no description or quality signal to vouch for it).
  2. Print a "why install" card: name, description, quality grade,
     github_url, related-entities count. User sees what they're
     approving.
  3. Unless ``--auto``, wait for explicit y/n confirmation.
  4. Shell out to ``claude mcp add <slug> -- <cmd tokens>``.
  5. On success: write ``install_cmd`` + ``status: installed`` into
     the entity frontmatter and add a manifest entry tagged
     ``entity_type: mcp-server``.

Uninstall mirrors ``skill_unload.py``:

  1. ``claude mcp remove <slug>``.
  2. Entity ``status`` flips back to ``cataloged``.
  3. Manifest: drop the load entry, add an unload entry.

The ``claude`` CLI is the source of truth for whether an MCP is
actually *running* — we write our status only on a zero exit code
from that CLI. Our manifest is a mirror for resolve/suggest to
consult.

Usage:
    ctx-mcp-install filesystem --cmd "npx -y @modelcontextprotocol/server-filesystem /data"
    ctx-mcp-install atlassian-cloud --cmd "uvx atlassian-mcp" --auto
    ctx-mcp-install my-server --cmd-json '{"command":"npx","args":["-y","pkg"]}'
    ctx-mcp-uninstall filesystem
    ctx-mcp-install atlassian-cloud --dry-run   # card only, no install
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from ctx_config import cfg
from install_utils import (
    bump_entity_status,
    record_install,
    record_uninstall,
)
from wiki_utils import validate_skill_name

_logger = logging.getLogger(__name__)

_SESSION_ID: str = uuid.uuid4().hex

# Allowlist of executables we trust as the first token of an MCP
# install command. ``install_cmd`` can round-trip through entity
# frontmatter on reinstall (see _install_cmd fallback in install_mcp);
# frontmatter is under file-system control and must be treated as
# untrusted. Bespoke runtimes should go through ``--cmd-json`` which
# passes the entire config to claude mcp add-json without argv splitting.
_ALLOWED_CMD_EXECS: frozenset[str] = frozenset({
    "npx",
    "uvx",
    "node",
    "python",
    "python3",
    "deno",
    "bunx",
})


@dataclass(frozen=True)
class InstallResult:
    slug: str
    status: str  # "installed" | "skipped-existing" | "aborted"
                 # | "not-in-wiki" | "no-command" | "invalid-cmd"
                 # | "claude-cli-failed"
    command: str | None
    message: str = ""


@dataclass(frozen=True)
class UninstallResult:
    slug: str
    status: str  # "uninstalled" | "not-installed" | "claude-cli-failed"
    message: str = ""


# ── Wiki lookups ─────────────────────────────────────────────────────────────


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)


def _mcp_shard(slug: str) -> str:
    """Mirror of McpRecord.entity_relpath shard convention."""
    first = slug[0] if slug else ""
    return first if first.isalpha() else "0-9"


def _entity_path(wiki_dir: Path, slug: str) -> Path:
    return wiki_dir / "entities" / "mcp-servers" / _mcp_shard(slug) / f"{slug}.md"


def _parse_entity_frontmatter(path: Path) -> dict[str, str]:
    """Cheap flat-scalar frontmatter read for the few fields we need.

    Avoids a yaml dep because the MCP entity frontmatter is flat
    scalars (no nested mappings, no multi-doc). Returns a
    string-keyed dict of raw string values (stars stays "12" not 12
    — the caller can convert when it cares).
    """
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return {}
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        # Skip list-item continuations and empty lines — not needed here.
        if not line or line.startswith(" ") or line.startswith("-"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        # Strip quotes for readability; YAML null sentinels → empty.
        if val in ("null", "~"):
            fm[key] = ""
            continue
        fm[key] = val.strip('"').strip("'")
    return fm


# ── claude mcp CLI wrapper ───────────────────────────────────────────────────


def _run_claude_mcp(args: list[str]) -> tuple[int, str, str]:
    """Run a ``claude mcp <args>`` invocation. Returns (rc, stdout, stderr).

    We allow-list the first argument to a small set of known mcp
    subcommands so a malformed user input can't turn this into a
    shell-command builder.
    """
    allowed = {"add", "add-json", "remove", "list", "get"}
    if not args or args[0] not in allowed:
        return 127, "", f"refused unknown mcp subcommand: {args[0] if args else '<empty>'}"
    try:
        proc = subprocess.run(
            ["claude", "mcp", *args],
            capture_output=True, text=True, check=False, timeout=60,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "claude CLI not found on PATH (install Claude Code first)"
    except subprocess.TimeoutExpired:
        return 124, "", "claude mcp timed out after 60s"


# ── Why-install card ─────────────────────────────────────────────────────────


def render_card(fm: dict[str, str], slug: str, *, command: str | None) -> str:
    """Render a human-readable 'why install' card.

    Deliberately concise so a user approving a suggestion sees only
    the load-bearing info: name, description, github URL, quality
    grade. No multi-line tag lists.
    """
    lines: list[str] = []
    lines.append(f"═══ Install MCP: {slug} ═══")
    if fm.get("name"):
        lines.append(f"  name:        {fm['name']}")
    if fm.get("description"):
        desc = fm["description"].strip()
        if len(desc) > 300:
            desc = desc[:297] + "…"
        lines.append(f"  description: {desc}")
    if fm.get("github_url"):
        lines.append(f"  github:      {fm['github_url']}")
    if fm.get("stars"):
        lines.append(f"  stars:       {fm['stars']}")
    if fm.get("quality_grade") and fm.get("quality_score"):
        lines.append(
            f"  quality:     grade {fm['quality_grade']} "
            f"(score {fm['quality_score']})"
        )
    if fm.get("author"):
        lines.append(f"  author:      {fm['author']}")
    if command:
        lines.append(f"  command:     claude mcp add {slug} -- {command}")
    lines.append("═" * 34)
    return "\n".join(lines)


# ── Install / uninstall ──────────────────────────────────────────────────────


def _prompt_confirm(prompt: str) -> bool:
    """Read a y/n answer from stdin. Treats EOF/non-interactive as 'no'."""
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def install_mcp(
    slug: str,
    *,
    wiki_dir: Path,
    command: str | None = None,
    json_config: str | None = None,
    auto: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> InstallResult:
    """Install one MCP from the wiki.

    Either ``command`` (a stdio command string like ``npx -y pkg``)
    or ``json_config`` (passed to ``claude mcp add-json``) is
    required unless ``dry_run=True`` (card-only).
    """
    try:
        validate_skill_name(slug)
    except ValueError as exc:
        return InstallResult(
            slug=slug, status="not-in-wiki", command=None,
            message=f"invalid slug: {exc}",
        )

    entity = _entity_path(wiki_dir, slug)
    if not entity.is_file():
        return InstallResult(
            slug=slug, status="not-in-wiki", command=None,
            message=f"no wiki entity at {entity}",
        )

    fm = _parse_entity_frontmatter(entity)
    existing_status = fm.get("status", "")

    if existing_status == "installed" and not force:
        return InstallResult(
            slug=slug, status="skipped-existing", command=None,
            message="already installed; pass --force to reinstall",
        )

    # Decide install command. If user passed --cmd, honor it. Else
    # try the frontmatter's install_cmd (set on a prior successful
    # install). Else require user input — we're not guessing.
    effective_cmd: str | None = command or fm.get("install_cmd") or None
    if not effective_cmd and not json_config and not dry_run:
        return InstallResult(
            slug=slug, status="no-command", command=None,
            message=(
                "no install command. Either pass --cmd '<invocation>' "
                "or --cmd-json '<json>', or look up "
                f"{fm.get('github_url', '<no github_url yet>')} README "
                "for the recommended invocation."
            ),
        )

    # Validate json_config IS parseable JSON before handing it to
    # claude mcp add-json. A malformed string would otherwise surface
    # as a confusing claude-cli error; prefer the clear local error.
    if json_config is not None:
        try:
            json.loads(json_config)
        except json.JSONDecodeError as exc:
            return InstallResult(
                slug=slug, status="invalid-cmd", command=None,
                message=f"--cmd-json is not valid JSON: {exc}",
            )

    card = render_card(fm, slug, command=effective_cmd)
    print(card)

    if dry_run:
        return InstallResult(
            slug=slug, status="aborted", command=effective_cmd,
            message="dry-run: no install performed",
        )

    if not auto and not _prompt_confirm(f"\nInstall {slug}?"):
        return InstallResult(
            slug=slug, status="aborted", command=effective_cmd,
            message="user declined",
        )

    # Run the actual claude mcp add invocation.
    if json_config:
        rc, stdout, stderr = _run_claude_mcp(["add-json", slug, json_config])
    else:
        assert effective_cmd is not None  # narrowed by dry_run branch above
        try:
            tokens = shlex.split(effective_cmd)
        except ValueError as exc:
            return InstallResult(
                slug=slug, status="invalid-cmd", command=effective_cmd,
                message=f"could not parse --cmd/install_cmd: {exc}",
            )
        if not tokens:
            return InstallResult(
                slug=slug, status="invalid-cmd", command=effective_cmd,
                message="empty install command",
            )
        # Executable allowlist. install_cmd can flow from frontmatter
        # (which is under entity-file control); treat it as untrusted.
        # Only known MCP-runtime launchers are allowed — if your
        # server needs a bespoke runtime, add-json is the right path.
        if tokens[0] not in _ALLOWED_CMD_EXECS:
            return InstallResult(
                slug=slug, status="invalid-cmd", command=effective_cmd,
                message=(
                    f"executable {tokens[0]!r} not in allowlist "
                    f"{sorted(_ALLOWED_CMD_EXECS)}; use --cmd-json for "
                    "bespoke runtimes."
                ),
            )
        rc, stdout, stderr = _run_claude_mcp(["add", slug, "--", *tokens])

    if rc != 0:
        return InstallResult(
            slug=slug, status="claude-cli-failed", command=effective_cmd,
            message=f"claude mcp add failed (rc={rc}): {stderr.strip() or stdout.strip()}",
        )

    bump_entity_status(
        _entity_path(wiki_dir, slug),
        status="installed",
        extra_fields={"install_cmd": effective_cmd or ""},
    )
    record_install(
        slug,
        entity_type="mcp-server",
        source="ctx-mcp-install",
        extra={"command": effective_cmd or json_config or ""},
    )

    return InstallResult(
        slug=slug, status="installed", command=effective_cmd,
        message=stdout.strip() or "registered",
    )


def uninstall_mcp(
    slug: str, *, wiki_dir: Path, force: bool = False, dry_run: bool = False,
) -> UninstallResult:
    """Uninstall one MCP.

    Idempotent with respect to already-uninstalled state: if the
    entity is ``cataloged`` we skip the claude-cli call unless
    ``--force`` (useful when the cli registration drifted from
    our mirror and you want to force-remove).
    """
    try:
        validate_skill_name(slug)
    except ValueError as exc:
        return UninstallResult(
            slug=slug, status="not-installed",
            message=f"invalid slug: {exc}",
        )

    entity = _entity_path(wiki_dir, slug)
    if entity.is_file():
        fm = _parse_entity_frontmatter(entity)
        if fm.get("status", "") != "installed" and not force:
            return UninstallResult(
                slug=slug, status="not-installed",
                message="entity status is not 'installed'; pass --force to run claude mcp remove anyway",
            )

    if dry_run:
        return UninstallResult(
            slug=slug, status="uninstalled",
            message="dry-run: would run `claude mcp remove`",
        )

    rc, stdout, stderr = _run_claude_mcp(["remove", slug])
    if rc != 0 and not force:
        return UninstallResult(
            slug=slug, status="claude-cli-failed",
            message=f"claude mcp remove failed (rc={rc}): {stderr.strip() or stdout.strip()}",
        )

    # Even on non-zero (with --force) we still flip local state, since
    # the user asked us to.
    if entity.is_file():
        bump_entity_status(
            entity, status="cataloged",
            extra_fields={"install_cmd": None},  # render as YAML null
        )
    record_uninstall(slug, entity_type="mcp-server", source="ctx-mcp-uninstall")

    return UninstallResult(
        slug=slug, status="uninstalled",
        message=stdout.strip() or "removed",
    )


# ── CLIs ─────────────────────────────────────────────────────────────────────


def _build_install_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx-mcp-install",
        description=(
            "Install an MCP server from the wiki into Claude Code. "
            "Prints a 'why install' card then runs `claude mcp add`."
        ),
    )
    parser.add_argument("slug", help="MCP slug (matches the wiki entity filename)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cmd", help="Stdio command invocation, e.g. 'npx -y @pkg'")
    group.add_argument("--cmd-json", help="Full JSON config for claude mcp add-json")
    parser.add_argument("--auto", action="store_true", help="Skip the y/N confirmation")
    parser.add_argument("--force", action="store_true",
                        help="Reinstall even when entity status is already 'installed'")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render the card and exit without installing")
    parser.add_argument("--wiki-dir", default=str(cfg.wiki_dir),
                        help="Wiki root (default: cfg.wiki_dir)")
    parser.add_argument("--json", action="store_true",
                        help="Emit the result as JSON instead of text")
    return parser


def _build_uninstall_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx-mcp-uninstall",
        description=(
            "Uninstall an MCP server. Runs `claude mcp remove` and "
            "resets the wiki entity status."
        ),
    )
    parser.add_argument("slug", help="MCP slug")
    parser.add_argument("--force", action="store_true",
                        help="Continue past claude-cli errors and reset local state")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report intent without calling claude mcp remove")
    parser.add_argument("--wiki-dir", default=str(cfg.wiki_dir))
    parser.add_argument("--json", action="store_true")
    return parser


def _force_utf8_stdio() -> None:
    """Mirror of mcp_fetch/mcp_ingest helper — card output includes
    ``═`` box-drawing characters that crash Windows' cp1252 stdout."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def install_main() -> None:
    _force_utf8_stdio()
    args = _build_install_parser().parse_args()
    result = install_mcp(
        args.slug,
        wiki_dir=Path(os.path.expanduser(args.wiki_dir)),
        command=args.cmd,
        json_config=args.cmd_json,
        auto=args.auto,
        dry_run=args.dry_run,
        force=args.force,
    )
    if args.json:
        print(json.dumps(result.__dict__, indent=2))
    else:
        tag = "[OK]" if result.status == "installed" else f"[{result.status.upper()}]"
        suffix = f" -- {result.message}" if result.message else ""
        print(f"{tag} {result.slug}{suffix}")
    sys.exit(0 if result.status in ("installed", "skipped-existing", "aborted") else 1)


def uninstall_main() -> None:
    _force_utf8_stdio()
    args = _build_uninstall_parser().parse_args()
    result = uninstall_mcp(
        args.slug,
        wiki_dir=Path(os.path.expanduser(args.wiki_dir)),
        force=args.force,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(result.__dict__, indent=2))
    else:
        tag = "[OK]" if result.status == "uninstalled" else f"[{result.status.upper()}]"
        suffix = f" -- {result.message}" if result.message else ""
        print(f"{tag} {result.slug}{suffix}")
    sys.exit(0 if result.status == "uninstalled" else 1)


# Allow ``python -m mcp_install`` to hit the install main; tests import
# the two ``*_main`` functions directly.
main = install_main


if __name__ == "__main__":
    main()
