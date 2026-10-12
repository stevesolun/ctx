"""Tests for ctx_init — bootstrap ~/.claude/ scaffolding."""

from __future__ import annotations

import builtins
import hashlib
import io
import importlib.resources
import json
import os
import sqlite3
import stat
import sys
import tarfile
import zlib
from pathlib import Path
from types import SimpleNamespace

import networkx as nx
import pytest
import ctx_init as ci
from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox
from ctx.adapters.generic.providers import ToolCall
from ctx.core.graph.graph_packs import write_base_pack
from ctx.core.graph import release_artifacts
from ctx.core.graph.graph_store import validate_graph_store
from ctx.core.install_policy_store import (
    has_persisted_install_policy as has_real_install_policy,
    load_current_install_policy as load_real_install_policy,
    persist_install_policy as persist_real_install_policy,
)
from ctx.core.wiki.wiki_packs import write_wiki_base_pack
from ctx.engine.installation import InstallConsentPolicy


@pytest.fixture(autouse=True)
def _mock_install_policy_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Keep ctx-init policy tests independent from the real user store."""

    state: dict[str, object] = {
        "persisted": False,
        "policy": InstallConsentPolicy.safe_default(),
        "writes": [],
    }

    def load() -> InstallConsentPolicy:
        policy = state["policy"]
        assert isinstance(policy, InstallConsentPolicy)
        return policy

    def persist(policy: InstallConsentPolicy) -> str:
        assert isinstance(policy, InstallConsentPolicy)
        writes = state["writes"]
        assert isinstance(writes, list)
        writes.append(policy)
        state["policy"] = policy
        state["persisted"] = True
        return policy.policy_digest

    monkeypatch.setattr(ci, "load_current_install_policy", load, raising=False)
    monkeypatch.setattr(ci, "persist_install_policy", persist, raising=False)
    monkeypatch.setattr(
        ci,
        "has_persisted_install_policy",
        lambda: state["persisted"],
        raising=False,
    )
    return state


@pytest.fixture
def historical_runtime_pack(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Opt in only for legacy materializer safety tests, never default bootstrap."""
    real_files = importlib.resources.files
    fixture_root = Path(__file__).parent / "fixtures" / "release_catalog"

    def fixture_files(package: str):
        return fixture_root if package == "ctx.assets" else real_files(package)

    monkeypatch.setattr(importlib.resources, "files", fixture_files)
    return ci._load_runtime_availability_pack()


def _write_dashboard_index(path: Path, *, export_id: str = "test-export") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE nodes(id TEXT PRIMARY KEY,label TEXT,type TEXT,tags TEXT,"
            "description TEXT,quality_score REAL,usage_score REAL,degree INTEGER)"
        )
        conn.execute(
            "CREATE TABLE slug_index(slug TEXT,type TEXT,node_id TEXT,"
            "PRIMARY KEY(slug,type,node_id))"
        )
        conn.execute("CREATE TABLE neighbors(source TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        conn.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [
                ("export_id", json.dumps(export_id)),
                ("nodes_count", "1"),
                ("edges_count", "0"),
                ("max_degree", "1"),
                ("top_k", "40"),
            ],
        )
        conn.execute(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
            ("skill:current", "current", "skill", "[]", "", None, None, 0),
        )
        conn.execute("INSERT INTO slug_index VALUES(?,?,?)", ("current", "skill", "skill:current"))
        conn.execute("INSERT INTO neighbors VALUES(?,?)", ("skill:current", zlib.compress(b"[]")))
        conn.commit()
    finally:
        conn.close()


def test_ensure_directories_creates_standard_tree(tmp_path: Path) -> None:
    created = ci.ensure_directories(tmp_path)
    # First call should create every standard subdir.
    assert len(created) == len(ci._STANDARD_SUBDIRS)
    for sub in ci._STANDARD_SUBDIRS:
        assert (tmp_path / sub).is_dir(), f"missing {sub}"


def test_ensure_directories_is_idempotent(tmp_path: Path) -> None:
    first = ci.ensure_directories(tmp_path)
    assert len(first) > 0
    second = ci.ensure_directories(tmp_path)
    assert second == [], "second call should not recreate anything"


def test_seed_user_config_writes_once(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    first = ci.seed_user_config(tmp_path)
    assert first is not None
    assert first.exists()
    body = first.read_text(encoding="utf-8")
    assert "skill-system-config.json" in body

    # Second call returns None (file already exists, force=False).
    second = ci.seed_user_config(tmp_path)
    assert second is None


def test_seed_user_config_respects_force(tmp_path: Path) -> None:
    target = tmp_path / "skill-system-config.json"
    target.write_text("user-custom-content", encoding="utf-8")
    # Without force → don't touch.
    assert ci.seed_user_config(tmp_path, force=False) is None
    assert target.read_text() == "user-custom-content"
    # With force → overwrite.
    result = ci.seed_user_config(tmp_path, force=True)
    assert result == target
    assert "skill-system-config.json" in target.read_text()


def test_main_creates_empty_offline_workspace_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "_stdio_is_interactive", lambda: False)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("default initialization must not download, seed tools, or spawn a command")

    monkeypatch.setattr(ci.subprocess, "run", forbidden)
    monkeypatch.setattr(ci, "_download_graph_archive", forbidden)
    monkeypatch.setattr(ci, "seed_toolboxes", forbidden)
    assert ci.main([]) == 0

    wiki = tmp_path / "skill-wiki"
    graph = json.loads((wiki / "graphify-out" / "graph.json").read_text())
    assert graph["nodes"] == []
    assert graph.get("edges", graph.get("links", [])) == []
    assert (wiki / "index.md").is_file()
    assert (wiki / "SCHEMA.md").is_file()
    assert not list((wiki / "entities").rglob("*.md"))
    assert not list((tmp_path / "skills").rglob("SKILL.md"))
    assert not list((tmp_path / "agents").rglob("*.md"))
    output = capsys.readouterr().out
    assert "[skip] hook injection" in output
    assert "no bundled tools" in output


def test_main_reinitialization_preserves_user_toolboxes_and_knowledge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    assert ci.main(["--model-mode", "skip"]) == 0
    config = tmp_path / "skill-system-config.json"
    config.write_text('{"toolboxes": {"my-tools": {"skills": ["private"]}}}\n')
    page = tmp_path / "skill-wiki" / "entities" / "skills" / "private.md"
    page.write_text("# My private knowledge\n")
    before = {path: path.read_bytes() for path in (config, page)}
    assert ci.main(["--model-mode", "skip"]) == 0
    assert {path: path.read_bytes() for path in before} == before


def test_main_propagates_workspace_failure_and_reports_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "build_graph", lambda *_args, **_kwargs: 11)

    assert ci.main(["--model-mode", "skip"]) == 11
    captured = capsys.readouterr()
    assert "ctx-init: completed with errors (exit 11)" in captured.out
    assert "ctx-init: done" not in captured.out


def test_main_auto_wizard_in_terminal_configures_custom_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        ci,
        "recommend_harnesses",
        lambda goal, top_k=5, model_provider=None, model=None: [],
    )

    answers = iter(
        [
            "y",  # hooks
            "local",  # knowledge mode
            "ask-each-time",  # skill install consent
            "",  # agent install consent: safe default
            "ask-each-time",  # MCP install consent
            "custom",  # model mode
            "openai/gpt-5.5",  # model
            "",  # provider default: openai
            "",  # api key env default: OPENAI_API_KEY
            "",  # base URL
            "build CAD artifacts",
            "linux python",  # runtime / OS
            "supervised",  # autonomy
            "filesystem shell",  # allowed tools
            "pytest ruff",  # verification
            "private repo",  # privacy / network
            "mcp",  # attach mode
            "n",  # validate model
        ]
    )
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))
    calls: list[list[str]] = []

    class _FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd: list[str], **_kwargs: object) -> _FakeResult:
        calls.append(list(cmd))
        return _FakeResult()

    monkeypatch.setattr(ci.subprocess, "run", _fake_run)

    rc = ci.main([])

    assert rc == 0
    assert any("ctx.adapters.claude_code.inject_hooks" in c for c in calls)
    assert not any("ctx.core.wiki.wiki_graphify" in c for c in calls)
    profile = json.loads((tmp_path / "ctx-model-profile.json").read_text())
    assert profile["mode"] == "custom"
    assert profile["provider"] == "openai"
    assert profile["model"] == "openai/gpt-5.5"
    assert profile["api_key_env"] == "OPENAI_API_KEY"
    assert profile["goal"] == "build CAD artifacts"
    assert profile["knowledge_mode"] == "local"
    assert profile["harness_requirements"] == {
        "runtime": "linux python",
        "autonomy": "supervised",
        "tools": "filesystem shell",
        "verification": "pytest ruff",
        "privacy": "private repo",
        "attach_mode": "mcp",
        "api_key_env": "OPENAI_API_KEY",
    }
    user_config = json.loads((tmp_path / "skill-system-config.json").read_text())
    assert user_config["knowledge"]["mode"] == "local"


def test_wizard_flag_prompts_without_tty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "_stdio_is_interactive", lambda: False)
    monkeypatch.setattr(
        ci,
        "recommend_harnesses",
        lambda goal, top_k=5, model_provider=None, model=None: [],
    )

    answers = iter(
        [
            "n",  # hooks
            "local",  # knowledge mode
            "",  # skill install consent: safe default
            "",  # agent install consent: safe default
            "",  # MCP install consent: safe default
            "claude-code",  # model mode
            "maintain FastAPI services",
        ]
    )
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))

    rc = ci.main(["--wizard"])

    assert rc == 0
    profile = json.loads((tmp_path / "ctx-model-profile.json").read_text())
    assert profile["mode"] == "claude-code"
    assert profile["goal"] == "maintain FastAPI services"
    assert profile["knowledge_mode"] == "local"
    user_config = json.loads((tmp_path / "skill-system-config.json").read_text())
    assert user_config["knowledge"]["mode"] == "local"


def test_explicit_args_do_not_auto_wizard_in_terminal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        builtins,
        "input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("unexpected prompt")),
    )

    assert ci.main(["--model-mode", "skip", "--knowledge-mode", "local"]) == 0
    user_config = json.loads((tmp_path / "skill-system-config.json").read_text())
    assert user_config["knowledge"]["mode"] == "local"


def test_explicit_install_consent_trio_persists_independent_modes_before_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _mock_install_policy_store: dict[str, object],
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    order: list[str] = []

    def persist(policy: InstallConsentPolicy) -> str:
        order.append("policy")
        writes = _mock_install_policy_store["writes"]
        assert isinstance(writes, list)
        writes.append(policy)
        _mock_install_policy_store["policy"] = policy
        _mock_install_policy_store["persisted"] = True
        return policy.policy_digest

    real_build_graph = ci.build_graph

    def record_workspace(claude: Path | None = None) -> int:
        order.append("workspace")
        return real_build_graph(claude)

    monkeypatch.setattr(ci, "persist_install_policy", persist)
    monkeypatch.setattr(ci, "build_graph", record_workspace)

    rc = ci.main(
        [
            "--model-mode",
            "skip",
            "--skill-install-consent",
            "preapproved-auto",
            "--agent-install-consent",
            "ask-each-time",
            "--mcp-install-consent",
            "preapproved-auto",
        ]
    )

    assert rc == 0
    assert order == ["policy", "workspace"]
    policy = _mock_install_policy_store["policy"]
    assert isinstance(policy, InstallConsentPolicy)
    assert policy.skill_mode == "preapproved-auto"
    assert policy.agent_mode == "ask-each-time"
    assert policy.mcp_server_mode == "preapproved-auto"
    output = capsys.readouterr().out
    assert policy.policy_digest in output
    assert "skills=preapproved-auto" in output
    assert "agents=ask-each-time" in output
    assert "mcp-servers=preapproved-auto" in output


def test_ctx_init_install_policy_round_trips_through_real_filesystem_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_root = tmp_path / "install-policy"
    claude_root = tmp_path / "claude"
    monkeypatch.setattr(
        ci,
        "has_persisted_install_policy",
        lambda: has_real_install_policy(policy_root),
    )
    monkeypatch.setattr(
        ci,
        "load_current_install_policy",
        lambda: load_real_install_policy(policy_root),
    )
    monkeypatch.setattr(
        ci,
        "persist_install_policy",
        lambda policy: persist_real_install_policy(policy, policy_root),
    )
    monkeypatch.setattr(ci, "_claude_dir", lambda: claude_root)

    rc = ci.main(
        [
            "--model-mode",
            "skip",
            "--skill-install-consent",
            "preapproved-auto",
            "--agent-install-consent",
            "ask-each-time",
            "--mcp-install-consent",
            "preapproved-auto",
        ]
    )

    loaded = load_real_install_policy(policy_root)
    expected = InstallConsentPolicy(
        skill_mode="preapproved-auto",
        agent_mode="ask-each-time",
        mcp_server_mode="preapproved-auto",
    )
    assert rc == 0
    assert loaded == expected
    assert loaded.policy_digest == expected.policy_digest
    current = policy_root / "current.json"
    snapshot = policy_root / "snapshots" / f"{expected.policy_digest}.json"
    assert current.is_file()
    assert snapshot.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(policy_root.stat().st_mode) == 0o700
        assert stat.S_IMODE((policy_root / "snapshots").stat().st_mode) == 0o700
        assert stat.S_IMODE(current.stat().st_mode) == 0o600
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_partial_noninteractive_install_consent_is_rejected_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ci,
        "ensure_directories",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("side effect")),
    )

    with pytest.raises(SystemExit) as exc_info:
        ci.main(
            [
                "--model-mode",
                "skip",
                "--skill-install-consent",
                "preapproved-auto",
            ]
        )

    assert exc_info.value.code == 2


def test_no_install_consent_flags_preserve_policy_even_with_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _mock_install_policy_store: dict[str, object],
) -> None:
    existing = InstallConsentPolicy(
        skill_mode="preapproved-auto",
        agent_mode="ask-each-time",
        mcp_server_mode="preapproved-auto",
    )
    _mock_install_policy_store["policy"] = existing
    _mock_install_policy_store["persisted"] = True
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)

    assert ci.main(["--force", "--model-mode", "skip"]) == 0

    assert _mock_install_policy_store["writes"] == []
    assert _mock_install_policy_store["policy"] == existing


def test_wizard_requires_explicit_approval_before_model_and_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _mock_install_policy_store: dict[str, object],
) -> None:
    existing = InstallConsentPolicy(
        skill_mode="preapproved-auto",
        agent_mode="ask-each-time",
        mcp_server_mode="ask-each-time",
    )
    _mock_install_policy_store["policy"] = existing
    _mock_install_policy_store["persisted"] = True
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "_stdio_is_interactive", lambda: False)
    prompts: list[str] = []
    answers = iter(
        [
            "n",  # hooks
            "local",  # knowledge
            "",  # skills default to explicit approval in the new wizard
            "preapproved-auto",  # rejected: automatic install is not offered by this release
            "ask-each-time",  # explicit per-install approval
            "",  # MCP preserve ask
            "skip",  # model
        ]
    )

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(builtins, "input", answer)

    assert ci.main(["--wizard"]) == 0
    policy = _mock_install_policy_store["policy"]
    assert isinstance(policy, InstallConsentPolicy)
    assert policy == InstallConsentPolicy(
        skill_mode="ask-each-time",
        agent_mode="ask-each-time",
        mcp_server_mode="ask-each-time",
    )
    assert next(index for index, value in enumerate(prompts) if "skill" in value.lower()) < next(
        index for index, value in enumerate(prompts) if "model" in value.lower()
    )


def test_policy_persistence_failure_stops_before_tool_graph_or_hook_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    calls: list[str] = []

    def fail_persist(_policy: InstallConsentPolicy) -> None:
        raise OSError("token=must-not-leak")

    def record_toolbox_seed(force: bool = False) -> int:
        calls.append("toolboxes")
        return 0

    def record_hook_install(**_kwargs: object) -> int:
        calls.append("hooks")
        return 0

    def record_graph_build(*_args: object, **_kwargs: object) -> int:
        calls.append("graph")
        return 0

    monkeypatch.setattr(ci, "persist_install_policy", fail_persist)
    monkeypatch.setattr(ci, "seed_toolboxes", record_toolbox_seed)
    monkeypatch.setattr(ci, "install_hooks", record_hook_install)
    monkeypatch.setattr(ci, "build_graph", record_graph_build)

    rc = ci.main(
        [
            "--hooks",
            "--graph",
            "--model-mode",
            "skip",
            "--skill-install-consent",
            "ask-each-time",
            "--agent-install-consent",
            "ask-each-time",
            "--mcp-install-consent",
            "ask-each-time",
        ]
    )

    assert rc == 1
    assert calls == []
    captured = capsys.readouterr()
    assert "install consent policy" in captured.err
    assert "must-not-leak" not in captured.err


def test_policy_persistence_digest_mismatch_fails_closed_before_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "persist_install_policy", lambda _policy: "0" * 64)
    monkeypatch.setattr(
        ci,
        "seed_toolboxes",
        lambda force=False: (_ for _ in ()).throw(AssertionError("toolbox side effect")),
    )

    rc = ci.main(
        [
            "--model-mode",
            "skip",
            "--skill-install-consent",
            "ask-each-time",
            "--agent-install-consent",
            "ask-each-time",
            "--mcp-install-consent",
            "ask-each-time",
        ]
    )

    assert rc == 1


def test_main_with_hooks_flag_invokes_inject(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    calls: list[list[str]] = []

    class _FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd: list[str], **_kwargs: object) -> _FakeResult:
        calls.append(list(cmd))
        return _FakeResult()

    monkeypatch.setattr(ci.subprocess, "run", _fake_run)
    rc = ci.main(["--hooks"])
    assert rc == 0
    assert any("ctx.adapters.claude_code.inject_hooks" in c for c in calls)
    assert not any(c == "inject_hooks" for call in calls for c in call)


def _write_graph_archive(
    tmp_path: Path,
    *,
    include_graph_pack: bool = False,
    graph_pack_export_id: str = "test-export",
    include_wiki_pack: bool = False,
    wiki_pack_export_id: str = "test-export",
    graph_nodes: list[dict[str, object]] | None = None,
) -> Path:
    source = tmp_path / "archive-source"
    graph_out = source / "graphify-out"
    graph_out.mkdir(parents=True)
    (graph_out / "graph.json").write_text(
        json.dumps(
            {
                "graph": {"export_id": "test-export"},
                "nodes": graph_nodes or [],
                "links": [],
            }
        ),
        encoding="utf-8",
    )
    (graph_out / "graph-delta.json").write_text(
        json.dumps({"export_id": "test-export", "nodes": [], "edges": []}),
        encoding="utf-8",
    )
    (graph_out / "communities.json").write_text(
        json.dumps({"export_id": "test-export", "total_communities": 0}),
        encoding="utf-8",
    )
    (graph_out / "graph-report.md").write_text(
        "# Graph Report\n\n> Export ID: test-export\n",
        encoding="utf-8",
    )
    (graph_out / "graph-export-manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "export_id": "test-export",
                "artifacts": {
                    "graph": "graph.json",
                    "delta": "graph-delta.json",
                    "communities": "communities.json",
                    "report": "graph-report.md",
                },
            }
        ),
        encoding="utf-8",
    )
    _write_dashboard_index(graph_out / "dashboard-neighborhoods.sqlite3")
    entities = source / "entities" / "skills"
    entities.mkdir(parents=True)
    (entities / "current.md").write_text("# Current\n", encoding="utf-8")
    (source / "index.md").write_text("# Wiki\n", encoding="utf-8")
    if include_graph_pack:
        graph = nx.Graph()
        graph.add_node("skill:current", label="current", type="skill")
        write_base_pack(
            pack_dir=graph_out / "packs" / f"base-{graph_pack_export_id}",
            pack_id=f"base-{graph_pack_export_id}",
            base_export_id=graph_pack_export_id,
            config_hash="config-1",
            model_id="model-1",
            graph=graph,
        )
    if include_wiki_pack:
        write_wiki_base_pack(
            pack_dir=source / "wiki-packs" / f"base-{wiki_pack_export_id}",
            pack_id=f"base-{wiki_pack_export_id}",
            base_export_id=wiki_pack_export_id,
            pages={
                "index.md": "# Wiki\n",
                "entities/skills/current.md": "# Current\n",
            },
        )
    archive = tmp_path / "wiki-graph.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                tf.add(path, arcname=path.relative_to(source).as_posix())
    return archive


def _tar_text(tf: tarfile.TarFile, name: str, text: str) -> None:
    payload = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    tf.addfile(info, io.BytesIO(payload))


def _tar_bytes(tf: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    tf.addfile(info, io.BytesIO(payload))


def test_download_graph_archive_verifies_sha256(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = b"graph archive bytes"

    class _Response(io.BytesIO):
        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        ci.urllib.request,
        "urlopen",
        lambda _url, timeout=120: _Response(payload),
    )

    destination = tmp_path / "wiki-graph.tar.gz"
    ci._download_graph_archive(
        destination,
        url="https://example.invalid/wiki-graph.tar.gz",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert destination.read_bytes() == payload

    bad_destination = tmp_path / "bad-wiki-graph.tar.gz"
    with pytest.raises(ValueError, match="checksum mismatch"):
        ci._download_graph_archive(
            bad_destination,
            url="https://example.invalid/wiki-graph.tar.gz",
            expected_sha256="0" * 64,
        )
    assert not bad_destination.exists()

    bad_size_destination = tmp_path / "bad-size-wiki-graph.tar.gz"
    with pytest.raises(ValueError, match="size mismatch"):
        ci._download_graph_archive(
            bad_size_destination,
            url="https://example.invalid/wiki-graph.tar.gz",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            expected_size=len(payload) + 1,
        )
    assert not bad_size_destination.exists()


def test_default_graph_does_not_infer_a_release_or_local_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("empty bootstrap must not look for an owned graph corpus")

    for name in (
        "_find_local_graph_archive",
        "_source_graph_release_contract",
        "_download_graph_archive",
        "_install_graph_entity_overlay",
        "_install_runtime_availability_overlay",
        "_ensure_runtime_availability_pack",
    ):
        monkeypatch.setattr(ci, name, forbidden)
    assert ci.build_graph(tmp_path) == 0
    graph = json.loads((tmp_path / "skill-wiki" / "graphify-out" / "graph.json").read_text())
    assert graph["nodes"] == []


def test_source_checkout_without_local_graph_archive_has_no_implicit_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(ci, "__file__", str(tmp_path / "src" / "ctx_init.py"))

    assert ci._find_local_graph_archive("runtime") is None


def test_source_checkout_release_contract_comes_from_the_strict_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    graph_dir = root / "graph"
    graph_dir.mkdir(parents=True)
    # Historical manifest parsing stays covered with synthetic metadata, not
    # a production corpus or release asset downloaded by this test.
    payload = {
        "schema_version": 1,
        "repository": "stevesolun/ctx",
        "source_release_tag": "graph-artifacts-test-source",
        "artifacts": [
            {"path": path, "asset_name": name, "size": 1, "sha256": "a" * 64, "hydrate": hydrate}
            for path, name, hydrate in release_artifacts.GRAPH_RELEASE_ARTIFACT_SPECS
        ],
    }
    (graph_dir / "release-artifacts.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    monkeypatch.setattr(ci, "__file__", str(root / "src" / "ctx_init.py"))

    contract = ci._source_graph_release_contract("runtime")
    assert contract is not None
    url, sha256, size = contract
    runtime = release_artifacts.load_manifest(
        graph_dir / "release-artifacts.json"
    ).artifact_for_path("graph/wiki-graph-runtime.tar.gz")

    assert url.endswith("/releases/download/graph-artifacts-test-source/wiki-graph-runtime.tar.gz")
    assert (sha256, size) == (runtime.sha256, runtime.size)


def test_local_graph_archive_checksum_is_verified(tmp_path: Path) -> None:
    archive = tmp_path / "wiki-graph-runtime.tar.gz"
    archive.write_bytes(b"not the shipped runtime archive")

    with pytest.raises(ValueError, match="local graph archive checksum mismatch"):
        ci._verify_local_graph_archive(archive, requested_install_mode="runtime")


def test_custom_graph_url_requires_checksum_or_explicit_opt_out(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ci, "_find_local_graph_archive", lambda _mode: None)
    monkeypatch.setattr(
        ci,
        "_download_graph_archive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("download should be blocked before network access")
        ),
    )

    rc = ci.build_graph(
        tmp_path / "home",
        graph_url="https://example.invalid/wiki-graph.tar.gz",
    )

    assert rc == 1


def test_custom_graph_url_bypasses_local_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = _write_graph_archive(tmp_path)
    archive_bytes = archive.read_bytes()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        ci,
        "_find_local_graph_archive",
        lambda _mode: (_ for _ in ()).throw(
            AssertionError("explicit graph_url must not use local archive")
        ),
    )

    def fake_download(destination: Path, **kwargs: object) -> None:
        calls.append(dict(kwargs))
        destination.write_bytes(archive_bytes)

    monkeypatch.setattr(ci, "_download_graph_archive", fake_download)
    monkeypatch.setattr(ci, "_install_graph_entity_overlay", lambda *_a, **_k: None)

    rc = ci.build_graph(
        tmp_path / "home",
        graph_url="https://example.invalid/custom-wiki-graph.tar.gz",
        graph_sha256=hashlib.sha256(archive_bytes).hexdigest(),
    )

    assert rc == 0
    assert calls == [
        {
            "url": "https://example.invalid/custom-wiki-graph.tar.gz",
            "expected_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        }
    ]


def test_main_imports_only_the_explicit_user_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = tmp_path / "home"
    archive = _write_graph_archive(tmp_path)
    monkeypatch.setattr(ci, "_claude_dir", lambda: claude)
    assert (
        ci.main(
            [
                "--graph",
                "--graph-url",
                archive.as_uri(),
                "--graph-sha256",
                hashlib.sha256(archive.read_bytes()).hexdigest(),
                "--model-mode",
                "skip",
            ]
        )
        == 0
    )
    graph_json = claude / "skill-wiki" / "graphify-out" / "graph.json"
    graph_payload = json.loads(graph_json.read_text(encoding="utf-8"))
    assert graph_payload["graph"]["export_id"] == "test-export"
    assert not (claude / "skill-wiki" / "entities" / "skills" / "current.md").exists()
    assert not (claude / "skill-wiki" / "graphify-out" / "entity-overlays.jsonl").exists()


@pytest.mark.parametrize("install_mode", ["runtime", "full"])
def test_user_archive_can_replace_unchanged_empty_scaffold(
    tmp_path: Path, install_mode: str
) -> None:
    archive = _write_graph_archive(tmp_path)
    claude = tmp_path / "home"
    assert ci.build_graph(claude) == 0
    wiki = claude / "skill-wiki"
    marker = wiki / ".ctx-empty-workspace.json"
    assert marker.is_file()

    assert (
        ci.build_graph(
            claude,
            graph_url=archive.as_uri(),
            graph_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            install_mode=install_mode,
        )
        == 0
    )

    graph = json.loads((wiki / "graphify-out" / "graph.json").read_text())
    assert graph["graph"]["export_id"] == "test-export"
    assert not marker.exists()
    assert (wiki / "SCHEMA.md").is_file()
    assert (wiki / "entities" / "skills" / "current.md").exists() == (install_mode == "full")


def test_user_archive_import_does_not_copy_an_unrequested_local_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = tmp_path / "home"
    archive = _write_graph_archive(tmp_path)
    overlay = tmp_path / "entity-overlays.jsonl"
    overlay.write_text(
        '{"overlay_id":"unrequested","nodes":[{"id":"skill:unrequested"}],"edges":[]}\n'
    )
    monkeypatch.setattr(ci, "_find_local_graph_entity_overlay", lambda: overlay)
    assert (
        ci.build_graph(
            claude,
            graph_url=archive.as_uri(),
            graph_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
        == 0
    )
    assert not (claude / "skill-wiki" / "graphify-out" / "entity-overlays.jsonl").exists()
    assert json.loads(overlay.read_text())["overlay_id"] == "unrequested"


@pytest.mark.parametrize("field", ["semantic_sim", "tag_sim", "token_sim"])
def test_graph_overlay_validation_rejects_out_of_range_similarity_fields(
    tmp_path: Path,
    field: str,
) -> None:
    overlay = tmp_path / "entity-overlays.jsonl"
    overlay.write_text(
        json.dumps(
            {
                "nodes": [{"id": "skill:a"}],
                "edges": [
                    {
                        "source": "skill:a",
                        "target": "skill:b",
                        "weight": 0.5,
                        "final_weight": 0.5,
                        field: 2.0,
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=f"{field} must be 0..1"):
        ci._validate_graph_entity_overlay(overlay)


def test_graph_overlay_validation_rejects_weight_final_weight_drift(
    tmp_path: Path,
) -> None:
    overlay = tmp_path / "entity-overlays.jsonl"
    overlay.write_text(
        json.dumps(
            {
                "nodes": [{"id": "skill:a"}],
                "edges": [
                    {
                        "source": "skill:a",
                        "target": "skill:b",
                        "weight": 0.7,
                        "final_weight": 0.5,
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="weight must equal final_weight"):
        ci._validate_graph_entity_overlay(overlay)


def test_runtime_graph_install_extracts_harness_pages_after_required_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = tmp_path / "ordered-runtime-wiki-graph.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        _tar_text(
            tf,
            "graphify-out/graph.json",
            json.dumps(
                {
                    "graph": {"export_id": "test-export"},
                    "nodes": [{"id": "harness:text-to-cad", "type": "harness"}],
                    "links": [],
                }
            ),
        )
        _tar_text(
            tf,
            "graphify-out/graph-delta.json",
            json.dumps({"export_id": "test-export", "nodes": [], "edges": []}),
        )
        _tar_text(
            tf,
            "graphify-out/communities.json",
            json.dumps({"export_id": "test-export", "total_communities": 0}),
        )
        _tar_text(tf, "graphify-out/graph-report.md", "# Graph Report\n")
        _tar_text(
            tf,
            "graphify-out/graph-export-manifest.json",
            json.dumps(
                {
                    "version": 1,
                    "export_id": "test-export",
                    "artifacts": {
                        "graph": "graph.json",
                        "delta": "graph-delta.json",
                        "communities": "communities.json",
                        "report": "graph-report.md",
                    },
                }
            ),
        )
        index_path = tmp_path / "runtime-dashboard-neighborhoods.sqlite3"
        _write_dashboard_index(index_path)
        _tar_bytes(
            tf,
            "graphify-out/dashboard-neighborhoods.sqlite3",
            index_path.read_bytes(),
        )
        _tar_text(tf, "index.md", "# Wiki\n")
        _tar_text(tf, "entities/harnesses/text-to-cad.md", "# Text to CAD\n")
        _tar_text(tf, "entities/skills/not-runtime.md", "# Not runtime\n")

    claude = tmp_path / "home"

    assert (
        ci.build_graph(
            claude,
            graph_url=archive.as_uri(),
            graph_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
        == 0
    )
    assert (claude / "skill-wiki" / "entities" / "harnesses" / "text-to-cad.md").is_file()
    assert not (claude / "skill-wiki" / "entities" / "skills" / "not-runtime.md").exists()


def test_explicit_user_import_adds_no_bundled_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _write_graph_archive(
        tmp_path,
        graph_nodes=[{"id": "skill:my-own-tool", "type": "skill", "label": "my own tool"}],
    )
    claude = tmp_path / "home"

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("explicit user import must never seed CTX capabilities")

    monkeypatch.setattr(ci, "_install_runtime_availability_overlay", forbidden)
    monkeypatch.setattr(ci, "_ensure_runtime_availability_pack", forbidden)
    assert (
        ci.build_graph(
            claude,
            graph_url=archive.as_uri(),
            graph_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
        == 0
    )
    wiki = claude / "skill-wiki"
    with sqlite3.connect(wiki / "graphify-out" / "graph-store.sqlite3") as conn:
        assert conn.execute("SELECT id FROM nodes").fetchall() == [("skill:my-own-tool",)]
    assert not list(wiki.rglob("SKILL.md"))
    assert not (wiki / "converted-agents").exists()
    assert not (wiki / "graphify-out" / "entity-overlays.jsonl").exists()
    before = {path: path.read_bytes() for path in wiki.rglob("*") if path.is_file()}
    assert ci.build_graph(claude, force=True) == 0
    assert all(path.read_bytes() == content for path, content in before.items())


def test_runtime_availability_overlay_rejects_reserved_identity_changes(
    tmp_path: Path,
    historical_runtime_pack: dict[str, object],
) -> None:
    wiki = tmp_path / "skill-wiki"
    graph_dir = wiki / "graphify-out"
    graph_dir.mkdir(parents=True)
    _write_dashboard_index(graph_dir / "dashboard-neighborhoods.sqlite3")
    pack = ci._load_runtime_availability_pack()
    altered = dict(pack["overlay"])
    altered["source"] = "untrusted-source"
    overlay_path = graph_dir / ci._GRAPH_ENTITY_OVERLAY_NAME
    overlay_path.write_text(json.dumps(altered) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="identity contains unexpected content"):
        ci._install_runtime_availability_overlay(wiki)
    assert json.loads(overlay_path.read_text(encoding="utf-8")) == altered


def test_runtime_wiki_get_rejects_symlinked_converted_body(tmp_path: Path) -> None:
    wiki = tmp_path / "skill-wiki"
    converted = wiki / "converted" / "ctx-python-testing"
    converted.mkdir(parents=True)
    secret = tmp_path / "outside-secret.md"
    secret.write_text("outside-secret-value\n", encoding="utf-8")
    candidate = converted / "SKILL.md"
    try:
        candidate.symlink_to(secret)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    toolbox = CtxCoreToolbox(wiki_dir=wiki)
    payload = json.loads(
        toolbox.dispatch(
            ToolCall(
                id="inspect-symlink",
                name="ctx__wiki_get",
                arguments={"slug": "ctx-python-testing", "entity_type": "skill"},
            )
        )
    )

    assert "error" in payload
    assert "outside-secret-value" not in json.dumps(payload)


def test_runtime_availability_overlay_rejects_shipped_graph_id_collision(
    tmp_path: Path,
    historical_runtime_pack: dict[str, object],
) -> None:
    wiki = tmp_path / "skill-wiki"
    graph_dir = wiki / "graphify-out"
    graph_dir.mkdir(parents=True)
    index = graph_dir / "dashboard-neighborhoods.sqlite3"
    _write_dashboard_index(index)
    with sqlite3.connect(index) as conn:
        conn.execute(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
            (
                "skill:ctx-python-testing",
                "ctx-python-testing",
                "skill",
                "[]",
                "",
                None,
                None,
                0,
            ),
        )
        conn.commit()

    with pytest.raises(ValueError, match="collide with shipped graph nodes"):
        ci._install_runtime_availability_overlay(wiki)


def test_legacy_materializer_rejects_tampered_reserved_body(
    tmp_path: Path,
    historical_runtime_pack: dict[str, object],
) -> None:
    archive = _write_graph_archive(tmp_path)
    wiki = tmp_path / "home" / "skill-wiki"
    ci._extract_graph_archive(archive, wiki, install_mode="runtime")
    ci._install_runtime_availability_overlay(wiki)
    ci._refresh_graph_store(wiki)
    reserved = wiki / "converted" / "ctx-python-testing" / "SKILL.md"
    reserved.parent.mkdir(parents=True)
    reserved.write_text("attacker-controlled body\n", encoding="utf-8")

    with pytest.raises(ValueError, match="reserved runtime availability body does not match"):
        ci._ensure_runtime_availability_pack(wiki)
    assert reserved.read_text(encoding="utf-8") == "attacker-controlled body\n"


def test_legacy_materializer_rejects_symlinked_availability_parent(
    tmp_path: Path,
    historical_runtime_pack: dict[str, object],
) -> None:
    archive = _write_graph_archive(tmp_path)
    wiki = tmp_path / "home" / "skill-wiki"
    ci._extract_graph_archive(archive, wiki, install_mode="runtime")
    ci._install_runtime_availability_overlay(wiki)
    ci._refresh_graph_store(wiki)
    converted = wiki / "converted"
    converted.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (converted / "ctx-python-testing").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="unsafe symlinked runtime availability path"):
        ci._ensure_runtime_availability_pack(wiki)
    assert not (outside / "SKILL.md").exists()


def test_default_reinitialization_preserves_all_existing_user_entity_kinds(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "home"
    local_skill = claude / "skill-wiki" / "entities" / "skills" / "private.md"
    local_agent = claude / "skill-wiki" / "entities" / "agents" / "private.md"
    local_mcp = claude / "skill-wiki" / "entities" / "mcp-servers" / "p" / "private.md"
    local_harness = claude / "skill-wiki" / "entities" / "harnesses" / "old.md"
    local_converted = claude / "skill-wiki" / "converted" / "private-skill" / "SKILL.md"
    for path in (local_skill, local_agent, local_mcp, local_harness, local_converted):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.stem}\n", encoding="utf-8")

    assert ci.build_graph(claude, force=True, install_mode="runtime") == 0

    assert local_skill.read_text(encoding="utf-8") == "# private\n"
    assert local_agent.read_text(encoding="utf-8") == "# private\n"
    assert local_mcp.read_text(encoding="utf-8") == "# private\n"
    assert local_converted.read_text(encoding="utf-8") == "# SKILL\n"
    assert local_harness.read_text(encoding="utf-8") == "# old\n"


def test_graph_install_rejects_incomplete_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "incomplete-source"
    graph_out = source / "graphify-out"
    graph_out.mkdir(parents=True)
    (graph_out / "graph.json").write_text(
        json.dumps({"graph": {"export_id": "partial"}, "nodes": []}),
        encoding="utf-8",
    )
    archive = tmp_path / "incomplete-wiki-graph.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(graph_out / "graph.json", arcname="graphify-out/graph.json")

    claude = tmp_path / "home"

    assert (
        ci.build_graph(
            claude,
            graph_url=archive.as_uri(),
            graph_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
        == 1
    )
    assert not (claude / "skill-wiki" / "graphify-out" / "graph.json").exists()


def test_graph_install_validation_does_not_parse_full_graph_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wiki = tmp_path / "wiki"
    graph_out = wiki / "graphify-out"
    graph_out.mkdir(parents=True)
    (wiki / "index.md").write_text("# Wiki\n", encoding="utf-8")
    (graph_out / "graph.json").write_text(
        json.dumps({"graph": {"export_id": "test-export"}, "nodes": [], "links": []}),
        encoding="utf-8",
    )
    (graph_out / "graph-delta.json").write_text(
        json.dumps({"export_id": "test-export", "nodes": [], "edges": []}),
        encoding="utf-8",
    )
    (graph_out / "communities.json").write_text(
        json.dumps({"export_id": "test-export", "total_communities": 0}),
        encoding="utf-8",
    )
    (graph_out / "graph-report.md").write_text(
        "# Graph Report\n\n> Export ID: test-export\n",
        encoding="utf-8",
    )
    (graph_out / "graph-export-manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "export_id": "test-export",
                "artifacts": {
                    "graph": "graph.json",
                    "delta": "graph-delta.json",
                    "communities": "communities.json",
                    "report": "graph-report.md",
                },
            }
        ),
        encoding="utf-8",
    )
    _write_dashboard_index(graph_out / "dashboard-neighborhoods.sqlite3")

    def guarded_read(path: Path) -> object:
        if path.name == "graph.json":
            raise AssertionError("install validation must not parse full graph.json")
        return json.loads(path.read_text(encoding="utf-8"))

    monkeypatch.setattr(ci, "_read_json_file", guarded_read)

    ci._validate_graph_install_tree(wiki)


def test_graph_json_outline_scans_middle_for_edges_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ci, "_GRAPH_JSON_OUTLINE_BYTES", 32)
    filler = "x" * 256
    graph_json = tmp_path / "graph.json"
    graph_json.write_text(
        '{"nodes":["' + filler + '"],"edges":[],"graph":"' + filler + '"}',
        encoding="utf-8",
    )

    ci._validate_graph_json_outline(graph_json)


def test_graph_install_validation_accepts_base_pack_without_graph_json(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    graph_out = wiki / "graphify-out"
    graph_out.mkdir(parents=True)
    (wiki / "index.md").write_text("# Wiki\n", encoding="utf-8")
    graph = nx.Graph()
    graph.add_node("skill:pack-only", label="pack-only", type="skill")
    write_base_pack(
        pack_dir=graph_out / "packs" / "base-export-1",
        pack_id="base-export-1",
        base_export_id="test-export",
        config_hash="config-1",
        model_id="model-1",
        graph=graph,
    )
    (graph_out / "graph-delta.json").write_text(
        json.dumps({"export_id": "test-export", "nodes": [], "edges": []}),
        encoding="utf-8",
    )
    (graph_out / "communities.json").write_text(
        json.dumps({"export_id": "test-export", "total_communities": 0}),
        encoding="utf-8",
    )
    (graph_out / "graph-report.md").write_text(
        "# Graph Report\n\n> Export ID: test-export\n",
        encoding="utf-8",
    )
    (graph_out / "graph-export-manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "export_id": "test-export",
                "artifacts": {
                    "graph": "graph.json",
                    "delta": "graph-delta.json",
                    "communities": "communities.json",
                    "report": "graph-report.md",
                },
            }
        ),
        encoding="utf-8",
    )
    _write_dashboard_index(graph_out / "dashboard-neighborhoods.sqlite3")

    ci._validate_graph_install_tree(wiki)


def test_runtime_graph_install_extracts_and_validates_wiki_packs(
    tmp_path: Path,
) -> None:
    archive = _write_graph_archive(tmp_path, include_wiki_pack=True)
    wiki = tmp_path / "installed-wiki"

    ci._extract_graph_archive(archive, wiki, install_mode="runtime")

    assert (wiki / "wiki-packs" / "base-test-export" / "wiki-pack-manifest.json").is_file()
    assert not (wiki / "entities" / "skills" / "current.md").exists()
    ci._validate_graph_install_tree(wiki)
    assert ci._graph_full_install_complete(wiki) is True


def test_runtime_graph_install_extracts_and_validates_graph_packs(
    tmp_path: Path,
) -> None:
    archive = _write_graph_archive(tmp_path, include_graph_pack=True)
    wiki = tmp_path / "installed-wiki"

    ci._extract_graph_archive(archive, wiki, install_mode="runtime")

    assert (
        wiki / "graphify-out" / "packs" / "base-test-export" / "graph-pack-manifest.json"
    ).is_file()
    ci._validate_graph_install_tree(wiki)


def test_build_graph_refreshes_operational_graph_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_graph_archive(tmp_path, include_graph_pack=True)
    claude = tmp_path / "home"

    assert (
        ci.build_graph(
            claude,
            graph_url=archive.as_uri(),
            graph_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
        == 0
    )
    graph_dir = claude / "skill-wiki" / "graphify-out"
    store = graph_dir / "graph-store.sqlite3"
    assert validate_graph_store(store, graph_dir)["ok"] is True

    store.unlink()
    assert ci.build_graph(claude) == 0
    assert validate_graph_store(store, graph_dir)["ok"] is True


def test_graph_install_rejects_mismatched_graph_pack_export_id(
    tmp_path: Path,
) -> None:
    archive = _write_graph_archive(
        tmp_path,
        include_graph_pack=True,
        graph_pack_export_id="wrong-export",
    )

    with pytest.raises(ValueError, match="graphify-out/packs export_id mismatch"):
        ci._extract_graph_archive(archive, tmp_path / "installed-wiki", install_mode="runtime")


def test_graph_install_rejects_mismatched_wiki_pack_export_id(
    tmp_path: Path,
) -> None:
    archive = _write_graph_archive(
        tmp_path,
        include_wiki_pack=True,
        wiki_pack_export_id="wrong-export",
    )

    with pytest.raises(ValueError, match="wiki-packs export_id mismatch"):
        ci._extract_graph_archive(archive, tmp_path / "installed-wiki", install_mode="runtime")


def test_runtime_graph_install_without_full_entities_is_not_full_install(
    tmp_path: Path,
) -> None:
    archive = _write_graph_archive(tmp_path)
    wiki = tmp_path / "installed-wiki"

    ci._extract_graph_archive(archive, wiki, install_mode="runtime")

    assert ci._graph_install_complete(wiki) is True
    assert ci._graph_full_install_complete(wiki) is False


def test_full_graph_install_uses_system_tar_after_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = _write_graph_archive(tmp_path)
    wiki = tmp_path / "installed-wiki"
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(list(cmd))
        target = Path(cmd[cmd.index("-C") + 1])
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(target)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ci.shutil, "which", lambda _name: "tar")
    monkeypatch.setattr(ci.subprocess, "run", fake_run)

    ci._extract_graph_archive(archive, wiki, install_mode="full")

    assert len(calls) == 1
    assert calls[0][:3] == ["tar", "-xzf", str(archive)]
    assert calls[0][3] == "-C"
    assert Path(calls[0][4]).name.startswith(".installed-wiki-stage-")
    assert ci._graph_full_install_complete(wiki) is True
    assert (wiki / "entities" / "skills" / "current.md").is_file()


def test_full_graph_install_prefers_wiki_packs_over_expanded_entities(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = _write_graph_archive(tmp_path, include_wiki_pack=True)
    wiki = tmp_path / "installed-wiki"

    def fail_if_system_tar_is_used(*_args: object, **_kwargs: object) -> None:
        pytest.fail("packed full install should use filtered extraction")

    monkeypatch.setattr(ci.shutil, "which", lambda _name: "tar")
    monkeypatch.setattr(ci.subprocess, "run", fail_if_system_tar_is_used)

    ci._extract_graph_archive(archive, wiki, install_mode="full")

    assert (wiki / "wiki-packs" / "base-test-export" / "wiki-pack-manifest.json").is_file()
    assert not (wiki / "entities" / "skills" / "current.md").exists()
    assert ci._graph_full_install_complete(wiki) is True


def test_full_user_import_refuses_to_prune_existing_knowledge_even_with_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = _write_graph_archive(tmp_path)
    claude = tmp_path / "home"
    existing = claude / "skill-wiki" / "entities" / "skills" / "private.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("# Private user skill\n")
    monkeypatch.setattr(ci, "_claude_dir", lambda: claude)
    monkeypatch.setattr(
        ci,
        "_download_graph_archive",
        lambda *_args, **_kwargs: pytest.fail("refuse a populated destination before download"),
    )
    assert (
        ci.main(
            [
                "--graph",
                "--graph-url",
                archive.as_uri(),
                "--graph-sha256",
                hashlib.sha256(archive.read_bytes()).hexdigest(),
                "--graph-install-mode",
                "full",
                "--force",
                "--model-mode",
                "skip",
            ]
        )
        == 1
    )
    assert "existing user knowledge was preserved" in capsys.readouterr().err
    assert existing.read_text() == "# Private user skill\n"
    assert not (existing.parent / "current.md").exists()


def test_explicit_low_level_full_archive_replacement_prunes_generated_files(
    tmp_path: Path,
) -> None:
    """Keep the legacy extraction/recovery contract separate from safe init."""
    archive = _write_graph_archive(tmp_path)
    wiki = tmp_path / "installed-wiki"
    stale = wiki / "entities" / "skills" / "stale.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("# Stale generated entity\n")
    ci._extract_graph_archive(archive, wiki, install_mode="full")
    assert not stale.exists()
    assert (stale.parent / "current.md").read_text() == "# Current\n"


def test_graph_install_rejects_path_traversal_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = tmp_path / "malicious-wiki-graph.tar.gz"
    payload = b"owned"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo("../evil.txt")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))

    claude = tmp_path / "home"

    assert (
        ci.build_graph(
            claude,
            graph_url=archive.as_uri(),
            graph_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
        == 1
    )
    assert not (tmp_path / "evil.txt").exists()
    assert not (claude / "evil.txt").exists()


def test_main_with_requested_hook_failure_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)

    class _FakeResult:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        if "ctx.adapters.claude_code.inject_hooks" in cmd:
            return _FakeResult(7)
        return _FakeResult(0)

    monkeypatch.setattr(ci.subprocess, "run", fake_run)

    assert ci.main(["--hooks"]) == 7


def test_main_registers_codex_hook_only_when_explicitly_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    calls: list[list[str]] = []

    class _FakeResult:
        returncode = 0
        stdout = "registered; trust remains host-owned"
        stderr = ""

    def fake_run(command: list[str], **_kwargs: object) -> _FakeResult:
        calls.append(command)
        return _FakeResult()

    monkeypatch.setattr(ci.subprocess, "run", fake_run)

    assert ci.main(["--codex-hooks", "--model-mode", "skip"]) == 0

    assert any("ctx.adapters.codex.install_query_hook" in call for call in calls)
    assert not any("ctx.adapters.claude_code.inject_hooks" in call for call in calls)
    assert "approve it in Codex /hooks" in capsys.readouterr().out


def test_main_propagates_requested_codex_hook_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)

    class _FakeResult:
        stdout = ""
        stderr = "managed policy"

        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(command: list[str], **_kwargs: object) -> _FakeResult:
        if "ctx.adapters.codex.install_query_hook" in command:
            return _FakeResult(9)
        return _FakeResult(0)

    monkeypatch.setattr(ci.subprocess, "run", fake_run)

    assert ci.main(["--codex-hooks", "--model-mode", "skip"]) == 9


def test_main_custom_model_writes_profile_and_recommends_harness(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)

    recommendation_calls: list[dict[str, object]] = []

    def fake_recommend(
        goal: str,
        top_k: int = 5,
        model_provider: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, object]]:
        recommendation_calls.append(
            {
                "goal": goal,
                "top_k": top_k,
                "model_provider": model_provider,
                "model": model,
            }
        )
        return [{"name": "text-to-cad", "type": "harness", "score": 0.8}]

    monkeypatch.setattr(
        ci,
        "recommend_harnesses",
        fake_recommend,
    )

    rc = ci.main(
        [
            "--model-mode",
            "custom",
            "--model",
            "openai/gpt-5.5",
            "--goal",
            "turn text prompts into CAD",
        ]
    )

    assert rc == 0
    profile = json.loads((tmp_path / "ctx-model-profile.json").read_text())
    assert profile["mode"] == "custom"
    assert profile["provider"] == "openai"
    assert profile["model"] == "openai/gpt-5.5"
    assert profile["api_key_env"] == "OPENAI_API_KEY"
    assert recommendation_calls[0]["model_provider"] == "openai"
    assert recommendation_calls[0]["model"] == "openai/gpt-5.5"
    assert "text-to-cad" in capsys.readouterr().out


@pytest.mark.parametrize("env_value", [None, "provider-secret-not-for-profile"])
@pytest.mark.parametrize("env_name", ["CTX_TEST_PROVIDER_KEY", "A" * 128])
def test_custom_model_profile_records_only_the_environment_name(
    tmp_path: Path,
    monkeypatch,
    capsys,
    env_value: str | None,
    env_name: str,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "recommend_harnesses", lambda *args, **kwargs: [])
    if env_value is None:
        monkeypatch.delenv(env_name, raising=False)
    else:
        monkeypatch.setenv(env_name, env_value)

    assert (
        ci.main(["--model-mode", "custom", "--model", "openai/test", "--api-key-env", env_name])
        == 0
    )

    profile_text = (tmp_path / "ctx-model-profile.json").read_text()
    profile = json.loads(profile_text)
    assert profile["api_key_env"] == env_name
    assert profile["harness_requirements"]["api_key_env"] == env_name
    output = capsys.readouterr()
    assert "provider-secret-not-for-profile" not in profile_text + output.out + output.err
    if env_value is None:
        assert f"set {env_name}" in output.out


@pytest.mark.parametrize(
    "env_name", ["sk-test-private-value", "KEY=value", "bad name", "9KEY", "A" * 129]
)
def test_custom_model_rejects_an_invalid_environment_name_without_echoing_it(
    tmp_path: Path,
    monkeypatch,
    capsys,
    env_name: str,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "recommend_harnesses", lambda *args, **kwargs: [])

    assert (
        ci.main(["--model-mode", "custom", "--model", "openai/test", "--api-key-env", env_name])
        == 1
    )

    assert not (tmp_path / "ctx-model-profile.json").exists()
    output = capsys.readouterr()
    assert "environment variable name" in output.err
    assert env_name not in output.out + output.err


@pytest.mark.parametrize("supplied_on_cli", [True, False])
def test_wizard_rejects_invalid_environment_names_before_display_or_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _mock_install_policy_store: dict[str, object],
    supplied_on_cli: bool,
) -> None:
    secret = "sk-private-wizard-value"
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        if prompt.startswith("API key environment variable") and not supplied_on_cli:
            return secret
        return ""

    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(builtins, "input", answer)
    argv = ["--wizard", "--model-mode", "custom", "--model", "openai/test"]
    if supplied_on_cli:
        argv.extend(["--api-key-env", secret])

    assert ci.main(argv) == 1

    output = capsys.readouterr()
    assert "environment variable name" in output.err
    assert secret not in output.out + output.err + "".join(prompts)
    assert _mock_install_policy_store["writes"] == []
    assert not (tmp_path / "ctx-model-profile.json").exists()
    assert not (tmp_path / "skill-system-config.json").exists()


def test_main_custom_model_records_structured_harness_requirements(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    recommendation_calls: list[dict[str, object]] = []

    def fake_recommend(
        goal: str,
        top_k: int = 5,
        model_provider: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, object]]:
        recommendation_calls.append(
            {
                "goal": goal,
                "top_k": top_k,
                "model_provider": model_provider,
                "model": model,
            }
        )
        return []

    monkeypatch.setattr(ci, "recommend_harnesses", fake_recommend)

    rc = ci.main(
        [
            "--model-mode",
            "custom",
            "--model",
            "openai/gpt-5.5",
            "--goal",
            "build a code agent",
            "--harness-runtime",
            "linux python",
            "--harness-autonomy",
            "supervised",
            "--harness-tools",
            "filesystem shell browser",
            "--harness-verify",
            "pytest ruff",
            "--harness-privacy",
            "private repo no secrets",
            "--harness-attach-mode",
            "mcp",
        ]
    )

    assert rc == 0
    profile = json.loads((tmp_path / "ctx-model-profile.json").read_text())
    assert profile["harness_requirements"] == {
        "runtime": "linux python",
        "autonomy": "supervised",
        "tools": "filesystem shell browser",
        "verification": "pytest ruff",
        "privacy": "private repo no secrets",
        "attach_mode": "mcp",
    }
    query = str(recommendation_calls[0]["goal"])
    assert "linux python" in query
    assert "filesystem shell browser" in query
    assert "pytest ruff" in query
    assert "private repo no secrets" in query
    assert "mcp" in query


def test_main_custom_model_no_fit_points_to_harness_plan(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "recommend_harnesses", lambda *args, **kwargs: [])

    rc = ci.main(
        [
            "--model-mode",
            "custom",
            "--model",
            "ollama/llama3.1",
            "--model-provider",
            "ollama",
            "--goal",
            "private local CAD workflow",
            "--harness-runtime",
            "linux server",
            "--harness-tools",
            "filesystem shell",
            "--harness-verify",
            "pytest",
            "--harness-privacy",
            "offline source code",
            "--harness-attach-mode",
            "mcp",
        ]
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "no harness recommendations matched yet" in output
    assert "python -m harness_install --recommend" in output
    assert '--model-provider "ollama"' in output
    assert '--harness-runtime "linux server"' in output
    assert '--harness-tools "filesystem shell"' in output
    assert '--harness-verify "pytest"' in output
    assert '--harness-privacy "offline source code"' in output
    assert '--harness-attach-mode "mcp"' in output
    assert "--plan-on-no-fit" in output


def test_recommend_harnesses_uses_wiki_frontmatter_for_fit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wiki = tmp_path / "wiki"
    page = wiki / "entities" / "harnesses" / "text-to-cad.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
title: Text to CAD
type: harness
tags:
  - cad
runtimes:
  - python
model_providers:
  - openai
capabilities:
  - Generate CAD artifacts from natural language prompts
    with OpenSCAD and mesh validation
repo_url: https://github.com/earthtojake/text-to-cad
---
# Text to CAD
""",
        encoding="utf-8",
    )
    graph = nx.Graph()
    graph.add_node(
        "harness:text-to-cad",
        label="text-to-cad",
        type="harness",
        tags=["cad"],
    )
    monkeypatch.setattr(ci, "_load_recommendation_graph", lambda: graph)
    import ctx_config

    monkeypatch.setattr(
        ctx_config,
        "cfg",
        SimpleNamespace(
            wiki_dir=wiki,
            claude_dir=tmp_path / ".claude",
            recommendation_top_k=5,
            harness_recommendation_min_fit_score=0.85,
        ),
    )

    results = ci.recommend_harnesses(
        "turn text prompts into CAD openscad openai gpt-5 harness",
        model_provider="openai",
        model="openai/gpt-5.5",
    )

    assert results
    assert results[0]["name"] == "text-to-cad"
    assert results[0]["fit_score"] >= 0.85
    assert "openai" in results[0]["fit_signals"]
    assert "gpt-5" not in results[0]["fit_signals"]
    assert "gpt-5" not in results[0]["missing_signals"]
    assert "openscad" in results[0]["fit_signals"]


def test_load_recommendation_graph_uses_configured_wiki_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wiki = tmp_path / "custom-wiki"
    out = wiki / "graphify-out"
    out.mkdir(parents=True)
    graph = nx.Graph()
    graph.add_node("harness:custom", label="custom", type="harness")
    data = nx.node_link_data(graph)
    (out / "graph.json").write_text(json.dumps(data), encoding="utf-8")

    import ctx_config

    monkeypatch.setattr(ctx_config, "cfg", SimpleNamespace(wiki_dir=wiki))

    loaded = ci._load_recommendation_graph()

    assert "harness:custom" in loaded


def test_recommend_harnesses_surfaces_reliability_rubric(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wiki = tmp_path / "wiki"
    harness_dir = wiki / "entities" / "harnesses"
    harness_dir.mkdir(parents=True)
    (harness_dir / "reliable-agent.md").write_text(
        """---
title: Reliable Agent
type: harness
tags:
  - agents
model_providers:
  - openai
capabilities:
  - Persistent project context and task state
  - Permission limits, sandbox rules, and policy checks
  - Automated tests, evals, retry loops, and validation gates
verify_commands:
  - pytest
repo_url: https://example.test/reliable-agent
---
# Reliable Agent
""",
        encoding="utf-8",
    )
    graph = nx.Graph()
    graph.add_node(
        "harness:reliable-agent",
        label="reliable-agent",
        type="harness",
        tags=["agents"],
    )
    monkeypatch.setattr(ci, "_load_recommendation_graph", lambda: graph)
    import ctx_config

    monkeypatch.setattr(
        ctx_config,
        "cfg",
        SimpleNamespace(
            wiki_dir=wiki,
            claude_dir=tmp_path / ".claude",
            recommendation_top_k=5,
            harness_recommendation_min_fit_score=0.20,
            harness_reliability_weights={
                "context": 0.34,
                "constraints": 0.33,
                "convergence": 0.33,
            },
        ),
    )

    results = ci.recommend_harnesses(
        "openai agent workflow with tests and sandbox",
        model_provider="openai",
        model="openai/gpt-5.5",
    )

    assert results
    recommendation = results[0]
    assert recommendation["name"] == "reliable-agent"
    assert recommendation["reliability_score"] >= 0.90
    assert set(recommendation["reliability_dimensions"]) == {
        "context",
        "constraints",
        "convergence",
    }
    assert recommendation["reliability_dimensions"]["context"]["matched_terms"]
    assert recommendation["reliability_dimensions"]["constraints"]["matched_terms"]
    assert recommendation["reliability_dimensions"]["convergence"]["matched_terms"]
    assert "context" in recommendation["reliability_reason"]
    assert "constraints" in recommendation["reliability_reason"]
    assert "convergence" in recommendation["reliability_reason"]


def test_recommend_harnesses_prefers_reliable_harness_when_fit_ties(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wiki = tmp_path / "wiki"
    harness_dir = wiki / "entities" / "harnesses"
    harness_dir.mkdir(parents=True)
    (harness_dir / "thin-agent.md").write_text(
        """---
title: Thin Agent
type: harness
tags:
  - agents
model_providers:
  - openai
capabilities:
  - Agent workflow orchestration
repo_url: https://example.test/thin-agent
---
# Thin Agent
""",
        encoding="utf-8",
    )
    (harness_dir / "reliable-agent.md").write_text(
        """---
title: Reliable Agent
type: harness
tags:
  - agents
model_providers:
  - openai
capabilities:
  - Agent workflow orchestration
  - Persistent context state and durable task documents
  - Permission limits, sandbox boundaries, and approval policies
  - Automated tests, evals, validation gates, and retry loops
verify_commands:
  - pytest
repo_url: https://example.test/reliable-agent
---
# Reliable Agent
""",
        encoding="utf-8",
    )
    graph = nx.Graph()
    for slug in ("thin-agent", "reliable-agent"):
        graph.add_node(
            f"harness:{slug}",
            label=slug,
            type="harness",
            tags=["agents"],
        )
    monkeypatch.setattr(ci, "_load_recommendation_graph", lambda: graph)
    import ctx_config

    monkeypatch.setattr(
        ctx_config,
        "cfg",
        SimpleNamespace(
            wiki_dir=wiki,
            claude_dir=tmp_path / ".claude",
            recommendation_top_k=5,
            harness_recommendation_min_fit_score=0.20,
            harness_reliability_weights={
                "context": 0.34,
                "constraints": 0.33,
                "convergence": 0.33,
            },
        ),
    )

    results = ci.recommend_harnesses(
        "openai agent workflow",
        model_provider="openai",
        model="openai/gpt-5.5",
    )

    assert [row["name"] for row in results[:2]] == [
        "reliable-agent",
        "thin-agent",
    ]
    assert results[0]["fit_score"] == results[1]["fit_score"]
    assert results[0]["reliability_score"] > results[1]["reliability_score"]


def test_recommend_harnesses_avoids_semantic_model_load_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = nx.Graph()
    graph.add_node("harness:langgraph", label="langgraph", type="harness")
    monkeypatch.setattr(ci, "_load_harness_recommendation_graph", lambda: graph)
    monkeypatch.setattr(ci, "_harness_supports_provider", lambda *args, **kwargs: True)
    monkeypatch.setattr(ci, "_installed_harness_slugs", lambda _path: set())
    monkeypatch.setattr(
        ci,
        "_annotate_harness_fit",
        lambda *_args, **_kwargs: {"fit_score": 0.99, "fit_signals": ["agent"]},
    )
    import ctx_config

    monkeypatch.setattr(
        ctx_config,
        "cfg",
        SimpleNamespace(
            claude_dir=tmp_path / ".claude",
            recommendation_top_k=5,
            harness_recommendation_min_fit_score=0.85,
        ),
    )
    calls: dict[str, object] = {}

    def fake_recommend_by_tags(*_args, **kwargs):
        calls.update(kwargs)
        return [{"name": "langgraph", "type": "harness", "score": 1.0}]

    monkeypatch.setitem(
        sys.modules,
        "ctx.core.resolve.recommendations",
        type(
            "FakeRecommendModule",
            (),
            {
                "query_to_tags": staticmethod(lambda _query: ["agent"]),
                "recommend_by_tags": staticmethod(fake_recommend_by_tags),
            },
        ),
    )

    results = ci.recommend_harnesses(
        "build an agent workflow",
        model_provider="openai",
        model="openai/gpt-5.5",
    )

    assert results[0]["name"] == "langgraph"
    assert calls["query"] == "build an agent workflow"
    assert calls["entity_types"] == ("harness",)
    assert calls["use_semantic_query"] is False


def test_main_custom_model_requires_model(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)

    assert ci.main(["--model-mode", "custom"]) == 1


def test_validate_model_flag_invokes_connection_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(
        ci,
        "recommend_harnesses",
        lambda goal, top_k=5, model_provider=None, model=None: [],
    )
    calls: list[dict] = []

    def fake_validate(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(ci, "validate_model_connection", fake_validate)

    rc = ci.main(
        [
            "--model-mode",
            "custom",
            "--model",
            "ollama/llama3.1",
            "--validate-model",
        ]
    )

    assert rc == 0
    assert calls == [
        {
            "model": "ollama/llama3.1",
            "api_key_env": None,
            "base_url": None,
        }
    ]
