"""Tests for ctx_monitor — dashboard aggregation and HTML rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ctx_monitor as cm


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch) -> Path:
    """Point ctx_monitor at a throwaway ~/.claude tree."""
    claude = tmp_path / ".claude"
    (claude / "skill-quality").mkdir(parents=True)
    monkeypatch.setattr(cm, "_claude_dir", lambda: claude)
    return claude


def _write_audit(claude: Path, records: list[dict]) -> None:
    path = claude / "ctx-audit.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _write_events(claude: Path, records: list[dict]) -> None:
    path = claude / "skill-events.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _write_sidecar(claude: Path, slug: str, body: dict) -> None:
    (claude / "skill-quality" / f"{slug}.json").write_text(
        json.dumps(body), encoding="utf-8",
    )


def test_summarize_sessions_merges_audit_and_events(fake_claude: Path) -> None:
    _write_audit(fake_claude, [
        {"ts": "2026-04-19T10:00:00Z", "event": "skill.loaded",
         "subject_type": "skill", "subject": "python-patterns",
         "actor": "hook", "session_id": "S1"},
        {"ts": "2026-04-19T10:05:00Z", "event": "skill.score_updated",
         "subject_type": "skill", "subject": "python-patterns",
         "actor": "hook", "session_id": "S1"},
        {"ts": "2026-04-19T10:10:00Z", "event": "agent.loaded",
         "subject_type": "agent", "subject": "code-reviewer",
         "actor": "hook", "session_id": "S2"},
    ])
    _write_events(fake_claude, [
        {"timestamp": "2026-04-19T10:01:00Z", "event": "load",
         "skill": "fastapi-pro", "session_id": "S1"},
        {"timestamp": "2026-04-19T10:02:00Z", "event": "unload",
         "skill": "fastapi-pro", "session_id": "S1"},
    ])
    sessions = cm._summarize_sessions()
    by_id = {s["session_id"]: s for s in sessions}
    assert "S1" in by_id
    assert "S2" in by_id
    assert "python-patterns" in by_id["S1"]["skills_loaded"]
    assert "fastapi-pro" in by_id["S1"]["skills_loaded"]
    assert "fastapi-pro" in by_id["S1"]["skills_unloaded"]
    assert by_id["S1"]["score_updates"] == 1
    assert "code-reviewer" in by_id["S2"]["agents_loaded"]


def test_grade_distribution(fake_claude: Path) -> None:
    _write_sidecar(fake_claude, "a", {"slug": "a", "grade": "A", "raw_score": 0.9})
    _write_sidecar(fake_claude, "b1", {"slug": "b1", "grade": "B", "raw_score": 0.7})
    _write_sidecar(fake_claude, "b2", {"slug": "b2", "grade": "B", "raw_score": 0.6})
    _write_sidecar(fake_claude, "f", {"slug": "f", "grade": "F", "raw_score": 0.1})
    dist = cm._grade_distribution()
    assert dist["A"] == 1
    assert dist["B"] == 2
    assert dist["F"] == 1


def test_grade_distribution_skips_dotfiles_and_lifecycle(fake_claude: Path) -> None:
    _write_sidecar(fake_claude, "real", {"slug": "real", "grade": "C", "raw_score": 0.4})
    (fake_claude / "skill-quality" / ".hook-state.json").write_text("{}",
                                                                     encoding="utf-8")
    (fake_claude / "skill-quality" / "real.lifecycle.json").write_text("{}",
                                                                        encoding="utf-8")
    dist = cm._grade_distribution()
    assert sum(dist.values()) == 1  # only "real.json"


def test_session_detail_filters_by_session_id(fake_claude: Path) -> None:
    _write_audit(fake_claude, [
        {"ts": "t1", "event": "skill.loaded",
         "subject_type": "skill", "subject": "x",
         "actor": "hook", "session_id": "A"},
        {"ts": "t2", "event": "skill.loaded",
         "subject_type": "skill", "subject": "y",
         "actor": "hook", "session_id": "B"},
    ])
    _write_events(fake_claude, [
        {"timestamp": "t3", "event": "load", "skill": "z", "session_id": "A"},
    ])
    detail = cm._session_detail("A")
    assert detail["session_id"] == "A"
    assert len(detail["audit_entries"]) == 1
    assert detail["audit_entries"][0]["subject"] == "x"
    assert len(detail["load_events"]) == 1
    assert detail["load_events"][0]["skill"] == "z"


def test_render_home_has_grade_pills(fake_claude: Path) -> None:
    _write_sidecar(fake_claude, "s1", {"slug": "s1", "grade": "A", "raw_score": 0.9})
    html = cm._render_home()
    assert "ctx monitor" in html
    assert "grade-A" in html
    assert "/sessions" in html


def test_render_session_detail_escapes_html(fake_claude: Path) -> None:
    hostile = "evil</script><script>alert(1)</script>"
    _write_audit(fake_claude, [
        {"ts": "t", "event": "skill.loaded",
         "subject_type": "skill", "subject": hostile,
         "actor": "hook", "session_id": "sess"},
    ])
    html = cm._render_session_detail("sess")
    assert "<script>alert(1)</script>" not in html
    # HTML-escaped form must appear
    assert "&lt;/script&gt;" in html or "&lt;script&gt;" in html


def test_render_skills_sorts_grade_then_score(fake_claude: Path) -> None:
    _write_sidecar(fake_claude, "low", {"slug": "low", "grade": "D", "raw_score": 0.2})
    _write_sidecar(fake_claude, "mid", {"slug": "mid", "grade": "B", "raw_score": 0.6})
    _write_sidecar(fake_claude, "top", {"slug": "top", "grade": "A", "raw_score": 0.9})
    html = cm._render_skills()
    # 'top' should appear before 'mid' before 'low' in the grade-sorted output
    idx_top = html.index("top</code>")
    idx_mid = html.index("mid</code>")
    idx_low = html.index("low</code>")
    assert idx_top < idx_mid < idx_low


def test_cli_argparser_exposes_serve() -> None:
    # argparse should not raise; subcommand "serve" is required
    with pytest.raises(SystemExit):
        cm.main([])
    # Valid invocation parses (we don't actually start the server; parse_args
    # returns args but cm.main() would call serve() which blocks. So just
    # test the parser path.)
    parser = __import__("argparse").ArgumentParser()
    # Minimal smoke: main with --help exits 0.
    with pytest.raises(SystemExit) as exc:
        cm.main(["serve", "--help"])
    assert exc.value.code == 0
