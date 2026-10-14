"""The distributed bootstrap contains infrastructure, never a tool catalog."""

import json
from importlib import resources

import pytest

import ctx_init
from ctx.core.graph.graph_store import graph_store_stats
from ctx.core.source_registry import BUILTIN_EXTERNAL_SOURCES
from ctx.core.wiki.wiki_query import load_all_pages
from ctx.fit.release_catalog import open_release_candidate_source


def test_default_graph_bootstrap_is_empty_offline_and_idempotent(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("empty bootstrap must not download or spawn a tool")

    monkeypatch.setattr(ctx_init.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(ctx_init.subprocess, "run", forbidden)
    assert ctx_init.build_graph(tmp_path) == 0
    wiki = tmp_path / "skill-wiki"
    assert (wiki / "SCHEMA.md").is_file()
    assert load_all_pages(wiki) == []
    assert graph_store_stats(wiki / "graphify-out" / "graph-store.sqlite3") == {
        "nodes": 0,
        "edges": 0,
    }
    user_page = wiki / "entities" / "skills" / "my-skill.md"
    user_page.write_text("---\ntitle: My Skill\ntype: skill\n---\nUser-owned content\n")
    before = {p: p.read_bytes() for p in wiki.rglob("*") if p.is_file()}
    assert ctx_init.build_graph(tmp_path, force=True) == 0
    assert all(path.read_bytes() == content for path, content in before.items())


def test_distributed_package_has_no_catalog_or_executable_material():
    assets = resources.files("ctx.assets")
    for name in (
        "runtime-availability.json",
        "benefit-eligible-catalog-v1.json",
        "release-query-catalog-root-v1.json",
        "release-load-skill-material-v1.json",
        "release-install-skill-material-v1.json",
        "license-evidence.json",
    ):
        assert not (assets / name).is_file(), name
    assert open_release_candidate_source() is None
    assert BUILTIN_EXTERNAL_SOURCES == ()


def test_default_config_is_user_owned():
    config = json.loads((resources.files("ctx") / "config.json").read_text())
    assert config["knowledge"]["mode"] == "local"


def test_fit_abstains_without_a_catalog_and_does_not_spend(tmp_path):
    from ctx.fit.experiment import resolve_experiment
    from ctx.fit.profile import build_fit_profile

    (tmp_path / "pyproject.toml").write_text("[project]\nname='empty-fixture'\nversion='0.1'\n")
    experiment = resolve_experiment(build_fit_profile(tmp_path))
    assert experiment.plan.executions == 0
    assert experiment.candidates.abstained is True


def test_bootstrap_rejects_symlinked_wiki_without_touching_target(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ctx_init.urllib.request,
        "urlopen",
        lambda *a, **kw: pytest.fail("must reject before any download"),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "home"
    root.mkdir()
    (root / "skill-wiki").symlink_to(outside, target_is_directory=True)
    assert ctx_init.build_graph(root) == 1
    assert list(outside.iterdir()) == []


def test_custom_import_refuses_to_replace_user_knowledge(tmp_path, monkeypatch):
    wiki = tmp_path / "skill-wiki"
    wiki.mkdir()
    page = wiki / "index.md"
    page.write_text("My irreplaceable knowledge\n")
    monkeypatch.setattr(
        ctx_init.urllib.request,
        "urlopen",
        lambda *a, **kw: pytest.fail("refusal must precede download"),
    )
    assert (
        ctx_init.build_graph(
            tmp_path,
            graph_url="https://example.test/user.tar.gz",
            graph_sha256="0" * 64,
            force=True,
        )
        == 1
    )
    assert page.read_text() == "My irreplaceable knowledge\n"


def test_import_rechecks_user_knowledge_after_staging(tmp_path, monkeypatch):
    assert ctx_init.build_graph(tmp_path) == 0
    wiki = tmp_path / "skill-wiki"
    page = wiki / "entities" / "skills" / "new-work.md"

    def extract(*args, **kwargs):
        page.write_text("Work created while import was staging\n")

    monkeypatch.setattr(ctx_init, "_extract_graph_archive_to_dir", extract)
    monkeypatch.setattr(ctx_init, "_validate_graph_install_tree", lambda path: None)
    with pytest.raises(ValueError, match="knowledge changed"):
        ctx_init._extract_graph_archive(
            tmp_path / "archive.tar.gz", wiki, install_mode="full", preserve_existing=True
        )
    assert page.read_text() == "Work created while import was staging\n"


def test_main_creates_empty_workspace_without_installing_or_spawning(tmp_path, monkeypatch):
    from ctx.engine.installation import InstallConsentPolicy

    def forbidden(*args, **kwargs):
        pytest.fail("fresh setup must neither spawn installers nor fetch a catalog")

    monkeypatch.setattr(ctx_init, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ctx_init, "load_current_install_policy", InstallConsentPolicy.safe_default)
    monkeypatch.setattr(ctx_init, "has_persisted_install_policy", lambda: False)
    monkeypatch.setattr(ctx_init.subprocess, "run", forbidden)
    monkeypatch.setattr(ctx_init.urllib.request, "urlopen", forbidden)
    assert ctx_init.main(["--model-mode", "skip"]) == 0
    assert not (tmp_path / "toolboxes.json").exists()
    assert list((tmp_path / "skills").iterdir()) == []
    assert list((tmp_path / "agents").iterdir()) == []
    assert (tmp_path / "skill-wiki" / "SCHEMA.md").is_file()
