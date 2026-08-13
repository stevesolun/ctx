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

import ast
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


#: The advertised command surface, and the harness commands that still work but
#: are deliberately not advertised. Mirrors ctx.cli.run; asserted from the
#: outside, through the CLI, so a change to either has to be intentional.
_PRODUCT_SURFACE: tuple[str, ...] = ("fit", "doctor", "advanced")
_UNADVERTISED_COMMANDS: tuple[str, ...] = ("run", "resume", "sessions")


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parent.parent.parent
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    return subprocess.run(
        [sys.executable, *args],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def test_python_m_ctx_delegates_to_console_cli() -> None:
    """``python -m ctx`` and the ``ctx`` console script are one surface.

    ``ctx = ctx.cli.run:main`` in [project.scripts], so ``python -m ctx`` is
    correct exactly when it produces what ``ctx.cli.run`` produces. The help
    text is compared in full rather than probed for words: the previous version
    of this test asserted ``"run"`` and ``"resume"`` appear in ``python -m ctx
    --help``, which is the opposite of what the product intends, and passed
    only on a substring of the ``advanced`` blurb.
    """
    package = _run_module("-m", "ctx", "--help")
    console = _run_module("-m", "ctx.cli.run", "--help")

    assert package.returncode == 0, package.stderr
    assert console.returncode == 0, console.stderr
    assert package.stdout == console.stdout, (
        "`python -m ctx` and the console script's module no longer render the "
        "same CLI, so the package entrypoint has drifted from the product."
    )


def test_python_m_ctx_advertises_exactly_the_product_surface() -> None:
    """The surface is {fit, doctor, advanced} -- nothing more, nothing less."""
    usage = _run_module("-m", "ctx", "--help").stdout.splitlines()[0]

    for command in _PRODUCT_SURFACE:
        assert command in usage, f"{command} must be advertised: {usage}"
    for command in _UNADVERTISED_COMMANDS:
        assert command not in usage, f"{command} must not be advertised: {usage}"


@pytest.mark.parametrize("command", _UNADVERTISED_COMMANDS)
def test_python_m_ctx_actually_dispatches_hidden_commands(command: str) -> None:
    """Hiding a command from help must not stop ``python -m ctx`` running it.

    Nothing previously checked that ``python -m ctx run ...`` dispatches at
    all, which is the one thing the package entrypoint exists to do.
    """
    result = _run_module("-m", "ctx", command, "--help")

    assert result.returncode == 0, result.stderr
    assert f"usage: ctx {command}" in result.stdout


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


# ---------------------------------------------------------------------------
# Retired-console-script guardrail
#
# Commit 9eccd4e7 cut the installed console scripts from 45 to 7. Prose in the
# docs was rewritten to the surviving ``python -m`` spellings, but the parts a
# user actually executes were not: fenced command blocks, shipped runtime
# strings (including the Claude Code hook payload and ctx-init's "Next steps"),
# and argparse ``prog=`` values still named commands that are no longer on
# PATH. Rewriting those by hand fixes today; this guardrail is what stops the
# next removal from silently re-introducing them.
# ---------------------------------------------------------------------------

#: Console scripts retired by 9eccd4e7. Pinned as literals rather than
#: recomputed from ``git show 9eccd4e7^:pyproject.toml`` so the guardrail still
#: runs in a shallow clone or an unpacked sdist, where history is unavailable.
#: ``test_retired_console_scripts_are_really_gone`` keeps the tuple honest.
_RETIRED_CONSOLE_SCRIPTS: tuple[str, ...] = (
    "ctx-agent-add",
    "ctx-agent-install",
    "ctx-agent-mirror",
    "ctx-agent-unload",
    "ctx-bundle-suggest",
    "ctx-dedup-check",
    "ctx-graph-store",
    "ctx-harness-add",
    "ctx-harness-install",
    "ctx-incremental-attach",
    "ctx-incremental-shadow",
    "ctx-install-codex-hook",
    "ctx-install-hooks",
    "ctx-lifecycle",
    "ctx-mcp-add",
    "ctx-mcp-enrich",
    "ctx-mcp-fetch",
    "ctx-mcp-ingest",
    "ctx-mcp-install",
    "ctx-mcp-quality",
    "ctx-mcp-rebuild-index",
    "ctx-mcp-uninstall",
    "ctx-monitor",
    "ctx-pack-compact",
    "ctx-recommend",
    "ctx-skill-add",
    "ctx-skill-health",
    "ctx-skill-install",
    "ctx-skill-mirror",
    "ctx-skill-quality",
    "ctx-skill-unload",
    "ctx-skillspector-audit",
    "ctx-skillspector-remediation",
    "ctx-skillspector-scan",
    "ctx-tag-backfill",
    "ctx-toolbox",
    "ctx-wiki-graphify",
    "ctx-wiki-worker",
)

#: Occurrences of a retired name that are deliberately kept. Keyed by
#: ``(repo-relative posix path, retired name)``, valued as ``(count, reason)``.
#: Every one is a value that travels over a wire or sits in an on-disk record
#: written by an earlier version -- rewriting it would change observable
#: protocol or split a user's existing history across two spellings. None of
#: them is a command a user or an agent is told to run.
#:
#: The count is what keeps this narrow. A blanket file skip would let the next
#: dead command hide inside an already-excused file; pinning the exact number
#: of occurrences means adding one fails the guardrail even here.
_RETIRED_NAME_EXCEPTIONS: dict[tuple[str, str], tuple[int, str]] = {
    ("src/mcp_sources/base.py", "ctx-mcp-fetch"): (
        1,
        "Default HTTP User-Agent sent to third-party MCP registries. It "
        "identifies the fetcher on the wire; it is not a command, and changing "
        "it changes what remote hosts log and rate-limit on.",
    ),
    ("src/ctx/monitor/services/audit.py", "ctx-monitor"): (
        3,
        "``meta['via']`` provenance value on audit rows. Audit logs are "
        "append-only and already contain this spelling on users' disks; "
        "renaming it would split one actor's history across two labels.",
    ),
    ("src/ctx/monitor/services/manifest.py", "ctx-monitor"): (
        1,
        "``source`` field of loaded-entity manifest rows written by earlier "
        "versions. The value is matched on read, so it is a data format, not "
        "an instruction.",
    ),
    ("src/ctx/monitor/services/manifest.py", "ctx-harness-install"): (
        1,
        "``source`` field synthesised for harness rows so they match the "
        "harness records earlier versions wrote under this exact string.",
    ),
    ("src/ctx/monitor/services/wiki.py", "ctx-monitor"): (
        2,
        "``source`` provenance stamped on wiki-queue jobs and persisted in the "
        "queue file; workers filter on the historical value.",
    ),
    ("src/ctx/adapters/claude_code/install/install_utils.py", "ctx-skill-install"): (
        1,
        "Documents the ``source`` values the install manifest stores. The "
        "docstring has to spell the data exactly as it is written on disk.",
    ),
    ("src/ctx/adapters/claude_code/install/install_utils.py", "ctx-agent-install"): (
        1,
        "Documents the ``source`` values the install manifest stores. The "
        "docstring has to spell the data exactly as it is written on disk.",
    ),
    ("src/ctx/adapters/claude_code/install/install_utils.py", "ctx-mcp-install"): (
        1,
        "Documents the ``source`` values the install manifest stores. The "
        "docstring has to spell the data exactly as it is written on disk.",
    ),
    ("src/ctx/adapters/claude_code/install/skill_install.py", "ctx-skill-install"): (
        2,
        "``source`` recorded on install/uninstall manifest rows. Changing it "
        "would orphan every row this CLI has already written.",
    ),
    ("src/ctx/adapters/claude_code/install/agent_install.py", "ctx-agent-install"): (
        2,
        "``source`` recorded on install/uninstall manifest rows. Changing it "
        "would orphan every row this CLI has already written.",
    ),
    ("src/ctx/adapters/claude_code/install/mcp_install.py", "ctx-mcp-install"): (
        2,
        "``source`` recorded on install manifest rows. Changing it would "
        "orphan every row this CLI has already written.",
    ),
    ("src/ctx/adapters/claude_code/install/mcp_install.py", "ctx-mcp-uninstall"): (
        1,
        "``source`` recorded on uninstall manifest rows, paired with the install spelling above.",
    ),
    ("docs/dashboard.md", "ctx-monitor"): (
        1,
        "Documents the audit rows' ``meta.via`` value, which the dashboard "
        "still writes under its historical spelling (see the "
        "monitor/services/audit.py entry above). The doc has to name the data "
        "as it is actually stored.",
    ),
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _load_pyproject() -> dict:
    try:
        import tomllib  # py 3.11+
    except ImportError:  # pragma: no cover - py3.10 fallback
        import tomli as tomllib  # type: ignore[no-redef]

    with open(_repo_root() / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def _live_console_scripts() -> frozenset[str]:
    return frozenset(_load_pyproject()["project"]["scripts"])


#: Matches a retired name only as a whole token, so ``ctx-monitor-docs-cache``
#: (a cache key) and ``ctx-monitor.log`` do not read as ``ctx-monitor``.
_RETIRED_TOKEN_RE = re.compile(
    r"(?<![\w./-])("
    + "|".join(re.escape(name) for name in _RETIRED_CONSOLE_SCRIPTS)
    + r")(?![\w-])"
)

#: A backslash escape inside a shipped string. The dashboard pages embed
#: JavaScript in Python strings, so a command can begin right after a ``\n``;
#: the ``n`` would otherwise satisfy the ``\w`` lookbehind above and hide the
#: command. Blanking escapes before matching restores the token boundary --
#: three live ``ctx-harness-install`` / ``ctx-monitor`` invocations in the
#: Harness Setup page's copy-paste box were found this way.
_STRING_ESCAPE_RE = re.compile(r"\\[a-zA-Z]")

#: A fenced line's first word, once a leading shell prompt and any backticks
#: are stripped. Only a bare ``ctx-...`` token counts: ``ctx-wiki:`` is a YAML
#: key, not a command, and the trailing colon keeps it out.
_COMMAND_WORD_RE = re.compile(r"^ctx-[a-z0-9-]+$")

#: Shell operators after which a new command begins. Splitting on these is what
#: makes the second half of a pipeline a checked command position too.
_SHELL_SEPARATOR_RE = re.compile(r"\|\||&&|[|;]")


def _shipped_source_files() -> list[Path]:
    """Every .py file that ships in the wheel (src/, minus the test suite)."""
    src = _repo_root() / "src"
    return sorted(p for p in src.rglob("*.py") if "tests" not in p.relative_to(src).parts)


def _rel(path: Path) -> str:
    return path.relative_to(_repo_root()).as_posix()


def test_retired_console_scripts_are_really_gone() -> None:
    """The pinned retired list must not contradict pyproject.

    Without this, re-adding a console script would leave the guardrail below
    rejecting the very command the product now ships.
    """
    resurrected = sorted(set(_RETIRED_CONSOLE_SCRIPTS) & _live_console_scripts())
    assert not resurrected, (
        "these names are in [project.scripts] but still listed as retired in "
        f"_RETIRED_CONSOLE_SCRIPTS: {resurrected}"
    )


def test_doc_command_blocks_only_invoke_live_console_scripts() -> None:
    """No fenced doc command invokes a console script that no longer exists.

    Fenced blocks are what users copy-paste. Prose can describe a retired
    command in past tense; a command block cannot, because running it is the
    whole point. Every command position on the line is checked, not just the
    first -- ``ctx-mcp-fetch ... | ctx-mcp-add --from-stdin`` hides a second
    invocation behind the pipe.
    """
    root = _repo_root()
    live = _live_console_scripts()
    docs = sorted((root / "docs").rglob("*.md")) + [root / "README.md"]

    offenders: list[str] = []
    for path in docs:
        in_fence = False
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if raw.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence:
                continue
            stripped = raw.strip()
            for segment in _SHELL_SEPARATOR_RE.split(stripped):
                segment = segment.strip()
                if segment.startswith("$ "):
                    segment = segment[2:].lstrip()
                words = segment.split()
                if not words:
                    continue
                first = words[0].strip("`")
                if _COMMAND_WORD_RE.match(first) and first not in live:
                    offenders.append(f"{_rel(path)}:{lineno}: {stripped}")

    assert not offenders, (
        f"{len(offenders)} copy-pasteable doc command(s) invoke a console script that is not "
        "in [project.scripts]. Rewrite each to its `python -m ...` form:\n" + "\n".join(offenders)
    )


def test_docs_do_not_name_retired_console_scripts_anywhere() -> None:
    """The check above only looks at command position; this one looks everywhere.

    A dead command also hides as a *value*: ``"install_command":
    "ctx-skill-install <slug>"`` in a JSON sample, ``mcp: ctx-mcp-add`` in a
    YAML registry entry. The first word of those lines is a key, so the
    command-position check reads right past them. Matching the retired names
    exactly (rather than any ``ctx-*`` token) keeps this quiet about the CSS
    classes, skill slugs, and MCP names that legitimately share the prefix.
    """
    root = _repo_root()
    offenders: list[str] = []
    excused: dict[tuple[str, str], int] = {}

    for path in sorted((root / "docs").rglob("*.md")) + [root / "README.md"]:
        rel = _rel(path)
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in _RETIRED_TOKEN_RE.findall(raw):
                key = (rel, name)
                if key in _RETIRED_NAME_EXCEPTIONS:
                    excused[key] = excused.get(key, 0) + 1
                    continue
                offenders.append(f"{rel}:{lineno}: [{name}] {raw.strip()[:120]}")

    assert not offenders, (
        f"{len(offenders)} doc reference(s) name a retired console script:\n" + "\n".join(offenders)
    )

    drifted = sorted(
        f"{path} [{name}]: excused {expected}, found {excused[(path, name)]}"
        for (path, name), (expected, _reason) in _RETIRED_NAME_EXCEPTIONS.items()
        if path.endswith(".md") and excused.get((path, name), 0) != expected
    )
    assert not drifted, "_RETIRED_NAME_EXCEPTIONS is out of date:\n" + "\n".join(drifted)


def test_shipped_strings_do_not_name_retired_console_scripts() -> None:
    """No shipped runtime string tells a user or an agent to run a dead command.

    This covers the strings that reach a human (``ctx-init``'s closing "Next
    steps", dashboard HTML) and the ones that reach a model (the Claude Code
    PostToolUse hook payload, ctx-core tool-error text) alike -- both are
    instructions someone will act on.
    """
    offenders: list[str] = []
    excused: dict[tuple[str, str], int] = {}

    for path in _shipped_source_files():
        rel = _rel(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            scannable = _STRING_ESCAPE_RE.sub(" ", node.value)
            for name in _RETIRED_TOKEN_RE.findall(scannable):
                key = (rel, name)
                if key in _RETIRED_NAME_EXCEPTIONS:
                    excused[key] = excused.get(key, 0) + 1
                    continue
                excerpt = " ".join(node.value.split())[:120]
                offenders.append(f"{rel}:{node.lineno}: [{name}] {excerpt}")

    assert not offenders, (
        f"{len(offenders)} shipped string(s) name a retired console script. Rewrite each to its "
        "`python -m ...` form, or add a justified entry to _RETIRED_NAME_EXCEPTIONS:\n"
        + "\n".join(offenders)
    )

    drifted = sorted(
        f"{path} [{name}]: excused {expected}, found {excused.get((path, name), 0)}"
        for (path, name), (expected, _reason) in _RETIRED_NAME_EXCEPTIONS.items()
        if not path.endswith(".md") and excused.get((path, name), 0) != expected
    )
    assert not drifted, (
        "_RETIRED_NAME_EXCEPTIONS is out of date. A count that grew means a new dead command "
        "slipped into an excused file; a count that shrank means the entry can go:\n"
        + "\n".join(drifted)
    )


def test_argparse_prog_values_are_runnable() -> None:
    """Every ``prog=`` names something you can actually type.

    argparse echoes ``prog`` in ``usage:`` and in every error message, so a
    stale value hands the user a command that does not exist at the exact
    moment they are looking for the right one.
    """
    live = _live_console_scripts()
    offenders: list[str] = []

    for path in _shipped_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "prog":
                    continue
                if not (isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)):
                    continue
                value = kw.value.value
                head = value.split()[0] if value.split() else ""
                if head.startswith("ctx-") and head not in live:
                    offenders.append(f"{_rel(path)}:{kw.value.lineno}: prog={value!r}")

    assert not offenders, (
        f"{len(offenders)} argparse parser(s) advertise a prog= that is not an installed console "
        "script. Set prog to the `python -m ...` invocation the docs point at:\n"
        + "\n".join(offenders)
    )
