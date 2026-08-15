"""The user-visible surfaces must describe the product that actually ships.

Every assertion here was written against a falsehood that was live in this
repository: the PyPI blurb described a product the pivot replaced, twenty-nine
documentation pages never named `ctx fit`, and the README described `--pr` as a
command that printed text when it opens a pull request.

The rule these tests encode is narrow on purpose. They do not check that prose
reads well -- they check that a specific claim a user can act on is still the
claim the code makes. Where a claim is about behaviour, the test reads the
behaviour (argparse help, the argv sequence in `apply.py`) rather than a second
copy of the prose, so the doc cannot drift while the test stays green.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
DOCS = REPO_ROOT / "docs"
MKDOCS = REPO_ROOT / "mkdocs.yml"
CLI_FIT = REPO_ROOT / "src" / "ctx" / "cli" / "fit.py"

#: The two surfaces that describe `--apply` and `--pr` to a user about to run them.
WRITE_SURFACES = ("README.md", "docs/index.md")


class _NavLoader(yaml.SafeLoader):
    """mkdocs.yml carries `!!python/name:` tags SafeLoader refuses to parse."""


_NavLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/name:",
    lambda loader, suffix, node: None,
)


def _nav_pages() -> list[Path]:
    config = yaml.load(MKDOCS.read_text(encoding="utf-8"), Loader=_NavLoader)
    docs_dir = REPO_ROOT / config.get("docs_dir", "docs")

    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, str) and node.endswith(".md"):
            found.append(node)

    walk(config["nav"])
    return [docs_dir / path for path in dict.fromkeys(found)]


def _pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _fit_argparse_help() -> str:
    """The real `ctx fit --help` text, built from the real parser.

    Importing the parser rather than shelling out keeps this usable in the
    dev-only CI environment, where the harness extra is absent.
    """

    import argparse

    from ctx.cli import fit as fit_cli

    parser = argparse.ArgumentParser(prog="ctx")
    subparsers = parser.add_subparsers(dest="command")
    fit_cli.register(subparsers)
    fit_parser: argparse.ArgumentParser = subparsers.choices["fit"]
    return fit_parser.format_help()


def _flat(text: str) -> str:
    """Collapse wrapping so a phrase assertion is not a line-width assertion."""

    return re.sub(r"\s+", " ", text)


def _write_section(surface: str) -> tuple[str, str]:
    """Split the `--apply` / `--pr` section of a surface into its two halves.

    Scoping every assertion below to the half it is about is what stops a true
    sentence about one command from satisfying a claim about the other. Both
    halves come back whitespace-collapsed: these pages are hard-wrapped, and a
    sentence that straddles a line break is the same sentence.
    """

    text = (REPO_ROOT / surface).read_text(encoding="utf-8")
    start = text.index("### ")
    while "--apply" not in text[start : text.index("\n", start)]:
        start = text.index("### ", start + 1)
    end = text.index("\n## ", start)
    section = text[start:end]

    pr_marker = "--pr` writes to a remote"
    split_at = section.index(pr_marker)
    return _flat(section[:split_at]), _flat(section[split_at:])


def _handle_apply_source() -> str:
    """The body of the CLI handler that prints the post-write advice."""

    source = CLI_FIT.read_text(encoding="utf-8")
    body = source.split("def _handle_apply(", 1)[1]
    return body.split("\ndef ", 1)[0]


# ---------------------------------------------------------------------------
# The single line most people read about this project.
# ---------------------------------------------------------------------------


def test_pypi_description_names_the_shipped_product() -> None:
    """`description` is the PyPI summary; it described the pre-pivot product.

    The exact string it used to hold is asserted absent, because a partial
    rewrite that kept "recommendation layer" as the headline claim would be the
    same defect with new words.
    """

    description = _pyproject()["project"]["description"]

    assert "CTX Fit" in description, (
        "the PyPI summary does not name the product; it is the one line most "
        f"users will ever read about this project. Got: {description!r}"
    )
    assert "Amazon-style catalog" not in description
    assert "recommendation layer" not in description


def test_mkdocs_site_description_names_the_shipped_product() -> None:
    """`site_description` is the docs-site meta description and search snippet."""

    config = yaml.load(MKDOCS.read_text(encoding="utf-8"), Loader=_NavLoader)
    description = config["site_description"]

    assert "CTX Fit" in description, f"docs site description omits the product: {description!r}"
    assert "recommendation layer" not in description


# ---------------------------------------------------------------------------
# The docs site had 29 nav pages and none of them named the product.
# ---------------------------------------------------------------------------


def test_docs_front_door_leads_with_ctx_fit() -> None:
    """docs/index.md is the site's home page and led with the old product."""

    text = (DOCS / "index.md").read_text(encoding="utf-8")
    heading = next(line for line in text.splitlines() if line.startswith("# "))

    assert "CTX Fit" in heading, f"docs home page headline is not about the product: {heading!r}"

    lead = text.split("## ", 1)[0]
    assert "ctx fit" in lead.lower(), "the docs home page never names the command"


def test_release_front_doors_describe_1_0_21_without_old_install_claims() -> None:
    """The release docs must not send a 1.0.21 user back to source or 1.0.20."""

    version = _pyproject()["project"]["version"]
    assert version == "1.0.21"

    for surface in (README, DOCS / "index.md"):
        text = _flat(surface.read_text(encoding="utf-8"))
        assert "CTX Fit is not released yet" not in text
        assert "not yet from PyPI" not in text
        assert "1.0.20 installs" not in text
        assert "contains none of CTX Fit" not in text
        assert "pip install --upgrade claude-ctx" in text
        assert "one coding-agent harness" in text
        assert "selected test" in text
        assert "verification authority" in text
        assert "does not prove" in text
        assert "already available in the repository" in text
        assert "isolated home" in text.lower()
        assert "without network access" in text
        assert "live-provider trial" in text

    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [1.0.21] - 2026-08-14" in changelog
    assert "compare/v1.0.20...v1.0.21" in changelog
    assert "[1.0.21]: https://github.com/stevesolun/ctx/releases/tag/v1.0.21" in changelog


def test_section_landing_pages_point_at_the_product() -> None:
    """A reader landing mid-site from a search result must not be stranded.

    These five pages are the entry points for the pre-pivot recommendation
    surface. Each one has to say what it is relative to CTX Fit, because on its
    own it reads as though it were the whole project.
    """

    landing_pages = (
        "catalog.md",
        "knowledge-graph.md",
        "entity-onboarding.md",
        "dashboard.md",
        "skill-router/index.md",
        "toolbox/index.md",
    )

    silent = [
        page
        for page in landing_pages
        if "ctx fit" not in (DOCS / page).read_text(encoding="utf-8").lower()
    ]
    assert silent == [], f"nav landing pages that never mention the product: {silent}"


def test_some_nav_page_documents_the_product() -> None:
    """The regression in one line: zero of the nav pages named `ctx fit`."""

    naming = [
        page for page in _nav_pages() if "ctx fit" in page.read_text(encoding="utf-8").lower()
    ]
    assert naming, "no page in the mkdocs nav mentions `ctx fit`"


# ---------------------------------------------------------------------------
# `--apply` and `--pr` write different things, and the docs said otherwise.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", WRITE_SURFACES)
def test_pr_documented_as_writing_to_a_remote(surface: str) -> None:
    """`--pr` pushes and opens a PR; both surfaces said it only printed text.

    The stale wording is asserted absent as well as the new wording present: the
    danger of this particular claim is a user believing nothing leaves their
    machine.
    """

    text = (REPO_ROOT / surface).read_text(encoding="utf-8")
    marker = "--pr` writes to a remote"
    assert marker in text, (
        f"{surface} does not state that `--pr` writes to a remote. It creates a "
        "branch, commits, pushes and opens a pull request, and a reader must not "
        "be able to believe nothing leaves their machine."
    )
    pr_section = text[text.index(marker) :][:2000]

    assert "git push" in pr_section, f"{surface} does not say that `--pr` pushes"
    assert "gh pr create" in pr_section, f"{surface} does not name the command that opens the PR"

    assert "creates no branch" not in text, f"{surface} still claims `--pr` creates no branch"
    assert "commits nothing" not in text, f"{surface} still claims `--pr` commits nothing"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("git", *args), cwd=repo, capture_output=True, text=True, check=False)


def _observed_apply_outcomes(workspace: Path) -> dict[str, dict[str, object]]:
    """Run the real planner and writer, and record what git can see afterwards.

    The documentation assertions below are written against this measurement
    rather than against a remembered fact. `--apply` writes the CTX-owned
    `.ctx/fit-configuration.json` sidecar, whose action depends on whether an
    owned manifest already exists. A `create` lands untracked, where `git diff`
    and `git checkout` -- the pair the CLI still prints unconditionally -- do
    nothing and fail respectively. If `--apply` ever starts staging what it
    writes, this dictionary changes and the doc assertions fail rather than
    quietly describing the old behaviour.
    """

    from ctx.fit.apply import apply_plan, plan_apply
    from ctx.fit.candidates import CapabilityMaterial, CandidateConfiguration
    from ctx.fit.recommend import RankedCandidate, Recommendation

    capability_id = "skill:ctx-python-testing"

    def candidate(body: str) -> CandidateConfiguration:
        material = CapabilityMaterial.from_content(
            capability_id=capability_id,
            delivery_mode="task-user-context",
            source_identity=f"package:ctx.assets/runtime-availability.json#{capability_id}",
            catalog_entry_digest=hashlib.sha256(b"surface-truth-catalog-entry").hexdigest(),
            content=body,
        )
        return CandidateConfiguration(
            candidate_id="lean",
            role="recommended",
            capability_ids=(capability_id,),
            model="openai/test-model",
            instructions=(),
            selection_reason="the single highest-ranked capability",
            capability_materials=(material,),
        )

    def recommendation() -> Recommendation:
        return Recommendation(
            schema="ctx.fit.recommendation-v1",
            verdict="recommend-change",
            winner_id="lean",
            ranked=(
                RankedCandidate(
                    candidate_id="lean",
                    reliability=1.0,
                    verified=9,
                    scored=9,
                    total_cost_usd=0.45,
                    capability_count=1,
                    qualified=True,
                ),
            ),
            reasoning=("lean verified 9/9",),
            limitations=("only 3 tasks were evaluated.",),
            confidence="medium",
        )

    winning_candidate = candidate("# Python testing\n\nRun the repository's own tests.\n")

    observed: dict[str, dict[str, object]] = {}
    for name, preexisting in (("without", False), ("with", True)):
        repo = workspace / name
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "surface-truth@example.invalid")
        _git(repo, "config", "user.name", "surface truth")
        (repo / "README.md").write_text("demo\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")

        if preexisting:
            previous_candidate = candidate("# Python testing\n\nUse pytest.\n")
            previous_plan = plan_apply(
                recommendation(), (previous_candidate,), repo_path=repo, run_id="previous"
            )
            assert previous_plan.can_apply, previous_plan.explanation
            apply_plan(previous_plan, repo)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "existing CTX Fit configuration")

        plan = plan_apply(recommendation(), (winning_candidate,), repo_path=repo, run_id="test")
        assert plan.can_apply, plan.explanation
        (artifact,) = plan.artifacts
        apply_plan(plan, repo)

        # Order matters: `git checkout` restores a tracked file, so measuring
        # the review commands after it would report an empty diff for the
        # `modify` case too and hide the difference this test exists to pin.
        shows_in_diff = bool(_git(repo, "diff").stdout.strip())
        shows_in_status = (
            artifact.path in _git(repo, "status", "--short", "--untracked-files=all").stdout
        )
        checkout = _git(repo, "checkout", "--", artifact.path)
        observed[artifact.action] = {
            "path": artifact.path,
            "git_diff_shows_it": shows_in_diff,
            "git_checkout_undoes_it": checkout.returncode == 0,
            "git_status_shows_it": shows_in_status,
            "survives_git_checkout": (repo / artifact.path).exists(),
        }
    return observed


def test_apply_writes_a_created_file_that_git_checkout_cannot_undo(tmp_path: Path) -> None:
    """The measurement the documentation assertions depend on.

    Separated out so a failure here reads as "the behaviour moved" rather than
    as "the prose is wrong", which are different repairs.
    """

    observed = _observed_apply_outcomes(tmp_path)

    assert set(observed) == {"create", "modify"}, (
        f"`--apply` no longer produces both a create and a modify: {observed}"
    )
    assert observed["create"] == {
        "path": ".ctx/fit-configuration.json",
        "git_diff_shows_it": False,
        "git_checkout_undoes_it": False,
        "git_status_shows_it": True,
        "survives_git_checkout": True,
    }, f"the untracked-create case changed: {observed['create']}"
    assert observed["modify"]["git_diff_shows_it"] is True
    assert observed["modify"]["git_checkout_undoes_it"] is True


@pytest.mark.parametrize("surface", WRITE_SURFACES)
def test_documented_undo_for_apply_matches_the_action_it_documents(
    surface: str, tmp_path: Path
) -> None:
    """The undo a surface prescribes has to be the undo that works.

    A newly created sidecar is untracked, so plain `git diff` does not show it
    and version control cannot restore it. An existing tracked sidecar is a
    normal modification and can be reviewed and restored through git.
    """

    observed = _observed_apply_outcomes(tmp_path)
    apply_half, _ = _write_section(surface)

    for action in sorted(observed):
        assert f"{action}: .ctx/fit-configuration.json" in apply_half, (
            f"{surface} does not distinguish the `{action}` case, but `--apply` "
            f"produces it and it is undone differently: {observed[action]}"
        )

    assert "untracked" in apply_half, (
        f"{surface} does not say that a created sidecar is untracked, which is "
        "the whole reason `git checkout` cannot discard it"
    )
    assert "git status" in apply_half, (
        f"{surface} names no command that shows an untracked create; `git diff` does not"
    )
    assert "cannot recover an untracked file" in apply_half, (
        f"{surface} does not tell the reader that version control cannot restore "
        "an untracked sidecar"
    )


@pytest.mark.parametrize("surface", WRITE_SURFACES)
def test_apply_no_git_claim_is_scoped_to_the_write(surface: str) -> None:
    """`--apply` stays local, but the command line that reaches it runs git.

    Bare `ctx fit --apply` is refused for want of evidence; the invocation that
    is not refused is `ctx fit --test --budget N --apply`, and deriving its
    tasks runs read-only `log`/`show`/`ls-tree`/`rev-parse` queries. Claiming
    "no git command at all" of the whole command is the smaller falsehood that
    replaced a larger one.
    """

    apply_half, _ = _write_section(surface)

    assert "write itself runs no git command" in apply_half, (
        f"{surface} no longer scopes the no-git claim to the write step:\n{apply_half}"
    )
    assert "no git command at all" not in apply_half, (
        f"{surface} claims `--apply` runs no git command at all; the evaluation "
        "it requires first does"
    )
    assert "--test --budget" in apply_half, (
        f"{surface} does not tell the reader that `--apply` needs evidence from "
        "`--test --budget N`, which is where the git reads happen"
    )


def test_docs_qualify_the_undo_line_that_ctx_fit_prints_after_a_write() -> None:
    """A reader who trusts the tool's own closing line is stuck on a create.

    The claim this pins is about `src/ctx/cli/fit.py`, which this lane does not
    own, so it is asserted against that file rather than restated. If the CLI
    starts branching on the artifact action, the caveat becomes wrong and this
    test turns red so it is deleted rather than left to rot.
    """

    handler = _handle_apply_source()
    unconditional = "git checkout" in handler and "untracked" not in handler

    assert unconditional, "the CLI now qualifies its undo advice; remove the docs caveat test"
    for surface in WRITE_SURFACES:
        apply_half, _ = _write_section(surface)
        assert "cannot recover an untracked file" in apply_half, (
            f"{surface} does not qualify the unconditional `git checkout` advice "
            "for a newly created sidecar"
        )


@pytest.mark.parametrize("surface", WRITE_SURFACES)
def test_pr_gate_probes_are_not_claimed_away(surface: str) -> None:
    """ "Exactly those commands and nothing else" was false by six subprocesses.

    `plan_pull_request` runs five read-only git probes and `gh auth status`
    before the announced sequence. The implementation is careful to say so; the
    documentation must not undo that precision.
    """

    _, pr_half = _write_section(surface)

    assert "and nothing else" not in pr_half, (
        f"{surface} claims `--pr` runs exactly the announced commands and nothing "
        "else; the gate ahead of them runs read-only probes"
    )
    assert "read-only probes" in pr_half, (
        f"{surface} does not mention the probes the `--pr` gate runs:\n{pr_half}"
    )
    assert "gh auth status" in pr_half, f"{surface} does not name the `gh` probe"


def test_documented_pr_command_sequence_matches_the_code() -> None:
    """The README prints the argv sequence; `apply.py` is where it comes from.

    Asserting against the source of the commands rather than a second copy of
    the list is what keeps this test load-bearing: if the implementation stops
    pushing, or starts merging, this fails without anyone touching the README.
    """

    apply_source = (REPO_ROOT / "src" / "ctx" / "fit" / "apply.py").read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")

    for fragment, documented in (
        ('("git", "checkout", "-b"', "git checkout -b"),
        ('("git", "commit", "-m"', "git commit -m"),
        ('("git", "push", "--set-upstream"', "git push --set-upstream"),
        ('("gh", "pr", "create"', "gh pr create"),
    ):
        assert fragment in apply_source, f"{fragment} is no longer how `--pr` works"
        assert documented in readme, f"README omits the `{documented}` step of `--pr`"

    assert "merge" not in apply_source.split("def plan_pull_request", 1)[1].split("\n\ndef ", 1)[0]


def test_fit_help_and_docs_agree_that_pr_needs_gh() -> None:
    """A refusal the user will hit: `gh` absent or logged out."""

    help_text = _fit_argparse_help()
    assert "gh" in help_text, "`ctx fit --help` no longer mentions `gh`"

    for surface in (README, DOCS / "index.md"):
        text = surface.read_text(encoding="utf-8")
        missing = [phrase for phrase in ("not installed", "not logged in") if phrase not in text]
        assert missing == [], (
            f"{surface.name} does not tell the user what `--pr` needs from `gh`; missing: {missing}"
        )


# ---------------------------------------------------------------------------
# Read-only claims. These are the ones a user relies on before running anything.
# ---------------------------------------------------------------------------


def test_fit_package_never_touches_the_user_home_directory() -> None:
    """The dashboard and docs both claim `ctx fit` writes nothing to `~/.claude`.

    The sandbox and live runner resolve the ambient home only to deny untrusted
    trial code access to it and to prevent an executable shim there from
    widening the readable runtime roots. The `.claude/...` string literals in
    `profile.py` are repository-relative reads of the *analyzed* repository and
    are excluded by requiring a home lookup.
    """

    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "src" / "ctx" / "fit").glob("*.py"))
        if re.search(r"Path\.home\(\)|expanduser|os\.environ\[.HOME.\]", path.read_text("utf-8"))
    ]
    assert offenders == ["src/ctx/fit/live_runner.py", "src/ctx/fit/sandbox.py"], (
        f"home lookups are permitted only at the trial deny boundaries; found: {offenders}"
    )
    live_runner = (REPO_ROOT / "src/ctx/fit/live_runner.py").read_text(encoding="utf-8")
    sandbox = (REPO_ROOT / "src/ctx/fit/sandbox.py").read_text(encoding="utf-8")
    assert "private_roots = (Path.home().resolve()" in live_runner
    assert "private_roots = (_real(Path.home())" in sandbox
    assert '"--tmpfs"' in sandbox
    assert "(deny file-read*" in sandbox


def test_dashboard_home_says_it_holds_no_fit_state() -> None:
    """The shipped dashboard showed the old surface with no scope statement."""

    from ctx.monitor.pages import home as home_page

    rendered = home_page.render_home(
        manifest={"load": []},
        sessions=[],
        wiki_stats={
            "skills": 0,
            "agents": 0,
            "mcps": 0,
            "harnesses": 0,
            "total": 0,
            "split_known": True,
        },
        graph_stats={"nodes": 0, "edges": 0},
        runtime_summary={
            "validations_total": 0,
            "validation_failures": 0,
            "open_escalations_total": 0,
        },
        audit_lines=0,
        recent_audit=[],
        layout=lambda _title, body: body,
        format_count=lambda n: str(n),
    )

    assert "ctx fit" in rendered, "the dashboard never tells the reader what it is not showing"
    assert "~/.claude" in rendered


def test_readme_does_not_promise_a_harness_dry_run_without_a_slug() -> None:
    """`python -m harness_install --dry-run` alone exits 2; the README sold it."""

    readme = README.read_text(encoding="utf-8")
    assert "python -m harness_install --dry-run" not in readme, (
        "the README documents a harness_install invocation that argparse rejects: "
        "`identifier is required unless --recommend is used`"
    )


def test_readme_cli_table_does_not_file_bare_ctx_under_recommendations() -> None:
    """Bare `ctx` runs the Fit profile, not the recommendation surface."""

    readme = README.read_text(encoding="utf-8")
    assert "## CLI Reference" in readme
    table = readme[readme.index("## CLI Reference") :]
    row = next(
        (line for line in table.splitlines() if line.startswith("| Profile a repository")),
        "",
    )

    assert row, (
        "the CLI table has no row for profiling a repository. Bare `ctx` runs "
        "`ctx fit`; the table used to file it under the recommendation surface "
        "and send readers to the entity-onboarding guide."
    )
    assert "`ctx`" in row and "ctx fit" in row, (
        f"the CLI table no longer says that bare `ctx` is `ctx fit`: {row!r}"
    )
