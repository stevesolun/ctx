"""Tests for ctx_init — bootstrap ~/.claude/ scaffolding."""

from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import networkx as nx
import ctx_init as ci


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


def test_main_creates_everything_in_dry_mode(tmp_path: Path, monkeypatch,
                                              capsys) -> None:
    """End-to-end: ``ctx-init`` (no flags) creates dirs + config + toolboxes
    without touching hooks or graph."""
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)

    # Short-circuit subprocess.run to avoid spawning a real toolbox/graph CLI
    # in tests. Verify that main() doesn't call install_hooks or build_graph
    # when those flags are absent.
    calls: list[list[str]] = []

    class _FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeResult()

    monkeypatch.setattr(ci.subprocess, "run", fake_run)

    rc = ci.main([])
    assert rc == 0
    # toolbox init should have been invoked
    toolbox_calls = [c for c in calls if "toolbox" in " ".join(c)]
    assert toolbox_calls, "toolbox init not invoked"
    # inject_hooks / wiki_graphify must NOT be invoked without flags
    for c in calls:
        assert "inject_hooks" not in " ".join(c)
        assert "wiki_graphify" not in " ".join(c)

    out = capsys.readouterr().out
    assert "[ok]" in out
    assert "[skip] hook injection" in out
    assert "[skip] graph build" in out


def test_main_treats_existing_toolboxes_as_idempotent_skip(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)

    class _FakeResult:
        returncode = 1
        stdout = ""
        stderr = (
            "Global config already has 5 toolbox(es). "
            "Use --force to overwrite."
        )

    monkeypatch.setattr(ci.subprocess, "run", lambda *_args, **_kwargs: _FakeResult())

    rc = ci.main(["--model-mode", "skip"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "starter toolboxes already present" in captured.out
    assert "toolbox init returned" not in captured.err
    assert "Global config already has" not in captured.err


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

    answers = iter([
        "y",                  # hooks
        "n",                  # graph
        "custom",             # model mode
        "openai/gpt-5.5",     # model
        "",                   # provider default: openai
        "",                   # api key env default: OPENAI_API_KEY
        "",                   # base URL
        "build CAD artifacts",
        "n",                  # validate model
    ])
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


def test_wizard_flag_prompts_without_tty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "_stdio_is_interactive", lambda: False)
    monkeypatch.setattr(ci, "seed_toolboxes", lambda force=False: 0)
    monkeypatch.setattr(
        ci,
        "recommend_harnesses",
        lambda goal, top_k=5, model_provider=None, model=None: [],
    )

    answers = iter([
        "n",                  # hooks
        "n",                  # graph
        "claude-code",        # model mode
        "maintain FastAPI services",
    ])
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))

    rc = ci.main(["--wizard"])

    assert rc == 0
    profile = json.loads((tmp_path / "ctx-model-profile.json").read_text())
    assert profile["mode"] == "claude-code"
    assert profile["goal"] == "maintain FastAPI services"


def test_explicit_args_do_not_auto_wizard_in_terminal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(ci, "seed_toolboxes", lambda force=False: 0)
    monkeypatch.setattr(
        builtins,
        "input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("unexpected prompt")),
    )

    assert ci.main(["--model-mode", "skip"]) == 0


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


def test_main_with_graph_flag_invokes_graphify(tmp_path: Path, monkeypatch) -> None:
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
    rc = ci.main(["--graph"])
    assert rc == 0
    assert any("ctx.core.wiki.wiki_graphify" in c for c in calls)
    assert not any(c == "wiki_graphify" for call in calls for c in call)


def test_main_with_requested_hook_failure_exits_nonzero(
    tmp_path: Path, monkeypatch
) -> None:
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


def test_main_custom_model_writes_profile_and_recommends_harness(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "seed_toolboxes", lambda force=False: 0)

    recommendation_calls: list[dict[str, object]] = []

    def fake_recommend(
        goal: str,
        top_k: int = 5,
        model_provider: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, object]]:
        recommendation_calls.append({
            "goal": goal,
            "top_k": top_k,
            "model_provider": model_provider,
            "model": model,
        })
        return [{"name": "text-to-cad", "type": "harness", "score": 0.8}]

    monkeypatch.setattr(
        ci,
        "recommend_harnesses",
        fake_recommend,
    )

    rc = ci.main([
        "--model-mode", "custom",
        "--model", "openai/gpt-5.5",
        "--goal", "turn text prompts into CAD",
    ])

    assert rc == 0
    profile = json.loads((tmp_path / "ctx-model-profile.json").read_text())
    assert profile["mode"] == "custom"
    assert profile["provider"] == "openai"
    assert profile["model"] == "openai/gpt-5.5"
    assert profile["api_key_env"] == "OPENAI_API_KEY"
    assert recommendation_calls[0]["model_provider"] == "openai"
    assert recommendation_calls[0]["model"] == "openai/gpt-5.5"
    assert "text-to-cad" in capsys.readouterr().out


def test_main_custom_model_no_fit_points_to_harness_plan(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "seed_toolboxes", lambda force=False: 0)
    monkeypatch.setattr(ci, "recommend_harnesses", lambda *args, **kwargs: [])

    rc = ci.main([
        "--model-mode", "custom",
        "--model", "ollama/llama3.1",
        "--goal", "private local CAD workflow",
    ])

    assert rc == 0
    output = capsys.readouterr().out
    assert "no harness recommendations matched yet" in output
    assert "ctx-harness-install --recommend" in output
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
    assert "gpt-5" in results[0]["fit_signals"]
    assert "openscad" in results[0]["fit_signals"]


def test_recommend_harnesses_avoids_semantic_model_load_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = nx.Graph()
    graph.add_node("harness:langgraph", label="langgraph", type="harness")
    monkeypatch.setattr(ci, "_load_recommendation_graph", lambda: graph)
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
    monkeypatch.setattr(ci, "seed_toolboxes", lambda force=False: 0)

    assert ci.main(["--model-mode", "custom"]) == 1


def test_validate_model_flag_invokes_connection_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ci, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(ci, "seed_toolboxes", lambda force=False: 0)
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

    rc = ci.main([
        "--model-mode", "custom",
        "--model", "ollama/llama3.1",
        "--validate-model",
    ])

    assert rc == 0
    assert calls == [{
        "model": "ollama/llama3.1",
        "api_key_env": None,
        "base_url": None,
    }]
