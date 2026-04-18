"""
test_skill_health.py -- Regression tests for the skill-health dashboard.

Covers:
- _split_frontmatter (valid / missing / unterminated / quoted values).
- _inspect / scan_skills / scan_agents (issue codes, severity ranking).
- EntityHealth.severity property ordering.
- detect_drift (manifest orphans, pending orphans, missing / corrupt JSON).
- build_report (tally totals, injected ``now``).
- format_dashboard (errors first, warnings grouped, all-healthy banner, drift).
- heal (_heal_manifest / _heal_pending atomic writes, no-op when clean).
- HealthReport.has_errors mixes severity + drift.
- CLI scan / dashboard / check --strict / heal exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import skill_health as sh


# ── Helpers ────────────────────────────────────────────────────────────────


def _write_skill(root: Path, name: str, body: str, *,
                 fm_name: str | None = None,
                 fm_description: str | None = None,
                 include_frontmatter: bool = True) -> Path:
    """Create a SKILL.md under root/<name>/. Returns the file path."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    path = d / "SKILL.md"
    fm_lines = []
    if include_frontmatter:
        fm_lines.append("---")
        if fm_name is not None:
            fm_lines.append(f"name: {fm_name}")
        if fm_description is not None:
            fm_lines.append(f"description: {fm_description}")
        fm_lines.append("---")
        fm_lines.append("")
    text = "\n".join(fm_lines) + body
    path.write_text(text, encoding="utf-8")
    return path


def _write_agent(root: Path, name: str, body: str, *,
                 fm_name: str | None = None,
                 fm_description: str | None = None,
                 include_frontmatter: bool = True) -> Path:
    """Create an agent .md under root/<name>.md."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.md"
    fm_lines = []
    if include_frontmatter:
        fm_lines.append("---")
        if fm_name is not None:
            fm_lines.append(f"name: {fm_name}")
        if fm_description is not None:
            fm_lines.append(f"description: {fm_description}")
        fm_lines.append("---")
        fm_lines.append("")
    text = "\n".join(fm_lines) + body
    path.write_text(text, encoding="utf-8")
    return path


def _healthy_body(n_lines: int = 20) -> str:
    return "\n".join(f"line {i}: content" for i in range(n_lines)) + "\n"


# ── _split_frontmatter ─────────────────────────────────────────────────────


def test_split_frontmatter_valid() -> None:
    text = "---\nname: foo\ndescription: bar\n---\nbody\n"
    fields, body = sh._split_frontmatter(text)
    assert fields == {"name": "foo", "description": "bar"}
    # splitlines drops the trailing newline; body content must still appear.
    assert "body" in body


def test_split_frontmatter_strips_quotes() -> None:
    text = '---\nname: "foo"\ndescription: \'bar\'\n---\nbody\n'
    fields, _ = sh._split_frontmatter(text)
    assert fields == {"name": "foo", "description": "bar"}


def test_split_frontmatter_no_fence_returns_empty() -> None:
    fields, body = sh._split_frontmatter("no frontmatter here\n")
    assert fields == {}
    assert body == "no frontmatter here\n"


def test_split_frontmatter_unterminated_returns_empty() -> None:
    text = "---\nname: foo\n(no closing fence)\n"
    fields, body = sh._split_frontmatter(text)
    assert fields == {}
    # When no closing fence, body returned is the original text.
    assert body == text


def test_split_frontmatter_empty_string() -> None:
    fields, body = sh._split_frontmatter("")
    assert fields == {}
    assert body == ""


def test_split_frontmatter_skips_lines_without_colon() -> None:
    text = "---\nname: foo\nrandom text\ndescription: bar\n---\nbody\n"
    fields, _ = sh._split_frontmatter(text)
    assert fields == {"name": "foo", "description": "bar"}


# ── _inspect ───────────────────────────────────────────────────────────────


def test_inspect_healthy_skill(tmp_path: Path) -> None:
    p = _write_skill(tmp_path, "good", _healthy_body(20),
                     fm_name="good", fm_description="a healthy skill")
    h = sh._inspect(p, "skill", "good", line_threshold=180, min_body_lines=5)
    assert h.severity == "ok"
    assert h.has_frontmatter is True
    assert h.issues == ()


def test_inspect_no_frontmatter(tmp_path: Path) -> None:
    d = tmp_path / "no-fm"
    d.mkdir()
    p = d / "SKILL.md"
    p.write_text(_healthy_body(10), encoding="utf-8")
    h = sh._inspect(p, "skill", "no-fm",
                    line_threshold=180, min_body_lines=5)
    codes = {i.code for i in h.issues}
    assert "no-frontmatter" in codes
    assert h.severity == "error"


def test_inspect_missing_name(tmp_path: Path) -> None:
    p = _write_skill(tmp_path, "noname", _healthy_body(10),
                     fm_description="has a description")
    h = sh._inspect(p, "skill", "noname",
                    line_threshold=180, min_body_lines=5)
    codes = {i.code for i in h.issues}
    assert "frontmatter-missing-name" in codes
    assert h.severity == "error"


def test_inspect_missing_description(tmp_path: Path) -> None:
    p = _write_skill(tmp_path, "nodesc", _healthy_body(10),
                     fm_name="nodesc")
    h = sh._inspect(p, "skill", "nodesc",
                    line_threshold=180, min_body_lines=5)
    codes = {i.code for i in h.issues}
    assert "frontmatter-missing-description" in codes
    # warning alone should not escalate to error
    assert h.severity == "warning"


def test_inspect_empty_body(tmp_path: Path) -> None:
    p = _write_skill(tmp_path, "empty", "\n",
                     fm_name="empty", fm_description="nothing here")
    h = sh._inspect(p, "skill", "empty",
                    line_threshold=180, min_body_lines=5)
    codes = {i.code for i in h.issues}
    assert "empty-body" in codes
    assert h.severity == "error"


def test_inspect_over_threshold(tmp_path: Path) -> None:
    body = _healthy_body(200)
    p = _write_skill(tmp_path, "big", body,
                     fm_name="big", fm_description="too big")
    h = sh._inspect(p, "skill", "big",
                    line_threshold=180, min_body_lines=5)
    codes = {i.code for i in h.issues}
    assert "over-threshold" in codes
    assert h.severity == "warning"


def test_inspect_unreadable_uses_error_issue(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch
                                             ) -> None:
    p = tmp_path / "bad.md"
    p.write_text("content", encoding="utf-8")
    monkeypatch.setattr(sh, "_read_safe", lambda _p: None)
    h = sh._inspect(p, "skill", "bad",
                    line_threshold=180, min_body_lines=5)
    assert h.issues[0].code == "unreadable"
    assert h.severity == "error"
    assert h.has_frontmatter is False


def test_entity_health_severity_ranks_error_over_warning() -> None:
    h = sh.EntityHealth(
        name="x", kind="skill", path="/tmp/x",
        lines=10, has_frontmatter=True,
        issues=(
            sh.Issue("over-threshold", "warning", "w"),
            sh.Issue("empty-body", "error", "e"),
        ),
    )
    assert h.severity == "error"


def test_entity_health_to_dict_injects_severity() -> None:
    h = sh.EntityHealth(
        name="x", kind="skill", path="/tmp/x",
        lines=10, has_frontmatter=True,
        issues=(sh.Issue("frontmatter-missing-description", "warning", "w"),),
    )
    d = h.to_dict()
    assert d["severity"] == "warning"
    assert d["name"] == "x"


# ── scan_skills / scan_agents ──────────────────────────────────────────────


def test_scan_skills_missing_dir(tmp_path: Path) -> None:
    assert sh.scan_skills(tmp_path / "does-not-exist",
                          line_threshold=180, min_body_lines=5) == []


def test_scan_skills_flags_missing_skill_md(tmp_path: Path) -> None:
    (tmp_path / "orphan").mkdir()
    results = sh.scan_skills(tmp_path,
                             line_threshold=180, min_body_lines=5)
    assert len(results) == 1
    assert results[0].issues[0].code == "missing-file"
    assert results[0].severity == "error"


def test_scan_skills_ignores_non_directories(tmp_path: Path) -> None:
    (tmp_path / "loose.md").write_text("ignore me", encoding="utf-8")
    _write_skill(tmp_path, "real", _healthy_body(),
                 fm_name="real", fm_description="desc")
    names = [e.name for e in sh.scan_skills(tmp_path,
                                            line_threshold=180,
                                            min_body_lines=5)]
    assert names == ["real"]


def test_scan_agents_flat_md(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    _write_agent(agents, "agent-a", _healthy_body(),
                 fm_name="agent-a", fm_description="ok")
    _write_agent(agents, "agent-b", _healthy_body(),
                 fm_name="agent-b", fm_description="ok")
    results = sh.scan_agents(agents, line_threshold=180, min_body_lines=5)
    names = sorted(e.name for e in results)
    assert names == ["agent-a", "agent-b"]
    assert all(e.kind == "agent" for e in results)


def test_scan_agents_missing_dir(tmp_path: Path) -> None:
    assert sh.scan_agents(tmp_path / "nope",
                          line_threshold=180, min_body_lines=5) == []


# ── detect_drift ───────────────────────────────────────────────────────────


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_detect_drift_finds_manifest_orphans(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    pending = tmp_path / "pending.json"
    _write_json(manifest, {"load": [
        {"skill": "alive"},
        {"skill": "ghost"},
    ]})
    entities = (
        sh.EntityHealth(name="alive", kind="skill", path="/tmp/alive",
                        lines=10, has_frontmatter=True, issues=()),
    )
    drift = sh.detect_drift(entities, manifest, pending)
    assert drift.orphaned_manifest == ("ghost",)
    assert drift.orphaned_pending == ()
    assert drift.empty is False


def test_detect_drift_finds_pending_orphans(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    pending = tmp_path / "pending.json"
    _write_json(pending, {
        "graph_suggestions": [
            {"name": "suggested-ghost"},
            {"name": "real-one"},
        ],
        "unmatched_signals": ["stray-signal"],
    })
    entities = (
        sh.EntityHealth(name="real-one", kind="skill", path="/tmp/real",
                        lines=10, has_frontmatter=True, issues=()),
    )
    drift = sh.detect_drift(entities, manifest, pending)
    assert drift.orphaned_manifest == ()
    assert set(drift.orphaned_pending) == {"suggested-ghost", "stray-signal"}


def test_detect_drift_missing_files_returns_clean(tmp_path: Path) -> None:
    drift = sh.detect_drift((), tmp_path / "none.json", tmp_path / "none.json")
    assert drift.empty is True


def test_detect_drift_ignores_corrupt_json(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{not valid json", encoding="utf-8")
    pending = tmp_path / "pending.json"
    pending.write_text("also bad", encoding="utf-8")
    drift = sh.detect_drift((), manifest, pending)
    assert drift.empty is True


def test_detect_drift_ignores_non_dict_json(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[1,2,3]", encoding="utf-8")
    drift = sh.detect_drift((), manifest, tmp_path / "nope.json")
    assert drift.empty is True


# ── build_report ───────────────────────────────────────────────────────────


def test_build_report_tallies_and_includes_drift(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    agents = tmp_path / "agents"
    _write_skill(skills, "ok", _healthy_body(),
                 fm_name="ok", fm_description="good")
    _write_skill(skills, "err", _healthy_body(),
                 include_frontmatter=False)  # -> no-frontmatter error
    _write_agent(agents, "warn", _healthy_body(),
                 fm_name="warn")             # -> missing description warn

    manifest = tmp_path / "manifest.json"
    _write_json(manifest, {"load": [{"skill": "ok"}, {"skill": "ghost"}]})
    pending = tmp_path / "pending.json"

    report = sh.build_report(
        skills_dir=skills, agents_dir=agents,
        line_threshold=180, min_body_lines=5,
        manifest_path=manifest, pending_path=pending,
        now=1234.0,
    )

    assert report.generated_at == 1234.0
    totals = report.totals
    assert totals["total"] == 3
    assert totals["ok"] == 1
    assert totals["warning"] == 1
    assert totals["error"] == 1
    assert report.drift.orphaned_manifest == ("ghost",)
    assert report.has_errors is True


def test_build_report_all_clean(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "ok", _healthy_body(),
                 fm_name="ok", fm_description="good")
    report = sh.build_report(
        skills_dir=skills, agents_dir=tmp_path / "agents",
        line_threshold=180, min_body_lines=5,
        manifest_path=tmp_path / "nope1.json",
        pending_path=tmp_path / "nope2.json",
        now=1.0,
    )
    assert report.totals["error"] == 0
    assert report.has_errors is False


# ── format_dashboard ───────────────────────────────────────────────────────


def test_format_dashboard_all_healthy(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "ok", _healthy_body(),
                 fm_name="ok", fm_description="good")
    report = sh.build_report(
        skills_dir=skills, agents_dir=tmp_path / "a",
        line_threshold=180, min_body_lines=5,
        manifest_path=tmp_path / "m.json",
        pending_path=tmp_path / "p.json",
        now=1.0,
    )
    out = sh.format_dashboard(report)
    assert "All healthy." in out
    assert "ok=1" in out


def test_format_dashboard_orders_errors_before_warnings(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "a-warn", _healthy_body(),
                 fm_name="a-warn")  # warning only
    _write_skill(skills, "b-err", _healthy_body(),
                 include_frontmatter=False)  # error
    report = sh.build_report(
        skills_dir=skills, agents_dir=tmp_path / "a",
        line_threshold=180, min_body_lines=5,
        manifest_path=tmp_path / "m.json",
        pending_path=tmp_path / "p.json",
        now=1.0,
    )
    out = sh.format_dashboard(report)
    err_pos = out.index("[error]")
    warn_pos = out.index("[warning]")
    assert err_pos < warn_pos
    assert "b-err" in out
    assert "a-warn" in out


def test_format_dashboard_drift_section(tmp_path: Path) -> None:
    manifest = tmp_path / "m.json"
    _write_json(manifest, {"load": [{"skill": "ghost"}]})
    pending = tmp_path / "p.json"
    _write_json(pending, {"unmatched_signals": ["stray"]})
    report = sh.build_report(
        skills_dir=tmp_path / "s", agents_dir=tmp_path / "a",
        line_threshold=180, min_body_lines=5,
        manifest_path=manifest, pending_path=pending,
        now=1.0,
    )
    out = sh.format_dashboard(report)
    assert "Drift:" in out
    assert "ghost" in out
    assert "stray" in out


# ── heal ───────────────────────────────────────────────────────────────────


def test_heal_noop_when_clean(tmp_path: Path) -> None:
    report = sh.HealthReport(
        generated_at=1.0, entities=(), drift=sh.DriftReport(), totals={},
    )
    result = sh.heal(report, tmp_path / "m.json", tmp_path / "p.json")
    assert result.empty is True


def test_heal_removes_manifest_orphans(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    _write_json(manifest, {"load": [
        {"skill": "alive"}, {"skill": "ghost-a"}, {"skill": "ghost-b"},
    ]})
    report = sh.HealthReport(
        generated_at=1.0, entities=(),
        drift=sh.DriftReport(orphaned_manifest=("ghost-a", "ghost-b")),
        totals={},
    )
    result = sh.heal(report, manifest, tmp_path / "p.json")
    assert set(result.manifest_removed) == {"ghost-a", "ghost-b"}
    after = json.loads(manifest.read_text(encoding="utf-8"))
    assert after["load"] == [{"skill": "alive"}]


def test_heal_removes_pending_orphans(tmp_path: Path) -> None:
    pending = tmp_path / "pending.json"
    _write_json(pending, {
        "graph_suggestions": [
            {"name": "keep-me"},
            {"name": "ghost"},
        ],
        "unmatched_signals": ["stray", "kept-signal"],
    })
    report = sh.HealthReport(
        generated_at=1.0, entities=(),
        drift=sh.DriftReport(orphaned_pending=("ghost", "stray")),
        totals={},
    )
    result = sh.heal(report, tmp_path / "m.json", pending)
    assert set(result.pending_removed) == {"ghost", "stray"}
    after = json.loads(pending.read_text(encoding="utf-8"))
    assert after["graph_suggestions"] == [{"name": "keep-me"}]
    assert after["unmatched_signals"] == ["kept-signal"]


def test_heal_skips_missing_files(tmp_path: Path) -> None:
    report = sh.HealthReport(
        generated_at=1.0, entities=(),
        drift=sh.DriftReport(orphaned_manifest=("ghost",),
                             orphaned_pending=("stray",)),
        totals={},
    )
    result = sh.heal(report, tmp_path / "absent1.json",
                     tmp_path / "absent2.json")
    assert result.empty is True


def test_heal_manifest_no_change_when_orphans_not_present(tmp_path: Path
                                                          ) -> None:
    manifest = tmp_path / "manifest.json"
    _write_json(manifest, {"load": [{"skill": "alive"}]})
    report = sh.HealthReport(
        generated_at=1.0, entities=(),
        drift=sh.DriftReport(orphaned_manifest=("ghost",)),
        totals={},
    )
    result = sh.heal(report, manifest, tmp_path / "p.json")
    assert result.manifest_removed == ()


# ── HealResult ─────────────────────────────────────────────────────────────


def test_heal_result_to_dict() -> None:
    r = sh.HealResult(manifest_removed=("a",), pending_removed=("b",))
    assert r.to_dict() == {
        "manifest_removed": ("a",),
        "pending_removed": ("b",),
    }
    assert r.empty is False


# ── CLI ────────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_cli(tmp_path: Path,
                 monkeypatch: pytest.MonkeyPatch) -> dict:
    """Redirect all module-level paths at tmp_path for CLI invocations."""
    skills = tmp_path / "skills"
    agents = tmp_path / "agents"
    manifest = tmp_path / "manifest.json"
    pending = tmp_path / "pending.json"
    monkeypatch.setattr(sh, "SKILLS_DIR", skills)
    monkeypatch.setattr(sh, "AGENTS_DIR", agents)
    monkeypatch.setattr(sh, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(sh, "PENDING_PATH", pending)
    return {
        "skills": skills,
        "agents": agents,
        "manifest": manifest,
        "pending": pending,
    }


def test_cli_scan_outputs_json(isolated_cli: dict,
                               capsys: pytest.CaptureFixture) -> None:
    _write_skill(isolated_cli["skills"], "ok", _healthy_body(),
                 fm_name="ok", fm_description="good")
    rc = sh.main(["scan"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["totals"]["total"] == 1
    assert data["entities"][0]["name"] == "ok"


def test_cli_dashboard_prints_human_summary(isolated_cli: dict,
                                            capsys: pytest.CaptureFixture
                                            ) -> None:
    _write_skill(isolated_cli["skills"], "ok", _healthy_body(),
                 fm_name="ok", fm_description="good")
    rc = sh.main(["dashboard"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[health]" in out
    assert "All healthy." in out


def test_cli_check_strict_nonzero_on_error(isolated_cli: dict,
                                           capsys: pytest.CaptureFixture
                                           ) -> None:
    _write_skill(isolated_cli["skills"], "bad", _healthy_body(),
                 include_frontmatter=False)  # no-frontmatter error
    rc = sh.main(["check", "--strict"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "[error]" in out


def test_cli_check_strict_zero_when_clean(isolated_cli: dict) -> None:
    _write_skill(isolated_cli["skills"], "ok", _healthy_body(),
                 fm_name="ok", fm_description="good")
    rc = sh.main(["check", "--strict"])
    assert rc == 0


def test_cli_check_without_strict_always_zero(isolated_cli: dict) -> None:
    _write_skill(isolated_cli["skills"], "bad", _healthy_body(),
                 include_frontmatter=False)
    rc = sh.main(["check"])
    assert rc == 0


def test_cli_heal_removes_orphans(isolated_cli: dict,
                                  capsys: pytest.CaptureFixture) -> None:
    _write_skill(isolated_cli["skills"], "alive", _healthy_body(),
                 fm_name="alive", fm_description="good")
    _write_json(isolated_cli["manifest"], {"load": [
        {"skill": "alive"}, {"skill": "ghost"},
    ]})
    rc = sh.main(["heal"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ghost" in out
    after = json.loads(isolated_cli["manifest"].read_text(encoding="utf-8"))
    assert after["load"] == [{"skill": "alive"}]


def test_cli_heal_noop(isolated_cli: dict,
                       capsys: pytest.CaptureFixture) -> None:
    _write_skill(isolated_cli["skills"], "ok", _healthy_body(),
                 fm_name="ok", fm_description="good")
    rc = sh.main(["heal"])
    assert rc == 0
    assert "nothing to do" in capsys.readouterr().out


def test_cli_unknown_subcommand_errors(isolated_cli: dict) -> None:
    with pytest.raises(SystemExit) as exc:
        sh.main(["bogus"])
    assert exc.value.code == 2
