"""
Security-hardening tests covering:

1. plan_hash path-traversal rejection in council_runner + toolbox_verdict.
2. Cross-process advisory file-lock behavior.
3. YAML validate-path misroute (toolbox.validate now reads the given file,
   not .toolbox.yaml in its parent directory).
4. Dashboard/install regressions for active HTML and secret persistence.

Each test targets a specific fix from the Phase 4b-6 code-review pass.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parent.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import council_runner as cr  # noqa: E402
import harness_install  # noqa: E402
import toolbox as tb  # noqa: E402
import toolbox_verdict as tv  # noqa: E402
from ctx import dashboard_docs  # noqa: E402
from ctx.adapters.claude_code.install import install_utils  # noqa: E402
from ctx.adapters.claude_code.install import mcp_install  # noqa: E402
from ctx.monitor.services import lifecycle as monitor_lifecycle  # noqa: E402
from ctx.utils._file_lock import file_lock  # noqa: E402


# ── plan_hash validation ────────────────────────────────────────────────────

BAD_HASHES = [
    "../../etc/passwd",
    "..\\..\\windows\\system32",
    "a/b",
    "a\\b",
    "foo.bar",
    "foo:bar",
    "",
    "x" * 65,  # too long
    "bad hash",
    "has\nnewline",
]


@pytest.mark.parametrize("bad", BAD_HASHES)
def test_council_runner_rejects_bad_plan_hash(bad):
    with pytest.raises(ValueError, match="invalid plan_hash"):
        cr._validate_plan_hash(bad)


@pytest.mark.parametrize("bad", BAD_HASHES)
def test_toolbox_verdict_rejects_bad_plan_hash(bad):
    with pytest.raises(ValueError, match="invalid plan_hash"):
        tv._validate_plan_hash(bad)


@pytest.mark.parametrize("good", ["a", "abc123", "plan-1", "plan_2", "0123456789abcdef", "A" * 64])
def test_good_plan_hashes_accepted(good):
    assert cr._validate_plan_hash(good) == good
    assert tv._validate_plan_hash(good) == good


def test_verdict_path_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(tv, "RUNS_DIR", tmp_path)
    with pytest.raises(ValueError):
        tv.verdict_path("../../etc/passwd")


def test_council_find_cached_rejects_traversal():
    with pytest.raises(ValueError):
        cr._find_cached_plan("../escape", window_seconds=60)


def test_verdict_from_dict_rejects_traversal():
    with pytest.raises(ValueError):
        tv.Verdict.from_dict({"plan_hash": "../oops", "findings": []})


def test_plan_from_dict_rejects_traversal():
    with pytest.raises(ValueError):
        cr._plan_from_dict({"toolbox": "t", "plan_hash": "../x"}, source="s")


# ── file_lock cross-thread serialization ────────────────────────────────────


def test_file_lock_serializes_concurrent_writers(tmp_path):
    """Two threads both incrementing under the lock must see +2, not +1."""
    target = tmp_path / "counter.json"
    target.write_text(json.dumps({"n": 0}))

    def bump():
        with file_lock(target):
            data = json.loads(target.read_text())
            current = data["n"]
            time.sleep(0.05)  # widen the race window
            target.write_text(json.dumps({"n": current + 1}))

    t1 = threading.Thread(target=bump)
    t2 = threading.Thread(target=bump)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert json.loads(target.read_text())["n"] == 2


def test_file_lock_creates_parent(tmp_path):
    target = tmp_path / "nested" / "deep" / "file.json"
    with file_lock(target):
        pass
    # Only the lock file and its parent dir need exist.
    assert target.parent.exists()


# ── toolbox.validate no longer misroutes YAML paths ─────────────────────────


def test_validate_reads_supplied_yaml_file(tmp_path, capsys, monkeypatch):
    """
    Prior behavior: `toolbox validate <path>.yaml` silently validated
    <path.parent>/.toolbox.yaml instead of the supplied path. This test
    pins the new behavior: the supplied file is the one validated.
    """
    # Create a valid-looking YAML file at a non-default location.
    yaml_path = tmp_path / "custom.yaml"
    yaml_path.write_text(
        "version: 1\n"
        "toolboxes:\n"
        "  ship-it:\n"
        "    description: 'Test'\n"
        "    post: ['code-reviewer']\n"
        "active: ['ship-it']\n",
        encoding="utf-8",
    )

    # Ensure no stray .toolbox.yaml exists in the parent directory.
    stray = tmp_path / ".toolbox.yaml"
    assert not stray.exists()

    args = tb.argparse.Namespace(path=str(yaml_path))
    rc = tb.cmd_validate(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 toolbox(es)" in out


# ── dashboard/install hardening ─────────────────────────────────────────────


def _write_mcp_entity(wiki_dir: Path, slug: str, frontmatter: dict[str, str]) -> Path:
    shard = slug[0] if slug and slug[0].isalpha() else "0-9"
    path = wiki_dir / "entities" / "mcp-servers" / shard / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    lines.extend(f"{key}: {value}" for key, value in frontmatter.items())
    lines.extend(["---", "", "# MCP"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_docs_sanitizer_rewrites_unquoted_active_urls() -> None:
    cleaned = dashboard_docs.sanitize_docs_html(
        "<a href=javascript:alert(1)>bad</a><img src=data:text/html,<svg/onload=alert(1)>>"
    )

    assert "javascript:" not in cleaned.lower()
    assert "data:text/html" not in cleaned.lower()


def test_dashboard_skill_load_requires_security_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.adapters.claude_code.install import skill_install

    calls: list[dict[str, object]] = []

    class Result:
        status = "installed"
        message = "installed"
        security_scan = None

    def fake_install_skill(*_args: object, **kwargs: object) -> Result:
        calls.append(kwargs)
        return Result()

    monkeypatch.setattr(skill_install, "install_skill", fake_install_skill)

    ok, _msg = monitor_lifecycle.perform_load(
        "python-patterns",
        entity_type="skill",
        wiki_dir=lambda: tmp_path / "wiki",
        claude_dir=lambda: tmp_path / ".claude",
        audit_log_path=lambda: tmp_path / ".claude" / "ctx-audit.jsonl",
        manifest_path=lambda: tmp_path / ".claude" / "skill-manifest.json",
    )

    assert ok is True
    assert calls
    assert calls[0]["security_scan"] is True
    assert calls[0]["security_scan_required"] is True


def test_mcp_skip_path_does_not_persist_inline_secret_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki = tmp_path / "wiki"
    manifest = tmp_path / "skill-manifest.json"
    monkeypatch.setattr(install_utils, "MANIFEST_PATH", manifest)
    _write_mcp_entity(
        wiki,
        "gh",
        {
            "status": "installed",
            "install_cmd": "npx -y pkg GITHUB_TOKEN=ghp_supersecret1234567890",
        },
    )

    result = mcp_install.install_mcp("gh", wiki_dir=wiki, auto=True)

    assert result.status == "skipped-existing"
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_data["load"] == [
        {
            "skill": "gh",
            "entity_type": "mcp-server",
            "source": "ctx-mcp-install",
        }
    ]


@dataclass
class _FakeRun:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def test_harness_run_redacts_token_shaped_output_not_present_in_parent_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def fake_run(_cmd: list[str], **_kwargs: Any) -> _FakeRun:
        return _FakeRun(
            stdout=("created OPENAI_API_KEY=sk-testsecret1234567890 and ghp_supersecret1234567890")
        )

    monkeypatch.setattr(harness_install.subprocess, "run", fake_run)

    run = harness_install._run_command("python --version", cwd=tmp_path)

    assert "sk-testsecret" not in run["stdout"]
    assert "ghp_supersecret" not in run["stdout"]
    assert harness_install._REDACTION in run["stdout"]
