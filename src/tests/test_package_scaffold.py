"""
test_package_scaffold.py -- Pin the ctx package tree.

Goal: a reorg phase that accidentally deletes, renames, or moves a
subpackage breaks these tests loudly instead of silently shipping a
broken import tree. Each R-phase adds content INSIDE a subpackage; the
existence + importability of the subpackages themselves is the
guardrail.

This guards the public package layout used by the console scripts and
custom-harness Python imports.
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


_EXPECTED_SUBPACKAGES: tuple[str, ...] = (
    "ctx",
    "ctx.assets",
    "ctx.core",
    "ctx.core.graph",
    "ctx.core.quality",
    "ctx.core.wiki",
    "ctx.core.resolve",
    "ctx.core.bundle",
    "ctx.engine",
    "ctx.fit",
    "ctx.runtime",
    "ctx.adapters",
    "ctx.adapters.claude_code",
    "ctx.adapters.claude_code.hooks",
    "ctx.adapters.claude_code.install",
    "ctx.adapters.codex",
    "ctx.adapters.generic",
    "ctx.adapters.generic.providers",
    "ctx.adapters.generic.tools",
    "ctx.cli",
    "ctx.monitor",
    "ctx.monitor.pages",
    "ctx.mcp_server",
    "ctx.utils",
)

#: The public surface is deliberately one command plus the machine entry
#: point whose name is a contract string in shipped assets. Adding a console
#: script is a product decision, so this tuple is pinned.
_EXPECTED_CONSOLE_SCRIPTS: tuple[str, ...] = (
    "ctx",
    "ctx-init",
    "ctx-mcp-server",
    "ctx-scan-repo",
    "ctx-source-registry",
    "ctx-telemetry-export",
    "ctx-telemetry-retention",
)


@pytest.mark.parametrize("qualified_name", _EXPECTED_SUBPACKAGES)
def test_subpackage_importable(qualified_name: str) -> None:
    """Every declared subpackage must import without error."""
    mod = importlib.import_module(qualified_name)
    assert mod is not None
    assert mod.__name__ == qualified_name


def test_ctx_has_version() -> None:
    """The top-level package exposes the same version pyproject ships."""
    try:
        import tomllib  # py 3.11+
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    import ctx

    root = Path(__file__).resolve().parent.parent.parent
    with open(root / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)

    assert isinstance(ctx.__version__, str)
    assert ctx.__version__  # non-empty
    assert ctx.__version__ == data["project"]["version"]
    source_init = (root / "src" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', source_init, re.MULTILINE)
    assert match
    assert match.group(1) == data["project"]["version"]


def test_python_m_ctx_delegates_to_console_cli() -> None:
    """The flagship package supports the same entry surface as `ctx`."""
    root = Path(__file__).resolve().parent.parent.parent
    env = {
        **os.environ,
        "PYTHONPATH": str(root / "src"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "ctx", "--help"],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "run" in result.stdout
    assert "resume" in result.stdout


def test_every_subpackage_has_docstring() -> None:
    """Each scaffolded __init__.py carries a docstring documenting its role.
    This is load-bearing during the R1-R6 migration — an empty __init__
    means a contributor doesn't know what belongs there.
    """
    missing: list[str] = []
    for name in _EXPECTED_SUBPACKAGES:
        mod = importlib.import_module(name)
        if not (mod.__doc__ and mod.__doc__.strip()):
            missing.append(name)
    assert not missing, f"The following ctx subpackages are missing module docstrings: {missing}"


def test_pyproject_declares_all_subpackages() -> None:
    """pyproject.toml's packages list must include every subpackage on disk.

    This walks the source tree rather than the hand-maintained
    ``_EXPECTED_SUBPACKAGES`` tuple. Comparing two hand-maintained lists to
    each other cannot catch a package nobody remembered to add to either one,
    which is exactly how ``ctx.fit`` -- the whole product -- was omitted from
    the wheel while every packaging lane stayed green.
    """
    try:
        import tomllib  # py 3.11+
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    root = Path(__file__).resolve().parent.parent.parent
    with open(root / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)

    declared = set(data["tool"]["setuptools"]["packages"])
    source_root = root / "src"
    on_disk = {
        ".".join(init.parent.relative_to(source_root).parts)
        for init in source_root.rglob("__init__.py")
        if "tests" not in init.parts
    }
    on_disk.discard("")  # the src/ root itself is not a package

    missing = on_disk - declared
    assert not missing, (
        f"these packages exist on disk but would not ship in the wheel: {sorted(missing)}"
    )
    assert set(_EXPECTED_SUBPACKAGES) <= declared, (
        f"pyproject.toml packages list is missing: {sorted(set(_EXPECTED_SUBPACKAGES) - declared)}"
    )


def test_runtime_availability_json_is_declared_as_package_data() -> None:
    """The truthful JSON resource must survive source-to-wheel packaging."""
    try:
        import tomllib  # py 3.11+
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    root = Path(__file__).resolve().parent.parent.parent
    with open(root / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)

    package_data = set(data["tool"]["setuptools"]["package-data"]["ctx"])
    assert "assets/*.json" in package_data
    assert (root / "src" / "ctx" / "assets" / "runtime-availability.json").is_file()
    for name in (
        "benefit-eligible-catalog-v1.json",
        "release-install-skill-material-v1.json",
        "release-load-skill-material-v1.json",
        "release-query-catalog-root-v1.json",
        "reviewed-benefit-profiles-v2.json",
        "reviewed-net-benefit-policy-v1.json",
    ):
        assert (root / "src" / "ctx" / "assets" / name).is_file()


def test_flat_console_scripts_are_packaged() -> None:
    """Flat console-script targets must be listed in py-modules.

    The package smoke job installs the wheel in a clean venv, where
    editable-source imports are unavailable. A flat entrypoint like
    ``ctx-harness-add = "harness_add:main"`` only works from the wheel when
    the target module is declared in ``tool.setuptools.py-modules``.
    """
    try:
        import tomllib  # py 3.11+
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    root = Path(__file__).resolve().parent.parent.parent
    with open(root / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)

    declared_scripts = set(data["project"]["scripts"])
    expected_scripts = set(_EXPECTED_CONSOLE_SCRIPTS)
    assert declared_scripts == expected_scripts, (
        "pyproject.toml console scripts changed without updating the "
        "package-surface contract. "
        f"Missing: {sorted(expected_scripts - declared_scripts)}; "
        f"extra: {sorted(declared_scripts - expected_scripts)}"
    )

    packaged_modules = set(data["tool"]["setuptools"].get("py-modules", []))
    flat_targets = {
        target.split(":", 1)[0]
        for target in data["project"]["scripts"].values()
        if "." not in target.split(":", 1)[0]
    }
    missing = flat_targets - packaged_modules
    assert not missing, f"Flat console script targets missing from py-modules: {sorted(missing)}"


def test_no_legacy_flat_shadow() -> None:
    """Phase R0 adds the ctx package alongside the legacy flat modules;
    no flat module should share a name with a ctx subpackage yet
    (e.g. there is no src/ctx.py that would shadow the package).
    R1 onward moves flat modules INTO the package; at that point each
    moved module gets a shim, but the shim lives at its OLD flat name
    and does NOT collide with the new package path."""
    src = Path(__file__).resolve().parent.parent
    collision = src / "ctx.py"
    assert not collision.exists(), f"{collision} shadows the ctx package — rename or remove it."
