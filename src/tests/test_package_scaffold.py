"""
test_package_scaffold.py -- Pin the ctx package tree scaffolded in Plan 001 R0.

Goal: a reorg phase that accidentally deletes, renames, or moves a
subpackage breaks these tests loudly instead of silently shipping a
broken import tree. Each R-phase adds content INSIDE a subpackage; the
existence + importability of the subpackages themselves is the
guardrail.

See docs/plans/001-model-agnostic-harness.md for the target layout.
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


@pytest.mark.parametrize("qualified_name", _EXPECTED_SUBPACKAGES)
def test_subpackage_importable(qualified_name: str) -> None:
    """Every declared subpackage must import without error."""
    mod = importlib.import_module(qualified_name)
    assert mod is not None
    assert mod.__name__ == qualified_name


def test_ctx_has_version() -> None:
    """The top-level package exposes a __version__ string."""
    import ctx
    assert isinstance(ctx.__version__, str)
    assert ctx.__version__  # non-empty


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
