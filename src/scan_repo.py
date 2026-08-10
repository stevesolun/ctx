#!/usr/bin/env python3
"""
scan_repo.py -- Analyze a repository and produce a stack profile JSON.

Usage:
    python scan_repo.py --repo /path/to/repo --output .ctx/stack-profile.json

The scanner reads directory structure and config files to detect:
- Languages, frameworks, infrastructure, data stores, testing, AI tooling,
  build systems, and documentation tools.

It avoids reading source files unless needed to disambiguate (e.g., checking
imports in an entry file to distinguish React from Preact).
"""

import argparse
import fnmatch
import json
import os
import re
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CTX_CFG: Any | None
try:
    from ctx_config import cfg as _ctx_cfg
except Exception:  # pragma: no cover - config load failures fall back below
    _CTX_CFG = None
else:
    _CTX_CFG = _ctx_cfg

# Directories to always skip
SKIP_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "env",
    ".env",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".cache",
    ".tox",
    "target",
    "vendor",
    ".terraform",
    ".serverless",
    "coverage",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "egg-info",
}

# Max depth for directory scanning
MAX_DEPTH = 3


def _default_output_path() -> Path:
    """Return the OS-portable default stack profile path."""
    configured = getattr(_CTX_CFG, "stack_profile_tmp", None)
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "skill-stack-profile.json"


def scan_directory(repo_path: str, max_depth: int = MAX_DEPTH) -> dict:
    """Walk the repo and collect file/dir signals without reading contents."""
    signals: dict[str, list[Any]] = {
        "files": [],  # (relative_path, extension)
        "dirs": [],  # relative directory paths
        "config_files": [],  # config files found (will be read)
        "unreadable_dirs": [],  # directories the walk could not enter
    }

    repo = Path(repo_path).resolve()
    config_names = {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "Pipfile",
        "Cargo.toml",
        "go.mod",
        "Gemfile",
        "composer.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "tsconfig.json",
        "next.config.js",
        "next.config.mjs",
        "next.config.ts",
        "nuxt.config.ts",
        "nuxt.config.js",
        "angular.json",
        "svelte.config.js",
        "vite.config.ts",
        "vite.config.js",
        "webpack.config.js",
        "tailwind.config.js",
        "tailwind.config.ts",
        "jest.config.js",
        "jest.config.ts",
        "vitest.config.ts",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
        "mkdocs.yml",
        ".gitlab-ci.yml",
        "Jenkinsfile",
        "turbo.json",
        "nx.json",
        "lerna.json",
        "pnpm-workspace.yaml",
        "fly.toml",
        "vercel.json",
        "netlify.toml",
        "render.yaml",
        "serverless.yml",
        "cdk.json",
        "Pulumi.yaml",
        "mcp.json",
        "CLAUDE.md",
        ".cursorrules",
        ".windsurfrules",
        "alembic.ini",
        "dbt_project.yml",
        "openapi.yaml",
        "openapi.json",
        "swagger.yaml",
        "swagger.json",
        ".coveragerc",
        "playwright.config.ts",
        "cypress.config.ts",
        "poetry.lock",
        "yarn.lock",
        "pnpm-lock.yaml",
        "package-lock.json",
        "Cargo.lock",
        "Gemfile.lock",
        "go.sum",
        "composer.lock",
    }

    # Hidden dirs that DO carry signal — must be walked. ``.github``
    # holds GitHub Actions workflows, ``.devcontainer`` holds container
    # configs, ``.vscode``/``.idea`` hold IDE integration. Without this
    # allowlist the default ``startswith(".")`` filter drops every one
    # of them and the corresponding stack detection silently no-ops.
    SIGNAL_HIDDEN_DIRS = {".github", ".devcontainer", ".vscode", ".idea"}

    def _on_walk_error(exc: OSError) -> None:
        # os.walk discards scandir errors by default, so an unreadable subtree
        # produced a profile that described nothing and said nothing about why.
        # Record it and say so, rather than let "could not look" be reported as
        # "there is nothing there".
        target = getattr(exc, "filename", None) or str(exc)
        try:
            target = os.path.relpath(target, repo)
        except (OSError, ValueError):  # pragma: no cover - defensive
            pass
        signals["unreadable_dirs"].append(target)
        print(f"Warning: could not read {target}: {exc.strerror or exc}", file=sys.stderr)

    for dirpath, dirnames, filenames in os.walk(repo, onerror=_on_walk_error):
        rel_dir = os.path.relpath(dirpath, repo)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1

        # Skip ignored dirs. Hidden dirs are dropped EXCEPT the
        # allowlisted signal-bearing ones (.github etc.).
        # Sorted so the walk order is a property of the repository rather than
        # of the filesystem: os.scandir order differs between APFS and ext4, and
        # it leaks into evidence lists and language tie-breaks downstream.
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SKIP_DIRS and (not d.startswith(".") or d in SIGNAL_HIDDEN_DIRS)
        )

        if depth > max_depth:
            dirnames.clear()
            continue

        signals["dirs"].append(rel_dir)

        for fname in sorted(filenames):
            ext = Path(fname).suffix.lower()
            rel_path = os.path.join(rel_dir, fname) if rel_dir != "." else fname
            signals["files"].append((rel_path, ext))

            if fname in config_names or fname.endswith(".tf"):
                signals["config_files"].append(os.path.join(dirpath, fname))

    return signals


def read_json_safe(path: str) -> dict | None:
    """Read a JSON file, return None on failure."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as exc:
        print(f"Warning: failed to read JSON file {path}: {exc}", file=sys.stderr)
        return None


def read_toml_deps(path: str, *, include_optional: bool = True) -> list[str]:
    """Extract dependency names from pyproject.toml.

    Covers PEP 621 ``[project].dependencies`` / ``optional-dependencies`` and
    Poetry-style ``[tool.poetry].dependencies`` / ``dev-dependencies``. Version
    specifiers and extras are stripped via PEP 508 splitting. Callers can set
    ``include_optional=False`` when deciding the primary project type; optional
    extras should remain searchable signals but should not make a docs/tooling
    repo look like an ML project just because it offers an embeddings extra.
    """
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"Warning: failed to read TOML deps from {path}: {exc}", file=sys.stderr)
        return []

    raw: list[str] = []

    project = data.get("project", {})
    if isinstance(project, dict):
        deps = project.get("dependencies", [])
        if isinstance(deps, list):
            raw.extend(d for d in deps if isinstance(d, str))
        if include_optional:
            opt = project.get("optional-dependencies", {})
            if isinstance(opt, dict):
                for group in opt.values():
                    if isinstance(group, list):
                        raw.extend(d for d in group if isinstance(d, str))

    poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
    if isinstance(poetry, dict):
        keys = ("dependencies", "dev-dependencies") if include_optional else ("dependencies",)
        for key in keys:
            deps = poetry.get(key, {})
            if isinstance(deps, dict):
                raw.extend(k for k in deps.keys() if isinstance(k, str) and k.lower() != "python")

    # PEP 508: strip ``[extras]``, version specifiers, and environment markers.
    names: list[str] = []
    for spec in raw:
        name = re.split(r"[\s\[><=!~;,]", spec, 1)[0].strip()
        if name:
            names.append(name.lower())
    return names


def read_requirements(path: str) -> list[str]:
    """Extract package names from requirements.txt."""
    try:
        with open(path) as f:
            lines = f.readlines()
        deps = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                name = re.split(r"[>=<!\[;]", line)[0].strip()
                if name:
                    deps.append(name.lower())
        return deps
    except Exception as exc:
        print(f"Warning: failed to read requirements from {path}: {exc}", file=sys.stderr)
        return []


#: Manifests that mark a directory as its own package. Used to spot workspace
#: members, which is how a non-JavaScript monorepo announces itself.
_PACKAGE_MANIFESTS = frozenset({"package.json", "pyproject.toml", "Cargo.toml", "go.mod"})

#: Conventional parents of workspace members. Restricted deliberately: "two
#: nested manifests anywhere" would call any repository with a fixture package
#: a monorepo.
_WORKSPACE_PARENTS = frozenset({"packages", "apps", "libs", "services", "crates", "modules"})

#: Manifests are small; refusing to slurp an arbitrarily large one keeps the
#: scan bounded.
_MAX_MANIFEST_CHARS = 512 * 1024


def _declared_workspace_globs(root_pkg_json: dict | None) -> list[str]:
    """The workspace globs a root ``package.json`` declares, if any.

    npm/yarn accept either a list or ``{"packages": [...]}``. A repository that
    names its members ``frontend/*`` is as much a monorepo as one that uses
    ``packages/*``, and reporting "0 workspace packages" for it states a count
    that the manifest on disk contradicts.
    """

    if not isinstance(root_pkg_json, dict):
        return []
    declared = root_pkg_json.get("workspaces")
    if isinstance(declared, dict):
        declared = declared.get("packages")
    if not isinstance(declared, list):
        return []
    return [item.rstrip("/") for item in declared if isinstance(item, str) and item]


def _root_file_declares(repo_abspath: str, name: str, pattern: str) -> bool:
    """Whether a root manifest contains ``pattern``. Missing file reads False."""

    path = os.path.join(repo_abspath, name)
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return re.search(pattern, handle.read(_MAX_MANIFEST_CHARS), re.MULTILINE) is not None
    except OSError:
        return False


def detect_stack(repo_path: str, signals: dict) -> dict:
    """Analyze signals and produce a stack profile."""
    profile: dict[str, Any] = {
        "repo_path": os.path.abspath(repo_path),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "languages": [],
        "frameworks": [],
        "infrastructure": [],
        "data_stores": [],
        "testing": [],
        "ai_tooling": [],
        "build_system": [],
        "docs": [],
        "project_type": "unknown",
        "monorepo": False,
        "workspace_packages": [],
        "custom_signals": {},
    }

    # Count extensions
    ext_counts: dict[str, int] = {}
    for _, ext in signals["files"]:
        if ext:
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

    config_basenames = {os.path.basename(p) for p in signals["config_files"]}
    dir_basenames = {os.path.basename(d) for d in signals["dirs"]}

    # Collect all deps from Python and JS configs
    all_py_deps: list[str] = []
    core_py_deps: list[str] = []
    all_js_deps: list[str] = []
    root_pkg_json: dict | None = None
    nested_manifest_dirs: set[str] = set()
    # realpath, not abspath: scan_directory resolves the root, so on macOS a
    # /tmp path would otherwise never compare equal to its /private/tmp form.
    repo_abspath = os.path.realpath(repo_path)

    for cfg in signals["config_files"]:
        base = os.path.basename(cfg)
        cfg_dir = os.path.dirname(os.path.realpath(cfg))
        is_root_manifest = cfg_dir == repo_abspath
        if base in _PACKAGE_MANIFESTS and not is_root_manifest:
            nested_manifest_dirs.add(os.path.relpath(cfg_dir, repo_abspath))
        if base == "pyproject.toml":
            all_py_deps.extend(read_toml_deps(cfg))
            core_py_deps.extend(read_toml_deps(cfg, include_optional=False))
        elif base == "requirements.txt":
            deps = read_requirements(cfg)
            all_py_deps.extend(deps)
            core_py_deps.extend(deps)
        elif base == "Pipfile":
            deps = read_requirements(cfg)
            all_py_deps.extend(deps)
            core_py_deps.extend(deps)
        elif base == "package.json":
            data = read_json_safe(cfg)
            # A manifest can be valid JSON and still not be an object, and its
            # dependency sections can be null or a list of numbers. Guarding
            # only against unparseable JSON let those shapes escape as an
            # unhandled traceback with an empty stdout.
            if isinstance(data, dict):
                # Only the ROOT manifest describes the workspace. os.walk is
                # top-down, so keeping the last one seen meant a nested package
                # always overwrote the root one — and `monorepo` came out false
                # for exactly the layouts that define a workspace monorepo.
                if is_root_manifest:
                    root_pkg_json = data
                for section in ("dependencies", "devDependencies", "peerDependencies"):
                    names = data.get(section)
                    if isinstance(names, (dict, list)):
                        all_js_deps.extend(k.lower() for k in names if isinstance(k, str))

    py_dep_set = set(all_py_deps)
    py_core_dep_set = set(core_py_deps)
    js_dep_set = set(all_js_deps)

    # --- LANGUAGES ---
    lang_map = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".kt": "kotlin",
        ".rb": "ruby",
        ".swift": "swift",
        ".cs": "csharp",
        ".php": "php",
    }
    detected_langs: dict[str, int] = {}
    for ext, count in ext_counts.items():
        if ext in lang_map:
            lang = lang_map[ext]
            detected_langs[lang] = detected_langs.get(lang, 0) + count

    # Name is the tie-break so that two languages with the same file count keep
    # a content-derived order instead of inheriting directory-walk order.
    for lang, count in sorted(detected_langs.items(), key=lambda x: (-x[1], x[0])):
        evidence = [f"{count} files with matching extensions"]
        conf = 0.8
        # Boost for lock files
        lock_signals = {
            "python": ["poetry.lock", "Pipfile", "pyproject.toml", "requirements.txt"],
            "typescript": ["tsconfig.json"],
            "javascript": ["package.json"],
            "rust": ["Cargo.toml", "Cargo.lock"],
            "go": ["go.mod", "go.sum"],
            "ruby": ["Gemfile", "Gemfile.lock"],
        }
        for lf in lock_signals.get(lang, []):
            if lf in config_basenames:
                evidence.append(lf)
                conf = min(conf + 0.1, 1.0)

        profile["languages"].append(
            {"name": lang, "confidence": round(conf, 2), "evidence": evidence}
        )

    # --- FRAMEWORKS (check deps) ---
    fw_checks = [
        # (dep_set, dep_name, stack_id, category, confidence)
        (py_dep_set, "fastapi", "fastapi", "web", 0.99),
        (py_dep_set, "django", "django", "web", 0.99),
        (py_dep_set, "flask", "flask", "web", 0.95),
        (py_dep_set, "torch", "pytorch", "ml", 0.95),
        (py_dep_set, "pytorch", "pytorch", "ml", 0.95),
        (py_dep_set, "tensorflow", "tensorflow", "ml", 0.95),
        (py_dep_set, "transformers", "huggingface", "ml", 0.9),
        (py_dep_set, "langchain", "langchain", "ai", 0.95),
        (py_dep_set, "langchain-core", "langchain", "ai", 0.95),
        (py_dep_set, "llama-index", "llamaindex", "ai", 0.95),
        (py_dep_set, "crewai", "crewai", "ai", 0.95),
        (py_dep_set, "dspy-ai", "dspy", "ai", 0.95),
        (py_dep_set, "openai", "openai-sdk", "ai", 0.8),
        (py_dep_set, "anthropic", "anthropic-sdk", "ai", 0.8),
        # Fintech / payments
        (py_dep_set, "stripe", "stripe", "payments", 0.99),
        (py_dep_set, "paypalrestsdk", "paypal", "payments", 0.95),
        (py_dep_set, "paypal-sdk", "paypal", "payments", 0.95),
        (py_dep_set, "plaid-python", "plaid", "payments", 0.95),
        (js_dep_set, "stripe", "stripe", "payments", 0.99),
        (js_dep_set, "@stripe/stripe-js", "stripe", "payments", 0.99),
        # Validation / schemas (often paired with FastAPI)
        (py_dep_set, "pydantic", "pydantic", "web", 0.85),
        (js_dep_set, "zod", "zod", "web", 0.85),
        (js_dep_set, "yup", "yup", "web", 0.8),
        (js_dep_set, "react", "react", "web", 0.9),
        (js_dep_set, "vue", "vue", "web", 0.9),
        (js_dep_set, "next", "nextjs", "web", 0.95),
        (js_dep_set, "@angular/core", "angular", "web", 0.95),
        (js_dep_set, "svelte", "svelte", "web", 0.95),
        (js_dep_set, "express", "express", "web", 0.95),
        (js_dep_set, "fastify", "fastify", "web", 0.95),
        (js_dep_set, "@nestjs/core", "nestjs", "web", 0.95),
    ]
    for dep_set, dep_name, stack_id, category, conf in fw_checks:
        if dep_name in dep_set:
            profile["frameworks"].append(
                {
                    "name": stack_id,
                    "category": category,
                    "confidence": conf,
                    "evidence": [f"{dep_name} in dependencies"],
                }
            )

    # Config-based framework detection
    config_fw = {
        "next.config.js": ("nextjs", "web", 1.0),
        "next.config.mjs": ("nextjs", "web", 1.0),
        "next.config.ts": ("nextjs", "web", 1.0),
        "nuxt.config.ts": ("nuxt", "web", 1.0),
        "nuxt.config.js": ("nuxt", "web", 1.0),
        "angular.json": ("angular", "web", 1.0),
        "svelte.config.js": ("svelte", "web", 1.0),
    }
    for cfg_name, (stack_id, cat, conf) in config_fw.items():
        if cfg_name in config_basenames:
            # Avoid duplicate if already detected via deps
            existing = [f for f in profile["frameworks"] if f["name"] == stack_id]
            if existing:
                existing[0]["confidence"] = max(existing[0]["confidence"], conf)
                existing[0]["evidence"].append(cfg_name)
            else:
                profile["frameworks"].append(
                    {"name": stack_id, "category": cat, "confidence": conf, "evidence": [cfg_name]}
                )

    # --- INFRASTRUCTURE ---
    infra_map = {
        "Dockerfile": ("docker", 1.0),
        "docker-compose.yml": ("docker-compose", 1.0),
        "docker-compose.yaml": ("docker-compose", 1.0),
        ".gitlab-ci.yml": ("gitlab-ci", 1.0),
        "Jenkinsfile": ("jenkins", 1.0),
        "fly.toml": ("fly-io", 1.0),
        "vercel.json": ("vercel", 1.0),
        "netlify.toml": ("netlify", 1.0),
        "render.yaml": ("render", 1.0),
        "serverless.yml": ("serverless", 1.0),
        "cdk.json": ("aws-cdk", 1.0),
        "Pulumi.yaml": ("pulumi", 1.0),
        "turbo.json": ("turborepo", 1.0),
        "nx.json": ("nx", 1.0),
    }
    for cfg_name, (stack_id, conf) in infra_map.items():
        if cfg_name in config_basenames:
            profile["infrastructure"].append(
                {"name": stack_id, "confidence": conf, "evidence": [cfg_name]}
            )

    # GitHub Actions
    if ".github" in dir_basenames:
        gh_wf = [d for d in signals["dirs"] if "workflows" in d and ".github" in d]
        if gh_wf:
            profile["infrastructure"].append(
                {"name": "github-actions", "confidence": 1.0, "evidence": [".github/workflows/"]}
            )

    # Terraform
    tf_files = [f for f, ext in signals["files"] if ext == ".tf"]
    if tf_files:
        profile["infrastructure"].append(
            {"name": "terraform", "confidence": 1.0, "evidence": [f"{len(tf_files)} .tf files"]}
        )

    # K8s
    k8s_dirs = {"k8s", "kubernetes", "helm", "charts"}
    # Sort before formatting: a set's repr follows per-process hash order, so
    # interpolating the set directly made the profile differ byte-for-byte
    # between runs and broke the reproducibility guarantee the profile makes.
    matched_k8s_dirs = sorted(k8s_dirs & dir_basenames)
    if matched_k8s_dirs:
        profile["infrastructure"].append(
            {
                "name": "kubernetes",
                "confidence": 0.95,
                "evidence": [f"directory: {name}" for name in matched_k8s_dirs],
            }
        )

    # --- DATA STORES ---
    data_checks = [
        (py_dep_set, "sqlalchemy", "sqlalchemy", 0.95),
        (py_dep_set, "alembic", "sqlalchemy", 0.95),
        (py_dep_set, "redis", "redis", 0.85),
        (py_dep_set, "celery", "celery", 0.95),
        (py_dep_set, "kafka-python", "kafka", 0.9),
        # Postgres explicit — the psycopg family installs as 'psycopg2',
        # 'psycopg2-binary', or 'psycopg' (3.x). Any of them → postgres.
        (py_dep_set, "psycopg2", "postgres", 0.95),
        (py_dep_set, "psycopg2-binary", "postgres", 0.95),
        (py_dep_set, "psycopg", "postgres", 0.95),
        (py_dep_set, "asyncpg", "postgres", 0.95),
        # MongoDB
        (py_dep_set, "pymongo", "mongodb", 0.95),
        (py_dep_set, "motor", "mongodb", 0.95),
        (js_dep_set, "mongodb", "mongodb", 0.95),
        (js_dep_set, "mongoose", "mongodb", 0.95),
        # Postgres from JS side
        (js_dep_set, "pg", "postgres", 0.9),
        (js_dep_set, "postgres", "postgres", 0.9),
        (js_dep_set, "prisma", "prisma", 0.95),
        (js_dep_set, "@prisma/client", "prisma", 0.95),
        (js_dep_set, "typeorm", "typeorm", 0.95),
        (js_dep_set, "drizzle-orm", "drizzle", 0.95),
        (js_dep_set, "sequelize", "sequelize", 0.95),
        (js_dep_set, "ioredis", "redis", 0.85),
        (js_dep_set, "redis", "redis", 0.85),
    ]
    for dep_set, dep_name, stack_id, conf in data_checks:
        if dep_name in dep_set:
            existing = [d for d in profile["data_stores"] if d["name"] == stack_id]
            if not existing:
                profile["data_stores"].append(
                    {
                        "name": stack_id,
                        "confidence": conf,
                        "evidence": [f"{dep_name} in dependencies"],
                    }
                )

    if "alembic" in dir_basenames or "alembic.ini" in config_basenames:
        existing = [d for d in profile["data_stores"] if d["name"] == "sqlalchemy"]
        if existing:
            existing[0]["evidence"].append("alembic/ directory")
        else:
            profile["data_stores"].append(
                {"name": "sqlalchemy", "confidence": 0.95, "evidence": ["alembic/ directory"]}
            )

    if "dbt_project.yml" in config_basenames:
        profile["data_stores"].append(
            {"name": "dbt", "confidence": 1.0, "evidence": ["dbt_project.yml"]}
        )

    # --- TESTING ---
    test_map = {
        "pytest.ini": ("pytest", 1.0),
        "conftest.py": ("pytest", 0.95),
        "jest.config.js": ("jest", 1.0),
        "jest.config.ts": ("jest", 1.0),
        "vitest.config.ts": ("vitest", 1.0),
        "vitest.config.js": ("vitest", 1.0),
        "playwright.config.ts": ("playwright", 1.0),
        "cypress.config.ts": ("cypress", 1.0),
    }
    for cfg_name, (stack_id, conf) in test_map.items():
        if cfg_name in config_basenames:
            existing = [t for t in profile["testing"] if t["name"] == stack_id]
            if not existing:
                profile["testing"].append(
                    {"name": stack_id, "confidence": conf, "evidence": [cfg_name]}
                )

    # Dev-dependency based test-framework detection — catches pytest /
    # jest / vitest / playwright / cypress declared in pyproject
    # [tool.pytest.ini_options] / [dependency-groups] / package.json
    # devDependencies without a dedicated config file on disk.
    dev_test_checks = [
        (py_dep_set, "pytest", "pytest", 0.9),
        (py_dep_set, "pytest-asyncio", "pytest", 0.9),
        (js_dep_set, "jest", "jest", 0.9),
        (js_dep_set, "vitest", "vitest", 0.9),
        (js_dep_set, "@playwright/test", "playwright", 0.9),
        (js_dep_set, "cypress", "cypress", 0.9),
    ]
    for dep_set, dep_name, stack_id, conf in dev_test_checks:
        if dep_name in dep_set:
            existing = [t for t in profile["testing"] if t["name"] == stack_id]
            if not existing:
                profile["testing"].append(
                    {
                        "name": stack_id,
                        "confidence": conf,
                        "evidence": [f"{dep_name} in dependencies"],
                    }
                )

    # --- AI TOOLING ---
    if "mcp.json" in config_basenames or ".mcp" in dir_basenames:
        profile["ai_tooling"].append(
            {"name": "mcp", "confidence": 1.0, "evidence": ["mcp.json or .mcp/ directory"]}
        )
    if "CLAUDE.md" in config_basenames:
        profile["ai_tooling"].append(
            {"name": "claude-code", "confidence": 0.95, "evidence": ["CLAUDE.md"]}
        )

    # --- BUILD SYSTEM ---
    build_map = {
        "vite.config.ts": "vite",
        "vite.config.js": "vite",
        "webpack.config.js": "webpack",
    }
    for cfg_name, stack_id in build_map.items():
        if cfg_name in config_basenames:
            profile["build_system"].append(
                {"name": stack_id, "confidence": 1.0, "evidence": [cfg_name]}
            )

    # --- DOCS ---
    doc_map = {
        "mkdocs.yml": "mkdocs",
        "docusaurus.config.js": "docusaurus",
        "docusaurus.config.ts": "docusaurus",
    }
    for cfg_name, stack_id in doc_map.items():
        if cfg_name in config_basenames:
            profile["docs"].append({"name": stack_id, "confidence": 1.0, "evidence": [cfg_name]})

    openapi_files = [
        f
        for f, _ in signals["files"]
        if os.path.basename(f) in ("openapi.yaml", "openapi.json", "swagger.yaml", "swagger.json")
    ]
    if openapi_files:
        # Sorted before slicing: which three paths are shown must not depend on
        # the order the filesystem handed them back.
        profile["docs"].append(
            {"name": "openapi", "confidence": 0.95, "evidence": sorted(openapi_files)[:3]}
        )

    # --- MONOREPO ---
    # Detection used to be JavaScript-only, so a uv/Cargo/Go workspace was told
    # "single-package layout, so change scope is unambiguous" — an affirmative
    # claim about the one layout the check exists to warn about.
    monorepo_signals = {"turbo.json", "nx.json", "lerna.json", "pnpm-workspace.yaml"}
    declared_globs = _declared_workspace_globs(root_pkg_json)
    workspace_members = sorted(
        member
        for member in nested_manifest_dirs
        if member.split(os.sep)[0] in _WORKSPACE_PARENTS
        # A declared glob is evidence, not a convention: when the root manifest
        # says where the members live, the conventional-parent restriction (a
        # guard for the *inferred* case below) would drop real members.
        or any(fnmatch.fnmatch(member, pattern) for pattern in declared_globs)
    )
    matched_signals = sorted(monorepo_signals & config_basenames)
    if matched_signals:
        profile["monorepo"] = True
    elif root_pkg_json and "workspaces" in root_pkg_json:
        profile["monorepo"] = True
    elif _root_file_declares(repo_abspath, "Cargo.toml", r"^\s*\[workspace\]"):
        profile["monorepo"] = True
    elif os.path.isfile(os.path.join(repo_abspath, "go.work")):
        profile["monorepo"] = True
    elif _root_file_declares(
        repo_abspath, "pyproject.toml", r"^\s*\[tool\.(?:uv|rye)\.workspace\]"
    ):
        profile["monorepo"] = True
    elif len(workspace_members) >= 2:
        profile["monorepo"] = True

    if profile["monorepo"]:
        profile["workspace_packages"] = workspace_members

    # --- PROJECT TYPE ---
    fw_names = {f["name"] for f in profile["frameworks"]}
    core_ml_deps = py_core_dep_set & {"torch", "pytorch", "tensorflow", "transformers"}
    core_ai_deps = py_core_dep_set & {
        "langchain",
        "langchain-core",
        "llama-index",
        "crewai",
        "dspy-ai",
    }
    if fw_names & {"react", "vue", "angular", "svelte", "nextjs", "nuxt"}:
        if fw_names & {"fastapi", "django", "flask", "express", "nestjs"}:
            profile["project_type"] = "fullstack"
        else:
            profile["project_type"] = "frontend"
    elif fw_names & {"fastapi", "django", "flask", "express", "nestjs", "gin", "actix"}:
        profile["project_type"] = "api-service"
    elif (fw_names & {"pytorch", "tensorflow", "huggingface"}) and core_ml_deps:
        profile["project_type"] = "ml-project"
    elif (fw_names & {"langchain", "llamaindex", "crewai"}) and core_ai_deps:
        profile["project_type"] = "ai-agent"
    elif profile["infrastructure"]:
        profile["project_type"] = "infrastructure"

    return profile


def _profile_recommendation_query(profile: dict) -> str:
    parts: list[str] = []
    project_type = str(profile.get("project_type") or "").strip()
    if project_type and project_type != "unknown":
        parts.append(project_type)
    for bucket in (
        "languages",
        "frameworks",
        "infrastructure",
        "data_stores",
        "testing",
        "ai_tooling",
        "build_system",
        "docs",
    ):
        for item in profile.get(bucket, []):
            if isinstance(item, dict) and item.get("name"):
                parts.append(str(item["name"]))
    return " ".join(parts)


def _shared_recommendations(profile: dict) -> list[dict[str, Any]] | None:
    """Return shared recommender rows, or None when no graph is available."""
    from ctx import recommend_bundle  # noqa: PLC0415
    from ctx_config import cfg  # noqa: PLC0415

    graph_path = Path(cfg.wiki_dir) / "graphify-out" / "graph.json"
    if not graph_path.is_file() and not (graph_path.parent / "packs").is_dir():
        return None
    query = _profile_recommendation_query(profile)
    if not query:
        return []
    languages = [
        str(item["name"])
        for item in profile.get("languages", [])
        if isinstance(item, dict) and item.get("name")
    ]
    testing = [
        str(item["name"])
        for item in profile.get("testing", [])
        if isinstance(item, dict) and item.get("name")
    ]
    query_specs: list[tuple[str, str | None]] = [(query, languages[0] if languages else None)]
    for detected_language in languages:
        testing_query = " ".join((detected_language, *(testing or ["testing"])))
        query_specs.extend(
            [
                (testing_query, detected_language),
                (f"ctx {testing_query}", detected_language),
            ]
        )
    if languages:
        query_specs.append((f"{languages[0]} reviewer", languages[0]))

    top_k = max(1, min(int(cfg.recommendation_top_k), 5))
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for focused_query, language_hint in dict.fromkeys(query_specs):
        rows = recommend_bundle(
            focused_query,
            top_k=top_k,
            local_code_task=True,
            no_api_keys=True,
            language=language_hint,
        )
        for row in rows:
            if row.get("installable") is not True or row.get("load_status") != "local-wiki":
                continue
            key = (str(row.get("type") or ""), str(row.get("name") or ""))
            if key in seen:
                continue
            seen.add(key)
            results.append(row)
            if len(results) >= top_k:
                return results
    return results


def _row_reason(row: dict[str, Any]) -> str:
    tags = row.get("matching_tags") or row.get("shared_tags") or []
    if tags:
        return "matched " + ", ".join(str(tag) for tag in tags[:4])
    score = row.get("normalized_score")
    if isinstance(score, (int, float)):
        return f"match score {score:.2f}"
    return "shared recommendation engine"


def _legacy_recommendation_manifest(profile: dict) -> dict:
    from ctx.core.resolve.resolve_skills import (  # noqa: PLC0415
        discover_available_skills,
        read_wiki_overrides,
        resolve,
    )
    from ctx_config import cfg  # noqa: PLC0415

    available = discover_available_skills(str(cfg.skills_dir))
    overrides = read_wiki_overrides(str(cfg.wiki_dir))
    return resolve(profile, available, overrides, max_skills=cfg.max_skills)


def _print_recommendations(repo: str, profile: dict) -> None:
    """Run the shared recommender and print recommendations by entity bucket.

    Phase 6a UX: previously only programmatic consumers (monitor, hooks)
    saw the manifest output. Running ``ctx-scan-repo --repo . --recommend`` now
    prints the same entity buckets to the terminal so users see tooling
    recommendations surface from real repos without opening the dashboard.
    """
    # Local imports — these pull in networkx and the full resolver graph
    shared = _shared_recommendations(profile)
    if shared is None:
        manifest = _legacy_recommendation_manifest(profile)
        load_entries = manifest.get("load", [])
        mcp_servers = manifest.get("mcp_servers", [])
        warnings = manifest.get("warnings", [])
    else:
        load_entries = [
            {
                "skill": row["name"],
                "entity_type": row.get("type", "skill"),
                "reason": _row_reason(row),
                "priority": round(float(row.get("normalized_score") or 0.0) * 20),
            }
            for row in shared
            if row.get("type") in {"skill", "agent"}
        ]
        mcp_servers = [
            {
                "name": row["name"],
                "score": row.get("score", 0.0),
                "normalized_score": row.get("normalized_score"),
                "shared_tags": row.get("matching_tags", []),
            }
            for row in shared
            if row.get("type") == "mcp-server"
        ]
        warnings = []

    print()
    print("=" * 60)
    print("Recommended for this repo")
    print("=" * 60)

    skills = [
        e for e in load_entries if (e.get("entity_type") or e.get("type") or "skill") == "skill"
    ]
    agents = [e for e in load_entries if (e.get("entity_type") or e.get("type")) == "agent"]

    # Skills section
    print(f"\n-- Skills ({len(skills)}) --")
    if skills:
        for entry in skills[:10]:
            reason = entry["reason"][:55]
            print(f"  {entry['skill']:<40s}  {reason}")
    else:
        print("  (no skills matched)")

    # Agents section — separate from skills by type
    print(f"\n-- Agents ({len(agents)}) --")
    if agents:
        for entry in agents[:10]:
            print(f"  {entry['skill']:<40s}  {entry['reason'][:55]}")
    else:
        print("  (no agents matched)")

    # MCP servers section — Phase 5 populated this bucket
    print(f"\n-- MCP Servers ({len(mcp_servers)}) --")
    if mcp_servers:
        for m in mcp_servers[:10]:
            shared_tag_text = ",".join(m.get("shared_tags", [])[:2]) or "-"
            score = float(m.get("score", 0.0) or 0.0)
            norm = m.get("normalized_score")
            score_text = f"score={score:.2f}"
            if isinstance(norm, (int, float)):
                score_text += f"  norm={norm:.2f}"
            print(f"  {m['name']:<40s}  {score_text}  via={shared_tag_text}")
    else:
        # The ctx-mcp-fetch / ctx-mcp-add console scripts were retired when the
        # public surface collapsed to `ctx`; the modules behind them still run
        # via `python -m`, so the hint has to name that form or it cannot be
        # followed.
        print("  (no MCP servers matched — try running")
        print(
            "   `python -m mcp_fetch --source awesome-mcp --limit 100 "
            "| python -m mcp_add --from-stdin`"
        )
        print("   to populate the catalog, then rescan)")

    # Warnings (missing skill installs etc.)
    if warnings:
        print(f"\n-- Notes ({len(warnings)}) --")
        for w in warnings[:5]:
            print(f"  {w}")


def main():
    parser = argparse.ArgumentParser(description="Scan a repo and produce a stack profile")
    parser.add_argument("--repo", required=True, help="Path to the repository")
    parser.add_argument("--output", default=str(_default_output_path()), help="Output JSON path")
    parser.add_argument("--depth", type=int, default=MAX_DEPTH, help="Max scan depth")
    parser.add_argument(
        "--recommend",
        action="store_true",
        help=(
            "After scanning, run the resolver and print recommended "
            "skills / agents / MCP servers to stderr. Requires an "
            "existing ~/.claude/skill-wiki graph (run "
            "`python -m ctx.core.wiki.wiki_graphify` first). "
            "Default: scan only, no recommendations."
        ),
    )
    args = parser.parse_args()

    if not os.path.isdir(args.repo):
        print(f"Error: {args.repo} is not a directory", file=sys.stderr)
        sys.exit(1)

    signals = scan_directory(args.repo, max_depth=args.depth)
    profile = detect_stack(args.repo, signals)

    # Ensure the output parent directory exists — users commonly pass
    # --output .ctx/stack.json without pre-creating .ctx/. Without this
    # the open() below raises FileNotFoundError.
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)

    # Summary to stdout
    total = (
        len(profile["languages"])
        + len(profile["frameworks"])
        + len(profile["infrastructure"])
        + len(profile["data_stores"])
        + len(profile["testing"])
        + len(profile["ai_tooling"])
        + len(profile["build_system"])
        + len(profile["docs"])
    )
    print(f"Scanned {args.repo}: {total} stack elements detected")
    print(f"Type: {profile['project_type']} | Monorepo: {profile['monorepo']}")
    print(f"Profile saved to {args.output}")

    if args.recommend:
        try:
            _print_recommendations(args.repo, profile)
        except Exception as exc:  # noqa: BLE001 — recommendation is advisory
            print(f"\n(recommender failed: {type(exc).__name__}: {exc})", file=sys.stderr)


if __name__ == "__main__":
    main()
