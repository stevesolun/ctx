"""
test_scan_repo.py -- Tests for scan_repo (directory walker + stack detector).

Every test builds its own minimal repository structure via tmp_path so no real
filesystem outside of pytest's temp tree is touched.

Coverage targets:
  - scan_directory(): walk behavior, SKIP_DIRS, hidden dirs, max_depth, config_files
  - read_json_safe(): valid/invalid JSON, missing file
  - read_toml_deps(): dep extraction from pyproject.toml snippets
  - read_requirements(): extraction, comment skip, version spec stripping
  - detect_stack(): all 10 sections (languages, frameworks, infrastructure,
    data stores, testing, ai tooling, build system, docs, monorepo, project_type)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure the project root is importable regardless of working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scan_repo as sr  # noqa: E402


# ===========================================================================
# Helpers
# ===========================================================================


def _write(path: Path, content: str = "") -> Path:
    """Create parent dirs and write ``content`` to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_signals(
    files: list[tuple[str, str]] | None = None,
    dirs: list[str] | None = None,
    config_files: list[str] | None = None,
) -> dict:
    """Build a minimal signals dict for detect_stack() unit tests."""
    return {
        "files": list(files or []),
        "dirs": list(dirs or []),
        "config_files": list(config_files or []),
    }


# ===========================================================================
# scan_directory
# ===========================================================================


class TestScanDirectoryCollectsFiles:
    """test_scan_directory_collects_files -- walks a repo and records files + extensions."""

    def test_collects_python_and_js_files(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "main.py", "print('hi')")
        _write(repo / "app.js", "console.log('hi')")
        _write(repo / "README.md", "# repo")

        signals = sr.scan_directory(str(repo))

        names = {os.path.basename(f) for f, _ in signals["files"]}
        assert "main.py" in names
        assert "app.js" in names
        assert "README.md" in names

    def test_records_extensions_lowercased(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "a.PY")
        _write(repo / "b.Ts")

        signals = sr.scan_directory(str(repo))
        exts = {ext for _, ext in signals["files"]}
        assert ".py" in exts
        assert ".ts" in exts


class TestScanDirectorySkipsIgnoredDirs:
    """test_scan_directory_skips_ignored_dirs -- node_modules, .git, venv etc. are ignored."""

    def test_node_modules_skipped(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "package.json", "{}")
        _write(repo / "node_modules" / "lodash" / "index.js", "")

        signals = sr.scan_directory(str(repo))
        paths = [f for f, _ in signals["files"]]
        assert not any("node_modules" in p for p in paths)

    def test_hidden_dirs_skipped(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "a.py")
        _write(repo / ".git" / "HEAD", "ref: refs/heads/main")

        signals = sr.scan_directory(str(repo))
        paths = [f for f, _ in signals["files"]]
        assert not any(".git" in p for p in paths)

    def test_pycache_and_venv_skipped(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "__pycache__" / "x.cpython-311.pyc")
        _write(repo / "venv" / "bin" / "python")
        _write(repo / "keep.py")

        signals = sr.scan_directory(str(repo))
        paths = [f for f, _ in signals["files"]]
        assert any(p.endswith("keep.py") for p in paths)
        assert not any("__pycache__" in p for p in paths)
        assert not any("venv" in p for p in paths)


class TestScanDirectoryCollectsConfigFiles:
    """test_scan_directory_collects_config_files -- known config files are flagged."""

    def test_package_json_collected(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "package.json", '{"name":"x"}')

        signals = sr.scan_directory(str(repo))
        names = {os.path.basename(p) for p in signals["config_files"]}
        assert "package.json" in names

    def test_terraform_files_collected(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "main.tf", "# terraform")
        _write(repo / "modules" / "vpc" / "vpc.tf", "# vpc")

        signals = sr.scan_directory(str(repo))
        names = {os.path.basename(p) for p in signals["config_files"]}
        assert "main.tf" in names
        assert "vpc.tf" in names

    def test_claude_md_collected(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "CLAUDE.md", "# rules")

        signals = sr.scan_directory(str(repo))
        names = {os.path.basename(p) for p in signals["config_files"]}
        assert "CLAUDE.md" in names


class TestScanDirectoryMaxDepth:
    """test_scan_directory_max_depth -- files deeper than max_depth are ignored."""

    def test_shallow_file_is_recorded(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "a" / "shallow.py")

        signals = sr.scan_directory(str(repo), max_depth=2)
        paths = [f for f, _ in signals["files"]]
        assert any(p.endswith("shallow.py") for p in paths)

    def test_deep_file_dropped_at_depth_1(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "a" / "b" / "c" / "deep.py")

        signals = sr.scan_directory(str(repo), max_depth=1)
        paths = [f for f, _ in signals["files"]]
        assert not any(p.endswith("deep.py") for p in paths)


# ===========================================================================
# read_json_safe
# ===========================================================================


class TestReadJsonSafe:
    """test_read_json_safe -- parses valid JSON, returns None on failure."""

    def test_valid_json_returns_dict(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "pkg.json", '{"name":"x","version":"1.0"}')
        result = sr.read_json_safe(str(p))
        assert result == {"name": "x", "version": "1.0"}

    def test_invalid_json_returns_none(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "bad.json", "not json {{")
        assert sr.read_json_safe(str(p)) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert sr.read_json_safe(str(tmp_path / "ghost.json")) is None


# ===========================================================================
# read_toml_deps
# ===========================================================================


class TestReadTomlDeps:
    """test_read_toml_deps -- extracts dep names from pyproject.toml (tomllib-based)."""

    def test_extracts_bare_deps_lowercased(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "pyproject.toml", """
[project]
dependencies = ["FastAPI", "pydantic", "SQLAlchemy"]
""")
        deps = sr.read_toml_deps(str(p))
        assert "fastapi" in deps
        assert "pydantic" in deps
        assert "sqlalchemy" in deps

    def test_extracts_version_pinned_deps(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "pyproject.toml", """
[project]
dependencies = [
    "fastapi>=0.100",
    "pydantic==2.5.0",
    "requests<3",
    "numpy[extra]>=1.20",
    'package-with-marker>=1.0; python_version>="3.10"',
]
""")
        deps = sr.read_toml_deps(str(p))
        assert "fastapi" in deps
        assert "pydantic" in deps
        assert "requests" in deps
        assert "numpy" in deps
        assert "package-with-marker" in deps

    def test_extracts_optional_deps(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "pyproject.toml", """
[project]
dependencies = ["core-dep"]

[project.optional-dependencies]
test = ["pytest>=7", "pytest-cov"]
dev = ["black"]
""")
        deps = sr.read_toml_deps(str(p))
        assert "core-dep" in deps
        assert "pytest" in deps
        assert "pytest-cov" in deps
        assert "black" in deps

    def test_extracts_poetry_deps(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "pyproject.toml", """
[tool.poetry]
name = "x"

[tool.poetry.dependencies]
python = "^3.11"
fastapi = "^0.100"
pydantic = "^2.0"

[tool.poetry.dev-dependencies]
pytest = "^7.0"
""")
        deps = sr.read_toml_deps(str(p))
        assert "fastapi" in deps
        assert "pydantic" in deps
        assert "pytest" in deps
        # The "python" version spec is stripped (it's the interpreter, not a dep).
        assert "python" not in deps

    def test_invalid_toml_returns_empty(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "broken.toml", "this is = not valid { toml")
        assert sr.read_toml_deps(str(p)) == []

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert sr.read_toml_deps(str(tmp_path / "ghost.toml")) == []


# ===========================================================================
# read_requirements
# ===========================================================================


class TestReadRequirements:
    """test_read_requirements -- parses requirements.txt with comments and version specs."""

    def test_plain_package_names(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "requirements.txt", "fastapi\npydantic\nsqlalchemy\n")
        deps = sr.read_requirements(str(p))
        assert deps == ["fastapi", "pydantic", "sqlalchemy"]

    def test_strips_version_specs(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "requirements.txt", """
fastapi>=0.100
pydantic==2.5.0
requests<3
numpy[extra]>=1.20
""")
        deps = sr.read_requirements(str(p))
        assert "fastapi" in deps
        assert "pydantic" in deps
        assert "requests" in deps
        assert "numpy" in deps

    def test_ignores_comments_and_dash_lines(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "requirements.txt", """\
# this is a comment
fastapi
-r other.txt
--index-url https://pypi.org
pydantic
""")
        deps = sr.read_requirements(str(p))
        assert "fastapi" in deps
        assert "pydantic" in deps
        assert "-r" not in deps
        assert not any(d.startswith("--") for d in deps)

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert sr.read_requirements(str(tmp_path / "ghost.txt")) == []


# ===========================================================================
# detect_stack — languages
# ===========================================================================


class TestDetectStackLanguages:
    """test_detect_stack_languages -- extension counts surface language entries."""

    def test_python_detected_from_py_files(self, tmp_path: Path) -> None:
        signals = _make_signals(files=[("a.py", ".py"), ("b.py", ".py")])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [lang["name"] for lang in profile["languages"]]
        assert "python" in names

    def test_typescript_boosted_by_tsconfig(self, tmp_path: Path) -> None:
        tsconfig = _write(tmp_path / "tsconfig.json", "{}")
        signals = _make_signals(
            files=[("a.ts", ".ts"), ("b.tsx", ".tsx")],
            config_files=[str(tsconfig)],
        )
        profile = sr.detect_stack(str(tmp_path), signals)
        ts = next(lang for lang in profile["languages"] if lang["name"] == "typescript")
        assert "tsconfig.json" in ts["evidence"]
        assert ts["confidence"] >= 0.9

    def test_languages_sorted_by_count_desc(self, tmp_path: Path) -> None:
        signals = _make_signals(files=[
            ("a.py", ".py"),
            ("b.go", ".go"), ("c.go", ".go"), ("d.go", ".go"),
        ])
        profile = sr.detect_stack(str(tmp_path), signals)
        assert profile["languages"][0]["name"] == "go"


# ===========================================================================
# detect_stack — frameworks
# ===========================================================================


class TestDetectStackFrameworks:
    """test_detect_stack_frameworks -- framework detection via deps and config files."""

    def test_fastapi_detected_from_pyproject(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "pyproject.toml", """
[project]
dependencies = ["fastapi>=0.100"]
""")
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [f["name"] for f in profile["frameworks"]]
        assert "fastapi" in names

    def test_langchain_detected_from_requirements(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "requirements.txt", "langchain\nopenai\n")
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [f["name"] for f in profile["frameworks"]]
        assert "langchain" in names
        assert "openai-sdk" in names

    def test_react_detected_from_package_json(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "package.json", json.dumps({
            "name": "app",
            "dependencies": {"react": "^18"},
        }))
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [f["name"] for f in profile["frameworks"]]
        assert "react" in names

    def test_nextjs_config_merges_with_dep(self, tmp_path: Path) -> None:
        pkg = _write(tmp_path / "package.json", json.dumps({
            "dependencies": {"next": "^14"},
        }))
        nxt = _write(tmp_path / "next.config.js", "module.exports = {}")
        signals = _make_signals(config_files=[str(pkg), str(nxt)])
        profile = sr.detect_stack(str(tmp_path), signals)
        nextjs = [f for f in profile["frameworks"] if f["name"] == "nextjs"]
        assert len(nextjs) == 1, "nextjs should be deduped across dep + config"
        assert "next.config.js" in nextjs[0]["evidence"]

    def test_angular_from_config_only(self, tmp_path: Path) -> None:
        ng = _write(tmp_path / "angular.json", "{}")
        signals = _make_signals(config_files=[str(ng)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [f["name"] for f in profile["frameworks"]]
        assert "angular" in names


# ===========================================================================
# detect_stack — infrastructure
# ===========================================================================


class TestDetectStackInfrastructure:
    """test_detect_stack_infrastructure -- docker, CI, IaC detection."""

    def test_dockerfile_detected(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "Dockerfile", "FROM python:3.11")
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [i["name"] for i in profile["infrastructure"]]
        assert "docker" in names

    def test_terraform_detected_from_tf_files(self, tmp_path: Path) -> None:
        signals = _make_signals(files=[("main.tf", ".tf"), ("variables.tf", ".tf")])
        profile = sr.detect_stack(str(tmp_path), signals)
        tf = [i for i in profile["infrastructure"] if i["name"] == "terraform"]
        assert tf and "2 .tf files" in tf[0]["evidence"]

    def test_kubernetes_detected_from_dir_basename(self, tmp_path: Path) -> None:
        signals = _make_signals(dirs=["k8s"])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [i["name"] for i in profile["infrastructure"]]
        assert "kubernetes" in names

    def test_github_actions_detected(self, tmp_path: Path) -> None:
        signals = _make_signals(dirs=[".github", ".github/workflows"])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [i["name"] for i in profile["infrastructure"]]
        assert "github-actions" in names


# ===========================================================================
# detect_stack — data stores
# ===========================================================================


class TestDetectStackDataStores:
    """test_detect_stack_data_stores -- ORM and cache/queue detection via deps."""

    def test_sqlalchemy_detected_from_alembic_dir(self, tmp_path: Path) -> None:
        signals = _make_signals(dirs=["alembic"])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [d["name"] for d in profile["data_stores"]]
        assert "sqlalchemy" in names

    def test_prisma_detected_from_package_json(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "package.json", json.dumps({
            "dependencies": {"@prisma/client": "^5"},
        }))
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [d["name"] for d in profile["data_stores"]]
        assert "prisma" in names

    def test_redis_deduped_across_py_and_js(self, tmp_path: Path) -> None:
        py = _write(tmp_path / "requirements.txt", "redis\n")
        js = _write(tmp_path / "package.json", json.dumps({
            "dependencies": {"ioredis": "^5"},
        }))
        signals = _make_signals(config_files=[str(py), str(js)])
        profile = sr.detect_stack(str(tmp_path), signals)
        redis = [d for d in profile["data_stores"] if d["name"] == "redis"]
        assert len(redis) == 1, "redis should only appear once"

    def test_dbt_detected(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "dbt_project.yml", "name: warehouse")
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [d["name"] for d in profile["data_stores"]]
        assert "dbt" in names


# ===========================================================================
# detect_stack — testing / ai / build / docs
# ===========================================================================


class TestDetectStackTesting:
    """test_detect_stack_testing -- testing framework detection."""

    def test_pytest_detected(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "pytest.ini", "[pytest]\n")
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [t["name"] for t in profile["testing"]]
        assert "pytest" in names

    def test_playwright_detected(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "playwright.config.ts", "export default {}")
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [t["name"] for t in profile["testing"]]
        assert "playwright" in names


class TestDetectStackAiTooling:
    """test_detect_stack_ai_tooling -- MCP and Claude Code detection."""

    def test_mcp_json_detected(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "mcp.json", "{}")
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [t["name"] for t in profile["ai_tooling"]]
        assert "mcp" in names

    def test_claude_md_detected(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "CLAUDE.md", "# rules")
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [t["name"] for t in profile["ai_tooling"]]
        assert "claude-code" in names


class TestDetectStackBuildAndDocs:
    """test_detect_stack_build_and_docs -- vite/webpack and mkdocs/openapi detection."""

    def test_vite_detected(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "vite.config.ts", "export default {}")
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [b["name"] for b in profile["build_system"]]
        assert "vite" in names

    def test_mkdocs_detected(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "mkdocs.yml", "site_name: Docs")
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [d["name"] for d in profile["docs"]]
        assert "mkdocs" in names

    def test_openapi_detected_from_files(self, tmp_path: Path) -> None:
        signals = _make_signals(files=[("openapi.yaml", ".yaml")])
        profile = sr.detect_stack(str(tmp_path), signals)
        names = [d["name"] for d in profile["docs"]]
        assert "openapi" in names


# ===========================================================================
# detect_stack — monorepo
# ===========================================================================


class TestDetectStackMonorepo:
    """test_detect_stack_monorepo -- turbo/nx/lerna/workspaces detection."""

    def test_turbo_json_flags_monorepo(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "turbo.json", "{}")
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        assert profile["monorepo"] is True

    def test_workspaces_in_package_json_flags_monorepo(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "package.json", json.dumps({
            "name": "root",
            "workspaces": ["packages/*"],
        }))
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        assert profile["monorepo"] is True

    def test_plain_project_is_not_monorepo(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "package.json", json.dumps({"name": "single"}))
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        assert profile["monorepo"] is False


# ===========================================================================
# detect_stack — project_type
# ===========================================================================


class TestDetectStackProjectType:
    """test_detect_stack_project_type -- frontend/fullstack/api/ml/ai classification."""

    def test_frontend_only(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "package.json", json.dumps({
            "dependencies": {"react": "^18"},
        }))
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        assert profile["project_type"] == "frontend"

    def test_fullstack_when_front_and_back(self, tmp_path: Path) -> None:
        pkg = _write(tmp_path / "package.json", json.dumps({
            "dependencies": {"react": "^18"},
        }))
        py = _write(tmp_path / "requirements.txt", "fastapi\n")
        signals = _make_signals(config_files=[str(pkg), str(py)])
        profile = sr.detect_stack(str(tmp_path), signals)
        assert profile["project_type"] == "fullstack"

    def test_api_service_when_backend_only(self, tmp_path: Path) -> None:
        py = _write(tmp_path / "requirements.txt", "fastapi\n")
        signals = _make_signals(config_files=[str(py)])
        profile = sr.detect_stack(str(tmp_path), signals)
        assert profile["project_type"] == "api-service"

    def test_ml_project_when_torch_present(self, tmp_path: Path) -> None:
        py = _write(tmp_path / "requirements.txt", "torch\n")
        signals = _make_signals(config_files=[str(py)])
        profile = sr.detect_stack(str(tmp_path), signals)
        assert profile["project_type"] == "ml-project"

    def test_ai_agent_when_langchain_present(self, tmp_path: Path) -> None:
        py = _write(tmp_path / "requirements.txt", "langchain\n")
        signals = _make_signals(config_files=[str(py)])
        profile = sr.detect_stack(str(tmp_path), signals)
        assert profile["project_type"] == "ai-agent"

    def test_infrastructure_when_only_iac(self, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "Dockerfile", "FROM alpine")
        signals = _make_signals(config_files=[str(cfg)])
        profile = sr.detect_stack(str(tmp_path), signals)
        assert profile["project_type"] == "infrastructure"

    def test_unknown_when_no_signals(self, tmp_path: Path) -> None:
        profile = sr.detect_stack(str(tmp_path), _make_signals())
        assert profile["project_type"] == "unknown"


# ===========================================================================
# End-to-end: scan_directory + detect_stack on a realistic fake repo
# ===========================================================================


class TestEndToEndFastApiRepo:
    """test_end_to_end_fastapi_repo -- build a fake FastAPI+Docker repo and check profile."""

    def test_profile_matches_expected_stack(self, tmp_path: Path) -> None:
        repo = tmp_path / "fake-api"
        _write(repo / "pyproject.toml", """
[project]
dependencies = ["fastapi>=0.100", "sqlalchemy>=2", "pydantic>=2"]
""")
        _write(repo / "Dockerfile", "FROM python:3.11")
        _write(repo / "pytest.ini", "[pytest]\n")
        _write(repo / "app" / "main.py", "from fastapi import FastAPI\napp = FastAPI()")
        _write(repo / "app" / "routes.py", "")
        _write(repo / "alembic" / "env.py", "")

        signals = sr.scan_directory(str(repo))
        profile = sr.detect_stack(str(repo), signals)

        lang_names = {lang["name"] for lang in profile["languages"]}
        fw_names = {f["name"] for f in profile["frameworks"]}
        infra_names = {i["name"] for i in profile["infrastructure"]}
        data_names = {d["name"] for d in profile["data_stores"]}
        test_names = {t["name"] for t in profile["testing"]}

        assert "python" in lang_names
        assert "fastapi" in fw_names
        assert "docker" in infra_names
        assert "sqlalchemy" in data_names
        assert "pytest" in test_names
        assert profile["project_type"] == "api-service"
        assert profile["monorepo"] is False
