#!/usr/bin/env python3
"""
scan_repo.py -- Analyze a repository and produce a stack profile JSON.

Usage:
    python scan_repo.py --repo /path/to/repo --output /tmp/stack-profile.json

The scanner reads directory structure and config files to detect:
- Languages, frameworks, infrastructure, data stores, testing, AI tooling,
  build systems, and documentation tools.

It avoids reading source files unless needed to disambiguate (e.g., checking
imports in an entry file to distinguish React from Preact).
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Directories to always skip
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv", "env",
    ".env", "dist", "build", ".next", ".nuxt", ".cache", ".tox",
    "target", "vendor", ".terraform", ".serverless", "coverage",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "egg-info",
}

# Max depth for directory scanning
MAX_DEPTH = 3


def scan_directory(repo_path: str, max_depth: int = MAX_DEPTH) -> dict:
    """Walk the repo and collect file/dir signals without reading contents."""
    signals = {
        "files": [],        # (relative_path, extension)
        "dirs": [],         # relative directory paths
        "config_files": [], # config files found (will be read)
    }

    repo = Path(repo_path).resolve()
    config_names = {
        "package.json", "pyproject.toml", "requirements.txt", "Pipfile",
        "Cargo.toml", "go.mod", "Gemfile", "composer.json", "pom.xml",
        "build.gradle", "build.gradle.kts",
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        "tsconfig.json", "next.config.js", "next.config.mjs", "next.config.ts",
        "nuxt.config.ts", "nuxt.config.js", "angular.json", "svelte.config.js",
        "vite.config.ts", "vite.config.js", "webpack.config.js",
        "tailwind.config.js", "tailwind.config.ts",
        "jest.config.js", "jest.config.ts", "vitest.config.ts",
        "pytest.ini", "setup.cfg", "tox.ini",
        "mkdocs.yml", ".gitlab-ci.yml", "Jenkinsfile",
        "turbo.json", "nx.json", "lerna.json", "pnpm-workspace.yaml",
        "fly.toml", "vercel.json", "netlify.toml", "render.yaml",
        "serverless.yml", "cdk.json", "Pulumi.yaml",
        "mcp.json", "CLAUDE.md", ".cursorrules", ".windsurfrules",
        "alembic.ini", "dbt_project.yml",
        "openapi.yaml", "openapi.json", "swagger.yaml", "swagger.json",
        ".coveragerc", "playwright.config.ts", "cypress.config.ts",
        "poetry.lock", "yarn.lock", "pnpm-lock.yaml", "package-lock.json",
        "Cargo.lock", "Gemfile.lock", "go.sum", "composer.lock",
    }

    for dirpath, dirnames, filenames in os.walk(repo):
        rel_dir = os.path.relpath(dirpath, repo)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1

        # Skip ignored dirs
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".")
        ]

        if depth > max_depth:
            dirnames.clear()
            continue

        signals["dirs"].append(rel_dir)

        for fname in filenames:
            ext = Path(fname).suffix.lower()
            rel_path = os.path.join(rel_dir, fname) if rel_dir != "." else fname
            signals["files"].append((rel_path, ext))

            if fname in config_names or fname.endswith(".tf"):
                signals["config_files"].append(
                    os.path.join(dirpath, fname)
                )

    return signals


def read_json_safe(path: str) -> dict | None:
    """Read a JSON file, return None on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        print(f"Warning: failed to read JSON file {path}: {exc}", file=sys.stderr)
        return None


def read_toml_deps(path: str) -> list[str]:
    """Extract dependency names from pyproject.toml (basic regex, no toml lib needed)."""
    try:
        with open(path) as f:
            content = f.read()
        # Match dependencies = [...] and [project.dependencies] style
        deps = re.findall(r'["\']([a-zA-Z0-9_-]+)(?:\[.*?\])?(?:>=|<=|==|~=|!=|>|<|,|\s)*["\']', content)
        return [d.lower() for d in deps]
    except Exception as exc:
        print(f"Warning: failed to read TOML deps from {path}: {exc}", file=sys.stderr)
        return []


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


def detect_stack(repo_path: str, signals: dict) -> dict:
    """Analyze signals and produce a stack profile."""
    profile = {
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
    all_js_deps: list[str] = []
    pkg_json = None

    for cfg in signals["config_files"]:
        base = os.path.basename(cfg)
        if base == "pyproject.toml":
            all_py_deps.extend(read_toml_deps(cfg))
        elif base == "requirements.txt":
            all_py_deps.extend(read_requirements(cfg))
        elif base == "Pipfile":
            all_py_deps.extend(read_requirements(cfg))
        elif base == "package.json":
            data = read_json_safe(cfg)
            if data:
                pkg_json = data
                for section in ("dependencies", "devDependencies", "peerDependencies"):
                    if section in data:
                        all_js_deps.extend(k.lower() for k in data[section])

    py_dep_set = set(all_py_deps)
    js_dep_set = set(all_js_deps)

    # --- LANGUAGES ---
    lang_map = {
        ".py": "python", ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
        ".rb": "ruby", ".swift": "swift", ".cs": "csharp", ".php": "php",
    }
    detected_langs = {}
    for ext, count in ext_counts.items():
        if ext in lang_map:
            lang = lang_map[ext]
            detected_langs[lang] = detected_langs.get(lang, 0) + count

    for lang, count in sorted(detected_langs.items(), key=lambda x: -x[1]):
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

        profile["languages"].append({
            "name": lang, "confidence": round(conf, 2), "evidence": evidence
        })

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
            profile["frameworks"].append({
                "name": stack_id, "category": category,
                "confidence": conf, "evidence": [f"{dep_name} in dependencies"]
            })

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
                profile["frameworks"].append({
                    "name": stack_id, "category": cat,
                    "confidence": conf, "evidence": [cfg_name]
                })

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
            profile["infrastructure"].append({
                "name": stack_id, "confidence": conf, "evidence": [cfg_name]
            })

    # GitHub Actions
    if ".github" in dir_basenames:
        gh_wf = [d for d in signals["dirs"] if "workflows" in d and ".github" in d]
        if gh_wf:
            profile["infrastructure"].append({
                "name": "github-actions", "confidence": 1.0,
                "evidence": [".github/workflows/"]
            })

    # Terraform
    tf_files = [f for f, ext in signals["files"] if ext == ".tf"]
    if tf_files:
        profile["infrastructure"].append({
            "name": "terraform", "confidence": 1.0,
            "evidence": [f"{len(tf_files)} .tf files"]
        })

    # K8s
    k8s_dirs = {"k8s", "kubernetes", "helm", "charts"}
    if k8s_dirs & dir_basenames:
        profile["infrastructure"].append({
            "name": "kubernetes", "confidence": 0.95,
            "evidence": [f"directory: {k8s_dirs & dir_basenames}"]
        })

    # --- DATA STORES ---
    data_checks = [
        (py_dep_set, "sqlalchemy", "sqlalchemy", 0.95),
        (py_dep_set, "alembic", "sqlalchemy", 0.95),
        (py_dep_set, "redis", "redis", 0.85),
        (py_dep_set, "celery", "celery", 0.95),
        (py_dep_set, "kafka-python", "kafka", 0.9),
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
                profile["data_stores"].append({
                    "name": stack_id, "confidence": conf,
                    "evidence": [f"{dep_name} in dependencies"]
                })

    if "alembic" in dir_basenames or "alembic.ini" in config_basenames:
        existing = [d for d in profile["data_stores"] if d["name"] == "sqlalchemy"]
        if existing:
            existing[0]["evidence"].append("alembic/ directory")
        else:
            profile["data_stores"].append({
                "name": "sqlalchemy", "confidence": 0.95,
                "evidence": ["alembic/ directory"]
            })

    if "dbt_project.yml" in config_basenames:
        profile["data_stores"].append({
            "name": "dbt", "confidence": 1.0, "evidence": ["dbt_project.yml"]
        })

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
                profile["testing"].append({
                    "name": stack_id, "confidence": conf, "evidence": [cfg_name]
                })

    # --- AI TOOLING ---
    if "mcp.json" in config_basenames or ".mcp" in dir_basenames:
        profile["ai_tooling"].append({
            "name": "mcp", "confidence": 1.0,
            "evidence": ["mcp.json or .mcp/ directory"]
        })
    if "CLAUDE.md" in config_basenames:
        profile["ai_tooling"].append({
            "name": "claude-code", "confidence": 0.95,
            "evidence": ["CLAUDE.md"]
        })

    # --- BUILD SYSTEM ---
    build_map = {
        "vite.config.ts": "vite", "vite.config.js": "vite",
        "webpack.config.js": "webpack",
    }
    for cfg_name, stack_id in build_map.items():
        if cfg_name in config_basenames:
            profile["build_system"].append({
                "name": stack_id, "confidence": 1.0, "evidence": [cfg_name]
            })

    # --- DOCS ---
    doc_map = {
        "mkdocs.yml": "mkdocs",
        "docusaurus.config.js": "docusaurus",
        "docusaurus.config.ts": "docusaurus",
    }
    for cfg_name, stack_id in doc_map.items():
        if cfg_name in config_basenames:
            profile["docs"].append({
                "name": stack_id, "confidence": 1.0, "evidence": [cfg_name]
            })

    openapi_files = [
        f for f, _ in signals["files"]
        if os.path.basename(f) in ("openapi.yaml", "openapi.json", "swagger.yaml", "swagger.json")
    ]
    if openapi_files:
        profile["docs"].append({
            "name": "openapi", "confidence": 0.95,
            "evidence": openapi_files[:3]
        })

    # --- MONOREPO ---
    monorepo_signals = {"turbo.json", "nx.json", "lerna.json", "pnpm-workspace.yaml"}
    if monorepo_signals & config_basenames:
        profile["monorepo"] = True
    elif pkg_json and "workspaces" in pkg_json:
        profile["monorepo"] = True

    # --- PROJECT TYPE ---
    fw_names = {f["name"] for f in profile["frameworks"]}
    if fw_names & {"react", "vue", "angular", "svelte", "nextjs", "nuxt"}:
        if fw_names & {"fastapi", "django", "flask", "express", "nestjs"}:
            profile["project_type"] = "fullstack"
        else:
            profile["project_type"] = "frontend"
    elif fw_names & {"fastapi", "django", "flask", "express", "nestjs", "gin", "actix"}:
        profile["project_type"] = "api-service"
    elif fw_names & {"pytorch", "tensorflow", "huggingface"}:
        profile["project_type"] = "ml-project"
    elif fw_names & {"langchain", "llamaindex", "crewai"}:
        profile["project_type"] = "ai-agent"
    elif profile["infrastructure"]:
        profile["project_type"] = "infrastructure"

    return profile


def main():
    parser = argparse.ArgumentParser(description="Scan a repo and produce a stack profile")
    parser.add_argument("--repo", required=True, help="Path to the repository")
    parser.add_argument("--output", default="/tmp/stack-profile.json", help="Output JSON path")
    parser.add_argument("--depth", type=int, default=MAX_DEPTH, help="Max scan depth")
    args = parser.parse_args()

    if not os.path.isdir(args.repo):
        print(f"Error: {args.repo} is not a directory", file=sys.stderr)
        sys.exit(1)

    signals = scan_directory(args.repo, max_depth=args.depth)
    profile = detect_stack(args.repo, signals)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)

    # Summary to stdout
    total = (
        len(profile["languages"]) + len(profile["frameworks"]) +
        len(profile["infrastructure"]) + len(profile["data_stores"]) +
        len(profile["testing"]) + len(profile["ai_tooling"]) +
        len(profile["build_system"]) + len(profile["docs"])
    )
    print(f"Scanned {args.repo}: {total} stack elements detected")
    print(f"Type: {profile['project_type']} | Monorepo: {profile['monorepo']}")
    print(f"Profile saved to {args.output}")


if __name__ == "__main__":
    main()
