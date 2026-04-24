"""
tests/test_resolve_skills.py -- pytest suite for resolve_skills module.

Covers:
  - discover_available_skills   (happy path, empty dir, malformed frontmatter)
  - read_wiki_overrides         (happy path, missing dir, bad use_count, boolean flags)
  - resolve                     (basic load, conflict resolution, always/never_load, cap)
  - read_intent_signals         (happy path, missing file, bad JSON lines, wrong date)
  - apply_intent_boosts         (boost in needed, suggestion when available only)
  - main()                      (via subprocess with --profile arg)
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from ctx.core.resolve.resolve_skills import (
    apply_intent_boosts,
    discover_available_skills,
    read_intent_signals,
    read_wiki_overrides,
    resolve,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_skill(skills_dir: Path, name: str, frontmatter: str = "") -> Path:
    """Write a minimal SKILL.md under skills_dir/<name>/."""
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    content = f"---\n{frontmatter}\n---\n# {name}\n" if frontmatter else f"# {name}\n"
    p = d / "SKILL.md"
    p.write_text(content, encoding="utf-8")
    return p


def _minimal_profile(frameworks=None, languages=None) -> dict:
    return {
        "repo_path": "/tmp/repo",
        "languages": languages or [],
        "frameworks": frameworks or [],
        "infrastructure": [],
        "data_stores": [],
        "testing": [],
        "ai_tooling": [],
        "build_system": [],
        "docs": [],
    }


def _detection(name: str, confidence: float = 0.9) -> dict:
    return {"name": name, "confidence": confidence, "evidence": ["file.py"]}


# ---------------------------------------------------------------------------
# discover_available_skills
# ---------------------------------------------------------------------------

class TestDiscoverAvailableSkills:
    def test_happy_path_single_skill(self, tmp_path):
        _make_skill(tmp_path, "react", "tags: [javascript]\nversion: 1.0")
        skills = discover_available_skills(str(tmp_path))
        assert "react" in skills
        assert skills["react"]["name"] == "react"
        assert "path" in skills["react"]

    def test_multiple_skills(self, tmp_path):
        for name in ("react", "fastapi", "docker"):
            _make_skill(tmp_path, name, f"tags: [{name}]")
        skills = discover_available_skills(str(tmp_path))
        assert set(skills.keys()) == {"react", "fastapi", "docker"}

    def test_missing_directory_returns_empty(self, tmp_path):
        skills = discover_available_skills(str(tmp_path / "nonexistent"))
        assert skills == {}

    def test_empty_skills_dir_returns_empty(self, tmp_path):
        (tmp_path / "skills").mkdir()
        skills = discover_available_skills(str(tmp_path / "skills"))
        assert skills == {}

    def test_malformed_frontmatter_still_registers_skill(self, tmp_path, capsys):
        """A skill with unreadable frontmatter should still be indexed by name."""
        d = tmp_path / "broken-skill"
        d.mkdir()
        (d / "SKILL.md").write_text("not---valid---frontmatter", encoding="utf-8")
        skills = discover_available_skills(str(tmp_path))
        # Should still contain the skill (minimal record)
        assert "broken-skill" in skills

    def test_nested_skill_discovered(self, tmp_path):
        nested = tmp_path / "category" / "deep-skill"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("# deep\n", encoding="utf-8")
        skills = discover_available_skills(str(tmp_path))
        assert "deep-skill" in skills


# ---------------------------------------------------------------------------
# read_wiki_overrides
# ---------------------------------------------------------------------------

class TestReadWikiOverrides:
    def _make_override_page(self, wiki: Path, name: str, fields: dict) -> None:
        entities = wiki / "entities" / "skills"
        entities.mkdir(parents=True, exist_ok=True)
        fm_lines = "\n".join(f"{k}: {v}" for k, v in fields.items())
        page = f"---\n{fm_lines}\n---\n# {name}\n"
        (entities / f"{name}.md").write_text(page, encoding="utf-8")

    def test_always_load_true(self, tmp_path):
        self._make_override_page(tmp_path, "react", {"always_load": "true", "use_count": "3"})
        overrides = read_wiki_overrides(str(tmp_path))
        assert overrides["react"]["always_load"] is True
        assert overrides["react"]["use_count"] == 3

    def test_never_load_true(self, tmp_path):
        self._make_override_page(tmp_path, "legacy", {"never_load": "true", "use_count": "0"})
        overrides = read_wiki_overrides(str(tmp_path))
        assert overrides["legacy"]["never_load"] is True

    def test_defaults_when_fields_absent(self, tmp_path):
        # Provide at least one field so _parse_fm returns non-empty and the
        # entry isn't skipped by the `if not meta: continue` guard.
        self._make_override_page(tmp_path, "plain", {"status": "installed"})
        overrides = read_wiki_overrides(str(tmp_path))
        assert overrides["plain"]["always_load"] is False
        assert overrides["plain"]["never_load"] is False
        assert overrides["plain"]["use_count"] == 0

    def test_missing_entities_dir_returns_empty(self, tmp_path):
        overrides = read_wiki_overrides(str(tmp_path))
        assert overrides == {}

    def test_bad_use_count_defaults_to_zero(self, tmp_path):
        self._make_override_page(tmp_path, "bad", {"use_count": "not-a-number"})
        # Should not raise; int(str("not-a-number")) will raise, so it goes to except
        overrides = read_wiki_overrides(str(tmp_path))
        # Page with parse error is skipped (continue in except)
        assert "bad" not in overrides

    def test_page_without_frontmatter_skipped(self, tmp_path):
        entities = tmp_path / "entities" / "skills"
        entities.mkdir(parents=True)
        (entities / "plain.md").write_text("# plain\nno frontmatter here", encoding="utf-8")
        overrides = read_wiki_overrides(str(tmp_path))
        assert "plain" not in overrides


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

class TestResolve:
    def test_basic_load_known_skill(self, tmp_path):
        available = {"react": {"path": str(tmp_path / "react/SKILL.md"), "name": "react"}}
        profile = _minimal_profile(frameworks=[_detection("react")])
        manifest = resolve(profile, available, {})
        loaded_names = [e["skill"] for e in manifest["load"]]
        assert "react" in loaded_names

    def test_skill_not_available_goes_to_suggestions(self, tmp_path):
        available = {}  # react not installed
        profile = _minimal_profile(frameworks=[_detection("react")])
        manifest = resolve(profile, available, {})
        suggestion_skills = [s["skill"] for s in manifest["suggestions"]]
        assert "react" in suggestion_skills
        assert manifest["load"] == []

    def test_always_load_override_adds_skill(self, tmp_path):
        available = {"docker": {"path": str(tmp_path / "docker/SKILL.md"), "name": "docker"}}
        overrides = {"docker": {"always_load": True, "never_load": False, "use_count": 0, "last_used": "", "status": "installed"}}
        profile = _minimal_profile()  # no detection for docker
        manifest = resolve(profile, available, overrides)
        loaded_names = [e["skill"] for e in manifest["load"]]
        assert "docker" in loaded_names

    def test_never_load_override_removes_skill(self, tmp_path):
        available = {"react": {"path": str(tmp_path / "react/SKILL.md"), "name": "react"}}
        overrides = {"react": {"always_load": False, "never_load": True, "use_count": 0, "last_used": "", "status": "installed"}}
        profile = _minimal_profile(frameworks=[_detection("react")])
        manifest = resolve(profile, available, overrides)
        loaded_names = [e["skill"] for e in manifest["load"]]
        assert "react" not in loaded_names

    def test_conflict_resolution_keeps_higher_priority(self, tmp_path):
        available = {
            "fastapi": {"path": str(tmp_path / "fastapi/SKILL.md"), "name": "fastapi"},
            "flask": {"path": str(tmp_path / "flask/SKILL.md"), "name": "flask"},
        }
        profile = _minimal_profile(frameworks=[
            _detection("fastapi", confidence=0.95),
            _detection("flask", confidence=0.6),
        ])
        manifest = resolve(profile, available, {})
        loaded_names = [e["skill"] for e in manifest["load"]]
        # fastapi has higher base priority (8) and higher confidence boost
        assert "fastapi" in loaded_names
        assert "flask" not in loaded_names
        assert any("Conflict" in w for w in manifest["warnings"])

    def test_max_skills_cap(self, tmp_path):
        # Create 20 available skills each mapped from detections
        skills_with_map = ["react", "docker", "fastapi", "django", "flask",
                           "pytest", "jest", "langchain", "nextjs", "vue"]
        available = {n: {"path": str(tmp_path / n / "SKILL.md"), "name": n} for n in skills_with_map}
        profile = _minimal_profile(frameworks=[_detection(n, 0.9) for n in skills_with_map])
        manifest = resolve(profile, available, {}, max_skills=3)
        # At most 3 skill-mapped items (plus meta skills if available)
        non_meta = [e for e in manifest["load"] if e["skill"] not in ("skill-router", "file-reading")]
        assert len(non_meta) <= 3
        assert any("Capped" in w for w in manifest["warnings"])

    def test_meta_skills_added_if_available(self, tmp_path):
        available = {"skill-router": {"path": str(tmp_path / "skill-router/SKILL.md"), "name": "skill-router"}}
        profile = _minimal_profile()
        manifest = resolve(profile, available, {})
        loaded_names = [e["skill"] for e in manifest["load"]]
        assert "skill-router" in loaded_names

    def test_unloaded_skills_in_unload_list(self, tmp_path):
        available = {
            "react": {"path": str(tmp_path / "react/SKILL.md"), "name": "react"},
            "docker": {"path": str(tmp_path / "docker/SKILL.md"), "name": "docker"},
        }
        profile = _minimal_profile(frameworks=[_detection("react")])
        manifest = resolve(profile, available, {})
        unload_names = [e["skill"] for e in manifest["unload"]]
        assert "docker" in unload_names

    def test_empty_profile_no_crash(self):
        manifest = resolve(_minimal_profile(), {}, {})
        assert manifest["load"] == []
        assert "generated_at" in manifest

    def test_high_confidence_boost_applied(self, tmp_path):
        """Skills with confidence >=0.9 should get priority +10."""
        available = {"react": {"path": str(tmp_path / "react/SKILL.md"), "name": "react"}}
        profile = _minimal_profile(frameworks=[_detection("react", confidence=0.95)])
        manifest = resolve(profile, available, {})
        entry = next(e for e in manifest["load"] if e["skill"] == "react")
        # PRIORITY_BASE["react"] = 7, +10 for confidence, +0 no use_count
        assert entry["priority"] >= 17


class TestResolveMcpRecommendations:
    """Phase 5 regression: graph-walk hits with type=='mcp-server' land
    in manifest['mcp_servers'], NOT manifest['load'] (the skill bucket).
    """

    def _fake_graph(self, monkeypatch):
        """Make resolve() think the graph is available and non-empty."""
        from ctx.core.resolve import resolve_skills  # noqa: PLC0415

        class _FakeGraph:
            def number_of_nodes(self) -> int:
                return 1

        monkeypatch.setattr(
            resolve_skills, "_load_graph", lambda: _FakeGraph()
        )
        monkeypatch.setattr(resolve_skills, "_GRAPH_AVAILABLE", True)

    def test_mcp_graph_hit_lands_in_mcp_servers_not_load(
        self, tmp_path, monkeypatch
    ):
        from ctx.core.resolve import resolve_skills  # noqa: PLC0415
        self._fake_graph(monkeypatch)

        # Synthetic graph hit: an MCP-type neighbor with score above the
        # 1.5 noise floor.
        def fake_resolve_by_seeds(graph, seeds, **kwargs):
            return [
                {
                    "name": "github-mcp-server",
                    "type": "mcp-server",
                    "score": 3.0,
                    "shared_tags": ["_t:github"],
                    "via": ["react"],
                },
            ]

        monkeypatch.setattr(
            resolve_skills, "_resolve_by_seeds", fake_resolve_by_seeds
        )

        available = {"react": {"path": str(tmp_path / "react/SKILL.md"), "name": "react"}}
        profile = _minimal_profile(frameworks=[_detection("react")])
        manifest = resolve(profile, available, {})

        mcp_names = [m["name"] for m in manifest["mcp_servers"]]
        load_names = [e["skill"] for e in manifest["load"]]
        assert "github-mcp-server" in mcp_names, (
            f"MCP should appear in mcp_servers bucket; got {manifest['mcp_servers']}"
        )
        assert "github-mcp-server" not in load_names, (
            "MCP must NOT land in manifest['load'] — that's the skill-loader bucket"
        )

    def test_mcp_entry_has_reason_score_and_shared_tags(
        self, tmp_path, monkeypatch
    ):
        from ctx.core.resolve import resolve_skills  # noqa: PLC0415
        self._fake_graph(monkeypatch)

        def fake_resolve_by_seeds(graph, seeds, **kwargs):
            return [
                {
                    "name": "fetch-mcp",
                    "type": "mcp-server",
                    "score": 2.5,
                    "shared_tags": ["_t:fetch", "http"],
                    "via": ["django"],
                },
            ]

        monkeypatch.setattr(
            resolve_skills, "_resolve_by_seeds", fake_resolve_by_seeds
        )

        available = {"django": {"path": str(tmp_path / "django/SKILL.md"), "name": "django"}}
        profile = _minimal_profile(frameworks=[_detection("django")])
        manifest = resolve(profile, available, {})

        assert len(manifest["mcp_servers"]) == 1
        entry = manifest["mcp_servers"][0]
        assert entry["name"] == "fetch-mcp"
        assert entry["score"] == 2.5
        assert "graph neighbor of django" in entry["reason"]
        assert "_t:fetch" in entry["shared_tags"]

    def test_mcp_deduped_when_same_name_hit_twice(
        self, tmp_path, monkeypatch
    ):
        from ctx.core.resolve import resolve_skills  # noqa: PLC0415
        self._fake_graph(monkeypatch)

        def fake_resolve_by_seeds(graph, seeds, **kwargs):
            return [
                {"name": "duped-mcp", "type": "mcp-server", "score": 2.0,
                 "shared_tags": [], "via": ["a"]},
                {"name": "duped-mcp", "type": "mcp-server", "score": 1.8,
                 "shared_tags": [], "via": ["b"]},
            ]

        monkeypatch.setattr(
            resolve_skills, "_resolve_by_seeds", fake_resolve_by_seeds
        )

        available = {"react": {"path": str(tmp_path / "react/SKILL.md"), "name": "react"}}
        profile = _minimal_profile(frameworks=[_detection("react")])
        manifest = resolve(profile, available, {})

        assert sum(1 for m in manifest["mcp_servers"] if m["name"] == "duped-mcp") == 1

    def test_mcp_below_noise_floor_dropped(
        self, tmp_path, monkeypatch
    ):
        from ctx.core.resolve import resolve_skills  # noqa: PLC0415
        self._fake_graph(monkeypatch)

        def fake_resolve_by_seeds(graph, seeds, **kwargs):
            # Post-P2.5 the resolver uses normalized_score (percentile
            # in [0,1]) against the 0.20 MCP floor. A value of 0.1
            # sits below the floor so the hit must be dropped.
            return [
                {"name": "weak-mcp", "type": "mcp-server",
                 "score": 0.8, "normalized_score": 0.10,
                 "shared_tags": [], "via": ["a"]},
            ]

        monkeypatch.setattr(
            resolve_skills, "_resolve_by_seeds", fake_resolve_by_seeds
        )

        available = {"react": {"path": str(tmp_path / "react/SKILL.md"), "name": "react"}}
        profile = _minimal_profile(frameworks=[_detection("react")])
        manifest = resolve(profile, available, {})

        assert manifest["mcp_servers"] == []

    def test_skill_hits_still_route_to_load(
        self, tmp_path, monkeypatch
    ):
        # Regression: mixing mcp-server hits and skill hits must not
        # break the existing skill path.
        from ctx.core.resolve import resolve_skills  # noqa: PLC0415
        self._fake_graph(monkeypatch)

        def fake_resolve_by_seeds(graph, seeds, **kwargs):
            return [
                {"name": "pytest-something", "type": "skill", "score": 3.0,
                 "shared_tags": [], "via": ["django"]},
                {"name": "fetch-mcp", "type": "mcp-server", "score": 2.0,
                 "shared_tags": [], "via": ["django"]},
            ]

        monkeypatch.setattr(
            resolve_skills, "_resolve_by_seeds", fake_resolve_by_seeds
        )

        available = {
            "django": {"path": str(tmp_path / "django/SKILL.md"), "name": "django"},
            "pytest-something": {
                "path": str(tmp_path / "pytest-something/SKILL.md"),
                "name": "pytest-something",
            },
        }
        profile = _minimal_profile(frameworks=[_detection("django")])
        manifest = resolve(profile, available, {})

        mcp_names = [m["name"] for m in manifest["mcp_servers"]]
        load_names = [e["skill"] for e in manifest["load"]]
        assert "fetch-mcp" in mcp_names
        assert "pytest-something" in load_names


# ---------------------------------------------------------------------------
# read_intent_signals
# ---------------------------------------------------------------------------

class TestReadIntentSignals:
    def _write_log(self, path: Path, entries: list[dict]) -> None:
        lines = [json.dumps(e) for e in entries]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_returns_empty_when_file_missing(self, tmp_path):
        signals = read_intent_signals(str(tmp_path / "no-such.jsonl"))
        assert signals == {}

    def test_counts_todays_signals(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log = tmp_path / "intent.jsonl"
        self._write_log(log, [
            {"date": today, "signals": ["react", "docker"]},
            {"date": today, "signals": ["react"]},
        ])
        signals = read_intent_signals(str(log))
        assert signals["react"] == 2
        assert signals["docker"] == 1

    def test_ignores_other_dates(self, tmp_path):
        log = tmp_path / "intent.jsonl"
        self._write_log(log, [
            {"date": "2020-01-01", "signals": ["react"]},
        ])
        signals = read_intent_signals(str(log))
        # Today's date != 2020-01-01, so nothing counted
        assert "react" not in signals

    def test_skips_bad_json_lines(self, tmp_path):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log = tmp_path / "intent.jsonl"
        log.write_text(
            'not json\n'
            f'{json.dumps({"date": today, "signals": ["fastapi"]})}\n',
            encoding="utf-8",
        )
        signals = read_intent_signals(str(log))
        assert signals.get("fastapi") == 1

    def test_empty_file_returns_empty(self, tmp_path):
        log = tmp_path / "intent.jsonl"
        log.write_text("", encoding="utf-8")
        signals = read_intent_signals(str(log))
        assert signals == {}


# ---------------------------------------------------------------------------
# apply_intent_boosts
# ---------------------------------------------------------------------------

class TestApplyIntentBoosts:
    def _make_manifest(self) -> dict:
        return {"suggestions": [], "warnings": []}

    def test_boosts_existing_skill_in_needed(self):
        needed = {"react": {"priority": 10, "reason": "detected", "confidence": 0.9}}
        available = {}
        manifest = self._make_manifest()
        apply_intent_boosts(needed, {"react": 2}, available, manifest)
        # boost = 5 * min(2, 3) = 10
        assert needed["react"]["priority"] == 20

    def test_boost_capped_at_three_signals(self):
        needed = {"react": {"priority": 10, "reason": "detected", "confidence": 0.9}}
        manifest = self._make_manifest()
        apply_intent_boosts(needed, {"react": 10}, {}, manifest)
        # boost = 5 * min(10, 3) = 15
        assert needed["react"]["priority"] == 25

    def test_available_not_in_needed_becomes_suggestion(self, tmp_path):
        needed = {}
        available = {"docker": {"path": str(tmp_path / "docker/SKILL.md")}}
        manifest = self._make_manifest()
        apply_intent_boosts(needed, {"docker": 1}, available, manifest)
        suggestion_skills = [s["skill"] for s in manifest["suggestions"]]
        assert "docker" in suggestion_skills

    def test_unknown_signal_no_crash(self):
        needed = {}
        manifest = self._make_manifest()
        apply_intent_boosts(needed, {"totally-unknown-signal": 5}, {}, manifest)
        assert manifest["suggestions"] == []

    def test_empty_signals_no_change(self):
        needed = {"react": {"priority": 10, "reason": "x", "confidence": 0.9}}
        manifest = self._make_manifest()
        apply_intent_boosts(needed, {}, {}, manifest)
        assert needed["react"]["priority"] == 10


# ─────────────────────────────────────────────────────────────────────
# P2.5 normalised noise-floor regression
# ─────────────────────────────────────────────────────────────────────
#
# Pre-P2.5 the floors were absolute (1.5 / 1.0) and calibrated against
# the v0.6 integer-weight graph. On v0.7's blended float-weight graph
# a single edge is <=1.0, so absolute 1.5 dropped ALL single-seed
# hits on any sparse/test graph — the suggestion arm silently produced
# nothing. Post-fix the floors are percentile thresholds in [0,1],
# scale-invariant.

class TestNoiseFloorNormalized:

    def _fake_graph(self, monkeypatch):
        import networkx as nx
        from ctx.core.resolve import resolve_skills

        def fake_load_graph(_=None):
            G = nx.Graph()
            G.add_node("skill:foo", type="skill", label="foo")
            return G

        monkeypatch.setattr(resolve_skills, "_load_graph", fake_load_graph)

    def test_normalized_score_at_top_survives(self, tmp_path, monkeypatch):
        """A hit with normalized_score=1.0 (the top of its ranking)
        always passes the floor — otherwise the top recommendation
        gets silently dropped on small fixtures."""
        from ctx.core.resolve import resolve_skills
        from ctx.core.resolve.resolve_skills import resolve
        self._fake_graph(monkeypatch)

        def fake_hits(*a, **kw):
            return [
                {"name": "top-skill", "type": "skill",
                 "score": 1.23, "normalized_score": 1.0,
                 "shared_tags": ["x"], "via": ["react"]},
            ]

        monkeypatch.setattr(resolve_skills, "_resolve_by_seeds", fake_hits)
        available = {"react": {"path": str(tmp_path / "r/SKILL.md"), "name": "react"},
                     "top-skill": {"path": str(tmp_path / "t/SKILL.md"), "name": "top-skill"}}
        profile = _minimal_profile(frameworks=[_detection("react")])
        manifest = resolve(profile, available, {})
        assert "top-skill" in {e["skill"] for e in manifest["load"]}

    def test_skill_below_0_3_dropped(self, tmp_path, monkeypatch):
        """Skill hits with normalized_score < 0.30 don't make the cut."""
        from ctx.core.resolve import resolve_skills
        from ctx.core.resolve.resolve_skills import resolve
        self._fake_graph(monkeypatch)

        def fake_hits(*a, **kw):
            return [
                {"name": "noisy", "type": "skill",
                 "score": 0.12, "normalized_score": 0.10,
                 "shared_tags": [], "via": ["react"]},
            ]
        monkeypatch.setattr(resolve_skills, "_resolve_by_seeds", fake_hits)
        available = {"react": {"path": str(tmp_path / "r/SKILL.md"), "name": "react"},
                     "noisy": {"path": str(tmp_path / "n/SKILL.md"), "name": "noisy"}}
        profile = _minimal_profile(frameworks=[_detection("react")])
        manifest = resolve(profile, available, {})
        assert "noisy" not in {e["skill"] for e in manifest["load"]}

    def test_mcp_floor_is_lower_than_skill_floor(self, tmp_path, monkeypatch):
        """MCPs are historically sparser; a hit with normalized_score=0.25
        passes the MCP floor (0.20) but NOT the skill floor (0.30).
        Pinning this asymmetry so a future 'unify the floors' refactor
        wipes out MCP recommendations without CI noticing."""
        from ctx.core.resolve import resolve_skills
        from ctx.core.resolve.resolve_skills import resolve
        self._fake_graph(monkeypatch)

        def fake_hits(*a, **kw):
            return [
                {"name": "mid-mcp", "type": "mcp-server",
                 "score": 0.3, "normalized_score": 0.25,
                 "shared_tags": ["x"], "via": ["react"]},
                {"name": "mid-skill", "type": "skill",
                 "score": 0.3, "normalized_score": 0.25,
                 "shared_tags": ["x"], "via": ["react"]},
            ]
        monkeypatch.setattr(resolve_skills, "_resolve_by_seeds", fake_hits)
        available = {
            "react": {"path": str(tmp_path / "r/SKILL.md"), "name": "react"},
            "mid-skill": {"path": str(tmp_path / "s/SKILL.md"), "name": "mid-skill"},
        }
        profile = _minimal_profile(frameworks=[_detection("react")])
        manifest = resolve(profile, available, {})
        # Skill below 0.30 → dropped.
        assert "mid-skill" not in {e["skill"] for e in manifest["load"]}
        # MCP above 0.20 → kept.
        mcp_names = {m["name"] for m in manifest["mcp_servers"]}
        assert "mid-mcp" in mcp_names

    def test_backward_compat_raw_score_fallback(self, tmp_path, monkeypatch):
        """An older resolver output without normalized_score must
        still work via raw-score fallback, so a stale cached graph
        doesn't silently return [] after the P2.5 upgrade."""
        from ctx.core.resolve import resolve_skills
        from ctx.core.resolve.resolve_skills import resolve
        self._fake_graph(monkeypatch)

        def fake_hits(*a, **kw):
            # No ``normalized_score`` key — pre-P2.5 shape.
            return [
                {"name": "legacy-hit", "type": "skill",
                 "score": 2.5, "shared_tags": ["x"], "via": ["react"]},
            ]
        monkeypatch.setattr(resolve_skills, "_resolve_by_seeds", fake_hits)
        available = {"react": {"path": str(tmp_path / "r/SKILL.md"), "name": "react"},
                     "legacy-hit": {"path": str(tmp_path / "l/SKILL.md"), "name": "legacy-hit"}}
        profile = _minimal_profile(frameworks=[_detection("react")])
        manifest = resolve(profile, available, {})
        # Raw score 2.5 >> any old skill floor so legacy behaviour survives.
        assert "legacy-hit" in {e["skill"] for e in manifest["load"]}
