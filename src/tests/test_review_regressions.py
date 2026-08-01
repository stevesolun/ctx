from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import ctx
import ctx_config
import import_strix_skills as strix_import
import scan_repo
import skill_add
from ctx.adapters import loopflow
from ctx.adapters.generic.adaptive_runtime import select_installed_skill
from ctx.adapters.generic.evaluator import _UsageTotals
from ctx.adapters.generic.loop import run_loop
from ctx.adapters.generic.providers import CompletionResponse, ModelProvider, Usage
from ctx.adapters.generic.runtime_lifecycle import RuntimeLifecycleStore
from ctx.cli import run as run_cli
from ctx.core import source_registry
from ctx.utils import _fs_utils
from scripts import build_reproducible_dist as reproducible
from scripts import validate_release_sbom as release_sbom


def test_adaptive_selection_rejects_secret_bearing_skill(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill = root / "security-review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\n"
        "name: security-review\n"
        "description: Review authentication security defects and credentials.\n"
        "---\n\n"
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n",
        encoding="utf-8",
    )

    assert (
        select_installed_skill(
            "Review authentication security defects",
            skill_roots=[root],
        )
        is None
    )


def test_remote_install_command_is_actionable_without_local_wiki() -> None:
    assert loopflow._is_actionable_capability_row(
        {
            "installable": False,
            "load_status": "wiki-unavailable",
            "install_command": "ctx-skill-install remote-security",
        }
    )


def test_builtin_registry_uses_packaged_evidence_without_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_registry, "REPO_ROOT", tmp_path)
    source_registry._packaged_evidence.cache_clear()

    validated = source_registry.validate_source_registry(
        source_registry.BUILTIN_EXTERNAL_SOURCES,
    )

    assert validated == source_registry.BUILTIN_EXTERNAL_SOURCES


def test_provider_usage_is_validated_before_accumulation() -> None:
    class InvalidUsageProvider:
        name = "invalid-usage"

        def complete(self, *_args: object, **_kwargs: object) -> CompletionResponse:
            return CompletionResponse(
                content="done",
                tool_calls=(),
                finish_reason="stop",
                usage=Usage(input_tokens=-1),
                provider=self.name,
                model="test",
            )

    with pytest.raises(ValueError, match="input_tokens"):
        run_loop(
            provider=cast(ModelProvider, InvalidUsageProvider()),
            system_prompt="",
            task="test",
        )


def test_auxiliary_usage_is_validated_before_accumulation() -> None:
    with pytest.raises(ValueError, match="cost_usd"):
        _UsageTotals().add(Usage(cost_usd=float("nan")))


@pytest.mark.parametrize(
    ("budget_usd", "budget_tokens"),
    [
        (float("nan"), None),
        (float("inf"), None),
        (None, -1),
        (None, True),
    ],
)
def test_public_loop_rejects_invalid_budgets(
    budget_usd: float | None,
    budget_tokens: int | None,
) -> None:
    with pytest.raises(ValueError, match="budget_"):
        run_loop(
            provider=cast(ModelProvider, object()),
            system_prompt="",
            task="test",
            budget_usd=budget_usd,
            budget_tokens=budget_tokens,
        )


def test_existing_session_blocks_planner_before_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session_path = sessions / "existing.jsonl"
    session_path.write_text("sentinel\n", encoding="utf-8")
    planner_called = False

    class UnexpectedPlanner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def plan(self, _task: str) -> None:
            nonlocal planner_called
            planner_called = True
            raise AssertionError("planner must not run")

    monkeypatch.setattr(run_cli, "get_provider", lambda **_kwargs: object())
    monkeypatch.setattr(run_cli, "Planner", UnexpectedPlanner)

    exit_code = run_cli.main(
        [
            "run",
            "--model",
            "ollama/test",
            "--task",
            "test",
            "--sessions-dir",
            str(sessions),
            "--session-id",
            "existing",
            "--planner",
            "--no-ctx-tools",
            "--quiet",
        ]
    )

    assert exit_code == 1
    assert planner_called is False
    assert session_path.read_text(encoding="utf-8") == "sentinel\n"


def test_unload_window_starts_at_first_applied_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.adapters.generic import runtime_lifecycle

    timestamps = iter([100.0, 150.0, 200.0, 201.0])
    monkeypatch.setattr(runtime_lifecycle.time, "time", lambda: next(timestamps))
    lifecycle = RuntimeLifecycleStore(root=tmp_path)

    lifecycle.load_entity(
        session_id="delayed",
        entity_type="skill",
        slug="review",
    )
    lifecycle.record_dev_event(session_id="delayed", event_type="edit")
    lifecycle.mark_entity_loaded(
        session_id="delayed",
        entity_type="skill",
        slug="review",
    )

    state = lifecycle.session_state(
        session_id="delayed",
        min_unused_seconds=50,
    )

    assert state["unload_candidates"] == []


def test_shared_scanner_does_not_deserialize_graph_before_recommending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph_dir = tmp_path / "graphify-out"
    graph_dir.mkdir()
    (graph_dir / "graph.json").write_text("not-json", encoding="utf-8")
    calls: list[str] = []

    def recommend(query: str, **_kwargs: object) -> list[dict[str, object]]:
        calls.append(query)
        return [
            {
                "name": "python-review",
                "type": "skill",
                "installable": True,
                "load_status": "local-wiki",
            }
        ]

    monkeypatch.setattr(
        ctx_config,
        "cfg",
        SimpleNamespace(
            wiki_dir=tmp_path,
            recommendation_top_k=5,
        ),
    )
    monkeypatch.setattr(ctx, "recommend_bundle", recommend)

    rows = scan_repo._shared_recommendations(
        {
            "project_type": "library",
            "languages": [{"name": "Python"}],
            "testing": [{"name": "pytest"}],
        }
    )

    assert rows
    assert calls


def test_strix_preflight_rejects_destination_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_root = tmp_path / "imported-skills" / "strix"
    source = import_root / "skills" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Source\n", encoding="utf-8")
    target = tmp_path / "skills"
    destination = target / "strix-coordination-demo" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("# Existing\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("sentinel\n", encoding="utf-8")
    manifest = {
        "upstream": "https://example.test/upstream",
        "upstream_revision": "0123456789abcdef",
        "license": "Apache-2.0",
        "entries": [
            {
                "category": "coordination",
                "name": "demo",
                "source_path": "skills/source.md",
            }
        ],
    }
    monkeypatch.setattr(strix_import, "IMPORT_ROOT", import_root)
    monkeypatch.setattr(strix_import, "_supports_directory_fds", lambda: True)
    original_open = os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "SKILL.md" and dir_fd is not None and not swapped:
            destination.unlink()
            destination.symlink_to(outside)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(strix_import.os, "open", swap_before_open)

    with pytest.raises((OSError, ValueError)):
        strix_import._preflight_manifest(manifest, target)

    assert swapped is True
    assert outside.read_text(encoding="utf-8") == "sentinel\n"


def test_skill_snapshot_rejects_file_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "candidate" / "SKILL.md"
    source.parent.mkdir()
    source.write_text("# Safe\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    original_open = os.open
    swapped = False
    monkeypatch.setattr(_fs_utils, "supports_secure_directory_fds", lambda: True)

    def swap_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "SKILL.md" and dir_fd is not None and not swapped:
            source.unlink()
            source.symlink_to(outside)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_open)

    with pytest.raises((OSError, ValueError)):
        skill_add._read_skill_snapshot(source)

    assert swapped is True


def test_distribution_build_stages_on_output_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='demo'\nversion='1'\n")
    output = tmp_path / "mounted-output"
    real_temporary_directory = reproducible.tempfile.TemporaryDirectory
    staging_parents: list[Path] = []

    def temporary_directory(*, prefix: str, dir: Path):  # noqa: A002
        staging_parents.append(dir)
        return real_temporary_directory(prefix=prefix, dir=dir)

    def build(command: list[str], **_kwargs: object) -> SimpleNamespace:
        staging = Path(command[command.index("--outdir") + 1])
        (staging / "demo-1-py3-none-any.whl").write_bytes(b"wheel")
        (staging / "demo-1.tar.gz").write_bytes(b"sdist")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(reproducible.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setattr(reproducible.subprocess, "run", build)
    monkeypatch.setattr(reproducible, "normalize_sdist", lambda *_args: None)

    reproducible.build_distributions(repo, output_dir=output, epoch=1)

    assert staging_parents == [output]


def test_release_sbom_detects_new_declared_runtime_extra() -> None:
    project = {
        "dependencies": [],
        "optional-dependencies": {
            "dev": [],
            "ann": [],
            "future-runtime": [],
        },
    }

    with pytest.raises(ValueError, match="future-runtime"):
        release_sbom._release_requirements(project, ("ann",))
