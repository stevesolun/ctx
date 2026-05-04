#!/usr/bin/env python3
"""Install cataloged harnesses from the ctx wiki.

Harness installation is intentionally conservative. A harness page may document
setup and verification commands, but this command never runs those commands
unless the user explicitly opts in with ``--approve-commands`` and
``--run-verify``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from ctx.core.entity_types import entity_page_path
from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body, validate_skill_name
from ctx.utils._fs_utils import atomic_write_json, atomic_write_text, reject_symlink_path
from ctx_config import cfg

_COMMAND_ENV_ALLOWLIST = {
    "CI",
    "COMSPEC",
    "HOME",
    "LANG",
    "PATH",
    "PATHEXT",
    "PYTHONPATH",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
}
_SECRET_NAME_MARKERS = ("API", "KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")
_REDACTION = "[redacted]"


@dataclass(frozen=True)
class HarnessRecord:
    slug: str
    path: Path
    title: str
    repo_url: str
    docs_url: str | None
    tags: tuple[str, ...]
    runtimes: tuple[str, ...]
    model_providers: tuple[str, ...]
    capabilities: tuple[str, ...]
    attach_modes: tuple[str, ...]
    setup_commands: tuple[str, ...]
    verify_commands: tuple[str, ...]


@dataclass(frozen=True)
class InstallResult:
    slug: str
    status: str
    target: Path | None = None
    manifest_path: Path | None = None
    message: str = ""


def _as_tuple(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(str(item) for item in raw if str(item).strip())
    return ()


def _load_page(path: Path, slug: str) -> HarnessRecord:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm, _body = parse_frontmatter_and_body(text)
    repo_url = str(fm.get("repo_url") or "").strip()
    if not repo_url:
        raise ValueError(f"harness page {path} has no repo_url")
    return HarnessRecord(
        slug=slug,
        path=path,
        title=str(fm.get("title") or slug),
        repo_url=repo_url,
        docs_url=str(fm["docs_url"]) if fm.get("docs_url") else None,
        tags=_as_tuple(fm.get("tags")),
        runtimes=_as_tuple(fm.get("runtimes")),
        model_providers=_as_tuple(fm.get("model_providers")),
        capabilities=_as_tuple(fm.get("capabilities")),
        attach_modes=_normalize_attach_modes(fm.get("attach_modes")),
        setup_commands=_as_tuple(fm.get("setup_commands")),
        verify_commands=_as_tuple(fm.get("verify_commands")),
    )


def _normalize_attach_modes(raw: object) -> tuple[str, ...]:
    aliases = {
        "mcp": "mcp",
        "mcp-server": "mcp",
        "python": "python-library",
        "python-library": "python-library",
        "library": "python-library",
        "ctx": "ctx-run",
        "ctx-run": "ctx-run",
        "cli": "ctx-run",
        "manual": "manual",
    }
    values = _as_tuple(raw) or ("mcp", "python-library", "ctx-run")
    modes: list[str] = []
    for value in values:
        mode = aliases.get(value.strip().lower())
        if mode and mode not in modes:
            modes.append(mode)
    return tuple(modes) or ("manual",)


def _repo_key(raw: str) -> str:
    value = raw.strip().removesuffix(".git").rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        return f"{parsed.netloc.lower()}{parsed.path.lower().rstrip('/')}"
    return value.lower()


def _is_repo_identifier(identifier: str) -> bool:
    parsed = urlparse(identifier)
    return parsed.scheme in {"http", "https"}


def resolve_harness(identifier: str, *, wiki_path: Path) -> HarnessRecord:
    """Resolve a harness page by slug or repository URL."""
    value = identifier.strip()
    if not value:
        raise LookupError("harness identifier must be non-empty")

    if _is_repo_identifier(value):
        wanted = _repo_key(value)
        harness_dir = wiki_path / "entities" / "harnesses"
        for path in sorted(harness_dir.glob("*.md")):
            record = _load_page(path, path.stem)
            if _repo_key(record.repo_url) == wanted:
                return record
        raise LookupError(f"no harness catalog entry for repo {identifier!r}")

    validate_skill_name(value)
    page = entity_page_path(wiki_path, "harness", value)
    if page is None or not page.is_file():
        raise LookupError(f"no harness catalog entry for slug {value!r}")
    return _load_page(page, value)


def _default_installs_root() -> Path:
    return cfg.claude_dir / "harnesses"


def _default_manifest_dir() -> Path:
    return cfg.claude_dir / "harness-installs"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_strictly_within(path: Path, root: Path) -> bool:
    return path != root and _is_within(path, root)


def _resolve_target(
    *,
    slug: str,
    installs_root: Path,
    target: Path | None,
) -> Path:
    root = installs_root.expanduser().resolve()
    chosen = (target.expanduser() if target is not None else root / slug).resolve()
    if not _is_strictly_within(chosen, root):
        raise ValueError(f"target must stay under installs root {root}")
    return chosen


def render_plan(record: HarnessRecord, *, target: Path) -> str:
    lines = [
        f"Harness: {record.title}",
        f"Slug: {record.slug}",
        f"Repository: {record.repo_url}",
        f"Target: {target}",
    ]
    if record.docs_url:
        lines.append(f"Docs: {record.docs_url}")
    if record.tags:
        lines.append(f"Tags: {', '.join(record.tags)}")
    if record.runtimes:
        lines.append(f"Runtimes: {', '.join(record.runtimes)}")
    if record.model_providers:
        lines.append(f"Model providers: {', '.join(record.model_providers)}")
    if record.capabilities:
        lines.append(f"Capabilities: {', '.join(record.capabilities)}")
    if record.attach_modes:
        lines.append(f"Attach modes: {', '.join(record.attach_modes)}")
    if record.setup_commands:
        lines.append("Setup commands:")
        lines.extend(f"  - {cmd}" for cmd in record.setup_commands)
    if record.verify_commands:
        lines.append("Verify commands:")
        lines.extend(f"  - {cmd}" for cmd in record.verify_commands)
    lines.append(
        "Commands are not executed unless --approve-commands/--run-verify are set."
    )
    return "\n".join(lines)


def _local_source_from_repo_url(repo_url: str) -> Path | None:
    parsed = urlparse(repo_url)
    if parsed.scheme == "file":
        file_path = parsed.path
        if parsed.netloc and parsed.netloc not in {"", "localhost"}:
            file_path = f"//{parsed.netloc}{file_path}"
        return Path(url2pathname(unquote(file_path)))
    candidate = Path(repo_url).expanduser()
    return candidate if candidate.exists() else None


def _reject_symlink_tree(root: Path) -> None:
    if root.is_symlink():
        raise ValueError(f"refusing symlinked harness source: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"refusing symlink inside harness source: {path}")


def _materialize_source(
    record: HarnessRecord,
    target: Path,
    *,
    allow_local_sources: bool,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    local_source = _local_source_from_repo_url(record.repo_url)
    if local_source is not None:
        if not allow_local_sources:
            raise ValueError(
                "local harness repo_url requires --allow-local-source; "
                "cataloged harnesses should normally use https:// repositories"
            )
        local_source = local_source.expanduser().resolve()
        if not local_source.is_dir():
            raise ValueError(f"local harness source is not a directory: {local_source}")
        _reject_symlink_tree(local_source)
        shutil.copytree(local_source, target)
        return

    proc = subprocess.run(
        ["git", "clone", "--depth", "1", record.repo_url, str(target)],
        env=_command_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"git clone failed: {stderr}")


def _run_command(command: str, *, cwd: Path) -> dict[str, Any]:
    tokens = shlex.split(command)
    if not tokens:
        raise ValueError("empty harness command")
    started = time.time()
    proc = subprocess.run(
        tokens,
        cwd=str(cwd),
        env=_command_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    return {
        "command": command,
        "returncode": proc.returncode,
        "stdout": _redact_output(proc.stdout)[-4000:],
        "stderr": _redact_output(proc.stderr)[-4000:],
        "duration_seconds": round(time.time() - started, 3),
    }


def _command_env() -> dict[str, str]:
    """Return a minimal environment for cataloged harness commands."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _COMMAND_ENV_ALLOWLIST:
            env[key] = value
    return env


def _redact_output(text: str) -> str:
    redacted = text or ""
    for key, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if any(marker in key.upper() for marker in _SECRET_NAME_MARKERS):
            redacted = redacted.replace(value, _REDACTION)
    return redacted


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _stage_harness(
    *,
    record: HarnessRecord,
    stage_path: Path,
    approve_commands: bool,
    run_verify: bool,
    allow_local_sources: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _materialize_source(
        record,
        stage_path,
        allow_local_sources=allow_local_sources,
    )
    setup_runs: list[dict[str, Any]] = []
    verify_runs: list[dict[str, Any]] = []
    if approve_commands:
        for command in record.setup_commands:
            run = _run_command(command, cwd=stage_path)
            setup_runs.append(run)
            if run["returncode"] != 0:
                raise RuntimeError(f"setup command failed: {command}")
    if run_verify:
        for command in record.verify_commands:
            run = _run_command(command, cwd=stage_path)
            verify_runs.append(run)
            if run["returncode"] != 0:
                raise RuntimeError(f"verify command failed: {command}")
    return setup_runs, verify_runs


def _atomic_replace_target(stage_path: Path, target_path: Path) -> None:
    backup_path: Path | None = None
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        backup_path = target_path.with_name(
            f".{target_path.name}.backup-{os.getpid()}-{time.time_ns()}"
        )
        target_path.rename(backup_path)
    try:
        stage_path.rename(target_path)
    except Exception:
        if backup_path is not None and backup_path.exists() and not target_path.exists():
            backup_path.rename(target_path)
        raise
    finally:
        if backup_path is not None and backup_path.exists():
            _remove_path(backup_path)


def _install_to_target(
    *,
    record: HarnessRecord,
    target_path: Path,
    installs_root: Path,
    force: bool,
    approve_commands: bool,
    run_verify: bool,
    allow_local_sources: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if target_path.exists() and not force:
        raise FileExistsError("target already exists; pass --force to replace it")
    root = installs_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    stage_path = root / f".{record.slug}.tmp-{os.getpid()}-{time.time_ns()}"
    try:
        setup_runs, verify_runs = _stage_harness(
            record=record,
            stage_path=stage_path,
            approve_commands=approve_commands,
            run_verify=run_verify,
            allow_local_sources=allow_local_sources,
        )
        _atomic_replace_target(stage_path, target_path)
        return setup_runs, verify_runs
    except Exception:
        if stage_path.exists():
            _remove_path(stage_path)
        raise


def _write_manifest(
    *,
    record: HarnessRecord,
    target: Path,
    manifest_dir: Path,
    setup_runs: list[dict[str, Any]],
    verify_runs: list[dict[str, Any]],
    attach_files: list[Path] | None = None,
) -> Path:
    path = manifest_dir / f"{record.slug}.json"
    reject_symlink_path(path)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    reject_symlink_path(path)
    payload = {
        "slug": record.slug,
        "status": "installed",
        "repo_url": record.repo_url,
        "target": str(target),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "attach_files": [
            str(path.relative_to(target)) for path in (attach_files or [])
        ],
        "setup_commands_run": setup_runs,
        "verify_commands_run": verify_runs,
    }
    atomic_write_json(path, payload, indent=2)
    return path


def _manifest_path(manifest_dir: Path, slug: str) -> Path:
    validate_skill_name(slug)
    return manifest_dir / f"{slug}.json"


def _read_manifest(manifest_dir: Path, slug: str) -> dict[str, Any]:
    path = _manifest_path(manifest_dir, slug)
    if not path.is_file():
        raise LookupError(f"harness {slug!r} is not installed")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"install manifest {path} is not an object")
    return data


def install_harness(
    identifier: str,
    *,
    wiki_path: Path,
    installs_root: Path,
    manifest_dir: Path,
    target: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    approve_commands: bool = False,
    run_verify: bool = False,
    allow_local_sources: bool = False,
) -> InstallResult:
    try:
        record = resolve_harness(identifier, wiki_path=wiki_path)
    except Exception as exc:  # noqa: BLE001
        return InstallResult(
            slug=identifier,
            status="not-found",
            message=str(exc),
        )

    try:
        target_path = _resolve_target(
            slug=record.slug,
            installs_root=installs_root,
            target=target,
        )
    except ValueError as exc:
        return InstallResult(
            slug=record.slug,
            status="invalid-target",
            message=str(exc),
        )

    print(render_plan(record, target=target_path))
    if dry_run:
        return InstallResult(record.slug, "dry-run", target=target_path)

    try:
        setup_runs, verify_runs = _install_to_target(
            record=record,
            target_path=target_path,
            installs_root=installs_root,
            force=force,
            approve_commands=approve_commands,
            run_verify=run_verify,
            allow_local_sources=allow_local_sources,
        )
        attach_files = _write_attach_files(record, target=target_path)
        manifest_path = _write_manifest(
            record=record,
            target=target_path,
            manifest_dir=manifest_dir,
            setup_runs=setup_runs,
            verify_runs=verify_runs,
            attach_files=attach_files,
        )
    except FileExistsError as exc:
        return InstallResult(
            record.slug,
            "skipped-existing",
            target=target_path,
            message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return InstallResult(
            record.slug,
            "install-failed",
            target=target_path,
            message=str(exc),
        )

    return InstallResult(
        record.slug,
        "installed",
        target=target_path,
        manifest_path=manifest_path,
    )


def _write_attach_files(record: HarnessRecord, *, target: Path) -> list[Path]:
    attach_dir = target / ".ctx" / "attach"
    attach_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    readme = attach_dir / "README.md"
    reject_symlink_path(readme)
    atomic_write_text(readme, _render_attach_readme(record))
    written.append(readme)

    if "mcp" in record.attach_modes:
        path = attach_dir / "mcp.json"
        reject_symlink_path(path)
        atomic_write_text(path, _render_mcp_attach_config())
        written.append(path)
    if "python-library" in record.attach_modes:
        path = attach_dir / "python.py"
        reject_symlink_path(path)
        atomic_write_text(path, _render_python_attach_snippet())
        written.append(path)
    if "ctx-run" in record.attach_modes:
        path = attach_dir / "ctx-run.txt"
        reject_symlink_path(path)
        atomic_write_text(path, _render_ctx_run_attach_template(record))
        written.append(path)
    return written


def _render_attach_readme(record: HarnessRecord) -> str:
    modes = ", ".join(record.attach_modes)
    return f"""# ctx Attachment for {record.title}

This harness was installed by `ctx-harness-install`.

Supported attach modes: {modes}

Use the files in this directory to connect the harness to ctx:

- `mcp.json`: start `ctx-mcp-server` from any MCP-speaking host.
- `python.py`: call ctx recommendation/wiki APIs from a Python loop.
- `ctx-run.txt`: run the built-in ctx generic harness with your model.

The attachment files do not run setup commands and do not contain secrets.
"""


def _render_mcp_attach_config() -> str:
    return json.dumps(
        {
            "mcpServers": {
                "ctx-wiki": {
                    "command": "ctx-mcp-server",
                    "args": [],
                }
            }
        },
        indent=2,
    ) + "\n"


def _render_python_attach_snippet() -> str:
    return """from ctx import graph_query, recommend_bundle, wiki_get, wiki_search


def recommend_for_turn(goal: str) -> list[dict]:
    return recommend_bundle(goal, top_k=5)


def load_entity(slug: str) -> dict | None:
    return wiki_get(slug)

"""


def _render_ctx_run_attach_template(record: HarnessRecord) -> str:
    task = record.capabilities[0] if record.capabilities else f"use {record.title}"
    return (
        "ctx run --model <provider/model> "
        f"--task {json.dumps(task)} "
        "--mcp ctx-wiki:ctx-mcp-server\n"
    )


def uninstall_harness(
    identifier: str,
    *,
    manifest_dir: Path,
    installs_root: Path | None = None,
    keep_files: bool = False,
    dry_run: bool = False,
) -> InstallResult:
    slug = identifier.strip()
    try:
        validate_skill_name(slug)
        manifest_path = _manifest_path(manifest_dir, slug)
        manifest = _read_manifest(manifest_dir, slug)
    except Exception as exc:  # noqa: BLE001
        return InstallResult(slug or identifier, "not-installed", message=str(exc))

    target = Path(str(manifest.get("target") or "")).expanduser().resolve()
    if installs_root is not None:
        root = installs_root.expanduser().resolve()
        if target and not _is_strictly_within(target, root):
            return InstallResult(
                slug,
                "invalid-target",
                target=target,
                manifest_path=manifest_path,
                message=f"manifest target is outside installs root {root}",
            )

    print(f"Uninstall harness: {slug}")
    print(f"Target: {target}")
    print(f"Manifest: {manifest_path}")
    if keep_files:
        print("Files: keep installed target; remove manifest only")
    if dry_run:
        return InstallResult(slug, "dry-run", target=target, manifest_path=manifest_path)

    try:
        if not keep_files and target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        manifest_path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        return InstallResult(
            slug,
            "uninstall-failed",
            target=target,
            manifest_path=manifest_path,
            message=str(exc),
        )
    return InstallResult(slug, "uninstalled", target=target, manifest_path=manifest_path)


def update_harness(
    identifier: str,
    *,
    wiki_path: Path,
    installs_root: Path,
    manifest_dir: Path,
    dry_run: bool = False,
    approve_commands: bool = False,
    run_verify: bool = False,
    allow_local_sources: bool = False,
) -> InstallResult:
    try:
        record = resolve_harness(identifier, wiki_path=wiki_path)
        manifest = _read_manifest(manifest_dir, record.slug)
    except Exception as exc:  # noqa: BLE001
        return InstallResult(identifier, "not-installed", message=str(exc))

    target = Path(str(manifest.get("target") or "")).expanduser()
    if not target:
        return InstallResult(
            record.slug,
            "invalid-target",
            message="install manifest does not include target",
        )
    try:
        target_path = _resolve_target(
            slug=record.slug,
            installs_root=installs_root,
            target=target,
        )
    except Exception as exc:  # noqa: BLE001
        return InstallResult(record.slug, "invalid-target", message=str(exc))

    if dry_run:
        print("Update harness:")
        print(render_plan(record, target=target_path))
        print("Action: replace installed target from cataloged source")
        return InstallResult(record.slug, "dry-run", target=target_path)

    result = install_harness(
        record.slug,
        wiki_path=wiki_path,
        installs_root=installs_root,
        manifest_dir=manifest_dir,
        target=target,
        force=True,
        approve_commands=approve_commands,
        run_verify=run_verify,
        allow_local_sources=allow_local_sources,
    )
    if result.status != "installed":
        return result
    return InstallResult(
        record.slug,
        "updated",
        target=result.target,
        manifest_path=result.manifest_path,
        message=result.message,
    )


def recommend_harnesses_for_cli(
    *,
    goal: str,
    model_provider: str | None,
    model: str | None,
    top_k: int,
) -> list[dict[str, Any]]:
    from ctx_init import recommend_harnesses  # noqa: PLC0415

    query = " ".join(
        part for part in [goal, model_provider or "", model or "", "harness"] if part
    )
    return recommend_harnesses(
        query,
        top_k=top_k,
        model_provider=model_provider,
        model=model,
    )


def render_no_fit_harness_plan(
    *,
    goal: str,
    model_provider: str | None,
    model: str | None,
) -> str:
    """Render a build handoff when no catalog harness fits the user's setup."""
    provider = model_provider or "unknown provider"
    model_name = model or "unspecified model"
    goal_text = goal.strip() or "unspecified development goal"
    return "\n".join([
        "# Custom Harness PRD",
        "",
        "ctx did not find a catalog harness above the configured fit score.",
        "Use this handoff to build an attachable harness for your local/API model.",
        "",
        "## Inputs",
        "",
        f"- Goal: {goal_text}",
        f"- Model provider: {provider}",
        f"- Model: {model_name}",
        "- Target operating systems: Windows, macOS, and Linux unless narrowed by the user",
        "",
        "## Required Interview Before Building",
        "",
        "- Confirm the exact model, API base URL or local runtime, context window, and tool-call support.",
        "- Confirm the user's goal, repository type, stack, expected autonomy, and time horizon.",
        "- Confirm filesystem, shell, browser, network, secret, and package-manager access.",
        "- Confirm verification commands: tests, lint, type check, build, smoke, and dashboard/browser checks.",
        "- Confirm approval policy for destructive commands, network calls, dependency installs, and secret use.",
        "",
        "## Harness Architecture",
        "",
        "- Instructions: short root instructions plus deeper task, quality, safety, and verification docs.",
        "- State: durable session file, active task summary, loaded ctx entities, and handoff/progress log.",
        "- Scope: explicit allow/deny tool registry, cwd boundaries, environment allowlist, and secret redaction.",
        "- Verification: every completion requires runnable evidence captured in the session log.",
        "- Lifecycle: start session, observe dev events, request ctx recommendations, mark use, propose unload, end session.",
        "",
        "## ctx Attachment Contract",
        "",
        "- MCP mode: start `ctx-mcp-server` and expose ctx tools to the host.",
        "- Python mode: call `ctx.recommend_bundle`, `ctx.wiki_get`, `ctx.wiki_search`, and `ctx.graph_query`.",
        "- CLI mode: use `ctx run --model <model> --task <task>` when the user wants ctx to own the loop.",
        "- Each dev window sends the current goal, stack, touched files, errors, and verification state to ctx.",
        "- ctx returns at most five skills/agents/MCPs. The harness asks before loading or unloading anything.",
        "",
        "## Acceptance Tests",
        "",
        "- Fresh install on Windows, macOS, and Linux can start the harness without secrets printed in logs.",
        "- A sample task triggers no more than five ctx skill/agent/MCP recommendations.",
        "- A selected recommendation is recorded as used with evidence.",
        "- An unused loaded entity produces an unload proposal and respects a user skip.",
        "- A failing verification command prevents the harness from reporting completion.",
        "",
        "## Prompt For A Strong LLM",
        "",
        "Build the harness described above. Keep ctx integration attachable through MCP or Python. "
        "Do not hard-code secrets. Implement cross-platform startup and verification commands. "
        "Produce a minimal working harness first, then add durable state and unload lifecycle.",
        "",
        "Design reference: https://github.com/walkinglabs/learn-harness-engineering",
    ]) + "\n"


def write_no_fit_harness_plan(
    path: Path,
    *,
    goal: str,
    model_provider: str | None,
    model: str | None,
) -> Path:
    target = path.expanduser()
    reject_symlink_path(target)
    atomic_write_text(
        target,
        render_no_fit_harness_plan(
            goal=goal,
            model_provider=model_provider,
            model=model,
        ),
    )
    return target


def print_recommendations(results: list[dict[str, Any]]) -> None:
    if not results:
        print("No harness recommendations matched.")
        return
    print("Recommended harnesses:")
    for row in results:
        slug = str(row.get("name") or "")
        score = float(row.get("fit_score") or row.get("normalized_score") or 0.0)
        reason = str(row.get("fit_reason") or row.get("reason") or "").strip()
        print(f"- {slug} (fit {score:.2f})")
        if reason:
            print(f"  reason: {reason}")
        print(f"  install: ctx-harness-install {slug} --dry-run")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install a cataloged harness into ~/.claude/harnesses"
    )
    parser.add_argument("identifier", nargs="?", help="Harness slug or repository URL")
    parser.add_argument("--wiki", default=str(cfg.wiki_dir), help="Wiki path")
    parser.add_argument(
        "--installs-root",
        default=str(_default_installs_root()),
        help="Directory that owns harness install targets",
    )
    parser.add_argument(
        "--manifest-dir",
        default=str(_default_manifest_dir()),
        help="Directory for harness install manifests",
    )
    parser.add_argument("--target", help="Install target under --installs-root")
    parser.add_argument("--dry-run", action="store_true", help="Print plan only")
    parser.add_argument(
        "--recommend",
        action="store_true",
        help="Recommend harnesses from --goal/--model instead of installing one",
    )
    parser.add_argument("--goal", help="What you want to build or automate")
    parser.add_argument("--model-provider", help="Model provider prefix, e.g. openai or ollama")
    parser.add_argument("--model", help="Model slug, e.g. openrouter/openai/gpt-5.5")
    parser.add_argument("--top-k", type=int, default=5, help="Maximum recommendations to print")
    parser.add_argument(
        "--plan-on-no-fit",
        action="store_true",
        help="When --recommend finds no harness, print a custom harness PRD",
    )
    parser.add_argument(
        "--plan-output",
        help="Write the no-fit custom harness PRD to this markdown file",
    )
    parser.add_argument("--force", action="store_true", help="Replace target if it exists")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Replace an installed harness target from the current catalog source",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove a harness install manifest and installed target",
    )
    parser.add_argument(
        "--keep-files",
        action="store_true",
        help="With --uninstall, remove only the manifest and keep installed files",
    )
    parser.add_argument(
        "--approve-commands",
        action="store_true",
        help="Run cataloged setup commands after materializing the harness",
    )
    parser.add_argument(
        "--run-verify",
        action="store_true",
        help="Run cataloged verification commands after setup/install",
    )
    parser.add_argument(
        "--allow-local-source",
        action="store_true",
        help=(
            "Allow file:// or local-path repo_url values. Intended for "
            "trusted offline tests; public catalog entries should use https."
        ),
    )
    args = parser.parse_args(argv)

    wiki_path = Path(os.path.expanduser(args.wiki))
    installs_root = Path(os.path.expanduser(args.installs_root))
    manifest_dir = Path(os.path.expanduser(args.manifest_dir))
    target = Path(os.path.expanduser(args.target)) if args.target else None

    if args.recommend and (args.update or args.uninstall):
        print("Error: --recommend cannot be combined with --update/--uninstall", file=sys.stderr)
        return 2
    if args.update and args.uninstall:
        print("Error: choose only one of --update or --uninstall", file=sys.stderr)
        return 2
    if args.keep_files and not args.uninstall:
        print("Error: --keep-files requires --uninstall", file=sys.stderr)
        return 2
    if args.recommend:
        goal = args.goal or args.identifier or ""
        if not goal.strip():
            print("Error: --recommend requires --goal or a free-text query", file=sys.stderr)
            return 2
        results = recommend_harnesses_for_cli(
            goal=goal,
            model_provider=args.model_provider,
            model=args.model,
            top_k=max(1, min(int(args.top_k), 5)),
        )
        print_recommendations(results)
        if not results and args.plan_on_no_fit:
            if args.plan_output:
                path = write_no_fit_harness_plan(
                    Path(os.path.expanduser(args.plan_output)),
                    goal=goal,
                    model_provider=args.model_provider,
                    model=args.model,
                )
                print(f"Custom harness plan: {path}")
            else:
                print()
                print(render_no_fit_harness_plan(
                    goal=goal,
                    model_provider=args.model_provider,
                    model=args.model,
                ), end="")
        elif not results:
            print("Use --plan-on-no-fit to generate a custom harness PRD.")
        return 0
    if not args.identifier:
        parser.error("identifier is required unless --recommend is used")
    if args.uninstall:
        result = uninstall_harness(
            args.identifier,
            manifest_dir=manifest_dir,
            installs_root=installs_root,
            keep_files=args.keep_files,
            dry_run=args.dry_run,
        )
    elif args.update:
        result = update_harness(
            args.identifier,
            wiki_path=wiki_path,
            installs_root=installs_root,
            manifest_dir=manifest_dir,
            dry_run=args.dry_run,
            approve_commands=args.approve_commands,
            run_verify=args.run_verify,
            allow_local_sources=args.allow_local_source,
        )
    else:
        result = install_harness(
            args.identifier,
            wiki_path=wiki_path,
            installs_root=installs_root,
            manifest_dir=manifest_dir,
            target=target,
            dry_run=args.dry_run,
            force=args.force,
            approve_commands=args.approve_commands,
            run_verify=args.run_verify,
            allow_local_sources=args.allow_local_source,
        )
    if result.status in {
        "installed",
        "updated",
        "uninstalled",
        "dry-run",
        "skipped-existing",
    }:
        print(f"{result.status}: {result.slug}")
        if result.target is not None:
            print(f"target: {result.target}")
        if result.manifest_path is not None:
            print(f"manifest: {result.manifest_path}")
        if result.message:
            print(result.message)
        return 0
    print(f"Error: {result.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
