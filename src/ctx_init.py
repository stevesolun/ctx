"""ctx_init.py -- One-shot ``ctx-init`` command to bootstrap ~/.claude/ for ctx.

Replaces the legacy ``install.sh`` flow for users who installed via
``pip install claude-ctx``. The goal is a single command that, run
once after installation, produces a working environment:

    $ pip install claude-ctx
    $ ctx-init

What it does:

  1. Ensures ``~/.claude`` + standard subdirectories exist
     (``skills/``, ``agents/``, ``skill-wiki/``, ``skill-quality/``,
     ``backups/``).
  2. Copies the shipped starter config if ``skill-system-config.json``
     is missing (otherwise leaves the user's config alone).
  3. Seeds the starter toolboxes via ``ctx-toolbox init`` if the
     global toolboxes file is empty.
  4. In a terminal, guides first-time users through hooks, graph install,
     model profile, and harness recommendation setup. Automation can
     keep the non-interactive path by passing explicit flags such as
     ``--model-mode skip``; ``--wizard`` forces the prompts.
  5. Optionally: injects PostToolUse + Stop hooks via
     ``ctx-install-hooks``. Skipped unless the wizard or ``--hooks`` asks
     for it, so the user has to opt in to modifying
     ``~/.claude/settings.json``.
  6. Optionally: installs the initial graph/wiki archive if missing.
     Skipped unless the wizard or ``--graph`` asks for it. Source
     checkouts use ``graph/wiki-graph-runtime.tar.gz`` by default and
     fall back to the full ``graph/wiki-graph.tar.gz`` archive; pip installs
     download the matching release asset.

Idempotent: re-running only writes what's missing. Never overwrites
a user's config or hook settings without an explicit ``--force`` flag.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zlib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any


# ─── Directory layout ───────────────────────────────────────────────────────


_STANDARD_SUBDIRS = (
    "skills",
    "agents",
    "skill-wiki",
    "skill-wiki/entities",
    "skill-wiki/entities/skills",
    "skill-wiki/entities/agents",
    "skill-wiki/entities/mcp-servers",
    "skill-wiki/entities/harnesses",
    "skill-wiki/concepts",
    "skill-wiki/converted",
    "skill-wiki/graphify-out",
    "skill-quality",
    "backups",
)


def _claude_dir() -> Path:
    return Path(os.path.expanduser("~/.claude"))


def ensure_directories(root: Path | None = None) -> list[Path]:
    """Create standard subdirectories. Returns the list of paths created."""
    claude = root if root is not None else _claude_dir()
    claude.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for sub in _STANDARD_SUBDIRS:
        p = claude / sub
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(p)
    return created


# ─── Config seeding ─────────────────────────────────────────────────────────


_STARTER_USER_CONFIG = """{
  "_comment": "User-level overrides for ctx (claude-ctx) defaults. Edit me.",
  "_config_path": "~/.claude/skill-system-config.json"
}
"""
_KNOWLEDGE_MODES = frozenset({"shipped", "local", "enriched", "skip"})
_ACTIVE_KNOWLEDGE_MODES = frozenset({"shipped", "local", "enriched"})


def seed_user_config(claude: Path, *, force: bool = False) -> Path | None:
    """Write a stub ``skill-system-config.json`` if missing. Returns path if written."""
    target = claude / "skill-system-config.json"
    if target.exists() and not force:
        return None
    target.write_text(_STARTER_USER_CONFIG, encoding="utf-8")
    return target


def write_knowledge_config(claude: Path, mode: str) -> Path | None:
    """Persist the user's knowledge-source choice in skill-system-config.json."""
    if mode == "skip":
        return None
    if mode not in _ACTIVE_KNOWLEDGE_MODES:
        raise ValueError(f"unknown knowledge mode: {mode}")
    target = claude / "skill-system-config.json"
    try:
        data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"{target} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{target} must contain a JSON object")
    data.setdefault(
        "_comment",
        "User-level overrides for ctx (claude-ctx) defaults. Edit me.",
    )
    data.setdefault("_config_path", "~/.claude/skill-system-config.json")
    data["knowledge"] = {
        "mode": mode,
        "use_shipped_graph": mode in {"shipped", "enriched"},
        "allow_user_enrichment": mode in {"local", "enriched"},
    }
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return target


# ─── Toolbox seeding ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolboxSeedResult:
    returncode: int
    already_present: bool = False


def _toolbox_init_already_present(stderr: str) -> bool:
    return (
        "Global config already has" in stderr
        and "toolbox" in stderr
        and "Use --force to overwrite" in stderr
    )


def seed_toolboxes(*, force: bool = False) -> ToolboxSeedResult:
    """Invoke ``toolbox init`` to drop the 5 starter templates.

    Returns 0 on success, non-zero on failure. Safe to call when the
    global config already has toolboxes — ``toolbox init`` refuses to
    overwrite without ``--force``.
    """
    cmd = [sys.executable, "-m", "toolbox", "init"]
    if force:
        cmd.append("--force")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if (
        not force
        and result.returncode != 0
        and _toolbox_init_already_present(result.stderr)
    ):
        return ToolboxSeedResult(returncode=0, already_present=True)
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.stderr.strip():
        print(result.stderr.rstrip(), file=sys.stderr)
    return ToolboxSeedResult(returncode=result.returncode)


# ─── Hook injection (opt-in) ────────────────────────────────────────────────


def install_hooks(*, ctx_src_dir: Path, settings_path: Path | None = None) -> int:
    """Run ``inject_hooks.main()`` to wire PostToolUse + Stop hooks."""
    target_settings = settings_path or (_claude_dir() / "settings.json")
    cmd = [
        sys.executable, "-m", "ctx.adapters.claude_code.inject_hooks",
        "--settings", str(target_settings),
        "--ctx-dir", str(ctx_src_dir),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.stderr.strip():
        print(result.stderr.rstrip(), file=sys.stderr)
    return result.returncode


def _resolve_ctx_src_dir() -> Path:
    """Best-guess the directory containing the runtime modules.

    When installed via pip, the modules land in ``<sitepackages>/``. The
    inject_hooks template writes absolute python3 ... paths into the hook
    commands, so we pass in the site-packages directory that *this* file
    lives in.
    """
    return Path(__file__).resolve().parent


# ─── Graph install (opt-in) ────────────────────────────────────────────────


_GRAPH_ARCHIVE_NAME = "wiki-graph.tar.gz"
_GRAPH_RUNTIME_ARCHIVE_NAME = "wiki-graph-runtime.tar.gz"
_GRAPH_ENTITY_OVERLAY_NAME = "entity-overlays.jsonl"
_GRAPH_ENTITY_OVERLAY_SCORE_FIELDS = (
    "weight",
    "final_weight",
    "similarity_score",
    "semantic_sim",
    "tag_sim",
    "token_sim",
)
_GRAPH_ENTITY_OVERLAY_SHA256 = (
    "cc1a69d3452d2018bec1e049fc4ab1fa8f933adecfdcae4802a815be03f8611c"
)
_GRAPH_ARCHIVE_NAMES = {
    "runtime": _GRAPH_RUNTIME_ARCHIVE_NAME,
    "full": _GRAPH_ARCHIVE_NAME,
}
_GRAPH_ARCHIVE_SHA256 = {
    "runtime": "334fb19bace3fd6e4b92087850f17297fb248032957d123f3f1432dfde2e36c0",
    "full": "91b30795e7d200cf31a62a8749969d12658f5f74636d2de06d6b2b24b393c12f",
}
_GRAPH_RELEASE_URL = (
    "https://github.com/stevesolun/ctx/releases/download/"
    "v{version}/{archive_name}"
)
_GRAPH_REQUIRED_FILES = frozenset({
    "index.md",
    "graphify-out/graph.json",
    "graphify-out/graph-delta.json",
    "graphify-out/communities.json",
    "graphify-out/graph-report.md",
    "graphify-out/graph-export-manifest.json",
    "graphify-out/dashboard-neighborhoods.sqlite3",
    "external-catalogs/skills-sh/catalog.json",
})
_GRAPH_MANAGED_PATHS = (
    "graphify-out",
    "entities",
    "converted",
    "concepts",
    "external-catalogs",
    "index.md",
    "catalog.md",
    "converted-index.md",
    "log.md",
    "SCHEMA.md",
    "versions-catalog.md",
    ".obsidian",
)
_GRAPH_RUNTIME_MANAGED_PATHS = tuple(
    name for name in _GRAPH_MANAGED_PATHS if name != "entities"
) + ("entities/harnesses",)
_GRAPH_JSON_OUTLINE_BYTES = 1024 * 1024
_GRAPH_INSTALL_MODES = ("runtime", "full")
_GRAPH_RUNTIME_PREFIXES = ("graphify-out/", "external-catalogs/", "entities/harnesses/")
_GRAPH_RUNTIME_ROOT_FILES = frozenset({
    "catalog.md",
    "converted-index.md",
    "index.md",
    "log.md",
    "SCHEMA.md",
    "versions-catalog.md",
})


def build_graph(
    claude: Path | None = None,
    *,
    force: bool = False,
    graph_url: str | None = None,
    graph_sha256: str | None = None,
    allow_unverified_graph_url: bool = False,
    install_mode: str = "runtime",
) -> int:
    """Install the pre-built knowledge graph into ``~/.claude/skill-wiki``."""
    if install_mode not in _GRAPH_INSTALL_MODES:
        raise ValueError(f"unknown graph install mode: {install_mode}")
    claude_dir = claude or _claude_dir()
    wiki_dir = claude_dir / "skill-wiki"
    graph_json = wiki_dir / "graphify-out" / "graph.json"
    install_complete = (
        _graph_full_install_complete(wiki_dir)
        if install_mode == "full"
        else _graph_install_complete(wiki_dir)
    )
    if not force and install_complete:
        try:
            _install_graph_entity_overlay(
                wiki_dir,
                allow_release_download=graph_url is None,
            )
        except Exception as exc:
            print(
                f"  [error] graph overlay install failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        print(f"Graph already installed at {graph_json}; use --force to refresh.")
        return 0

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    archive = None if graph_url is not None else _find_local_graph_archive(install_mode)
    try:
        if archive is None:
            temp_dir = tempfile.TemporaryDirectory(prefix="ctx-graph-download-")
            archive = Path(temp_dir.name) / _graph_archive_name(install_mode)
            url = graph_url or _release_graph_url(install_mode)
            expected_sha256 = _expected_graph_archive_sha256(
                install_mode=install_mode,
                graph_url=graph_url,
                graph_sha256=graph_sha256,
                allow_unverified_graph_url=allow_unverified_graph_url,
            )
            print(f"Downloading pre-built graph from {url}")
            _download_graph_archive(archive, url=url, expected_sha256=expected_sha256)
        else:
            _verify_local_graph_archive(archive, requested_install_mode=install_mode)
            print(f"Installing pre-built graph from {archive}")
        _extract_graph_archive(archive, wiki_dir, install_mode=install_mode)
        _install_graph_entity_overlay(
            wiki_dir,
            allow_release_download=graph_url is None,
        )
    except Exception as exc:
        print(
            f"  [error] graph install failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    try:
        _validate_graph_install_tree(wiki_dir)
    except ValueError as exc:
        print(f"  [error] graph install validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _find_local_graph_archive(install_mode: str = "runtime") -> Path | None:
    module_path = Path(__file__).resolve()
    archive_names = [_graph_archive_name(install_mode)]
    if install_mode == "runtime":
        archive_names.append(_GRAPH_ARCHIVE_NAME)
    graph_dirs = (module_path.parent.parent / "graph", Path.cwd() / "graph")
    candidates = [
        graph_dir / archive_name
        for archive_name in archive_names
        for graph_dir in graph_dirs
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _find_local_graph_entity_overlay() -> Path | None:
    module_path = Path(__file__).resolve()
    graph_dirs = (module_path.parent.parent / "graph", Path.cwd() / "graph")
    for graph_dir in graph_dirs:
        candidate = graph_dir / _GRAPH_ENTITY_OVERLAY_NAME
        if candidate.is_file():
            return candidate
    return None


def _install_graph_entity_overlay(
    wiki_dir: Path,
    *,
    allow_release_download: bool = True,
) -> None:
    overlay = _find_local_graph_entity_overlay()
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if overlay is None and allow_release_download:
        temp_dir = tempfile.TemporaryDirectory(prefix="ctx-graph-overlay-")
        candidate = Path(temp_dir.name) / _GRAPH_ENTITY_OVERLAY_NAME
        try:
            _download_graph_archive(
                candidate,
                url=_release_asset_url(_GRAPH_ENTITY_OVERLAY_NAME),
                expected_sha256=_GRAPH_ENTITY_OVERLAY_SHA256,
            )
        except OSError:
            temp_dir.cleanup()
            return
        overlay = candidate
    if overlay is None:
        return
    try:
        _validate_graph_entity_overlay(overlay)
        destination = wiki_dir / "graphify-out" / _GRAPH_ENTITY_OVERLAY_NAME
        target_root = wiki_dir.resolve()
        _ensure_path_under_root(destination.parent, target_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            destination.unlink()
        tmp = destination.with_name(f".{destination.name}.tmp")
        tmp.write_bytes(overlay.read_bytes())
        os.replace(tmp, destination)
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def _validate_graph_entity_overlay(path: Path) -> None:
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path} line {lineno} must contain a JSON object")
        nodes = payload.get("nodes", [])
        edges = payload.get("edges", [])
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise ValueError(f"{path} line {lineno} must contain nodes/edges lists")
        for index, node in enumerate(nodes, 1):
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                raise ValueError(f"{path} line {lineno} node {index} must contain id")
        for index, edge in enumerate(edges, 1):
            if not isinstance(edge, dict):
                raise ValueError(f"{path} line {lineno} edge {index} must be an object")
            if not isinstance(edge.get("source"), str) or not isinstance(edge.get("target"), str):
                raise ValueError(
                    f"{path} line {lineno} edge {index} must contain source/target"
                )
            numeric_scores: dict[str, float] = {}
            for field in _GRAPH_ENTITY_OVERLAY_SCORE_FIELDS:
                value = edge.get(field)
                if value is None:
                    continue
                if not isinstance(value, int | float) or not 0 <= float(value) <= 1:
                    raise ValueError(
                        f"{path} line {lineno} edge {index} {field} must be 0..1"
                    )
                numeric_scores[field] = float(value)
            if (
                "weight" in numeric_scores
                and "final_weight" in numeric_scores
                and abs(numeric_scores["weight"] - numeric_scores["final_weight"]) > 1e-9
            ):
                raise ValueError(
                    f"{path} line {lineno} edge {index} weight must equal final_weight"
                )


def _release_graph_url(install_mode: str = "runtime") -> str:
    return _release_asset_url(_graph_archive_name(install_mode))


def _release_asset_url(asset_name: str) -> str:
    return _GRAPH_RELEASE_URL.format(
        version=_package_version(),
        archive_name=asset_name,
    )


def _graph_archive_name(install_mode: str) -> str:
    return _GRAPH_ARCHIVE_NAMES.get(install_mode, _GRAPH_RUNTIME_ARCHIVE_NAME)


def _expected_graph_archive_sha256(
    *,
    install_mode: str,
    graph_url: str | None,
    graph_sha256: str | None,
    allow_unverified_graph_url: bool,
) -> str | None:
    if graph_sha256:
        normalized = graph_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("--graph-sha256 must be a 64-character SHA-256 hex digest")
        return normalized
    if graph_url is None or graph_url == _release_graph_url(install_mode):
        return _GRAPH_ARCHIVE_SHA256[install_mode]
    if allow_unverified_graph_url:
        return None
    raise ValueError(
        "custom --graph-url requires --graph-sha256, or pass "
        "--allow-unverified-graph-url to opt out explicitly"
    )


def _verify_local_graph_archive(archive: Path, *, requested_install_mode: str) -> None:
    archive_mode = (
        "full" if archive.name == _GRAPH_ARCHIVE_NAME else requested_install_mode
    )
    expected = _GRAPH_ARCHIVE_SHA256.get(archive_mode)
    if expected is None:
        return
    hasher = hashlib.sha256()
    with archive.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            hasher.update(chunk)
    actual = hasher.hexdigest()
    if actual.lower() != expected.lower():
        raise ValueError(
            "local graph archive checksum mismatch: "
            f"{archive} expected {expected.lower()} got {actual.lower()}"
        )


def _package_version() -> str:
    try:
        return package_version("claude-ctx")
    except PackageNotFoundError:
        try:
            from ctx import __version__
        except Exception:
            return "0.7.13"
        return str(__version__)


def _download_graph_archive(
    destination: Path,
    *,
    url: str,
    expected_sha256: str | None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
        with destination.open("wb") as fh:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                hasher.update(chunk)
    if expected_sha256 is not None:
        actual_sha256 = hasher.hexdigest()
        if actual_sha256.lower() != expected_sha256.lower():
            destination.unlink(missing_ok=True)
            raise ValueError(
                "graph archive checksum mismatch: "
                f"expected {expected_sha256.lower()} got {actual_sha256.lower()}"
            )


def _extract_graph_archive(
    archive: Path,
    target_dir: Path,
    *,
    install_mode: str,
) -> None:
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{target_dir.name}-stage-",
        dir=target_dir.parent,
    ) as staging_name:
        staging_dir = Path(staging_name)
        _extract_graph_archive_to_dir(
            archive,
            staging_dir,
            install_mode=install_mode,
        )
        _validate_graph_install_tree(staging_dir)
        _promote_graph_tree(staging_dir, target_dir, install_mode=install_mode)


def _extract_graph_archive_to_dir(
    archive: Path,
    target_dir: Path,
    *,
    install_mode: str,
) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    target_root = target_dir.resolve()
    extracted_required: set[str] = set()
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf:
            _validate_graph_tar_member(member)
            safe_name = _safe_graph_member_name(member.name)
            if not _should_extract_graph_member(
                safe_name,
                member,
                install_mode=install_mode,
            ):
                continue
            destination = _graph_member_destination(target_dir, target_root, member)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            source = tf.extractfile(member)
            if source is None:
                raise ValueError(f"graph archive file is unreadable: {member.name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            _ensure_path_under_root(destination.parent, target_root)
            with source, destination.open("wb") as fh:
                shutil.copyfileobj(source, fh)
            if safe_name in _GRAPH_REQUIRED_FILES:
                extracted_required.add(safe_name)


def _graph_install_complete(wiki_dir: Path) -> bool:
    try:
        _validate_graph_install_tree(wiki_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return True


def _graph_full_install_complete(wiki_dir: Path) -> bool:
    if not _graph_install_complete(wiki_dir):
        return False
    entities = wiki_dir / "entities"
    return entities.is_dir() and any(entities.iterdir())


def _validate_graph_install_tree(wiki_dir: Path) -> None:
    missing = [
        name
        for name in sorted(_GRAPH_REQUIRED_FILES)
        if not (wiki_dir / name).is_file() or (wiki_dir / name).stat().st_size == 0
    ]
    if missing:
        raise ValueError(f"graph archive is missing required files: {missing}")

    _validate_graph_json_outline(wiki_dir / "graphify-out" / "graph.json")

    manifest = _read_json_file(wiki_dir / "graphify-out" / "graph-export-manifest.json")
    if not isinstance(manifest, dict):
        raise ValueError("graph-export-manifest.json must contain a JSON object")
    if manifest.get("version") != 1:
        raise ValueError("graph export manifest version must be 1")
    export_id = manifest.get("export_id")
    if not isinstance(export_id, str) or not export_id.strip():
        raise ValueError("graph export manifest is missing export_id")
    artifacts = manifest.get("artifacts")
    expected_artifacts = {
        "graph": "graph.json",
        "delta": "graph-delta.json",
        "communities": "communities.json",
        "report": "graph-report.md",
    }
    if not isinstance(artifacts, dict) or artifacts != expected_artifacts:
        raise ValueError("graph export manifest artifacts map is incomplete")
    _validate_dashboard_index_file(
        wiki_dir / "graphify-out" / "dashboard-neighborhoods.sqlite3",
        expected_export_id=export_id.strip(),
    )


def _validate_graph_json_outline(path: Path) -> None:
    size = path.stat().st_size
    read_size = min(size, _GRAPH_JSON_OUTLINE_BYTES)
    with path.open("rb") as f:
        head = f.read(read_size)
        if size > read_size:
            f.seek(max(0, size - read_size))
            tail = f.read(read_size)
        else:
            tail = b""
    head_text = head.decode("utf-8", errors="ignore")
    tail_text = tail.decode("utf-8", errors="ignore")
    if not head_text.lstrip().startswith("{"):
        raise ValueError("graphify-out/graph.json must contain a JSON object")
    if tail_text and not tail_text.rstrip().endswith("}"):
        raise ValueError("graphify-out/graph.json appears truncated")
    outline = f"{head_text}\n{tail_text}"
    if '"nodes"' not in outline:
        raise ValueError("graphify-out/graph.json is missing a nodes list")
    if '"edges"' not in outline and '"links"' not in outline:
        raise ValueError("graphify-out/graph.json is missing an edges/links list")


def _validate_dashboard_index_file(path: Path, *, expected_export_id: str) -> None:
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise ValueError(f"dashboard-neighborhoods.sqlite3 is not valid SQLite: {exc}") from exc
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()
        if quick is None or str(quick[0]).lower() != "ok":
            raise ValueError("dashboard-neighborhoods.sqlite3 failed quick_check")
        tables = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {"meta", "nodes", "slug_index", "neighbors"}
        missing = sorted(required - tables)
        if missing:
            raise ValueError(f"dashboard-neighborhoods.sqlite3 missing tables: {missing}")
        meta = {
            str(row["key"]): json.loads(str(row["value"]))
            for row in conn.execute("SELECT key,value FROM meta")
        }
        if meta.get("export_id") != expected_export_id:
            raise ValueError(
                "dashboard-neighborhoods.sqlite3 export_id mismatch: "
                f"expected {expected_export_id}, got {meta.get('export_id') or 'missing'}",
            )
        nodes_count = int(meta.get("nodes_count") or 0)
        actual_nodes = int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
        if nodes_count != actual_nodes:
            raise ValueError("dashboard-neighborhoods.sqlite3 nodes_count mismatch")
        payload = conn.execute("SELECT payload FROM neighbors LIMIT 1").fetchone()
        if payload is not None:
            decoded = json.loads(zlib.decompress(payload["payload"]).decode("utf-8"))
            if not isinstance(decoded, list):
                raise ValueError("dashboard-neighborhoods.sqlite3 neighbor payload is invalid")
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError, zlib.error) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"dashboard-neighborhoods.sqlite3 validation failed: {exc}") from exc
    finally:
        conn.close()


def _read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _promote_graph_tree(
    staging_dir: Path,
    target_dir: Path,
    *,
    install_mode: str,
) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    target_root = target_dir.resolve()
    managed_paths = (
        _GRAPH_MANAGED_PATHS
        if install_mode == "full"
        else _GRAPH_RUNTIME_MANAGED_PATHS
    )
    for name in managed_paths:
        source = staging_dir / name
        destination = target_dir / name
        _ensure_path_under_root(destination.parent, target_root)
        _remove_existing_graph_path(destination)
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
    _validate_graph_install_tree(target_dir)


def _remove_existing_graph_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _graph_member_destination(
    target_dir: Path,
    target_root: Path,
    member: tarfile.TarInfo,
) -> Path:
    destination = target_dir.joinpath(*PurePosixPath(member.name.replace("\\", "/")).parts)
    _ensure_path_under_root(destination, target_root)
    return destination


def _safe_graph_member_name(name: str) -> str:
    path = PurePosixPath(name.replace("\\", "/"))
    return path.as_posix()


def _should_extract_graph_member(
    safe_name: str,
    member: tarfile.TarInfo,
    *,
    install_mode: str,
) -> bool:
    if install_mode == "full":
        return True
    if member.isdir():
        return False
    return (
        safe_name in _GRAPH_RUNTIME_ROOT_FILES
        or safe_name in _GRAPH_REQUIRED_FILES
        or any(safe_name.startswith(prefix) for prefix in _GRAPH_RUNTIME_PREFIXES)
    )


def _ensure_path_under_root(path: Path, root: Path) -> None:
    resolved = path.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"graph archive path escapes target: {path}")


def _validate_graph_tar_member(member: tarfile.TarInfo) -> None:
    name = member.name.replace("\\", "/")
    path = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"unsafe graph archive path: {member.name}")
    if member.issym() or member.islnk():
        raise ValueError(f"graph archive links are not allowed: {member.name}")
    if not (member.isdir() or member.isfile()):
        raise ValueError(f"unsupported graph archive member: {member.name}")
    if name.endswith(".original") or name.endswith(".lock"):
        raise ValueError(f"graph archive backup/lock members are not allowed: {member.name}")


# ─── Model onboarding ───────────────────────────────────────────────────────


_MODEL_PROFILE_NAME = "ctx-model-profile.json"

_HARNESS_TOKEN_RE = re.compile(r"[a-z0-9]+")

_HARNESS_GOAL_NOISE = frozenset({
    "build",
    "create",
    "into",
    "make",
    "turn",
    "write",
    "need",
    "want",
    "using",
    "custom",
    "harness",
    "harnesses",
    "model",
    "models",
    "llm",
    "llms",
    "api",
    "apis",
    "work",
    "workflow",
    "workflows",
    "project",
    "repo",
    "development",
    "dev",
    "from",
    "only",
})

_HARNESS_SIGNAL_ALIASES: dict[str, set[str]] = {
    "ai": {"ai", "llm", "model"},
    "agent": {"agent", "agents", "agentic"},
    "agents": {"agent", "agents", "agentic"},
    "browser": {"browser", "viewer", "web"},
    "cad": {"cad", "3d", "modeling", "modelling"},
    "checks": {"check", "checks", "test", "tests", "validation", "verify", "verification"},
    "checkpoint": {"checkpoint", "checkpoints", "checkpointing"},
    "checkpointing": {"checkpoint", "checkpoints", "checkpointing"},
    "cli": {"cli", "command", "commands"},
    "export": {"export", "dxf", "glb", "stl"},
    "files": {"file", "files", "filesystem", "local"},
    "filesystem": {"file", "files", "filesystem", "local"},
    "geometry": {"cad", "geometry", "3d"},
    "local": {"local", "ollama", "vllm"},
    "mcp": {"mcp", "server", "servers"},
    "openai": {"openai", "gpt"},
    "private": {"private", "local", "offline", "self", "hosted"},
    "pytest": {"pytest", "test", "tests", "validation", "verify", "verification"},
    "python": {"python", "py"},
    "robotics": {"robot", "robotics", "urdf"},
    "shell": {"shell", "cli", "command", "commands", "script", "scripts"},
    "tool": {"tool", "tools"},
    "tools": {"tool", "tools"},
}

_HARNESS_SOFT_REQUIREMENT_SIGNALS = frozenset({
    "browser",
    "check",
    "checks",
    "darwin",
    "linux",
    "mac",
    "macos",
    "gpt",
    "mcp",
    "npm",
    "pytest",
    "secret",
    "secrets",
    "shell",
    "win32",
    "windows",
})

_DEFAULT_HARNESS_RELIABILITY_WEIGHTS = {
    "context": 0.34,
    "constraints": 0.33,
    "convergence": 0.33,
}

_HARNESS_RELIABILITY_TERMS = {
    "context": frozenset({
        "context",
        "contexts",
        "document",
        "documents",
        "durable",
        "knowledge",
        "memory",
        "persistent",
        "replay",
        "retrieval",
        "state",
        "wiki",
    }),
    "constraints": frozenset({
        "access",
        "approval",
        "approvals",
        "boundaries",
        "boundary",
        "governance",
        "limits",
        "permission",
        "permissions",
        "policy",
        "policies",
        "rules",
        "sandbox",
        "security",
    }),
    "convergence": frozenset({
        "automated",
        "check",
        "checks",
        "eval",
        "evals",
        "evaluation",
        "gates",
        "idempotence",
        "monitoring",
        "pytest",
        "retries",
        "retry",
        "tests",
        "tracing",
        "validation",
        "verify",
    }),
}

_MODEL_VERSION_PREFIXES = (
    "claude",
    "codellama",
    "deepseek",
    "gemini",
    "glm",
    "gpt",
    "llama",
    "mistral",
    "mixtral",
    "phi",
    "qwen",
)

_PROVIDER_KEY_ENV: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "together": "TOGETHER_API_KEY",
    "groq": "GROQ_API_KEY",
    "ollama": "",
}

_HARNESS_REQUIREMENT_FIELDS = (
    ("runtime", "harness_runtime"),
    ("autonomy", "harness_autonomy"),
    ("tools", "harness_tools"),
    ("verification", "harness_verify"),
    ("privacy", "harness_privacy"),
    ("attach_mode", "harness_attach_mode"),
)

_HARNESS_REQUIREMENT_FLAGS = {
    "runtime": "--harness-runtime",
    "autonomy": "--harness-autonomy",
    "tools": "--harness-tools",
    "verification": "--harness-verify",
    "privacy": "--harness-privacy",
    "attach_mode": "--harness-attach-mode",
}


def _model_provider_prefix(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else model


def _resolve_api_key_env(
    explicit: str | None,
    model: str | None,
    provider: str | None,
) -> str | None:
    if explicit is not None:
        return explicit or None
    prefix = provider or (_model_provider_prefix(model) if model else "")
    env_name = _PROVIDER_KEY_ENV.get(prefix, "")
    return env_name or None


def write_model_profile(
    claude: Path,
    profile: dict[str, Any],
    *,
    force: bool = False,
) -> Path | None:
    """Write the user's ctx model/onboarding profile if allowed."""
    target = claude / _MODEL_PROFILE_NAME
    if target.exists() and not force:
        return None
    target.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return target


def recommend_harnesses(
    goal: str,
    *,
    top_k: int = 5,
    model_provider: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Return high-confidence harness catalog recommendations."""
    if not goal.strip():
        return []
    try:
        from ctx.core.resolve.recommendations import (  # noqa: PLC0415
            query_to_tags,
            recommend_by_tags,
        )
        from ctx_config import cfg  # noqa: PLC0415

        graph = _load_recommendation_graph()
        if graph.number_of_nodes() == 0:
            return []
        limit = max(1, min(int(top_k), cfg.recommendation_top_k))
        candidate_limit = max(limit * 4, 25)
        signals = query_to_tags(goal)
        results = recommend_by_tags(
            graph,
            signals,
            top_n=candidate_limit,
            query=goal,
            entity_types=("harness",),
            min_normalized_score=0.0,
            # Harness fit is recomputed from provider/runtime/capability terms below.
            # Avoid loading local embedding models on the latency-sensitive CLI path.
            use_semantic_query=False,
        )
        results = _add_unranked_harness_candidates(graph, results)
        results = [
            row for row in results
            if _harness_supports_provider(
                graph,
                str(row.get("name") or ""),
                model_provider,
                model=model,
            )
        ]
        installed = _installed_harness_slugs(cfg.claude_dir / "harness-installs")
        if installed:
            results = [
                row for row in results
                if str(row.get("name") or "") not in installed
            ]
        threshold = cfg.harness_recommendation_min_fit_score
        for row in results:
            row.update(_annotate_harness_fit(graph, row, signals))
        if results:
            results = [
                row for row in results
                if float(row.get("fit_score") or 0.0) >= threshold
            ]
            results.sort(
                key=lambda row: (
                    -float(row.get("fit_score") or 0.0),
                    -float(row.get("reliability_score") or 0.0),
                    -float(row.get("normalized_score") or 0.0),
                    str(row.get("name") or ""),
                )
            )
    except Exception as exc:  # noqa: BLE001
        print(
            f"  [warn] harness recommendation failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return []
    return results[:limit]


def _add_unranked_harness_candidates(
    graph: Any,
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add catalog harnesses that tag ranking missed, then let fit scoring decide."""
    expanded = list(results)
    seen = {str(row.get("name") or "") for row in expanded}
    try:
        nodes = graph.nodes(data=True)
    except Exception:
        return expanded
    for node_id, data in nodes:
        if str(data.get("type")) != "harness":
            continue
        slug = str(data.get("label") or str(node_id).rsplit(":", 1)[-1]).strip()
        if not slug or slug in seen:
            continue
        expanded.append({
            "name": slug,
            "type": "harness",
            "score": 75.0,
            "normalized_score": 0.0,
            "matching_tags": [],
            "source": data.get("source") or "catalog",
        })
        seen.add(slug)
    return expanded


def _annotate_harness_fit(
    graph: Any,
    row: dict[str, Any],
    signals: list[str],
) -> dict[str, Any]:
    """Return absolute fit metadata for a harness recommendation row."""
    relevant_signals = _relevant_harness_signals(signals)
    terms = _harness_candidate_terms(graph, row)
    scored_signals: list[str] = []
    matched: list[str] = []
    soft_matched: list[str] = []
    for signal in relevant_signals:
        if _looks_like_model_version_signal(signal):
            continue
        signal_matches = _harness_signal_matches(signal, terms)
        if _is_soft_harness_requirement_signal(signal):
            if signal_matches:
                soft_matched.append(signal)
            continue
        scored_signals.append(signal)
        if signal_matches:
            matched.append(signal)
    matched_set = set(matched)
    all_matched_set = matched_set | set(soft_matched)
    missing = [
        signal for signal in scored_signals
        if signal not in matched_set
    ]

    coverage = (
        len(matched) / len(scored_signals)
        if scored_signals else 0.0
    )
    breadth = min(len(all_matched_set) / 3.0, 1.0)
    raw_strength = _clamp_harness_score(float(row.get("score") or 0.0) / 75.0)
    fit_score = round(
        _clamp_harness_score((0.8 * coverage * breadth) + (0.2 * raw_strength)),
        4,
    )
    return {
        "fit_score": fit_score,
        "fit_signals": sorted(all_matched_set),
        "missing_signals": missing[:8],
        "fit_reason": _harness_fit_reason(matched, scored_signals),
        **_harness_reliability_metadata(terms),
    }


def _harness_fit_reason(matched: list[str], relevant_signals: list[str]) -> str:
    if not relevant_signals:
        return "no concrete goal signals were provided"
    if not matched:
        return "no concrete goal signals matched this harness"
    matched_set = set(matched)
    if len(matched_set) < min(3, len(set(relevant_signals))):
        return "matched too few concrete goal signals: " + ", ".join(sorted(matched_set))
    return "matched concrete goal signals: " + ", ".join(sorted(matched_set))


def _relevant_harness_signals(signals: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for signal in signals:
        token = signal.strip().lower()
        if len(token) < 3 or token in _HARNESS_GOAL_NOISE:
            continue
        seen.setdefault(token, None)
    return list(seen.keys())


def _looks_like_model_version_signal(signal: str) -> bool:
    token = signal.strip().lower()
    return any(char.isdigit() for char in token) and token.startswith(
        _MODEL_VERSION_PREFIXES
    )


def _is_soft_harness_requirement_signal(signal: str) -> bool:
    """Return true for install/environment details that should not dominate fit."""
    return signal.strip().lower() in _HARNESS_SOFT_REQUIREMENT_SIGNALS


def _harness_candidate_terms(graph: Any, row: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    slug = str(row.get("name") or "")
    terms.update(_harness_tokens(slug))
    terms.update(_harness_tokens(str(row.get("type") or "")))
    terms.update(_harness_tokens(str(row.get("source") or "")))
    for key in ("matching_tags", "shared_tags", "tags"):
        raw = row.get(key)
        if isinstance(raw, (list, tuple, set, frozenset)):
            for item in raw:
                terms.update(_harness_tokens(str(item)))
    node_data = _harness_node_data(graph, slug)
    for key in ("label", "description", "summary", "source"):
        terms.update(_harness_tokens(str(node_data.get(key) or "")))
    for key in (
        "tags",
        "capabilities",
        "model_providers",
        "runtimes",
        "setup_commands",
        "verify_commands",
        "attach_modes",
        "sources",
    ):
        _add_harness_terms(terms, node_data.get(key))
    wiki_fm = _harness_frontmatter_from_wiki(slug)
    for key in ("title", "description", "summary", "repo_url", "docs_url"):
        terms.update(_harness_tokens(str(wiki_fm.get(key) or "")))
    for key in (
        "tags",
        "capabilities",
        "model_providers",
        "runtimes",
        "setup_commands",
        "verify_commands",
        "attach_modes",
        "sources",
    ):
        _add_harness_terms(terms, wiki_fm.get(key))
    return terms


def _harness_reliability_metadata(terms: set[str]) -> dict[str, Any]:
    """Score the harness-engineering 3C contract: context/constraints/convergence."""
    weights = _harness_reliability_weights()
    dimensions: dict[str, dict[str, Any]] = {}
    weighted_total = 0.0
    total_weight = 0.0
    covered: list[str] = []

    for dimension, required_terms in _HARNESS_RELIABILITY_TERMS.items():
        matched = sorted(required_terms & terms)
        score = _clamp_harness_score(len(matched) / 2.0)
        weight = weights.get(dimension, 0.0)
        weighted_total += weight * score
        total_weight += weight
        if matched:
            covered.append(dimension)
        dimensions[dimension] = {
            "score": round(score, 4),
            "matched_terms": matched[:8],
        }

    reliability_score = round(
        _clamp_harness_score(weighted_total / total_weight)
        if total_weight > 0 else 0.0,
        4,
    )
    return {
        "reliability_score": reliability_score,
        "reliability_dimensions": dimensions,
        "reliability_reason": _harness_reliability_reason(covered),
    }


def _harness_reliability_weights() -> dict[str, float]:
    try:
        from ctx_config import cfg  # noqa: PLC0415

        raw = getattr(cfg, "harness_reliability_weights", None)
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return dict(_DEFAULT_HARNESS_RELIABILITY_WEIGHTS)
    weights: dict[str, float] = {}
    for dimension, default in _DEFAULT_HARNESS_RELIABILITY_WEIGHTS.items():
        try:
            value = float(raw.get(dimension, default))
        except (TypeError, ValueError):
            value = default
        weights[dimension] = max(0.0, value)
    total = sum(weights.values())
    if total <= 0:
        return dict(_DEFAULT_HARNESS_RELIABILITY_WEIGHTS)
    return {dimension: value / total for dimension, value in weights.items()}


def _harness_reliability_reason(covered: list[str]) -> str:
    if not covered:
        return "no harness reliability signals for context, constraints, or convergence"
    missing = [
        dimension for dimension in _DEFAULT_HARNESS_RELIABILITY_WEIGHTS
        if dimension not in covered
    ]
    if not missing:
        return "covers context, constraints, and convergence"
    return (
        "covers " + ", ".join(covered)
        + "; missing " + ", ".join(missing)
    )


def _add_harness_terms(terms: set[str], raw: object) -> None:
    if isinstance(raw, str):
        terms.update(_harness_tokens(raw))
    elif isinstance(raw, (list, tuple, set, frozenset)):
        for item in raw:
            terms.update(_harness_tokens(str(item)))


def _harness_node_data(graph: Any, slug: str) -> dict[str, Any]:
    try:
        nodes = graph.nodes(data=True)
    except Exception:
        return {}
    for node_id, data in nodes:
        if str(data.get("type")) != "harness":
            continue
        label = str(data.get("label") or "")
        if label == slug or str(node_id).rsplit(":", 1)[-1] == slug:
            return dict(data)
    return {}


def _harness_signal_matches(signal: str, terms: set[str]) -> bool:
    candidates = set(_HARNESS_SIGNAL_ALIASES.get(signal, {signal}))
    candidates.update(_harness_tokens(signal))
    if signal.endswith("ies"):
        candidates.add(signal[:-3] + "y")
    elif signal.endswith("s") and len(signal) > 3:
        candidates.add(signal[:-1])
    else:
        candidates.add(signal + "s")
    return bool(candidates & terms)


def _harness_tokens(value: str) -> set[str]:
    tokens = {
        token for token in _HARNESS_TOKEN_RE.findall(value.lower())
        if len(token) >= 2
    }
    expanded = set(tokens)
    for token in tokens:
        expanded.update(_HARNESS_SIGNAL_ALIASES.get(token, ()))
    return expanded


def _clamp_harness_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def _installed_harness_slugs(manifest_dir: Path) -> set[str]:
    """Return harness slugs with an active install manifest."""
    if not manifest_dir.exists():
        return set()
    slugs: set[str] = set()
    for path in manifest_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - corrupt manifests should not break onboarding.
            continue
        if str(data.get("status") or "installed") != "installed":
            continue
        slug = str(data.get("slug") or path.stem).strip()
        if slug:
            slugs.add(slug)
    return slugs


def _harness_supports_provider(
    graph: Any,
    slug: str,
    model_provider: str | None,
    *,
    model: str | None = None,
) -> bool:
    """Return true when a harness is compatible with the requested provider."""
    requested = _provider_match_candidates(model_provider, model)
    if not requested:
        return True
    providers = _harness_model_providers_from_graph(graph, slug)
    if not providers:
        providers = _harness_model_providers_from_wiki(slug)
    if not providers:
        return True
    if providers.intersection({"model-agnostic", "any", "all", "litellm"}):
        return True
    return bool(requested & providers)


def _provider_match_candidates(
    model_provider: str | None,
    model: str | None,
) -> set[str]:
    providers = {
        candidate for candidate in (
            _normalise_model_provider(model_provider),
            _normalise_model_provider(_model_provider_prefix(model or "")),
        ) if candidate
    }
    parts = [part for part in (model or "").split("/") if part]
    if parts and _normalise_model_provider(parts[0]) in {"openrouter", "litellm"}:
        providers.update(
            _normalise_model_provider(part)
            for part in parts[1:2]
            if _normalise_model_provider(part)
        )
    return providers


def _normalise_model_provider(value: str | None) -> str:
    provider = (value or "").strip().lower()
    if not provider:
        return ""
    aliases = {
        "azure": "azure-openai",
        "azure_openai": "azure-openai",
        "googleai": "google",
        "gemini": "google",
        "local": "ollama",
        "model_agnostic": "model-agnostic",
        "model agnostic": "model-agnostic",
    }
    return aliases.get(provider, provider)


def _normalise_model_providers(raw: object) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = [str(item) for item in raw]
    else:
        return set()
    return {
        provider for value in values
        if (provider := _normalise_model_provider(value))
    }


def _harness_model_providers_from_graph(graph: Any, slug: str) -> set[str]:
    for _node_id, data in graph.nodes(data=True):
        if str(data.get("type")) != "harness":
            continue
        if str(data.get("label") or "") != slug:
            continue
        return _normalise_model_providers(data.get("model_providers"))
    return set()


def _harness_model_providers_from_wiki(slug: str) -> set[str]:
    return _normalise_model_providers(
        _harness_frontmatter_from_wiki(slug).get("model_providers")
    )


def _harness_frontmatter_from_wiki(slug: str) -> dict[str, Any]:
    try:
        from ctx.core.entity_types import entity_page_path  # noqa: PLC0415
        from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body  # noqa: PLC0415
        from ctx_config import cfg  # noqa: PLC0415

        path = entity_page_path(cfg.wiki_dir, "harness", slug)
        if path is None or not path.is_file():
            return {}
        fm, _body = parse_frontmatter_and_body(
            path.read_text(encoding="utf-8", errors="replace"),
        )
        return fm
    except Exception:
        return {}


def _load_recommendation_graph() -> Any:
    """Load the ctx knowledge graph for harness onboarding."""
    from ctx.core.graph.resolve_graph import load_graph  # noqa: PLC0415
    from ctx_config import cfg  # noqa: PLC0415

    return load_graph(cfg.wiki_dir / "graphify-out" / "graph.json")


def validate_model_connection(
    *,
    model: str,
    api_key_env: str | None,
    base_url: str | None,
) -> int:
    """Make one tiny provider call when the user explicitly asks."""
    try:
        from ctx.adapters.generic.providers import Message, get_provider  # noqa: PLC0415

        client = get_provider(
            default_model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            timeout=30.0,
        )
        client.complete(
            [Message(role="user", content="Reply exactly: ctx-ok")],
            model=model,
            temperature=0.0,
            max_tokens=8,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"  [warn] model validation failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


def _prompt_yes_no(prompt: str, *, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} [{suffix}] ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("  Please answer yes or no.")


def _prompt_text(prompt: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or (default or "")


def _prompt_model_mode(default: str = "claude-code") -> str:
    while True:
        answer = input(
            "Use Claude Code or a custom model with ctx? "
            f"[{default}; choices: claude-code/custom/skip] "
        ).strip().lower()
        mode = answer or default
        if mode in {"claude-code", "custom", "skip"}:
            return mode
        print("  Please choose claude-code, custom, or skip.")


def _prompt_knowledge_mode(default: str = "shipped") -> str:
    while True:
        answer = input(
            "Use ctx shipped knowledge, local/private knowledge, or both? "
            f"[{default}; choices: shipped/local/enriched/skip] "
        ).strip().lower()
        mode = answer or default
        if mode in _KNOWLEDGE_MODES:
            return mode
        print("  Please choose shipped, local, enriched, or skip.")


def _harness_requirements_from_args(args: argparse.Namespace) -> dict[str, str]:
    requirements: dict[str, str] = {}
    for key, attr in _HARNESS_REQUIREMENT_FIELDS:
        value = str(getattr(args, attr, "") or "").strip()
        if value:
            requirements[key] = value
    return requirements


def _harness_requirements_text(requirements: dict[str, str]) -> str:
    return " ".join(
        value for _key, value in requirements.items()
        if value.strip()
    )


def _harness_plan_command(
    *,
    goal: str,
    model_provider: str | None,
    model: str | None,
    harness_requirements: dict[str, str],
) -> str:
    parts = [
        "ctx-harness-install --recommend",
        f"--goal {json.dumps(goal or model or 'custom model work')}",
    ]
    if model_provider:
        parts.append(f"--model-provider {json.dumps(model_provider)}")
    if model:
        parts.append(f"--model {json.dumps(model)}")
    for key, _attr in _HARNESS_REQUIREMENT_FIELDS:
        value = harness_requirements.get(key)
        if value:
            parts.append(f"{_HARNESS_REQUIREMENT_FLAGS[key]} {json.dumps(value)}")
    parts.append("--plan-on-no-fit")
    return " ".join(parts)


def _prompt_harness_requirements(args: argparse.Namespace) -> None:
    args.harness_runtime = _prompt_text(
        "Runtime / OS target for the harness",
        default=getattr(args, "harness_runtime", None),
    )
    args.harness_autonomy = _prompt_text(
        "Autonomy level, e.g. supervised or autonomous",
        default=getattr(args, "harness_autonomy", None) or "supervised",
    )
    args.harness_tools = _prompt_text(
        "Allowed tools/access, e.g. filesystem shell browser",
        default=getattr(args, "harness_tools", None),
    )
    args.harness_verify = _prompt_text(
        "Verification commands or gates, e.g. pytest ruff",
        default=getattr(args, "harness_verify", None),
    )
    args.harness_privacy = _prompt_text(
        "Privacy/network constraints",
        default=getattr(args, "harness_privacy", None),
    )
    args.harness_attach_mode = _prompt_text(
        "How ctx should attach, e.g. mcp, cli, or api",
        default=getattr(args, "harness_attach_mode", None) or "mcp",
    )


def _stdio_is_interactive() -> bool:
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


def _should_run_wizard(
    args: argparse.Namespace,
    raw_argv: list[str],
) -> bool:
    return bool(args.wizard or (not raw_argv and _stdio_is_interactive()))


def run_wizard(args: argparse.Namespace) -> None:
    """Prompt for first-run choices and mutate parsed args in place."""
    print("ctx-init wizard:")
    args.hooks = _prompt_yes_no(
        "Install Claude Code observation hooks now?",
        default=args.hooks,
    )
    args.knowledge_mode = _prompt_knowledge_mode(args.knowledge_mode or "shipped")
    if args.knowledge_mode in {"shipped", "enriched"}:
        args.graph = _prompt_yes_no(
            "Install the shipped ctx graph/wiki now?",
            default=args.graph,
        )
    elif args.knowledge_mode == "local":
        args.graph = False

    args.model_mode = _prompt_model_mode(args.model_mode or "claude-code")
    if args.model_mode == "skip":
        return

    if args.model_mode == "custom":
        args.model = _prompt_text(
            "Model slug, e.g. openai/gpt-5.5 or ollama/llama3.1",
            default=args.model,
        )
        provider_default = args.model_provider or (
            _model_provider_prefix(args.model) if args.model else None
        )
        args.model_provider = _prompt_text(
            "Provider prefix",
            default=provider_default,
        ) or None
        api_key_default = _resolve_api_key_env(
            args.api_key_env,
            args.model,
            args.model_provider,
        )
        args.api_key_env = _prompt_text(
            "API key environment variable (blank for local/no key)",
            default=api_key_default,
        )
        args.base_url = _prompt_text(
            "Provider base URL (blank for default)",
            default=args.base_url,
        ) or None

    args.goal = _prompt_text(
        "What do you want ctx to help you build or maintain?",
        default=args.goal,
    )
    if args.model_mode == "custom":
        _prompt_harness_requirements(args)
        args.validate_model = _prompt_yes_no(
            "Validate the model with one tiny provider call now?",
            default=args.validate_model,
        )


def run_model_onboarding(args: argparse.Namespace, claude: Path) -> int:
    """Record model choice and print harness recommendations."""
    mode = args.model_mode
    if mode is None or mode == "skip":
        print("  [skip] model onboarding (run ctx-init --wizard to configure)")
        return 0
    if mode not in {"claude-code", "custom"}:
        print(f"  [warn] unknown model mode: {mode}", file=sys.stderr)
        return 1

    goal = args.goal or ""
    if mode == "custom" and not args.model:
        print("  [warn] --model-mode custom requires --model", file=sys.stderr)
        return 1

    provider = args.model_provider or (
        _model_provider_prefix(args.model) if args.model else None
    )
    api_key_env = _resolve_api_key_env(args.api_key_env, args.model, provider)
    harness_requirements = (
        _harness_requirements_from_args(args) if mode == "custom" else {}
    )
    profile: dict[str, Any] = {
        "mode": mode,
        "provider": provider,
        "model": args.model,
        "api_key_env": api_key_env,
        "base_url": args.base_url,
        "goal": goal,
        "knowledge_mode": getattr(args, "knowledge_mode", "shipped"),
    }
    if harness_requirements:
        profile["harness_requirements"] = harness_requirements
    written = write_model_profile(claude, profile, force=args.force)
    if written:
        print(f"  [ok] wrote {written.name}")
    else:
        print(f"  [skip] {_MODEL_PROFILE_NAME} already present (use --force)")

    rc = 0
    if mode == "custom" and api_key_env and not os.environ.get(api_key_env):
        print(f"  [warn] set {api_key_env} before running ctx with this model")
    if mode == "custom" and args.validate_model:
        rc = validate_model_connection(
            model=args.model,
            api_key_env=api_key_env,
            base_url=args.base_url,
        )
        if rc == 0:
            print("  [ok] model connection validated")

    if mode != "custom":
        return rc

    recommendation_query = " ".join(
        part for part in [
            goal,
            _harness_requirements_text(harness_requirements),
            provider or "",
            args.model or "",
            "harness",
        ]
        if part
    )
    harnesses = recommend_harnesses(
        recommendation_query,
        model_provider=provider,
        model=args.model,
    )
    if harnesses:
        print("  [ok] recommended harnesses:")
        for row in harnesses:
            fit = float(row.get("fit_score") or row.get("normalized_score") or 0.0)
            name = row.get("name")
            print(f"       - {name} (fit {fit:.2f})")
            print(f"         install: ctx-harness-install {name} --dry-run")
    elif goal or mode == "custom":
        print("  [info] no harness recommendations matched yet")
        print("       build plan: " + _harness_plan_command(
            goal=goal,
            model_provider=provider,
            model=args.model,
            harness_requirements=harness_requirements,
        ))
    return rc


# ─── CLI ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ctx-init",
        description="Bootstrap ~/.claude/ for ctx (claude-ctx).",
    )
    parser.add_argument(
        "--hooks", action="store_true",
        help="Inject PostToolUse + Stop hooks into ~/.claude/settings.json",
    )
    parser.add_argument(
        "--graph", action="store_true",
        help=(
            "Install the pre-built knowledge graph after setup. Uses local "
            "graph/wiki-graph.tar.gz when present; otherwise downloads the "
            "matching release asset."
        ),
    )
    parser.add_argument(
        "--graph-url",
        help="Override the pre-built wiki-graph.tar.gz download URL",
    )
    parser.add_argument(
        "--graph-sha256",
        help="Expected SHA-256 digest for a custom --graph-url archive",
    )
    parser.add_argument(
        "--allow-unverified-graph-url",
        action="store_true",
        help=(
            "Allow a custom --graph-url without checksum verification. "
            "Use only for local/private trusted mirrors."
        ),
    )
    parser.add_argument(
        "--graph-install-mode",
        choices=_GRAPH_INSTALL_MODES,
        default="runtime",
        help=(
            "Graph install shape: runtime extracts graph/catalog artifacts "
            "needed for recommendations; full expands every wiki markdown file."
        ),
    )
    parser.add_argument(
        "--knowledge-mode",
        choices=("shipped", "local", "enriched", "skip"),
        help=(
            "Knowledge source policy: shipped ctx graph/wiki, local/private "
            "only, shipped plus user enrichment, or skip recording a policy."
        ),
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing config files if present",
    )
    parser.add_argument(
        "--wizard",
        action="store_true",
        help=(
            "Prompt for hooks, graph install, model profile, and harness "
            "recommendation setup. Plain ctx-init does this automatically "
            "when run in an interactive terminal."
        ),
    )
    parser.add_argument(
        "--model-mode",
        choices=("claude-code", "custom", "skip"),
        help="Record whether this install uses Claude Code or a custom model",
    )
    parser.add_argument("--model-provider", help="Custom model provider prefix")
    parser.add_argument("--model", help="Custom model slug, e.g. openai/gpt-5.5")
    parser.add_argument(
        "--api-key-env",
        help="Environment variable that stores the custom provider API key",
    )
    parser.add_argument("--base-url", help="Custom provider base URL")
    parser.add_argument(
        "--goal",
        help="What the user wants to build; used for harness recommendations",
    )
    parser.add_argument(
        "--harness-runtime",
        help="Runtime/OS target for custom-model harness recommendations",
    )
    parser.add_argument(
        "--harness-autonomy",
        help="Desired harness autonomy level, e.g. supervised or autonomous",
    )
    parser.add_argument(
        "--harness-tools",
        help="Allowed harness tools/access, e.g. filesystem shell browser",
    )
    parser.add_argument(
        "--harness-verify",
        help="Verification commands or gates the harness should run",
    )
    parser.add_argument(
        "--harness-privacy",
        help="Privacy, network, or data-access constraints for the harness",
    )
    parser.add_argument(
        "--harness-attach-mode",
        help="Preferred ctx attachment mode, e.g. mcp, cli, or api",
    )
    parser.add_argument(
        "--validate-model",
        action="store_true",
        help="Make one tiny provider call to validate the custom model connection",
    )
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    args = parser.parse_args(raw_argv)
    if _should_run_wizard(args, raw_argv):
        run_wizard(args)
    if args.knowledge_mode == "local" and args.graph:
        print(
            "  [warn] --knowledge-mode local cannot be combined with --graph; "
            "use --knowledge-mode enriched to install shipped ctx knowledge "
            "and add your own.",
            file=sys.stderr,
        )
        return 1

    claude = _claude_dir()
    print(f"ctx-init: setting up {claude}")

    created = ensure_directories(claude)
    if created:
        print(f"  [ok] created {len(created)} subdirectories")
    else:
        print("  [ok] all standard subdirectories exist")

    seeded_config = seed_user_config(claude, force=args.force)
    if seeded_config:
        print(f"  [ok] wrote {seeded_config.name}")
    else:
        print("  [skip] skill-system-config.json already present (use --force to overwrite)")

    if args.knowledge_mode and args.knowledge_mode != "skip":
        try:
            written = write_knowledge_config(claude, args.knowledge_mode)
        except ValueError as exc:
            print(f"  [warn] {exc}", file=sys.stderr)
            return 1
        if written:
            print(f"  [ok] recorded knowledge mode: {args.knowledge_mode}")

    toolbox_seed = seed_toolboxes(force=args.force)
    if isinstance(toolbox_seed, ToolboxSeedResult):
        toolbox_rc = toolbox_seed.returncode
        toolbox_already_present = toolbox_seed.already_present
    else:
        toolbox_rc = int(toolbox_seed)
        toolbox_already_present = False
    final_rc = 0
    if toolbox_already_present:
        print("  [skip] starter toolboxes already present (use --force to overwrite)")
    elif toolbox_rc == 0:
        print("  [ok] toolboxes seeded")
    else:
        print(f"  [warn] toolbox init returned {toolbox_rc} — inspect above", file=sys.stderr)

    if args.hooks:
        rc = install_hooks(ctx_src_dir=_resolve_ctx_src_dir())
        if rc == 0:
            print("  [ok] PostToolUse + Stop hooks injected")
        else:
            print(f"  [warn] hook injection returned {rc}", file=sys.stderr)
            final_rc = rc
    else:
        print("  [skip] hook injection (pass --hooks to enable)")

    if args.graph:
        rc = build_graph(
            claude,
            force=args.force,
            graph_url=args.graph_url,
            graph_sha256=args.graph_sha256,
            allow_unverified_graph_url=args.allow_unverified_graph_url,
            install_mode=args.graph_install_mode,
        )
        if rc == 0:
            print("  [ok] knowledge graph installed")
            if args.graph_install_mode == "runtime":
                print(
                    "  [info] runtime graph install only; pass "
                    "--graph-install-mode full to expand the full wiki"
                )
        else:
            print(f"  [warn] graph install returned {rc}", file=sys.stderr)
            if final_rc == 0:
                final_rc = rc
    else:
        print("  [skip] graph install (pass --graph to install)")

    rc = run_model_onboarding(args, claude)
    if rc != 0 and final_rc == 0:
        final_rc = rc

    print("\nctx-init: done. Next steps:")
    print("  - ctx-toolbox list                 # see starter toolboxes")
    print("  - ctx-skill-health dashboard       # baseline health scan")
    print("  - ctx-monitor serve                # local dashboard at :8765")
    if not args.hooks:
        print("  - ctx-init --hooks                 # wire live observation")
    if not args.graph and args.knowledge_mode != "local":
        print("  - ctx-init --graph                 # install knowledge graph")
    return final_rc


if __name__ == "__main__":
    sys.exit(main())
