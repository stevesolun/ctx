"""
test_bundle_orchestrator.py -- pins the cross-type bundle contract.

Covers:
  - categorise_bundle: top-K across all types (not per-type), preserves
    graph-score order within each type, supports all-one-type and mixed.
  - render_bundle_message: categorised output with install-cli hints,
    omits empty type sections, includes unmatched signals + unload block.
  - main(): reads pending-skills.json, caps at cfg.recommendation_top_k,
    emits the Claude Code hook JSON envelope.
  - Backward compat: skill_suggest.py shim still calls the new main().
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from ctx.adapters.claude_code.hooks import bundle_orchestrator as _bo


# ────────────────────────────────────────────────────────────────────
# categorise_bundle — top-K is TOTAL, not per-type
# ────────────────────────────────────────────────────────────────────


class TestCategoriseBundle:
    def _sug(self, *entries) -> list[dict]:
        return [
            {"name": n, "type": t, "score": s, "matching_tags": ["x"]}
            for n, t, s in entries
        ]

    def test_bundle_can_be_mixed_all_three_types(self):
        """A top-5 bundle with entries spanning skill/agent/mcp-server
        must preserve all three types in the output."""
        sugs = self._sug(
            ("fastapi-pro", "skill", 90),
            ("code-reviewer", "agent", 85),
            ("anthropic-python-sdk", "mcp-server", 80),
            ("python-patterns", "skill", 75),
            ("devops-engineer", "agent", 70),
        )
        grouped = _bo.categorise_bundle(sugs, top_k=5)
        assert len(grouped["skill"]) == 2
        assert len(grouped["agent"]) == 2
        assert len(grouped["mcp-server"]) == 1

    def test_bundle_can_be_single_type(self):
        """If the top-K entries are all skills, the bundle contains only
        skills — agents/MCPs lists are empty (caller omits their headers)."""
        sugs = self._sug(
            ("python-a", "skill", 90),
            ("python-b", "skill", 85),
            ("python-c", "skill", 80),
        )
        grouped = _bo.categorise_bundle(sugs, top_k=5)
        assert len(grouped["skill"]) == 3
        assert grouped["agent"] == []
        assert grouped["mcp-server"] == []

    def test_top_k_is_total_not_per_type(self):
        """top_k=5 with 10 skills available returns 5 skills TOTAL,
        not 5 per type. User ask: 'don't show a lot of options'."""
        sugs = self._sug(*[(f"skill-{i}", "skill", 100 - i) for i in range(10)])
        grouped = _bo.categorise_bundle(sugs, top_k=5)
        assert sum(len(v) for v in grouped.values()) == 5

    def test_input_order_preserved_within_type(self):
        """Graph-score order from context_monitor.graph_suggest is
        authoritative. categorise_bundle must not re-sort."""
        sugs = self._sug(
            ("alpha", "skill", 99),
            ("beta", "skill", 50),
            ("gamma", "skill", 70),
        )
        grouped = _bo.categorise_bundle(sugs, top_k=5)
        names = [e["name"] for e in grouped["skill"]]
        # Called input order: alpha, beta, gamma -- caller has already
        # sorted by score. categorise_bundle preserves that order.
        assert names == ["alpha", "beta", "gamma"]

    def test_top_k_one(self):
        """Edge case: top_k=1 returns one entry total."""
        sugs = self._sug(
            ("skill-a", "skill", 90),
            ("agent-a", "agent", 85),
        )
        grouped = _bo.categorise_bundle(sugs, top_k=1)
        assert sum(len(v) for v in grouped.values()) == 1

    def test_empty_input(self):
        grouped = _bo.categorise_bundle([], top_k=5)
        assert grouped == {"skill": [], "agent": [], "mcp-server": []}

    def test_unknown_type_still_included(self):
        """A suggestion with an unexpected ``type`` value doesn't crash
        the categoriser; it lands in its own bucket. Defensive — the
        graph should never produce these, but a malformed pending file
        shouldn't break the hook."""
        sugs = [{"name": "weird", "type": "future-type", "score": 50, "matching_tags": []}]
        grouped = _bo.categorise_bundle(sugs, top_k=5)
        assert "future-type" in grouped
        assert grouped["future-type"][0]["name"] == "weird"


# ────────────────────────────────────────────────────────────────────
# render_bundle_message — user-facing layout
# ────────────────────────────────────────────────────────────────────


class TestRenderBundleMessage:
    def _sug(self, *entries) -> list[dict]:
        return [
            {"name": n, "type": t, "score": s, "matching_tags": ["stack-x"]}
            for n, t, s in entries
        ]

    def test_categorised_headers_only_for_types_with_entries(self):
        """Skills/Agents/MCPs headers appear ONLY when that type has
        entries in the bundle. Empty sections are omitted so the user
        doesn't see dead headers."""
        sugs = self._sug(("a", "skill", 90), ("b", "skill", 80))
        msg = _bo.render_bundle_message(sugs, [], [], top_k=5)
        assert "Skills:" in msg
        assert "Agents:" not in msg
        assert "MCPs:" not in msg

    def test_install_cli_hint_per_type(self):
        """Each category header is followed by its install-CLI hint.
        User sees how to act on each type."""
        sugs = self._sug(
            ("a", "skill", 90),
            ("b", "agent", 80),
            ("c", "mcp-server", 70),
        )
        msg = _bo.render_bundle_message(sugs, [], [], top_k=5)
        assert "ctx-skill-install" in msg
        assert "ctx-agent-install" in msg
        assert "ctx-mcp-install" in msg

    def test_unmatched_signals_surfaced(self):
        msg = _bo.render_bundle_message([], ["fastapi", "docker"], [], top_k=5)
        assert "Unmatched signals" in msg
        assert "fastapi" in msg
        assert "docker" in msg

    def test_unload_block_separate_from_bundle(self):
        unload = [{"name": "old-skill", "reason": "unused for 30 days"}]
        msg = _bo.render_bundle_message([], [], unload, top_k=5)
        assert "loaded but never used" in msg
        assert "old-skill" in msg

    def test_empty_everything_yields_empty_message(self):
        assert _bo.render_bundle_message([], [], [], top_k=5).strip() == ""

    def test_top_k_enforced_in_render(self):
        """If input has 10 suggestions but top_k=3, only 3 show."""
        sugs = self._sug(*[(f"s-{i}", "skill", 100 - i) for i in range(10)])
        msg = _bo.render_bundle_message(sugs, [], [], top_k=3)
        # Count the bullet lines that start with "- " (our bundle-item marker).
        item_lines = [l for l in msg.splitlines() if l.strip().startswith("- ")]
        assert len(item_lines) == 3


# ────────────────────────────────────────────────────────────────────
# main() — reads pending files, emits Claude-Code hook payload
# ────────────────────────────────────────────────────────────────────


class TestMainEndToEnd:
    def _setup_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_bo, "CLAUDE_DIR", tmp_path)
        monkeypatch.setattr(_bo, "PENDING_SKILLS", tmp_path / "pending-skills.json")
        monkeypatch.setattr(_bo, "PENDING_UNLOAD", tmp_path / "pending-unload.json")
        monkeypatch.setattr(_bo, "SHOWN_FLAG", tmp_path / ".bundle-shown")

    def test_no_pending_exits_0(self, tmp_path, monkeypatch):
        self._setup_paths(tmp_path, monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _bo.main()
        assert exc.value.code == 0

    def test_pending_with_bundle_emits_hook_json(
        self, tmp_path, monkeypatch, capsys,
    ):
        self._setup_paths(tmp_path, monkeypatch)
        pending = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "unmatched_signals": ["fastapi"],
            "graph_suggestions": [
                {"name": "fastapi-pro", "type": "skill", "score": 90,
                 "matching_tags": ["fastapi"]},
                {"name": "anthropic-python-sdk", "type": "mcp-server",
                 "score": 75, "matching_tags": []},
            ],
        }
        (tmp_path / "pending-skills.json").write_text(json.dumps(pending))

        _bo.main()
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        msg = payload["hookSpecificOutput"]["additionalContext"]
        assert "fastapi-pro" in msg
        assert "anthropic-python-sdk" in msg
        assert "Skills:" in msg
        assert "MCPs:" in msg

    def test_already_shown_suppresses_output(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Second invocation in the same session doesn't re-emit."""
        self._setup_paths(tmp_path, monkeypatch)
        pending_at = datetime.now(timezone.utc).isoformat()
        (tmp_path / "pending-skills.json").write_text(json.dumps({
            "generated_at": pending_at,
            "unmatched_signals": ["x"],
            "graph_suggestions": [
                {"name": "a", "type": "skill", "score": 50, "matching_tags": []},
            ],
        }))
        # Pre-mark shown with a timestamp AFTER pending_at so the guard trips.
        later = datetime.now(timezone.utc).isoformat()
        (tmp_path / ".bundle-shown").write_text(json.dumps({"shown_at": later}))

        with pytest.raises(SystemExit) as exc:
            _bo.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""

    def test_top_k_from_config(self, tmp_path, monkeypatch, capsys):
        """A user override of recommendation_top_k in config propagates."""
        self._setup_paths(tmp_path, monkeypatch)

        # Monkey-patch the lazy ctx_config import path used inside _top_k.
        import ctx_config as _cfg_mod
        monkeypatch.setattr(_cfg_mod.cfg, "recommendation_top_k", 2)

        pending = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "unmatched_signals": [],
            "graph_suggestions": [
                {"name": f"s-{i}", "type": "skill", "score": 100 - i, "matching_tags": []}
                for i in range(10)
            ],
        }
        (tmp_path / "pending-skills.json").write_text(json.dumps(pending))

        _bo.main()
        out = capsys.readouterr().out
        payload = json.loads(out.strip())
        msg = payload["hookSpecificOutput"]["additionalContext"]
        item_lines = [l for l in msg.splitlines() if l.strip().startswith("- ")]
        assert len(item_lines) == 2, f"expected top_k=2 cap, got {len(item_lines)}"


# ────────────────────────────────────────────────────────────────────
# Backward-compat: skill_suggest shim
# ────────────────────────────────────────────────────────────────────


class TestSkillSuggestShim:
    """skill_suggest.py must remain importable and call through to
    bundle_orchestrator.main — otherwise existing ~/.claude/settings.json
    hook configs that invoke ``python skill_suggest.py`` break silently."""

    def test_shim_re_exports_main(self):
        from ctx.adapters.claude_code.hooks import skill_suggest
        # The shim re-imports main from bundle_orchestrator. The function
        # object must be the SAME instance to guarantee behavioural parity.
        assert skill_suggest.main is _bo.main

    def test_shim_re_exports_constants(self):
        from ctx.adapters.claude_code.hooks import skill_suggest
        assert skill_suggest.PENDING_SKILLS == _bo.PENDING_SKILLS
        assert skill_suggest.PENDING_UNLOAD == _bo.PENDING_UNLOAD
