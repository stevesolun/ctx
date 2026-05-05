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
from pathlib import Path

import pytest


_EXPECTED_SUBPACKAGES: tuple[str, ...] = (
    "ctx",
    "ctx.core",
    "ctx.core.graph",
    "ctx.core.quality",
    "ctx.core.wiki",
    "ctx.core.resolve",
    "ctx.core.bundle",
    "ctx.adapters",
    "ctx.adapters.claude_code",
    "ctx.adapters.claude_code.hooks",
    "ctx.adapters.claude_code.install",
    "ctx.adapters.generic",
    "ctx.adapters.generic.providers",
    "ctx.adapters.generic.tools",
    "ctx.cli",
    "ctx.mcp_server",
    "ctx.utils",
)

_EXPECTED_CONSOLE_SCRIPTS: tuple[str, ...] = (
    "ctx",
    "ctx-agent-add",
    "ctx-agent-install",
    "ctx-agent-mirror",
    "ctx-bundle-suggest",
    "ctx-dedup-check",
    "ctx-harness-add",
    "ctx-harness-install",
    "ctx-init",
    "ctx-install-hooks",
    "ctx-lifecycle",
    "ctx-mcp-add",
    "ctx-mcp-enrich",
    "ctx-mcp-fetch",
    "ctx-mcp-ingest",
    "ctx-mcp-install",
    "ctx-mcp-quality",
    "ctx-mcp-rebuild-index",
    "ctx-mcp-server",
    "ctx-mcp-uninstall",
    "ctx-monitor",
    "ctx-recommend",
    "ctx-scan-repo",
    "ctx-skill-add",
    "ctx-skill-health",
    "ctx-skill-install",
    "ctx-skill-mirror",
    "ctx-skill-quality",
    "ctx-tag-backfill",
    "ctx-toolbox",
    "ctx-wiki-graphify",
    "ctx-wiki-worker",
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
    assert not missing, (
        "The following ctx subpackages are missing module docstrings: "
        f"{missing}"
    )


def test_pyproject_declares_all_subpackages() -> None:
    """pyproject.toml's packages list must include every scaffolded
    subpackage. A package that exists on disk but isn't declared won't
    ship in the wheel."""
    try:
        import tomllib  # py 3.11+
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    root = Path(__file__).resolve().parent.parent.parent
    with open(root / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)

    declared = set(data["tool"]["setuptools"]["packages"])
    expected = set(_EXPECTED_SUBPACKAGES)
    missing = expected - declared
    assert not missing, (
        f"pyproject.toml packages list is missing: {sorted(missing)}"
    )


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
    assert not missing, (
        "Flat console script targets missing from py-modules: "
        f"{sorted(missing)}"
    )


def test_no_legacy_flat_shadow() -> None:
    """Phase R0 adds the ctx package alongside the legacy flat modules;
    no flat module should share a name with a ctx subpackage yet
    (e.g. there is no src/ctx.py that would shadow the package).
    R1 onward moves flat modules INTO the package; at that point each
    moved module gets a shim, but the shim lives at its OLD flat name
    and does NOT collide with the new package path."""
    src = Path(__file__).resolve().parent.parent
    collision = src / "ctx.py"
    assert not collision.exists(), (
        f"{collision} shadows the ctx package — rename or remove it."
    )
