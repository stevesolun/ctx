"""
test_toolbox_verdict.py -- Regression tests for the guardrail verdict module.

Covers:
- Evidence / Finding / Verdict dataclass round-trips and defensive parsing.
- parse_evidence() for plain-file / file:line / file:line:note / Windows paths.
- build_finding() level validation, evidence filtering, default id generation.
- record_finding() merge-by-id, level escalation, persistence.
- clear_finding() removal + level recalculation.
- load_verdict() with missing file / corrupt JSON.
- recent_verdicts() sort order and min_level filter.
- explain() and format_retro() renderers.
- CLI record/show/retro/explain/clear paths including error exit codes.
- Storage path aligns with toolbox_hooks verdict lookup
  (Path(plan_file).with_suffix(".verdict.json")).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import toolbox_verdict as tv


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolate_runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect toolbox_verdict.RUNS_DIR to a tmp folder for every test."""
    runs = tmp_path / "toolbox-runs"
    monkeypatch.setattr(tv, "RUNS_DIR", runs)
    return runs


# ── parse_evidence ─────────────────────────────────────────────────────────


def test_parse_evidence_plain_file() -> None:
    ev = tv.parse_evidence("src/foo.py")
    assert ev == tv.Evidence(file="src/foo.py", line=None, note="")


def test_parse_evidence_file_line() -> None:
    ev = tv.parse_evidence("src/foo.py:42")
    assert ev == tv.Evidence(file="src/foo.py", line=42, note="")


def test_parse_evidence_file_line_note() -> None:
    ev = tv.parse_evidence("src/foo.py:42:race on counter")
    assert ev == tv.Evidence(file="src/foo.py", line=42, note="race on counter")


def test_parse_evidence_windows_path_with_line() -> None:
    # Drive-letter colon must not be mistaken for a line delimiter.
    ev = tv.parse_evidence("C:/Users/me/foo.py:17")
    assert ev.file == "C:/Users/me/foo.py"
    assert ev.line == 17


def test_parse_evidence_windows_path_with_line_and_note() -> None:
    ev = tv.parse_evidence("C:/Users/me/foo.py:17:bad merge")
    assert ev.file == "C:/Users/me/foo.py"
    assert ev.line == 17
    assert ev.note == "bad merge"


def test_parse_evidence_empty_returns_empty_file() -> None:
    assert tv.parse_evidence("").file == ""


def test_parse_evidence_non_numeric_line_treated_as_file() -> None:
    # No digit after last colon -> whole string is the file.
    ev = tv.parse_evidence("src/foo.py:notaline")
    assert ev.file == "src/foo.py:notaline"
    assert ev.line is None


# ── Evidence / Finding from_dict ───────────────────────────────────────────


def test_evidence_from_dict_bad_line_falls_back_to_none() -> None:
    ev = tv.Evidence.from_dict({"file": "x.py", "line": "not-a-number"})
    assert ev.line is None
    assert ev.file == "x.py"


def test_finding_from_dict_unknown_level_normalises_to_low() -> None:
    f = tv.Finding.from_dict({"id": "abc", "level": "bogus", "title": "t"})
    assert f.level == "LOW"


def test_finding_from_dict_filters_non_dict_evidence() -> None:
    f = tv.Finding.from_dict({
        "id": "abc",
        "level": "HIGH",
        "title": "t",
        "evidence": [{"file": "a.py"}, "garbage", None],
    })
    assert len(f.evidence) == 1
    assert f.evidence[0].file == "a.py"


# ── build_finding ──────────────────────────────────────────────────────────


def test_build_finding_rejects_unknown_level() -> None:
    with pytest.raises(ValueError):
        tv.build_finding(level="INFO", title="t")


def test_build_finding_accepts_lowercase_level() -> None:
    f = tv.build_finding(level="high", title="t")
    assert f.level == "HIGH"


def test_build_finding_filters_empty_evidence() -> None:
    f = tv.build_finding(
        level="HIGH",
        title="t",
        evidence=["", "src/x.py:10", tv.Evidence(file=""), tv.Evidence(file="y.py")],
    )
    files = [e.file for e in f.evidence]
    assert files == ["src/x.py", "y.py"]


def test_build_finding_default_id_stable_for_same_inputs() -> None:
    a = tv.build_finding(level="HIGH", title="t", agent="reviewer", now=1.0)
    b = tv.build_finding(level="HIGH", title="t", agent="reviewer", now=2.0)
    assert a.id == b.id  # id is hash of level|agent|title, not time


def test_build_finding_default_id_differs_on_agent() -> None:
    a = tv.build_finding(level="HIGH", title="t", agent="reviewer")
    b = tv.build_finding(level="HIGH", title="t", agent="security")
    assert a.id != b.id


def test_build_finding_explicit_id_wins() -> None:
    f = tv.build_finding(level="HIGH", title="t", finding_id="custom-123")
    assert f.id == "custom-123"


# ── record_finding / clear_finding ─────────────────────────────────────────


def test_record_finding_creates_verdict_when_absent() -> None:
    f = tv.build_finding(level="MEDIUM", title="style", agent="style-bot", now=100.0)
    v = tv.record_finding("plan-1", f, now=100.0)
    assert v.plan_hash == "plan-1"
    assert v.level == "MEDIUM"
    assert len(v.findings) == 1
    assert v.created_at == 100.0
    assert v.updated_at == 100.0
    # persistence
    assert tv.verdict_path("plan-1").exists()


def test_record_finding_merges_by_id_refining_previous() -> None:
    f1 = tv.build_finding(level="LOW", title="possible leak", agent="sec", now=1.0)
    tv.record_finding("plan-2", f1, now=1.0)
    # Same id (same level|agent|title), now upgraded rationale.
    f2 = tv.build_finding(
        level="LOW", title="possible leak", agent="sec",
        rationale="confirmed after deeper trace", now=2.0,
    )
    v = tv.record_finding("plan-2", f2, now=2.0)
    assert len(v.findings) == 1
    assert v.findings[0].rationale == "confirmed after deeper trace"


def test_record_finding_escalates_to_max_level() -> None:
    tv.record_finding("plan-3", tv.build_finding(level="LOW", title="a"), now=1.0)
    v = tv.record_finding(
        "plan-3", tv.build_finding(level="CRITICAL", title="b"), now=2.0
    )
    assert v.level == "CRITICAL"
    assert v.blocks is True


def test_record_finding_preserves_created_at_on_update() -> None:
    tv.record_finding("plan-4", tv.build_finding(level="LOW", title="a"), now=10.0)
    v = tv.record_finding(
        "plan-4", tv.build_finding(level="HIGH", title="b"), now=20.0
    )
    assert v.created_at == 10.0
    assert v.updated_at == 20.0


def test_record_finding_persist_false_does_not_write() -> None:
    f = tv.build_finding(level="HIGH", title="t")
    tv.record_finding("plan-nowrite", f, persist=False)
    assert not tv.verdict_path("plan-nowrite").exists()


def test_clear_finding_removes_and_recomputes_level() -> None:
    high = tv.build_finding(level="HIGH", title="h", agent="a")
    low = tv.build_finding(level="LOW", title="l", agent="a")
    tv.record_finding("plan-5", high, now=1.0)
    tv.record_finding("plan-5", low, now=2.0)
    v = tv.clear_finding("plan-5", high.id, now=3.0)
    assert v is not None
    assert len(v.findings) == 1
    assert v.level == "LOW"
    assert v.blocks is False


def test_clear_finding_missing_verdict_returns_none() -> None:
    assert tv.clear_finding("nope", "any") is None


def test_clear_finding_missing_id_is_noop() -> None:
    tv.record_finding("plan-6", tv.build_finding(level="HIGH", title="h"), now=1.0)
    before = tv.load_verdict("plan-6")
    v = tv.clear_finding("plan-6", "nonexistent-id")
    assert v is not None
    assert v.to_dict() == before.to_dict()  # unchanged


# ── load_verdict robustness ────────────────────────────────────────────────


def test_load_verdict_missing_returns_none() -> None:
    assert tv.load_verdict("never-written") is None


def test_load_verdict_corrupt_json_returns_none(isolate_runs_dir: Path) -> None:
    isolate_runs_dir.mkdir(parents=True, exist_ok=True)
    (isolate_runs_dir / "bad.verdict.json").write_text(
        "not json {{{", encoding="utf-8"
    )
    assert tv.load_verdict("bad") is None


def test_load_verdict_non_dict_payload_returns_none(
    isolate_runs_dir: Path,
) -> None:
    isolate_runs_dir.mkdir(parents=True, exist_ok=True)
    (isolate_runs_dir / "arr.verdict.json").write_text("[1,2,3]", encoding="utf-8")
    assert tv.load_verdict("arr") is None


# ── recent_verdicts ────────────────────────────────────────────────────────


def test_recent_verdicts_sorted_by_updated_desc() -> None:
    tv.record_finding("plan-a", tv.build_finding(level="LOW", title="a"), now=1.0)
    tv.record_finding("plan-b", tv.build_finding(level="LOW", title="b"), now=3.0)
    tv.record_finding("plan-c", tv.build_finding(level="LOW", title="c"), now=2.0)
    got = [v.plan_hash for v in tv.recent_verdicts()]
    assert got == ["plan-b", "plan-c", "plan-a"]


def test_recent_verdicts_respects_limit() -> None:
    for i in range(5):
        tv.record_finding(
            f"plan-{i}", tv.build_finding(level="LOW", title=f"t{i}"), now=float(i)
        )
    assert len(tv.recent_verdicts(limit=2)) == 2


def test_recent_verdicts_filters_by_min_level() -> None:
    tv.record_finding("plan-low", tv.build_finding(level="LOW", title="l"), now=1.0)
    tv.record_finding("plan-hi", tv.build_finding(level="HIGH", title="h"), now=2.0)
    tv.record_finding(
        "plan-crit", tv.build_finding(level="CRITICAL", title="c"), now=3.0
    )
    hashes = [v.plan_hash for v in tv.recent_verdicts(min_level="HIGH")]
    assert hashes == ["plan-crit", "plan-hi"]


def test_recent_verdicts_no_runs_dir_returns_empty(
    isolate_runs_dir: Path,
) -> None:
    # isolate_runs_dir is assigned but never created; iter_verdicts must
    # handle the missing-directory case.
    assert not isolate_runs_dir.exists()
    assert tv.recent_verdicts() == ()


# ── Renderers ──────────────────────────────────────────────────────────────


def test_explain_includes_level_title_and_evidence() -> None:
    f = tv.build_finding(
        level="HIGH",
        title="SQL injection",
        agent="security-reviewer",
        evidence=["src/db.py:42:unescaped input"],
        rationale="input from req.form flows into raw SQL",
    )
    v = tv.record_finding("plan-x", f, now=1.0, persist=False)
    out = tv.explain(v)
    assert "plan=plan-x" in out
    assert "HIGH" in out
    assert "SQL injection" in out
    assert "security-reviewer" in out
    assert "src/db.py:42" in out
    assert "unescaped input" in out
    assert "input from req.form" in out


def test_explain_orders_findings_by_severity_desc() -> None:
    low = tv.build_finding(level="LOW", title="low-issue", agent="a")
    crit = tv.build_finding(level="CRITICAL", title="crit-issue", agent="a")
    tv.record_finding("plan-ord", low, now=1.0)
    v = tv.record_finding("plan-ord", crit, now=2.0)
    out = tv.explain(v)
    assert out.index("crit-issue") < out.index("low-issue")


def test_format_retro_empty_message() -> None:
    assert tv.format_retro(()) == "[retro] no verdicts yet."


def test_format_retro_marks_blocking() -> None:
    tv.record_finding(
        "plan-block", tv.build_finding(level="CRITICAL", title="c"), now=1.0
    )
    tv.record_finding("plan-ok", tv.build_finding(level="LOW", title="l"), now=2.0)
    out = tv.format_retro(tv.recent_verdicts())
    assert "BLOCK" in out
    assert "ok" in out


# ── Storage alignment with toolbox_hooks reader ────────────────────────────


def test_verdict_path_matches_hook_lookup(isolate_runs_dir: Path) -> None:
    """
    toolbox_hooks computes the verdict location as
    ``Path(plan_file).with_suffix(".verdict.json")`` where plan_file is
    ``RUNS_DIR/<hash>.json``. Our verdict_path(hash) must equal that.
    """
    plan_file = isolate_runs_dir / "abc123.json"
    expected = Path(plan_file).with_suffix(".verdict.json")
    assert tv.verdict_path("abc123") == expected


# ── CLI ────────────────────────────────────────────────────────────────────


def test_cli_record_prints_verdict_json(capsys: pytest.CaptureFixture) -> None:
    rc = tv.main([
        "record",
        "--plan-hash", "cli-1",
        "--level", "HIGH",
        "--title", "bad thing",
        "--agent", "reviewer",
        "--evidence", "src/a.py:10",
        "--rationale", "because",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan_hash"] == "cli-1"
    assert payload["level"] == "HIGH"
    assert payload["findings"][0]["title"] == "bad thing"


def test_cli_record_rejects_bad_level(capsys: pytest.CaptureFixture) -> None:
    # argparse's choices= rejects before our code runs -> exits 2 itself.
    with pytest.raises(SystemExit) as exc:
        tv.main([
            "record",
            "--plan-hash", "cli-2",
            "--level", "INFO",
            "--title", "t",
        ])
    assert exc.value.code == 2


def test_cli_show_missing_returns_1(capsys: pytest.CaptureFixture) -> None:
    rc = tv.main(["show", "--plan-hash", "nope"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No verdict" in err


def test_cli_show_json_roundtrip(capsys: pytest.CaptureFixture) -> None:
    tv.record_finding(
        "cli-3", tv.build_finding(level="LOW", title="t"), now=1.0
    )
    rc = tv.main(["show", "--plan-hash", "cli-3", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan_hash"] == "cli-3"


def test_cli_show_default_is_explain(capsys: pytest.CaptureFixture) -> None:
    tv.record_finding(
        "cli-4", tv.build_finding(level="HIGH", title="explain-me", agent="a"),
        now=1.0,
    )
    rc = tv.main(["show", "--plan-hash", "cli-4"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "explain-me" in out
    assert "HIGH" in out


def test_cli_retro_json(capsys: pytest.CaptureFixture) -> None:
    tv.record_finding(
        "cli-5", tv.build_finding(level="LOW", title="t"), now=1.0
    )
    rc = tv.main(["retro", "--limit", "5", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert any(v["plan_hash"] == "cli-5" for v in data)


def test_cli_retro_human(capsys: pytest.CaptureFixture) -> None:
    rc = tv.main(["retro"])
    assert rc == 0
    assert "[retro]" in capsys.readouterr().out


def test_cli_explain_missing_returns_1() -> None:
    assert tv.main(["explain", "--plan-hash", "ghost"]) == 1


def test_cli_clear_removes_finding(capsys: pytest.CaptureFixture) -> None:
    f = tv.build_finding(level="HIGH", title="t", agent="a")
    tv.record_finding("cli-6", f, now=1.0)
    rc = tv.main(["clear", "--plan-hash", "cli-6", "--id", f.id])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"] == []
    assert payload["level"] == "LOW"


def test_cli_clear_missing_verdict_returns_1() -> None:
    assert tv.main(["clear", "--plan-hash", "ghost", "--id", "x"]) == 1
