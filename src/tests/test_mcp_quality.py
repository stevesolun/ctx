"""
test_mcp_quality.py -- Tests for the MCP quality orchestrator (mcp_quality.py).

Covers:
  - McpQualityConfig validation (weights, grade thresholds)
  - compute_quality weighted aggregation + grade assignment
  - extract_signals_for_slug filesystem integration (entity frontmatter,
    graph_index lookup, missing-entity error)
  - persist_quality sidecar JSON + frontmatter + wiki-body injection (idempotent)
  - load_graph_index (missing file, single node, cross-type edge counting)
  - CLI verbs: --help, recompute, show, list
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

_IMPORT_OK = False
try:
    import mcp_quality as mq  # noqa: E402
    from ctx.core.quality.quality_signals import SignalResult  # noqa: E402

    _IMPORT_OK = True
except ImportError:
    pass

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK,
    reason="mcp_quality or its dependencies not yet available",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIGNAL_NAMES = ["popularity", "freshness", "structural", "graph", "trust", "runtime"]

_DEFAULT_WEIGHTS = {
    "popularity": 0.30,
    "freshness": 0.20,
    "structural": 0.15,
    "graph": 0.15,
    "trust": 0.10,
    "runtime": 0.10,
}


def _all_signals(score: float = 1.0) -> dict[str, SignalResult]:
    """Build a dict of six SignalResult objects all set to *score*."""
    return {name: SignalResult(score=score) for name in _SIGNAL_NAMES}


def _entity_frontmatter(
    name: str = "github",
    description: str = "A GitHub MCP server for testing.",
    stars: int | None = 42,
    last_commit_at: str | None = "2026-01-01T00:00:00+00:00",
    tags: str = "[utility]",
    transports: str = "[stdio]",
    language: str | None = "python",
    license_: str | None = "MIT",
    author: str | None = "testorg",
    author_type: str | None = "org",
) -> str:
    """Return a minimal Markdown entity string with YAML frontmatter."""
    parts = [
        "---",
        f"name: {name}",
        f"description: {description}",
        "type: mcp-server",
        f"tags: {tags}",
        f"transports: {transports}",
    ]
    if stars is not None:
        parts.append(f"stars: {stars}")
    if last_commit_at is not None:
        parts.append(f"last_commit_at: {last_commit_at!r}")
    if language is not None:
        parts.append(f"language: {language}")
    if license_ is not None:
        parts.append(f"license: {license_}")
    if author is not None:
        parts.append(f"author: {author}")
    if author_type is not None:
        parts.append(f"author_type: {author_type}")
    parts += ["---", "", "# GitHub MCP", "", "## Overview", "", "Test entity body.", ""]
    return "\n".join(parts)


def _write_entity(wiki_dir: Path, slug: str, content: str | None = None) -> Path:
    """Write an entity page under wiki/entities/mcp-servers/<shard>/<slug>.md."""
    shard = slug[0] if slug[0].isalpha() else "0-9"
    entity_dir = wiki_dir / "entities" / "mcp-servers" / shard
    entity_dir.mkdir(parents=True, exist_ok=True)
    path = entity_dir / f"{slug}.md"
    path.write_text(content or _entity_frontmatter(name=slug), encoding="utf-8")
    return path


def _make_score(
    slug: str = "github",
    score: float = 0.8,
    grade: str = "A",
    computed_at: str = "2026-04-20T00:00:00+00:00",
) -> "mq.McpQualityScore":
    """Build a McpQualityScore from all-uniform signals."""
    return mq.compute_quality(
        slug=slug,
        signals=_all_signals(score),
        config=mq.McpQualityConfig(weights=_DEFAULT_WEIGHTS),
        computed_at=computed_at,
    )


# ---------------------------------------------------------------------------
# TestMcpQualityConfig
# ---------------------------------------------------------------------------


class TestMcpQualityConfig:
    def test_default_weights_sum_to_one(self) -> None:
        cfg = mq.McpQualityConfig(weights=_DEFAULT_WEIGHTS)
        assert sum(cfg.weights.values()) == pytest.approx(1.0)

    def test_weights_with_wrong_keys_raise(self) -> None:
        bad_weights = {
            "popularity": 0.20,
            "freshness": 0.20,
            "structural": 0.20,
            "graph": 0.20,
            "trust": 0.10,
            "extra_unknown_key": 0.10,
        }
        with pytest.raises(ValueError):
            mq.McpQualityConfig(weights=bad_weights)

    def test_weights_summing_to_less_than_one_raise(self) -> None:
        bad_weights = {
            "popularity": 0.20,
            "freshness": 0.15,
            "structural": 0.15,
            "graph": 0.15,
            "trust": 0.10,
            "runtime": 0.10,  # total = 0.85
        }
        with pytest.raises(ValueError):
            mq.McpQualityConfig(weights=bad_weights)

    def test_grade_thresholds_a_below_b_raise(self) -> None:
        with pytest.raises(ValueError):
            mq.McpQualityConfig(
                weights=_DEFAULT_WEIGHTS,
                grade_thresholds={"A": 0.3, "B": 0.5, "C": 0.3},
            )


# ---------------------------------------------------------------------------
# TestComputeQuality
# ---------------------------------------------------------------------------


class TestComputeQuality:
    def test_all_one_signals_scores_one_grade_a(self) -> None:
        result = mq.compute_quality(
            slug="github",
            signals=_all_signals(1.0),
            config=mq.McpQualityConfig(weights=_DEFAULT_WEIGHTS),
        )
        assert result.score == pytest.approx(1.0)
        assert result.grade == "A"

    def test_all_half_signals_grade_c_or_below(self) -> None:
        result = mq.compute_quality(
            slug="github",
            signals=_all_signals(0.5),
            config=mq.McpQualityConfig(weights=_DEFAULT_WEIGHTS),
        )
        assert result.score == pytest.approx(0.5)
        # 0.5 is below the B threshold (0.60) — expect C or lower.
        assert result.grade in ("C", "D", "F")

    def test_all_zero_signals_scores_zero_grade_f(self) -> None:
        result = mq.compute_quality(
            slug="github",
            signals=_all_signals(0.0),
            config=mq.McpQualityConfig(weights=_DEFAULT_WEIGHTS),
        )
        assert result.score == pytest.approx(0.0)
        assert result.grade == "F"

    def test_missing_signal_key_raises(self) -> None:
        sigs = _all_signals(1.0)
        del sigs["graph"]
        with pytest.raises(ValueError):
            mq.compute_quality(
                slug="github",
                signals=sigs,
                config=mq.McpQualityConfig(weights=_DEFAULT_WEIGHTS),
            )

    def test_extra_signal_key_raises(self) -> None:
        sigs = _all_signals(1.0)
        sigs["bonus_signal"] = SignalResult(score=0.9)
        with pytest.raises(ValueError):
            mq.compute_quality(
                slug="github",
                signals=sigs,
                config=mq.McpQualityConfig(weights=_DEFAULT_WEIGHTS),
            )

    def test_score_stored_on_result(self) -> None:
        result = mq.compute_quality(
            slug="github",
            signals=_all_signals(0.8),
            config=mq.McpQualityConfig(weights=_DEFAULT_WEIGHTS),
        )
        assert result.slug == "github"
        assert 0.0 <= result.score <= 1.0

    def test_computed_at_defaults_to_iso_string(self) -> None:
        result = mq.compute_quality(
            slug="github",
            signals=_all_signals(1.0),
        )
        # Should be a non-empty ISO-format string.
        assert isinstance(result.computed_at, str)
        assert "T" in result.computed_at

    def test_to_dict_is_json_serializable(self) -> None:
        result = mq.compute_quality(
            slug="github",
            signals=_all_signals(0.7),
        )
        d = result.to_dict()
        # Must not raise.
        serialized = json.dumps(d)
        parsed = json.loads(serialized)
        assert parsed["slug"] == "github"


# ---------------------------------------------------------------------------
# TestExtractSignalsForSlug
# ---------------------------------------------------------------------------


class TestExtractSignalsForSlug:
    def test_full_frontmatter_returns_all_six_signals(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        _write_entity(wiki_dir, "github")

        sigs = mq.extract_signals_for_slug("github", wiki_dir=wiki_dir)

        assert set(sigs.keys()) == set(_SIGNAL_NAMES)
        for name, result in sigs.items():
            assert isinstance(result, SignalResult), (
                f"signal '{name}' is not a SignalResult"
            )

    def test_no_graph_index_graph_signal_isolated(self, tmp_path: Path) -> None:
        wiki_dir = tmp_path / "wiki"
        _write_entity(wiki_dir, "github")

        sigs = mq.extract_signals_for_slug(
            "github", wiki_dir=wiki_dir, graph_index=None
        )

        assert sigs["graph"].evidence.get("isolated") is True

    def test_graph_index_propagates_degree(self, tmp_path: Path) -> None:
        wiki_dir = tmp_path / "wiki"
        _write_entity(wiki_dir, "github")
        graph_index = {
            "mcp-server:github": {"degree": 12, "cross_type_degree": 3}
        }

        sigs = mq.extract_signals_for_slug(
            "github", wiki_dir=wiki_dir, graph_index=graph_index
        )

        assert sigs["graph"].evidence.get("degree") == 12
        assert sigs["graph"].evidence.get("isolated") is False

    def test_missing_entity_raises_file_not_found(self, tmp_path: Path) -> None:
        wiki_dir = tmp_path / "wiki"
        (wiki_dir / "entities" / "mcp-servers").mkdir(parents=True)

        with pytest.raises(FileNotFoundError):
            mq.extract_signals_for_slug("does-not-exist", wiki_dir=wiki_dir)


# ---------------------------------------------------------------------------
# TestPersistQuality
# ---------------------------------------------------------------------------


class TestPersistQuality:
    def test_sidecar_json_written_at_expected_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        _write_entity(wiki_dir, "github")
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        score = _make_score(slug="github")
        mq.persist_quality(score, wiki_dir=wiki_dir)

        sidecar = fake_home / ".claude" / "skill-quality" / "mcp" / "github.json"
        assert sidecar.is_file(), f"sidecar not found at {sidecar}"

    def test_frontmatter_keys_injected_after_persist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        entity_path = _write_entity(wiki_dir, "github")
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        score = _make_score(slug="github")
        mq.persist_quality(score, wiki_dir=wiki_dir)

        content = entity_path.read_text(encoding="utf-8")
        assert "quality_score:" in content
        assert "quality_grade:" in content
        assert "quality_updated_at:" in content

    def test_quality_block_markers_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        entity_path = _write_entity(wiki_dir, "github")
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        score = _make_score(slug="github")
        mq.persist_quality(score, wiki_dir=wiki_dir)

        content = entity_path.read_text(encoding="utf-8")
        assert "<!-- quality:begin -->" in content
        assert "<!-- quality:end -->" in content

    def test_re_persist_does_not_duplicate_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        entity_path = _write_entity(wiki_dir, "github")
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        score = _make_score(slug="github")
        mq.persist_quality(score, wiki_dir=wiki_dir)
        mq.persist_quality(score, wiki_dir=wiki_dir)

        content = entity_path.read_text(encoding="utf-8")
        assert content.count("<!-- quality:begin -->") == 1
        assert content.count("<!-- quality:end -->") == 1
        assert content.count("quality_score:") == 1


# ---------------------------------------------------------------------------
# TestLoadGraphIndex
# ---------------------------------------------------------------------------


class TestLoadGraphIndex:
    def test_missing_graph_json_returns_empty_dict(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        result = mq.load_graph_index(wiki_dir)
        assert result == {}

    def test_single_mcp_server_node_returns_degree(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        graph_dir = wiki_dir / "graphify-out"
        graph_dir.mkdir(parents=True)

        # Minimal NetworkX node-link format (edges key).
        graph_data = {
            "directed": False,
            "multigraph": False,
            "graph": {},
            "nodes": [
                {"id": "mcp-server:github", "type": "mcp-server"},
                {"id": "skill:git", "type": "skill"},
            ],
            "edges": [
                {"source": "mcp-server:github", "target": "skill:git"},
            ],
        }
        (graph_dir / "graph.json").write_text(
            json.dumps(graph_data), encoding="utf-8"
        )

        index = mq.load_graph_index(wiki_dir)
        assert "mcp-server:github" in index
        node = index["mcp-server:github"]
        assert node["degree"] >= 1

    def test_multiple_cross_type_edges_counted_correctly(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        graph_dir = wiki_dir / "graphify-out"
        graph_dir.mkdir(parents=True)

        graph_data = {
            "directed": False,
            "multigraph": False,
            "graph": {},
            "nodes": [
                {"id": "mcp-server:github", "type": "mcp-server"},
                {"id": "skill:git", "type": "skill"},
                {"id": "agent:coder", "type": "agent"},
                {"id": "mcp-server:other", "type": "mcp-server"},
            ],
            "edges": [
                # 2 cross-type edges (to skill + agent)
                {"source": "mcp-server:github", "target": "skill:git"},
                {"source": "mcp-server:github", "target": "agent:coder"},
                # 1 same-type edge
                {"source": "mcp-server:github", "target": "mcp-server:other"},
            ],
        }
        (graph_dir / "graph.json").write_text(
            json.dumps(graph_data), encoding="utf-8"
        )

        index = mq.load_graph_index(wiki_dir)
        node = index["mcp-server:github"]
        # Total degree = 3 (skill + agent + mcp-server)
        assert node["degree"] == 3
        # Cross-type = 2 (skill + agent only)
        assert node["cross_type_degree"] == 2


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------


class TestCLI:
    def _setup_wiki(self, tmp_path: Path) -> Path:
        wiki_dir = tmp_path / "wiki"
        _write_entity(wiki_dir, "github")
        return wiki_dir

    def test_help_exits_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["mcp_quality", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            mq.main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert len(out) > 0

    def test_recompute_slug_writes_sidecar(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wiki_dir = self._setup_wiki(tmp_path)
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(
            sys, "argv", ["mcp_quality", "recompute", "--slug", "github",
                           "--wiki-dir", str(wiki_dir)]
        )
        mq.main()
        sidecar = fake_home / ".claude" / "skill-quality" / "mcp" / "github.json"
        assert sidecar.is_file()

    def test_show_slug_prints_json(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wiki_dir = self._setup_wiki(tmp_path)
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        # First recompute so sidecar exists.
        monkeypatch.setattr(
            sys, "argv", ["mcp_quality", "recompute", "--slug", "github",
                           "--wiki-dir", str(wiki_dir)]
        )
        mq.main()
        capsys.readouterr()

        monkeypatch.setattr(
            sys, "argv", ["mcp_quality", "show", "github",
                           "--wiki-dir", str(wiki_dir), "--json"]
        )
        mq.main()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["slug"] == "github"
        assert "grade" in parsed

    def test_list_prints_slug_grade_lines(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wiki_dir = self._setup_wiki(tmp_path)
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        monkeypatch.setattr(
            sys, "argv", ["mcp_quality", "recompute", "--slug", "github",
                           "--wiki-dir", str(wiki_dir)]
        )
        mq.main()
        capsys.readouterr()

        monkeypatch.setattr(
            sys, "argv", ["mcp_quality", "list",
                           "--wiki-dir", str(wiki_dir)]
        )
        mq.main()
        out = capsys.readouterr().out
        assert len(out.strip()) > 0
        # Each line must contain a tab separating slug and grade.
        first_line = out.strip().splitlines()[0]
        assert "\t" in first_line
        # Format: <slug>\t<grade>\tscore=N.NN
        parts = first_line.split("\t")
        assert parts[0] == "github"
        assert parts[1] in ("A", "B", "C", "D", "F")


# ─────────────────────────────────────────────────────────────────────
# default_sidecar_dir honors Path.home monkeypatching (pinned 2026-04-23)
# ─────────────────────────────────────────────────────────────────────
#
# Pre-fix, default_sidecar_dir used os.path.expanduser on the
# config-derived path, which bypasses any Path.home() monkeypatch
# and writes to the real ~/.claude/. Tests monkeypatching Path.home
# (the existing pattern in this file) silently escaped their
# sandbox. The fix swaps the ``~`` -> str(Path.home()) manually
# instead of deferring to os.path.expanduser, so monkeypatched
# home propagates through the configured-path branch too.


class TestDefaultSidecarDirHonorsPathHome:

    def test_configured_path_expands_via_path_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        # default_sidecar_dir should now point under fake_home, not
        # the real user's home dir.
        result = mq.default_sidecar_dir()
        assert str(result).startswith(str(fake_home)), (
            f"default_sidecar_dir ignored Path.home() monkeypatch: "
            f"{result} not under {fake_home}"
        )

    def test_unconfigured_path_falls_back_to_path_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """When config has no mcp_quality.paths.sidecar_dir, the
        fallback path is Path.home()/.claude/skill-quality/mcp —
        honors the monkeypatch via the fallback branch."""
        import ctx_config
        # Strip the configured sidecar_dir so the fallback fires.
        raw = ctx_config._load_raw()
        if "mcp_quality" in raw and isinstance(raw["mcp_quality"], dict):
            raw["mcp_quality"].pop("paths", None)
        monkeypatch.setattr(ctx_config, "cfg", ctx_config.Config(raw))

        fake_home = tmp_path / "fallback-home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        result = mq.default_sidecar_dir()
        assert str(result).startswith(str(fake_home))
        assert ".claude" in str(result)
