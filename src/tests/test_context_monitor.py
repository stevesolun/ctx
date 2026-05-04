"""
tests/test_context_monitor.py -- pytest suite for context_monitor module.

Covers:
  - extract_signals             (keyword match, extension match, Bash tool signals, empty input)
  - load_manifest_skills        (happy, missing file, bad json)
  - append_intent_log           (creates file, appends)
  - count_recent_unmatched      (matched vs unmatched)
  - write_pending_skills        (creates file, content)
  - load_recent_unmatched_count (happy path, missing file, skips bad lines)
  - _parse_stdin_payload        (valid JSON, empty stdin, non-dict, bad JSON)
  - main()                      (--tool/--input flags, --from-stdin flag, no signals exit-0)
"""

import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from ctx.adapters.claude_code.hooks import context_monitor as _cm
from ctx.adapters.claude_code.hooks.context_monitor import (
    _parse_stdin_payload,
    append_intent_log,
    count_recent_unmatched,
    extract_signals,
    load_manifest_skills,
    load_recent_unmatched_count,
    write_pending_skills,
)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# extract_signals
# ---------------------------------------------------------------------------

class TestExtractSignals:
    def test_keyword_match_react(self):
        signals = extract_signals("Read", {"file_path": "src/App.tsx"})
        assert "react" in signals

    def test_keyword_match_docker(self):
        signals = extract_signals("Read", {"file_path": "Dockerfile"})
        assert "docker" in signals

    def test_extension_tsx_triggers_react(self):
        signals = extract_signals("Read", {"file_path": "component.tsx"})
        assert "react" in signals

    def test_extension_tf_triggers_terraform(self):
        signals = extract_signals("Write", {"file_path": "main.tf"})
        assert "terraform" in signals

    def test_bash_pip_install_triggers_python(self):
        signals = extract_signals("Bash", {"command": "pip install fastapi"})
        assert "python" in signals

    def test_bash_npm_install_triggers_javascript(self):
        signals = extract_signals("Bash", {"command": "npm install react"})
        assert "javascript" in signals

    def test_empty_input_returns_empty(self):
        signals = extract_signals("Read", {})
        assert signals == []

    def test_no_match_returns_empty(self):
        signals = extract_signals("Read", {"file_path": "README.md"})
        assert signals == []

    def test_multiple_signals_deduped(self):
        signals = extract_signals("Read", {"content": "react jsx tsx"})
        # react should appear only once despite 3 matching keywords
        assert signals.count("react") == 1

    def test_signals_sorted(self):
        signals = extract_signals("Bash", {"command": "docker-compose up"})
        assert signals == sorted(signals)

    def test_anthropic_keyword_maps_to_sdk(self):
        signals = extract_signals("Read", {"content": "from anthropic import Anthropic"})
        assert "anthropic-sdk" in signals

    def test_kubernetes_keyword_match(self):
        signals = extract_signals("Bash", {"command": "kubectl apply -f deployment.yaml"})
        assert "kubernetes" in signals

    def test_case_insensitive_match(self):
        signals = extract_signals("Read", {"content": "FASTAPI app = FastAPI()"})
        assert "fastapi" in signals


# ---------------------------------------------------------------------------
# load_manifest_skills
# ---------------------------------------------------------------------------

class TestLoadManifestSkills:
    def test_happy_path(self, tmp_path, monkeypatch):
        manifest = {"load": [{"skill": "react"}, {"skill": "docker"}]}
        mpath = tmp_path / "manifest.json"
        mpath.write_text(json.dumps(manifest))
        monkeypatch.setattr(_cm, "MANIFEST_PATH", mpath)
        result = load_manifest_skills()
        assert result == {"react", "docker"}

    def test_missing_file_returns_empty_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cm, "MANIFEST_PATH", tmp_path / "no-such.json")
        assert load_manifest_skills() == set()

    def test_bad_json_returns_empty_set(self, tmp_path, monkeypatch):
        mpath = tmp_path / "manifest.json"
        mpath.write_text("not-json")
        monkeypatch.setattr(_cm, "MANIFEST_PATH", mpath)
        assert load_manifest_skills() == set()

    def test_empty_load_list(self, tmp_path, monkeypatch):
        mpath = tmp_path / "manifest.json"
        mpath.write_text(json.dumps({"load": []}))
        monkeypatch.setattr(_cm, "MANIFEST_PATH", mpath)
        assert load_manifest_skills() == set()


# ---------------------------------------------------------------------------
# append_intent_log
# ---------------------------------------------------------------------------

class TestAppendIntentLog:
    def test_creates_file_and_appends(self, tmp_path, monkeypatch):
        log = tmp_path / "intent.jsonl"
        monkeypatch.setattr(_cm, "CLAUDE_DIR", tmp_path)
        monkeypatch.setattr(_cm, "INTENT_LOG", log)
        entry = {"date": TODAY, "signals": ["react"]}
        append_intent_log(entry)
        lines = [line for line in log.read_text().strip().split("\n") if line]
        assert len(lines) == 1
        assert json.loads(lines[0])["signals"] == ["react"]

    def test_appends_multiple_entries(self, tmp_path, monkeypatch):
        log = tmp_path / "intent.jsonl"
        monkeypatch.setattr(_cm, "CLAUDE_DIR", tmp_path)
        monkeypatch.setattr(_cm, "INTENT_LOG", log)
        append_intent_log({"date": TODAY, "signals": ["react"]})
        append_intent_log({"date": TODAY, "signals": ["docker"]})
        lines = [line for line in log.read_text().strip().split("\n") if line]
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# count_recent_unmatched
# ---------------------------------------------------------------------------

class TestCountRecentUnmatched:
    def test_all_matched(self):
        unmatched = count_recent_unmatched(["react", "docker"], {"react", "docker"})
        assert unmatched == []

    def test_none_matched(self):
        unmatched = count_recent_unmatched(["react", "docker"], set())
        assert set(unmatched) == {"react", "docker"}

    def test_partial_match(self):
        unmatched = count_recent_unmatched(["react", "docker"], {"react"})
        assert unmatched == ["docker"]

    def test_empty_signals(self):
        assert count_recent_unmatched([], {"react"}) == []


# ---------------------------------------------------------------------------
# write_pending_skills
# ---------------------------------------------------------------------------

class TestWritePendingSkills:
    def test_writes_pending_skills_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cm, "PENDING_SKILLS", tmp_path / "pending.json")
        monkeypatch.setattr(_cm, "graph_suggest", lambda unmatched: [])
        write_pending_skills(["react", "docker"])
        pending = json.loads((tmp_path / "pending.json").read_text())
        assert pending["unmatched_signals"] == ["react", "docker"]
        assert "suggestion" in pending

    def test_suggestion_mentions_signals(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cm, "PENDING_SKILLS", tmp_path / "pending.json")
        monkeypatch.setattr(_cm, "graph_suggest", lambda unmatched: [])
        write_pending_skills(["fastapi"])
        pending = json.loads((tmp_path / "pending.json").read_text())
        assert "fastapi" in pending["suggestion"]

    def test_empty_unmatched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cm, "PENDING_SKILLS", tmp_path / "pending.json")
        monkeypatch.setattr(_cm, "graph_suggest", lambda unmatched: [])
        write_pending_skills([])
        pending = json.loads((tmp_path / "pending.json").read_text())
        assert pending["unmatched_signals"] == []

    def test_graph_suggestions_list_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cm, "PENDING_SKILLS", tmp_path / "pending.json")
        monkeypatch.setattr(
            _cm,
            "graph_suggest",
            lambda unmatched: [{"name": "fastapi-pro", "type": "skill"}],
        )
        write_pending_skills(["unknown-signal"])
        pending = json.loads((tmp_path / "pending.json").read_text())
        assert pending["graph_suggestions"] == [{"name": "fastapi-pro", "type": "skill"}]

    def test_graph_suggest_filters_to_execution_bundle_types(self, tmp_path, monkeypatch):
        graph_path = tmp_path / "skill-wiki" / "graphify-out" / "graph.json"
        graph_path.parent.mkdir(parents=True)
        graph_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(_cm, "CLAUDE_DIR", tmp_path)
        calls = {}

        class FakeGraph:
            def number_of_nodes(self):
                return 1

        fake_graph_module = type(
            "FakeGraphModule",
            (),
            {"load_graph": staticmethod(lambda _path=None: FakeGraph())},
        )

        def fake_recommend_by_tags(graph, tags, **kwargs):
            calls["entity_types"] = kwargs.get("entity_types")
            return [{"name": "fastapi-pro", "type": "skill"}]

        fake_recommend_module = type(
            "FakeRecommendModule",
            (),
            {"recommend_by_tags": staticmethod(fake_recommend_by_tags)},
        )
        monkeypatch.setitem(sys.modules, "ctx.core.graph.resolve_graph", fake_graph_module)
        monkeypatch.setitem(
            sys.modules,
            "ctx.core.resolve.recommendations",
            fake_recommend_module,
        )

        assert _cm.graph_suggest(["fastapi"]) == [{"name": "fastapi-pro", "type": "skill"}]
        assert calls["entity_types"] == ("skill", "agent", "mcp-server")


# ---------------------------------------------------------------------------
# load_recent_unmatched_count
# ---------------------------------------------------------------------------

class TestLoadRecentUnmatchedCount:
    def _write_log(self, path: Path, entries: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    def test_happy_path_counts_distinct(self, tmp_path, monkeypatch):
        log = tmp_path / "intent.jsonl"
        self._write_log(log, [
            {"date": TODAY, "unmatched": ["react", "docker"]},
            {"date": TODAY, "unmatched": ["react"]},  # react already counted
        ])
        monkeypatch.setattr(_cm, "INTENT_LOG", log)
        count = load_recent_unmatched_count()
        assert count == 2  # react + docker distinct

    def test_missing_file_returns_0(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cm, "INTENT_LOG", tmp_path / "nope.jsonl")
        assert load_recent_unmatched_count() == 0

    def test_skips_bad_lines(self, tmp_path, monkeypatch):
        log = tmp_path / "intent.jsonl"
        log.write_text(f'not-json\n{json.dumps({"date": TODAY, "unmatched": ["react"]})}\n')
        monkeypatch.setattr(_cm, "INTENT_LOG", log)
        assert load_recent_unmatched_count() == 1

    def test_other_dates_not_counted(self, tmp_path, monkeypatch):
        log = tmp_path / "intent.jsonl"
        self._write_log(log, [{"date": "2000-01-01", "unmatched": ["react"]}])
        monkeypatch.setattr(_cm, "INTENT_LOG", log)
        assert load_recent_unmatched_count() == 0


# ---------------------------------------------------------------------------
# _parse_stdin_payload
# ---------------------------------------------------------------------------

class TestParseStdinPayload:
    def test_valid_payload(self, monkeypatch):
        payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "foo.py"}})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        tool_name, tool_input = _parse_stdin_payload()
        assert tool_name == "Read"
        assert tool_input == {"file_path": "foo.py"}

    def test_empty_stdin(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        tool_name, tool_input = _parse_stdin_payload()
        assert tool_name == "unknown"
        assert tool_input == {}

    def test_non_dict_json(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps([1, 2, 3])))
        tool_name, tool_input = _parse_stdin_payload()
        assert tool_name == "unknown"

    def test_bad_json(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
        tool_name, tool_input = _parse_stdin_payload()
        assert tool_name == "unknown"
        assert tool_input == {}

    def test_missing_tool_input_defaults_to_empty(self, monkeypatch):
        payload = json.dumps({"tool_name": "Bash"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        _, tool_input = _parse_stdin_payload()
        assert tool_input == {}

    def test_non_dict_tool_input_defaults_to_empty(self, monkeypatch):
        payload = json.dumps({"tool_name": "Bash", "tool_input": "not-a-dict"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        _, tool_input = _parse_stdin_payload()
        assert tool_input == {}


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    def test_no_signals_exits_0(self, tmp_path, monkeypatch):
        """When tool input produces no signals, main exits 0."""
        monkeypatch.setattr(_cm, "CLAUDE_DIR", tmp_path)
        monkeypatch.setattr(_cm, "INTENT_LOG", tmp_path / "intent.jsonl")
        monkeypatch.setattr(_cm, "MANIFEST_PATH", tmp_path / "manifest.json")
        monkeypatch.setattr(_cm, "PENDING_SKILLS", tmp_path / "pending.json")
        monkeypatch.setattr(sys, "argv", ["context_monitor.py", "--tool", "Read", "--input", "{}"])
        with pytest.raises(SystemExit) as exc:
            _cm.main()
        assert exc.value.code == 0

    def test_signals_appended_to_log(self, tmp_path, monkeypatch):
        """Signals from --input are written to the intent log."""
        monkeypatch.setattr(_cm, "CLAUDE_DIR", tmp_path)
        log = tmp_path / "intent.jsonl"
        monkeypatch.setattr(_cm, "INTENT_LOG", log)
        monkeypatch.setattr(_cm, "MANIFEST_PATH", tmp_path / "manifest.json")
        monkeypatch.setattr(_cm, "PENDING_SKILLS", tmp_path / "pending.json")
        monkeypatch.setattr(_cm, "_THRESHOLD", 999)  # prevent pending write
        tool_input = json.dumps({"file_path": "Dockerfile"})
        monkeypatch.setattr(sys, "argv", ["context_monitor.py", "--tool", "Read", "--input", tool_input])
        _cm.main()
        lines = [line for line in log.read_text().strip().split("\n") if line]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert "docker" in entry["signals"]

    def test_from_stdin_flag(self, tmp_path, monkeypatch):
        """--from-stdin reads tool payload from stdin."""
        monkeypatch.setattr(_cm, "CLAUDE_DIR", tmp_path)
        log = tmp_path / "intent.jsonl"
        monkeypatch.setattr(_cm, "INTENT_LOG", log)
        monkeypatch.setattr(_cm, "MANIFEST_PATH", tmp_path / "manifest.json")
        monkeypatch.setattr(_cm, "PENDING_SKILLS", tmp_path / "pending.json")
        monkeypatch.setattr(_cm, "_THRESHOLD", 999)
        payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "Dockerfile"}})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        monkeypatch.setattr(sys, "argv", ["context_monitor.py", "--from-stdin"])
        _cm.main()
        lines = [line for line in log.read_text().strip().split("\n") if line]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert "docker" in entry["signals"]

    def test_bad_json_input_falls_back_to_raw(self, tmp_path, monkeypatch):
        """Malformed --input falls back to {'raw': value} and may produce no signals."""
        monkeypatch.setattr(_cm, "CLAUDE_DIR", tmp_path)
        log = tmp_path / "intent.jsonl"
        monkeypatch.setattr(_cm, "INTENT_LOG", log)
        monkeypatch.setattr(_cm, "MANIFEST_PATH", tmp_path / "manifest.json")
        monkeypatch.setattr(_cm, "PENDING_SKILLS", tmp_path / "pending.json")
        monkeypatch.setattr(sys, "argv", ["context_monitor.py", "--tool", "Read", "--input", "not-json"])
        # Should not crash
        try:
            _cm.main()
        except SystemExit as e:
            assert e.code == 0  # exits 0 when no signals

    def test_threshold_triggers_pending_write(self, tmp_path, monkeypatch):
        """When unmatched count >= threshold, pending-skills.json is written."""
        monkeypatch.setattr(_cm, "CLAUDE_DIR", tmp_path)
        log = tmp_path / "intent.jsonl"
        pending = tmp_path / "pending.json"
        monkeypatch.setattr(_cm, "INTENT_LOG", log)
        monkeypatch.setattr(_cm, "MANIFEST_PATH", tmp_path / "manifest.json")
        monkeypatch.setattr(_cm, "PENDING_SKILLS", pending)
        monkeypatch.setattr(_cm, "_THRESHOLD", 1)  # trigger on 1 unmatched
        tool_input = json.dumps({"file_path": "Dockerfile"})
        monkeypatch.setattr(sys, "argv", ["context_monitor.py", "--tool", "Read", "--input", tool_input])
        _cm.main()
        assert pending.exists()


# ---------------------------------------------------------------------------
# Cumulative-threshold regression (code-reviewer finding BLOCKER)
# ---------------------------------------------------------------------------
#
# Prior impl used ``len(unmatched) >= THRESHOLD`` where ``unmatched`` is the
# per-invocation list. A single tool call almost never surfaces 3 unmatched
# signals alone, so the default threshold=3 meant pending-skills.json was
# essentially never written — silently killing the suggestion arm of the
# alive loop. The fix walks the intent log and checks the cumulative
# (per-day, across all invocations) unmatched count against the threshold.

class TestCumulativeThreshold:
    """The threshold must fire on cumulative unmatched across today's log,
    not just on a single invocation's count.
    """

    def _setup_paths(self, tmp_path, monkeypatch):
        log = tmp_path / "intent.jsonl"
        pending = tmp_path / "pending.json"
        monkeypatch.setattr(_cm, "CLAUDE_DIR", tmp_path)
        monkeypatch.setattr(_cm, "INTENT_LOG", log)
        monkeypatch.setattr(_cm, "MANIFEST_PATH", tmp_path / "manifest.json")
        monkeypatch.setattr(_cm, "PENDING_SKILLS", pending)
        return log, pending

    def _run_main(self, monkeypatch, tool_name: str, tool_input: dict):
        monkeypatch.setattr(
            sys, "argv",
            ["context_monitor.py", "--tool", tool_name, "--input", json.dumps(tool_input)],
        )
        _cm.main()

    def test_fires_on_cumulative_across_three_invocations(
        self, tmp_path, monkeypatch,
    ):
        """Three invocations each surfacing ONE unmatched signal should
        trigger pending-skills write when THRESHOLD=3.

        This is the case the old len(unmatched)>=THRESHOLD check missed.
        The user types a react file, then opens a Dockerfile, then edits
        terraform — three separate tool calls, three different unmatched
        signals, cumulatively at threshold.
        """
        _, pending = self._setup_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(_cm, "_THRESHOLD", 3)

        # Three invocations, one unique unmatched signal each.
        self._run_main(monkeypatch, "Read", {"file_path": "App.tsx"})       # react
        assert not pending.exists(), "fired after 1 cumulative unmatched"

        self._run_main(monkeypatch, "Read", {"file_path": "Dockerfile"})    # docker
        assert not pending.exists(), "fired after 2 cumulative unmatched"

        self._run_main(monkeypatch, "Read", {"file_path": "main.tf"})       # terraform
        assert pending.exists(), (
            "did NOT fire after 3 cumulative unmatched — "
            "the per-invocation threshold bug regressed"
        )

    def test_does_not_fire_below_cumulative_threshold(
        self, tmp_path, monkeypatch,
    ):
        """Below-threshold cumulative count must NOT trigger.

        Otherwise every first-keystroke would spam pending-skills.json.
        """
        _, pending = self._setup_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(_cm, "_THRESHOLD", 3)

        self._run_main(monkeypatch, "Read", {"file_path": "App.tsx"})
        self._run_main(monkeypatch, "Read", {"file_path": "Dockerfile"})
        # Only 2 unique signals accumulated; THRESHOLD=3 => no write.
        assert not pending.exists()

    def test_fires_when_single_invocation_surfaces_multiple(
        self, tmp_path, monkeypatch,
    ):
        """Backward-compat: a single invocation carrying threshold-worth of
        new signals on its own still fires (the old happy path)."""
        _, pending = self._setup_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(_cm, "_THRESHOLD", 2)

        # A Bash pip install surfaces multiple signals in one invocation.
        self._run_main(
            monkeypatch, "Bash",
            {"command": "pip install fastapi django"},
        )
        assert pending.exists()

    def test_pending_contains_cumulative_union(
        self, tmp_path, monkeypatch,
    ):
        """When the threshold fires, pending-skills.json lists ALL of today's
        unmatched signals, not just the current invocation's."""
        _, pending = self._setup_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(_cm, "_THRESHOLD", 2)

        self._run_main(monkeypatch, "Read", {"file_path": "App.tsx"})
        self._run_main(monkeypatch, "Read", {"file_path": "Dockerfile"})

        assert pending.exists()
        payload = json.loads(pending.read_text())
        unmatched = set(payload.get("unmatched_signals", []))
        # Both react (from App.tsx) and docker (from Dockerfile) must be
        # present — a fix that only reports the current invocation's
        # signals would miss react.
        assert {"react", "docker"}.issubset(unmatched), (
            f"pending does not include cumulative signals: {unmatched}"
        )

    def test_duplicate_signals_across_invocations_count_once(
        self, tmp_path, monkeypatch,
    ):
        """Unique-set cumulative count — repeated signals don't inflate.

        The same file edited twice should not double-count toward the
        threshold; otherwise a tight edit-save-edit-save loop would
        auto-trigger suggestions on a single concept.
        """
        _, pending = self._setup_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(_cm, "_THRESHOLD", 2)

        self._run_main(monkeypatch, "Read", {"file_path": "App.tsx"})       # react
        self._run_main(monkeypatch, "Read", {"file_path": "index.tsx"})     # react (dup)
        # Only 1 unique unmatched concept; THRESHOLD=2 => no write.
        assert not pending.exists()
