"""Tests for ctx-harness-install."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import harness_install
import pytest


def _write_harness_page(
    wiki: Path,
    slug: str = "text-to-cad",
    **frontmatter: Any,
) -> Path:
    data: dict[str, Any] = {
        "title": "Text to CAD",
        "type": "harness",
        "status": "cataloged",
        "repo_url": "https://github.com/earthtojake/text-to-cad",
        "tags": ["cad", "3d"],
        "runtimes": ["python", "node"],
        "setup_commands": ["python -m pip install -e ."],
        "verify_commands": ["python -m pytest"],
    }
    data.update(frontmatter)
    page = wiki / "entities" / "harnesses" / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.extend(["---", "", "# Harness"])
    page.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return page


@dataclass
class _FakeRun:
    returncode: int = 0
    stdout: str = "ok"
    stderr: str = ""


def test_dry_run_prints_plan_without_writing(tmp_path: Path, capsys: Any) -> None:
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki)
    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
        dry_run=True,
    )

    assert result.status == "dry-run"
    assert not (tmp_path / "installs").exists()
    assert "Text to CAD" in capsys.readouterr().out


def test_install_copies_local_source_and_writes_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("harness", encoding="utf-8")
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))

    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
        allow_local_sources=True,
    )

    assert result.status == "installed"
    assert (tmp_path / "installs" / "text-to-cad" / "README.md").exists()
    manifest = json.loads(
        (tmp_path / "manifests" / "text-to-cad.json").read_text(encoding="utf-8")
    )
    assert manifest["slug"] == "text-to-cad"
    assert manifest["status"] == "installed"
    assert {Path(path).as_posix() for path in manifest["attach_files"]} == {
        ".ctx/attach/README.md",
        ".ctx/attach/ctx-run.txt",
        ".ctx/attach/mcp.json",
        ".ctx/attach/python.py",
    }
    assert manifest["setup_commands_run"] == []
    assert manifest["verify_commands_run"] == []
    attach_dir = tmp_path / "installs" / "text-to-cad" / ".ctx" / "attach"
    assert json.loads((attach_dir / "mcp.json").read_text(encoding="utf-8")) == {
        "mcpServers": {"ctx-wiki": {"command": "ctx-mcp-server", "args": []}}
    }
    assert "recommend_bundle" in (attach_dir / "python.py").read_text(encoding="utf-8")
    assert "ctx run --model" in (attach_dir / "ctx-run.txt").read_text(encoding="utf-8")


def test_write_manifest_uses_atomic_json_writer(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    calls: list[tuple[Path, dict[str, Any], int | None]] = []

    def fake_atomic_write_json(
        path: Path,
        obj: Any,
        indent: int | None = 2,
    ) -> None:
        assert isinstance(obj, dict)
        calls.append((path, obj, indent))
        path.write_text(json.dumps(obj, indent=indent) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        harness_install,
        "atomic_write_json",
        fake_atomic_write_json,
        raising=False,
    )
    record = harness_install.HarnessRecord(
        slug="text-to-cad",
        path=tmp_path / "page.md",
        title="Text to CAD",
        repo_url="https://github.com/earthtojake/text-to-cad",
        docs_url=None,
        tags=("cad",),
        runtimes=("python",),
        model_providers=("openai",),
        capabilities=("Generate CAD",),
        attach_modes=("mcp", "python-library", "ctx-run"),
        setup_commands=(),
        verify_commands=(),
    )

    path = harness_install._write_manifest(
        record=record,
        target=tmp_path / "installs" / "text-to-cad",
        manifest_dir=tmp_path / "manifests",
        setup_runs=[],
        verify_runs=[],
        attach_files=[],
    )

    assert path == tmp_path / "manifests" / "text-to-cad.json"
    assert calls == [(path, json.loads(path.read_text(encoding="utf-8")), 2)]


def test_install_respects_catalog_attach_modes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source), attach_modes=["mcp"])

    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
        allow_local_sources=True,
    )

    assert result.status == "installed"
    attach_dir = tmp_path / "installs" / "text-to-cad" / ".ctx" / "attach"
    assert (attach_dir / "README.md").exists()
    assert (attach_dir / "mcp.json").exists()
    assert not (attach_dir / "python.py").exists()
    assert not (attach_dir / "ctx-run.txt").exists()


def test_dry_run_does_not_write_attach_files(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki)
    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
        dry_run=True,
    )

    assert result.status == "dry-run"
    assert not (tmp_path / "installs" / "text-to-cad" / ".ctx").exists()


def test_install_accepts_file_uri_local_source_with_opt_in(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("harness", encoding="utf-8")
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=source.as_uri())

    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
        allow_local_sources=True,
    )

    assert result.status == "installed"
    assert (tmp_path / "installs" / "text-to-cad" / "README.md").exists()


def test_install_refuses_local_source_without_explicit_opt_in(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))

    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
    )

    assert result.status == "install-failed"
    assert "--allow-local-source" in result.message
    assert not (tmp_path / "installs" / "text-to-cad").exists()


def test_install_failure_after_target_replace_rolls_back_new_target(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("harness", encoding="utf-8")
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))

    def fail_attach(*_args: Any, **_kwargs: Any) -> list[Path]:
        raise OSError("attach write failed")

    monkeypatch.setattr(harness_install, "_write_attach_files", fail_attach)

    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
        allow_local_sources=True,
    )

    assert result.status == "install-failed"
    assert "attach write failed" in result.message
    assert not (tmp_path / "installs" / "text-to-cad").exists()
    assert not (tmp_path / "manifests" / "text-to-cad.json").exists()


def test_install_refuses_symlink_inside_local_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        (source / "leak.txt").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))

    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
        allow_local_sources=True,
    )

    assert result.status == "install-failed"
    assert "symlink inside harness source" in result.message


def test_setup_and_verify_commands_require_explicit_flags(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeRun:
        calls.append(cmd)
        return _FakeRun()

    monkeypatch.setattr(harness_install.subprocess, "run", fake_run)

    harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs-a",
        manifest_dir=tmp_path / "manifests-a",
        allow_local_sources=True,
    )
    assert calls == []

    harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs-b",
        manifest_dir=tmp_path / "manifests-b",
        approve_commands=True,
        run_verify=True,
        allow_local_sources=True,
    )
    assert Path(calls[0][0]).name.lower().startswith("python")
    assert calls[0][1:] == ["-m", "pip", "install", "-e", "."]
    assert Path(calls[1][0]).name.lower().startswith("python")
    assert calls[1][1:] == ["-m", "pytest"]


def test_target_must_stay_inside_installs_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))

    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
        target=tmp_path / "outside",
    )

    assert result.status == "invalid-target"
    assert not (tmp_path / "outside").exists()


def test_target_cannot_be_installs_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))
    installs_root = tmp_path / "installs"

    result = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=installs_root,
        manifest_dir=tmp_path / "manifests",
        target=installs_root,
        allow_local_sources=True,
    )

    assert result.status == "invalid-target"
    assert not installs_root.exists()


def test_repo_url_identifier_resolves_matching_page(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url="https://github.com/earthtojake/text-to-cad")

    record = harness_install.resolve_harness(
        "https://github.com/earthtojake/text-to-cad",
        wiki_path=wiki,
    )

    assert record.slug == "text-to-cad"


def test_missing_harness_fails_clearly(tmp_path: Path) -> None:
    result = harness_install.install_harness(
        "missing",
        wiki_path=tmp_path / "wiki",
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
    )

    assert result.status == "not-found"
    assert "missing" in result.message


def test_uninstall_removes_target_and_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("v1", encoding="utf-8")
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))
    manifest_dir = tmp_path / "manifests"
    install = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=manifest_dir,
        allow_local_sources=True,
    )
    assert install.target is not None
    assert install.manifest_path is not None

    result = harness_install.uninstall_harness(
        "text-to-cad",
        manifest_dir=manifest_dir,
    )

    assert result.status == "uninstalled"
    assert not install.target.exists()
    assert not install.manifest_path.exists()


def test_uninstall_marks_manifest_uninstalling_before_deleting_files(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("v1", encoding="utf-8")
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))
    manifest_dir = tmp_path / "manifests"
    install = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=manifest_dir,
        allow_local_sources=True,
    )
    assert install.target is not None
    assert install.manifest_path is not None

    original_unlink = Path.unlink

    def fail_manifest_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == install.manifest_path:
            raise OSError("crash after deleting files")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_manifest_unlink)

    result = harness_install.uninstall_harness(
        "text-to-cad",
        manifest_dir=manifest_dir,
    )

    assert result.status == "uninstall-failed"
    assert not install.target.exists()
    manifest = json.loads(install.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "uninstalling"


def test_uninstall_keep_files_only_removes_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("v1", encoding="utf-8")
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))
    manifest_dir = tmp_path / "manifests"
    install = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=manifest_dir,
        allow_local_sources=True,
    )
    assert install.target is not None
    assert install.manifest_path is not None

    result = harness_install.uninstall_harness(
        "text-to-cad",
        manifest_dir=manifest_dir,
        keep_files=True,
    )

    assert result.status == "uninstalled"
    assert install.target.exists()
    assert not install.manifest_path.exists()


def test_uninstall_refuses_manifest_target_at_installs_root(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    installs_root = tmp_path / "installs"
    installs_root.mkdir()
    sibling = installs_root / "other"
    sibling.mkdir()
    (manifest_dir / "text-to-cad.json").write_text(
        json.dumps({"slug": "text-to-cad", "target": str(installs_root)}),
        encoding="utf-8",
    )

    result = harness_install.uninstall_harness(
        "text-to-cad",
        manifest_dir=manifest_dir,
        installs_root=installs_root,
    )

    assert result.status == "invalid-target"
    assert installs_root.exists()
    assert sibling.exists()


def test_update_replaces_installed_target_from_catalog_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("v1", encoding="utf-8")
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))
    manifest_dir = tmp_path / "manifests"
    installs_root = tmp_path / "installs"
    install = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=installs_root,
        manifest_dir=manifest_dir,
        allow_local_sources=True,
    )
    assert install.target is not None
    (source / "README.md").write_text("v2", encoding="utf-8")

    result = harness_install.update_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=installs_root,
        manifest_dir=manifest_dir,
        allow_local_sources=True,
    )

    assert result.status == "updated"
    assert (install.target / "README.md").read_text(encoding="utf-8") == "v2"


def test_update_failure_preserves_existing_target_and_manifest(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("v1", encoding="utf-8")
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki, repo_url=str(source))
    manifest_dir = tmp_path / "manifests"
    installs_root = tmp_path / "installs"
    install = harness_install.install_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=installs_root,
        manifest_dir=manifest_dir,
        allow_local_sources=True,
    )
    assert install.target is not None
    assert install.manifest_path is not None
    before_manifest = install.manifest_path.read_text(encoding="utf-8")
    (source / "README.md").write_text("v2", encoding="utf-8")

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeRun:
        return _FakeRun(returncode=1, stderr="setup failed")

    monkeypatch.setattr(harness_install.subprocess, "run", fake_run)

    result = harness_install.update_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=installs_root,
        manifest_dir=manifest_dir,
        approve_commands=True,
        allow_local_sources=True,
    )

    assert result.status == "install-failed"
    assert (install.target / "README.md").read_text(encoding="utf-8") == "v1"
    assert install.manifest_path.read_text(encoding="utf-8") == before_manifest


def test_update_requires_existing_manifest(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki)

    result = harness_install.update_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=tmp_path / "installs",
        manifest_dir=tmp_path / "manifests",
    )

    assert result.status == "not-installed"


def test_update_dry_run_validates_manifest_target(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _write_harness_page(wiki)
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    installs_root = tmp_path / "installs"
    installs_root.mkdir()
    (manifest_dir / "text-to-cad.json").write_text(
        json.dumps({"slug": "text-to-cad", "target": str(installs_root)}),
        encoding="utf-8",
    )

    result = harness_install.update_harness(
        "text-to-cad",
        wiki_path=wiki,
        installs_root=installs_root,
        manifest_dir=manifest_dir,
        dry_run=True,
    )

    assert result.status == "invalid-target"


def test_cataloged_commands_use_sanitized_env_and_redact_output(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    captured_env: dict[str, str] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeRun:
        captured_env.update(kwargs["env"])
        return _FakeRun(stdout="token sk-secret-value")

    monkeypatch.setattr(harness_install.subprocess, "run", fake_run)

    run = harness_install._run_command("python --version", cwd=tmp_path)

    assert "OPENAI_API_KEY" not in captured_env
    assert run["stdout"] == f"token {harness_install._REDACTION}"


def test_run_command_resolves_bare_executable(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    resolved = str(tmp_path / "npx.cmd")
    captured_cmd: list[str] = []

    def fake_which(command: str, *, path: str | None = None) -> str | None:
        return resolved if command == "npx" else None

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeRun:
        captured_cmd.extend(cmd)
        return _FakeRun(stdout="9.0.0")

    monkeypatch.setattr(harness_install.shutil, "which", fake_which)
    monkeypatch.setattr(harness_install.subprocess, "run", fake_run)

    run = harness_install._run_command("npx --version", cwd=tmp_path)

    assert run["returncode"] == 0
    assert captured_cmd[0] == resolved


def test_split_command_preserves_windows_backslashes(monkeypatch: Any) -> None:
    monkeypatch.setattr(harness_install.os, "name", "nt")

    parts = harness_install._split_command(r'python "C:\Users\me\script.py"')

    assert parts == ["python", r"C:\Users\me\script.py"]


def test_failed_run_message_includes_redacted_output() -> None:
    message = harness_install._failed_run_message(
        "setup",
        "npm install",
        {"stderr": "token leaked", "stdout": ""},
    )

    assert message == "setup command failed: npm install: token leaked"


def test_recommend_mode_prints_install_handoff(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_recommend(**kwargs: Any) -> list[dict[str, object]]:
        calls.append(kwargs)
        return [{
            "name": "text-to-cad",
            "normalized_score": 0.91,
            "reason": "cad tag match",
        }]

    monkeypatch.setattr(harness_install, "recommend_harnesses_for_cli", fake_recommend)

    rc = harness_install.main([
        "--recommend",
        "--goal",
        "generate CAD from text",
        "--model-provider",
        "openai",
        "--model",
        "openai/gpt-5.5",
    ])

    assert rc == 0
    assert calls[0]["model_provider"] == "openai"
    assert calls[0]["model"] == "openai/gpt-5.5"
    output = capsys.readouterr().out
    assert "Recommended harnesses" in output
    assert "ctx-harness-install text-to-cad --dry-run" in output


def test_recommend_no_fit_prints_custom_harness_plan(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    monkeypatch.setattr(harness_install, "recommend_harnesses_for_cli", lambda **_: [])

    rc = harness_install.main([
        "--recommend",
        "--goal",
        "build a private CAD workflow with a local model",
        "--model-provider",
        "ollama",
        "--model",
        "ollama/llama3.1",
        "--plan-on-no-fit",
    ])

    assert rc == 0
    output = capsys.readouterr().out
    assert "No harness recommendations matched." in output
    assert "# Custom Harness PRD" in output
    assert "ctx-mcp-server" in output
    assert "ctx.recommend_bundle" in output
    assert "Windows, macOS, and Linux" in output


def test_recommend_no_fit_writes_custom_harness_plan(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    monkeypatch.setattr(harness_install, "recommend_harnesses_for_cli", lambda **_: [])
    target = tmp_path / "custom-harness.md"

    rc = harness_install.main([
        "--recommend",
        "--goal",
        "repair a legacy Python service",
        "--model",
        "openrouter/openai/gpt-5.5",
        "--plan-on-no-fit",
        "--plan-output",
        str(target),
    ])

    assert rc == 0
    assert f"Custom harness plan: {target}" in capsys.readouterr().out
    text = target.read_text(encoding="utf-8")
    assert "repair a legacy Python service" in text
    assert "openrouter/openai/gpt-5.5" in text
    assert "Build the harness described above" in text
