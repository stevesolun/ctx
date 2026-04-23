"""
tests/test_usage_tracker.py -- pytest suite for usage_tracker module.

Covers:
  - read_today_signals         (happy, missing file, bad json, wrong date)
  - signals_to_skills          (known signals, unknown passthrough)
  - read_loaded_skills         (happy, missing file, bad json)
  - _set_frontmatter_field     (replace existing, field absent = no-op)
  - update_skill_page          (used=True, used=False, stale threshold, invalid name)
  - append_wiki_log            (happy path, missing log)
  - truncate_intent_log        (keeps last KEEP_DAYS dates)
  - main()                     (--sync path with monkeypatched globals)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import usage_tracker as _ut
from usage_tracker import (
    _set_frontmatter_field,
    append_wiki_log,
    read_loaded_skills,
    read_today_signals,
    signals_to_skills,
    truncate_intent_log,
    update_skill_page,
)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entity_page(entities_dir: Path, name: str, extra_fields: dict | None = None) -> Path:
    """Write a minimal wiki entity page."""
    fields = {
        "title": name,
        "use_count": "0",
        "session_count": "0",
        "last_used": "2024-01-01",
        "updated": "2024-01-01",
        "status": "installed",
    }
    if extra_fields:
        fields.update(extra_fields)
    fm = "\n".join(f"{k}: {v}" for k, v in fields.items())
    content = f"---\n{fm}\n---\n# {name}\n"
    page = entities_dir / f"{name}.md"
    page.write_text(content, encoding="utf-8")
    return page


def _write_intent_log(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# read_today_signals
# ---------------------------------------------------------------------------

class TestReadTodaySignals:
    def test_happy_path(self, tmp_path, monkeypatch):
        log = tmp_path / "intent.jsonl"
        _write_intent_log(log, [
            {"date": TODAY, "signals": ["react", "docker"]},
            {"date": TODAY, "signals": ["react"]},
        ])
        monkeypatch.setattr(_ut, "INTENT_LOG", log)
        result = read_today_signals()
        assert result["react"] == 2
        assert result["docker"] == 1

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_ut, "INTENT_LOG", tmp_path / "no-such.jsonl")
        assert read_today_signals() == {}

    def test_ignores_other_dates(self, tmp_path, monkeypatch):
        log = tmp_path / "intent.jsonl"
        _write_intent_log(log, [{"date": "2000-01-01", "signals": ["react"]}])
        monkeypatch.setattr(_ut, "INTENT_LOG", log)
        result = read_today_signals()
        assert "react" not in result

    def test_skips_malformed_lines(self, tmp_path, monkeypatch):
        log = tmp_path / "intent.jsonl"
        log.write_text(f'bad-json\n{json.dumps({"date": TODAY, "signals": ["fastapi"]})}\n')
        monkeypatch.setattr(_ut, "INTENT_LOG", log)
        result = read_today_signals()
        assert result.get("fastapi") == 1

    def test_empty_file_returns_empty(self, tmp_path, monkeypatch):
        log = tmp_path / "intent.jsonl"
        log.write_text("")
        monkeypatch.setattr(_ut, "INTENT_LOG", log)
        assert read_today_signals() == {}


# ---------------------------------------------------------------------------
# signals_to_skills
# ---------------------------------------------------------------------------

class TestSignalsToSkills:
    def test_known_signal_mapped(self):
        skills = signals_to_skills({"react": 2})
        assert "react" in skills
        assert "frontend-design" in skills

    def test_unknown_signal_returns_no_skills(self):
        """An unmapped signal must NOT leak its raw name as a skill.

        Prior impl had ``SIGNAL_SKILL_MAP.get(signal, [signal])`` — the
        fallback put the raw signal name into the returned set, which
        update_skill_page then treated as a skill slug. The downstream
        effect was arbitrary corruption of use_count on wiki pages whose
        slug happened to match a signal name (code-reviewer HIGH).
        """
        skills = signals_to_skills({"totally-unknown": 1})
        assert "totally-unknown" not in skills
        assert skills == set()

    def test_unknown_signal_does_not_poison_mapped_result(self):
        """Mixed known + unknown input returns only the known mapping."""
        skills = signals_to_skills({"docker": 1, "some-unmapped-stack": 5})
        assert "docker" in skills
        assert "some-unmapped-stack" not in skills

    def test_empty_input(self):
        assert signals_to_skills({}) == set()

    def test_multiple_signals(self):
        skills = signals_to_skills({"docker": 1, "pytest": 2})
        assert "docker" in skills
        assert "pytest" in skills

    def test_empty_mapping_value_returns_nothing(self, monkeypatch):
        """Defensive: if a signal maps to an empty list (a maintenance
        state between adding a signal and picking its skills), we
        return nothing — not the raw signal.

        Post-P2.4 the map is a ``MappingProxyType`` — immutable. The
        old copy-mutate-restore pattern doesn't work anymore. Use
        monkeypatch to swap in a plain dict for the duration of the
        test, then the original immutable view restores automatically.
        """
        import usage_tracker as _ut_mod
        override = dict(_ut_mod.SIGNAL_SKILL_MAP)
        override["tmp-signal"] = []
        monkeypatch.setattr(_ut_mod, "SIGNAL_SKILL_MAP", override)
        assert "tmp-signal" not in signals_to_skills({"tmp-signal": 1})


# ---------------------------------------------------------------------------
# read_loaded_skills
# ---------------------------------------------------------------------------

class TestReadLoadedSkills:
    def test_happy_path(self, tmp_path, monkeypatch):
        manifest = {"load": [{"skill": "react"}, {"skill": "docker"}]}
        mpath = tmp_path / "manifest.json"
        mpath.write_text(json.dumps(manifest))
        monkeypatch.setattr(_ut, "MANIFEST_PATH", mpath)
        result = read_loaded_skills()
        assert result == ["react", "docker"]

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_ut, "MANIFEST_PATH", tmp_path / "no-manifest.json")
        assert read_loaded_skills() == []

    def test_bad_json_returns_empty(self, tmp_path, monkeypatch):
        mpath = tmp_path / "manifest.json"
        mpath.write_text("not-json")
        monkeypatch.setattr(_ut, "MANIFEST_PATH", mpath)
        assert read_loaded_skills() == []

    def test_empty_load_list(self, tmp_path, monkeypatch):
        mpath = tmp_path / "manifest.json"
        mpath.write_text(json.dumps({"load": []}))
        monkeypatch.setattr(_ut, "MANIFEST_PATH", mpath)
        assert read_loaded_skills() == []


# ---------------------------------------------------------------------------
# _set_frontmatter_field
# ---------------------------------------------------------------------------

class TestSetFrontmatterField:
    def test_replaces_existing_field(self):
        content = "---\nuse_count: 0\nstatus: installed\n---\n# body\n"
        result = _set_frontmatter_field(content, "use_count", "5")
        assert "use_count: 5" in result
        assert "use_count: 0" not in result

    def test_field_not_present_is_inserted_into_frontmatter(self):
        """The prior implementation silently no-op'd when the field was
        missing — which meant session_count never persisted on pages
        without it and the staleness gate never fired. New behavior:
        insert the field at the end of the frontmatter block."""
        content = "---\ntitle: react\n---\n# body\n"
        result = _set_frontmatter_field(content, "session_count", "1")
        assert result != content
        assert "session_count: 1" in result
        assert result.startswith("---\n")
        assert "# body" in result

    def test_insert_skipped_when_no_frontmatter(self):
        content = "no frontmatter here\n"
        result = _set_frontmatter_field(content, "session_count", "0")
        # No block to extend — leave content alone.
        assert result == content

    def test_multiline_content_only_replaces_target(self):
        content = "---\nuse_count: 0\nlast_used: 2024-01-01\n---\n# body\n"
        result = _set_frontmatter_field(content, "use_count", "3")
        assert "last_used: 2024-01-01" in result
        assert "use_count: 3" in result


# ---------------------------------------------------------------------------
# update_skill_page
# ---------------------------------------------------------------------------

class TestUpdateSkillPage:
    def test_used_true_increments_use_count(self, tmp_path, monkeypatch):
        entities = tmp_path / "entities" / "skills"
        entities.mkdir(parents=True)
        _make_entity_page(entities, "react")
        monkeypatch.setattr(_ut, "ENTITIES_DIR", entities)
        updated, queued = update_skill_page("react", used=True)
        assert updated is True
        content = (entities / "react.md").read_text()
        assert "use_count: 1" in content

    def test_used_true_updates_last_used(self, tmp_path, monkeypatch):
        entities = tmp_path / "entities" / "skills"
        entities.mkdir(parents=True)
        _make_entity_page(entities, "react")
        monkeypatch.setattr(_ut, "ENTITIES_DIR", entities)
        update_skill_page("react", used=True)
        content = (entities / "react.md").read_text()
        assert f"last_used: {TODAY}" in content

    def test_used_false_increments_session_count(self, tmp_path, monkeypatch):
        entities = tmp_path / "entities" / "skills"
        entities.mkdir(parents=True)
        _make_entity_page(entities, "docker")
        monkeypatch.setattr(_ut, "ENTITIES_DIR", entities)
        monkeypatch.setattr(_ut, "PENDING_UNLOAD", tmp_path / "pending.json")
        updated, queued = update_skill_page("docker", used=False)
        assert updated is True
        content = (entities / "docker.md").read_text()
        assert "session_count: 1" in content

    def test_stale_threshold_queues_unload(self, tmp_path, monkeypatch):
        entities = tmp_path / "entities" / "skills"
        entities.mkdir(parents=True)
        # session_count at threshold, use_count=0
        _make_entity_page(entities, "old-skill", {
            "session_count": str(_ut.STALE_THRESHOLD),
            "use_count": "0",
        })
        monkeypatch.setattr(_ut, "ENTITIES_DIR", entities)
        monkeypatch.setattr(_ut, "PENDING_UNLOAD", tmp_path / "pending.json")
        updated, queued = update_skill_page("old-skill", used=False)
        assert queued is True
        pending = json.loads((tmp_path / "pending.json").read_text())
        names = [s["name"] for s in pending["suggestions"]]
        assert "old-skill" in names

    def test_invalid_skill_name_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_ut, "ENTITIES_DIR", tmp_path)
        updated, queued = update_skill_page("../../etc/passwd", used=True)
        assert updated is False
        assert queued is False

    def test_missing_page_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_ut, "ENTITIES_DIR", tmp_path)
        updated, queued = update_skill_page("nonexistent", used=True)
        assert updated is False

    def test_used_true_resets_status_to_installed(self, tmp_path, monkeypatch):
        entities = tmp_path / "entities" / "skills"
        entities.mkdir(parents=True)
        _make_entity_page(entities, "react", {"status": "stale"})
        monkeypatch.setattr(_ut, "ENTITIES_DIR", entities)
        update_skill_page("react", used=True)
        content = (entities / "react.md").read_text()
        assert "status: installed" in content


# ---------------------------------------------------------------------------
# append_wiki_log
# ---------------------------------------------------------------------------

class TestAppendWikiLog:
    def test_appends_to_existing_log(self, tmp_path, monkeypatch):
        log = tmp_path / "log.md"
        log.write_text("# Log\n")
        monkeypatch.setattr(_ut, "LOG_PATH", log)
        append_wiki_log(5, {"react", "docker"}, 0)
        content = log.read_text()
        assert "session-end" in content
        assert "Skills loaded: 5" in content

    def test_lists_used_skills(self, tmp_path, monkeypatch):
        log = tmp_path / "log.md"
        log.write_text("# Log\n")
        monkeypatch.setattr(_ut, "LOG_PATH", log)
        append_wiki_log(3, {"react"}, 0)
        assert "react" in log.read_text()

    def test_no_error_when_log_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_ut, "LOG_PATH", tmp_path / "no-log.md")
        # Should not raise even though file doesn't exist
        append_wiki_log(0, set(), 0)

    def test_empty_used_skills(self, tmp_path, monkeypatch):
        log = tmp_path / "log.md"
        log.write_text("# Log\n")
        monkeypatch.setattr(_ut, "LOG_PATH", log)
        append_wiki_log(0, set(), 0)
        content = log.read_text()
        assert "Skills actively used (signals): 0" in content


# ---------------------------------------------------------------------------
# truncate_intent_log
# ---------------------------------------------------------------------------

class TestTruncateIntentLog:
    def test_keeps_last_n_dates(self, tmp_path, monkeypatch):
        log = tmp_path / "intent.jsonl"
        entries = [{"date": f"2024-01-{i:02d}", "signals": ["x"]} for i in range(1, 12)]
        _write_intent_log(log, entries)
        monkeypatch.setattr(_ut, "INTENT_LOG", log)
        monkeypatch.setattr(_ut, "KEEP_DAYS", 3)
        truncate_intent_log()
        lines = [l for l in log.read_text().strip().split("\n") if l]
        dates = {json.loads(l)["date"] for l in lines}
        assert len(dates) <= 3
        # Most recent dates should be kept
        assert "2024-01-11" in dates

    def test_missing_file_no_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_ut, "INTENT_LOG", tmp_path / "nope.jsonl")
        truncate_intent_log()  # should not raise

    def test_malformed_lines_skipped(self, tmp_path, monkeypatch):
        log = tmp_path / "intent.jsonl"
        log.write_text(f'not-json\n{json.dumps({"date": TODAY, "signals": []})}\n')
        monkeypatch.setattr(_ut, "INTENT_LOG", log)
        monkeypatch.setattr(_ut, "KEEP_DAYS", 5)
        truncate_intent_log()
        lines = [l for l in log.read_text().strip().split("\n") if l]
        # The valid line should remain; bad line dropped
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------

class TestMain:
    def test_sync_without_wiki_exits_0(self, tmp_path, monkeypatch, capsys):
        """When wiki dir doesn't exist, main exits 0 silently."""
        monkeypatch.setattr(_ut, "WIKI_DIR", tmp_path / "no-wiki")
        monkeypatch.setattr(sys, "argv", ["usage_tracker.py", "--sync", "--wiki", str(tmp_path / "no-wiki")])
        with pytest.raises(SystemExit) as exc:
            _ut.main()
        assert exc.value.code == 0

    def test_no_sync_flag_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["usage_tracker.py"])
        with pytest.raises(SystemExit) as exc:
            _ut.main()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# TODAY freshness regression (code-reviewer HIGH)
# ---------------------------------------------------------------------------

class TestTodayIsFresh:
    """``_today()`` computes the date at call-time, not at import.

    Prior impl cached ``TODAY = datetime.now(...)`` at module import.
    A long-running process that crossed midnight kept using yesterday's
    date — ``read_today_signals`` would silently drop new-day entries,
    ``update_skill_page`` would stamp yesterday's date as ``last_used``,
    and ``append_wiki_log`` would file today's events under yesterday's
    header.
    """

    def test_today_follows_wall_clock(self, monkeypatch):
        """Changing the system clock changes _today() without module reload."""
        from datetime import datetime, timezone

        import usage_tracker as _ut_mod

        class _FakeDT:
            """Minimal datetime stand-in returning a fixed UTC date."""
            @classmethod
            def now(cls, tz=None):
                # Return 2027-06-15 UTC regardless of the real clock.
                return datetime(2027, 6, 15, 12, 0, 0, tzinfo=tz or timezone.utc)

        monkeypatch.setattr(_ut_mod, "datetime", _FakeDT)
        assert _ut_mod._today() == "2027-06-15"

    def test_today_changes_when_clock_changes_between_calls(self, monkeypatch):
        """Simulates a midnight crossing: two calls on different calendar
        days must return different strings. Pre-fix, both would have
        returned the module-import date."""
        from datetime import datetime, timezone

        import usage_tracker as _ut_mod

        state = {"day": 1}

        class _DTOnTheRun:
            @classmethod
            def now(cls, tz=None):
                # Alternate between two days on each call.
                if state["day"] == 1:
                    return datetime(2027, 6, 15, 23, 59, 59, tzinfo=tz or timezone.utc)
                return datetime(2027, 6, 16, 0, 0, 1, tzinfo=tz or timezone.utc)

        monkeypatch.setattr(_ut_mod, "datetime", _DTOnTheRun)

        before = _ut_mod._today()
        state["day"] = 2
        after = _ut_mod._today()

        assert before == "2027-06-15"
        assert after == "2027-06-16"
        assert before != after

    def test_read_today_signals_uses_fresh_date(
        self, tmp_path, monkeypatch,
    ):
        """The log reader must filter on the CURRENT date, not the
        frozen-at-import one. If a caller writes an entry timestamped
        with TODAY and reads it back after a midnight crossing, the
        reader should skip it — verifying the comparison is dynamic."""
        from datetime import datetime, timezone

        import usage_tracker as _ut_mod

        log = tmp_path / "intent.jsonl"
        # Two entries: one "today" (day 1), one "tomorrow" (day 2).
        log.write_text(
            json.dumps({"date": "2027-06-15", "signals": ["docker"]}) + "\n" +
            json.dumps({"date": "2027-06-16", "signals": ["kubernetes"]}),
            encoding="utf-8",
        )
        monkeypatch.setattr(_ut_mod, "INTENT_LOG", log)

        class _DTDay2:
            @classmethod
            def now(cls, tz=None):
                return datetime(2027, 6, 16, 10, 0, 0, tzinfo=tz or timezone.utc)

        monkeypatch.setattr(_ut_mod, "datetime", _DTDay2)
        signals = _ut_mod.read_today_signals()

        # On day 2, only the kubernetes entry matches today — docker
        # (day 1) must not leak in.
        assert "kubernetes" in signals
        assert "docker" not in signals
