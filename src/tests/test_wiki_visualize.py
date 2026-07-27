"""
test_wiki_visualize.py -- User-behavior and security tests for wiki_visualize.

Covers the three Strix-validated XSS sinks:
  1. Caller-provided titles interpolated into ``<title>`` and ``<div id="title">``
  2. Untrusted graph tag names interpolated into filter-button HTML
  3. Untrusted node labels embedded in a raw ``<script>`` block via ``json.dumps``
     (``</script>`` breakout)

Tests build HTML via ``build_html_with_filters`` with attacker-controlled inputs
and assert that the payload never appears in an executable form.
"""

from __future__ import annotations

import builtins
import importlib
import json
import os
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import networkx as nx
import pytest

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _ScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.start_count = 0
        self.end_count = 0
        self.scripts: list[tuple[dict[str, str | None], list[str]]] = []
        self._current_data: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.start_count += 1
            self._current_data = []
            self.scripts.append((dict(attrs), self._current_data))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.end_count += 1
            self._current_data = None

    def handle_data(self, data: str) -> None:
        if self._current_data is not None:
            self._current_data.append(data)


@pytest.fixture()
def graph_with_hostile_labels() -> tuple[nx.Graph, dict]:
    G = nx.Graph()
    G.add_node(
        "skill:evil",
        label="</script><script>window.__pwn=1</script><!--<script>",
        type="skill",
        tags=['"><img src=x onerror="window.__tagpwn=1">'],
    )
    G.add_node("skill:benign", label="benign", type="skill", tags=["safe"])
    G.add_edge("skill:evil", "skill:benign", weight=1)
    pos = {"skill:evil": (0.0, 0.0), "skill:benign": (1.0, 1.0)}
    return G, pos


@pytest.fixture()
def cli_graph_files(tmp_path: Path) -> tuple[Path, Path]:
    graph_path = tmp_path / "graph.json"
    communities_path = tmp_path / "communities.json"
    graph_path.write_text(
        json.dumps(
            {
                "directed": False,
                "multigraph": False,
                "graph": {"export_id": "ctx-cli-test-1"},
                "nodes": [
                    {
                        "id": "skill:alpha",
                        "label": "alpha",
                        "type": "skill",
                        "tags": ["security", "python"],
                    },
                    {
                        "id": "agent:beta",
                        "label": "beta",
                        "type": "agent",
                        "tags": ["security"],
                    },
                    {
                        "id": "harness:gamma",
                        "label": "gamma",
                        "type": "harness",
                        "tags": ["security"],
                    },
                    {
                        "id": "mcp-server:delta",
                        "label": "delta",
                        "type": "mcp-server",
                        "tags": ["external"],
                    },
                ],
                "edges": [
                    {"source": "skill:alpha", "target": "agent:beta", "weight": 3},
                    {"source": "skill:alpha", "target": "harness:gamma", "weight": 1},
                    {"source": "skill:alpha", "target": "mcp-server:delta", "weight": 4},
                ],
            }
        ),
        encoding="utf-8",
    )
    communities_path.write_text(
        json.dumps(
            {
                "communities": {
                    "7": {
                        "label": "secure automation",
                        "members": ["skill:alpha", "agent:beta"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return graph_path, communities_path


def _run_main(monkeypatch: pytest.MonkeyPatch, *args: object) -> None:
    import wiki_visualize as wv

    monkeypatch.setattr(sys, "argv", ["wiki_visualize.py", *(str(arg) for arg in args)])
    wv.main()


def _render_cli_graph(
    monkeypatch: pytest.MonkeyPatch,
    cli_graph_files: tuple[Path, Path],
    tmp_path: Path,
    *filters: object,
) -> str:
    graph_path, communities_path = cli_graph_files
    output_path = tmp_path / "orthogonal-filter.html"
    _run_main(
        monkeypatch,
        "--graph-json",
        graph_path,
        "--communities-json",
        communities_path,
        *filters,
        "--output",
        output_path,
    )
    return output_path.read_text(encoding="utf-8")


def test_html_visualizer_imports_without_optional_plotly(monkeypatch):
    previous = sys.modules.pop("wiki_visualize", None)
    real_import = builtins.__import__

    def block_plotly(name, *args, **kwargs):
        if name == "plotly" or name.startswith("plotly."):
            raise ImportError("plotly intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_plotly)
    try:
        wv = importlib.import_module("wiki_visualize")
        G = nx.Graph()
        G.add_node("skill:a", label="alpha", type="skill", tags=["python"])
        pos = {"skill:a": (0.0, 0.0)}

        html = wv.build_html_with_filters(G, pos, title="Knowledge Graph")

        assert "<title>Knowledge Graph</title>" in html
        assert "alpha" in html
    finally:
        sys.modules.pop("wiki_visualize", None)
        if previous is not None:
            sys.modules["wiki_visualize"] = previous


def test_title_is_html_escaped(graph_with_hostile_labels):
    import wiki_visualize as wv

    G, pos = graph_with_hostile_labels
    payload = '<img src=x onerror="window.__title_pwn=1">'
    html = wv.build_html_with_filters(G, pos, title=payload)
    assert payload not in html, "raw title payload must not appear"
    assert "&lt;img src=x onerror=" in html, "title must be HTML-escaped"
    # onerror= may appear inside safe contexts (e.g. inside escaped strings), but
    # not as a live attribute on a real <img> tag.
    assert '<img src=x onerror="window.__title_pwn=1">' not in html


def test_tag_names_are_html_escaped(graph_with_hostile_labels):
    import wiki_visualize as wv

    G, pos = graph_with_hostile_labels
    html = wv.build_html_with_filters(G, pos, title="safe")
    # Tag name payload should be escaped in both the attribute and the element text
    assert '"><img src=x onerror="window.__tagpwn=1">' not in html
    # Escaped form must appear (& becomes &amp; after escape)
    assert "&quot;&gt;&lt;img src=x onerror=" in html


def test_script_breakout_via_node_label_is_neutralized(graph_with_hostile_labels):
    import wiki_visualize as wv

    G, pos = graph_with_hostile_labels
    rendered = wv.build_html_with_filters(G, pos, title="safe")
    parser = _ScriptParser()
    parser.feed(rendered)
    parser.close()

    assert parser.start_count == 2
    assert parser.end_count == 2
    assert len(parser.scripts) == 2
    assert parser.scripts[0][0]["src"] == "https://cdn.plot.ly/plotly-2.35.2.min.js"
    assert "src" not in parser.scripts[1][0]

    inline_script = "".join(parser.scripts[1][1])
    assert "</script>" not in inline_script.lower()

    nodes_line = next(
        line for line in inline_script.splitlines() if line.startswith("const NODES = ")
    )
    assert nodes_line.endswith(";")
    nodes_json = nodes_line.removeprefix("const NODES = ").removesuffix(";")
    assert "<" not in nodes_json
    assert r"\u003c/script>" in nodes_json
    assert r"\u003c!--\u003cscript>" in nodes_json

    nodes = json.loads(nodes_json)
    nodes_by_id = {node["id"]: node for node in nodes}
    assert nodes_by_id["skill:evil"]["label"] == G.nodes["skill:evil"]["label"]
    assert nodes_by_id["skill:evil"]["tags"] == G.nodes["skill:evil"]["tags"]


def test_benign_render_still_works():
    import wiki_visualize as wv

    G = nx.Graph()
    G.add_node("skill:a", label="alpha", type="skill", tags=["python"])
    G.add_node("skill:b", label="beta", type="skill", tags=["python"])
    G.add_edge("skill:a", "skill:b", weight=2)
    pos = {"skill:a": (0.0, 0.0), "skill:b": (1.0, 1.0)}
    html = wv.build_html_with_filters(G, pos, title="Knowledge Graph")
    assert "<title>Knowledge Graph</title>" in html
    assert "alpha" in html and "beta" in html


def test_default_min_weight_preserves_fractional_semantic_edges():
    import wiki_visualize as wv

    G = nx.Graph()
    G.add_node("skill:a", label="alpha", type="skill", tags=[])
    G.add_node("skill:b", label="beta", type="skill", tags=[])
    G.add_edge("skill:a", "skill:b", weight=0.42)

    sub = wv.extract_subgraph(G, seeds=["alpha"], hops=1)

    assert sub.number_of_edges() == 1


def test_cli_seed_filter_selects_only_the_seed_neighborhood(
    cli_graph_files: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rendered = _render_cli_graph(monkeypatch, cli_graph_files, tmp_path, "--seed", "beta")

    assert '"view_nodes": 2' in rendered
    assert '"view_edges": 1' in rendered
    assert '"label": "alpha"' in rendered
    assert '"label": "beta"' in rendered
    assert '"label": "gamma"' not in rendered
    assert '"label": "delta"' not in rendered


def test_cli_zero_hops_keeps_the_matched_seed(
    cli_graph_files: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rendered = _render_cli_graph(
        monkeypatch, cli_graph_files, tmp_path, "--seed", "beta", "--hops", 0
    )

    assert '"view_nodes": 1' in rendered
    assert '"view_edges": 0' in rendered
    assert '"label": "beta"' in rendered
    assert '"label": "alpha"' not in rendered


def test_cli_tag_filter_selects_only_tagged_nodes(
    cli_graph_files: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rendered = _render_cli_graph(monkeypatch, cli_graph_files, tmp_path, "--tag", "security")

    assert '"view_nodes": 3' in rendered
    assert '"view_edges": 2' in rendered
    assert '"label": "alpha"' in rendered
    assert '"label": "beta"' in rendered
    assert '"label": "gamma"' in rendered
    assert '"label": "delta"' not in rendered


def test_cli_top_filter_keeps_the_selected_singleton(
    cli_graph_files: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rendered = _render_cli_graph(monkeypatch, cli_graph_files, tmp_path, "--top", 1)

    assert '"view_nodes": 1' in rendered
    assert '"view_edges": 0' in rendered
    assert '"label": "alpha"' in rendered
    assert '"label": "beta"' not in rendered
    assert '"label": "gamma"' not in rendered
    assert '"label": "delta"' not in rendered


def test_min_weight_blocks_seed_bfs_at_a_weak_bridge():
    import wiki_visualize as wv

    G = nx.Graph()
    for label in ("seed", "kept", "bridge", "hidden"):
        G.add_node(f"skill:{label}", label=label)
    G.add_edge("skill:seed", "skill:kept", weight=3)
    G.add_edge("skill:seed", "skill:bridge", weight=1)
    G.add_edge("skill:bridge", "skill:hidden", weight=3)

    sub = wv.extract_subgraph(G, seeds=["seed"], hops=2, min_weight=2)

    assert set(sub) == {"skill:seed", "skill:kept"}
    assert sub.number_of_edges() == 1
    assert sub.has_edge("skill:seed", "skill:kept")


@pytest.mark.parametrize("primary_filter", ["tag", "community"])
def test_min_weight_filters_edges_after_non_seed_selection(primary_filter: str):
    import wiki_visualize as wv

    G = nx.Graph()
    for label in ("alpha", "beta", "gamma"):
        G.add_node(f"skill:{label}", label=label, tags=["security"])
    G.add_edge("skill:alpha", "skill:beta", weight=3)
    G.add_edge("skill:beta", "skill:gamma", weight=1)

    if primary_filter == "tag":
        sub = wv.extract_subgraph(G, tag_filter="security", min_weight=2)
    else:
        communities = {"communities": {"7": {"members": list(G)}}}
        sub = wv.extract_subgraph(G, community_id=7, communities=communities, min_weight=2)

    assert set(sub) == {"skill:alpha", "skill:beta"}
    assert sub.number_of_edges() == 1
    assert sub.has_edge("skill:alpha", "skill:beta")


def test_tied_top_filter_is_deterministic_across_hash_seeds() -> None:
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(_SRC)!r})\n"
        "import networkx as nx\n"
        "import wiki_visualize as wv\n"
        "graph = nx.Graph()\n"
        "graph.add_edges_from([('skill:a', 'skill:b'), ('skill:b', 'skill:c'), "
        "('skill:c', 'skill:d'), ('skill:d', 'skill:a')])\n"
        "print(json.dumps(sorted(wv.extract_subgraph(graph, top_n=2).nodes())))\n"
    )
    outputs: dict[str, str] = {}
    for hash_seed in ("1", "2", "7", "42"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = hash_seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        outputs[hash_seed] = completed.stdout.strip()

    assert set(outputs.values()) == {'["skill:a", "skill:b"]'}, outputs


def test_visualizer_renders_mcp_and_harness_type_filters():
    import wiki_visualize as wv

    G = nx.Graph()
    G.add_node("mcp-server:filesystem", label="filesystem", type="mcp-server", tags=[])
    G.add_node("harness:text-to-cad", label="text-to-cad", type="harness", tags=[])
    G.add_edge("mcp-server:filesystem", "harness:text-to-cad", weight=1)
    pos = {
        "mcp-server:filesystem": (0.0, 0.0),
        "harness:text-to-cad": (1.0, 1.0),
    }

    html = wv.build_html_with_filters(G, pos)

    assert 'data-type="mcp-server"' in html
    assert 'data-type="harness"' in html
    assert '"mcp-server": "#06b6d4"' in html
    assert '"harness": "#22c55e"' in html


def test_visualizer_can_load_explicit_graph_and_communities(tmp_path):
    import wiki_visualize as wv

    graph_path = tmp_path / "graph.json"
    communities_path = tmp_path / "communities.json"
    graph_path.write_text(
        json.dumps(
            {
                "directed": False,
                "multigraph": False,
                "graph": {"export_id": "ctx-graph-test-2-1"},
                "nodes": [
                    {"id": "skill:a", "label": "alpha", "type": "skill", "tags": ["python"]},
                    {"id": "harness:b", "label": "beta", "type": "harness", "tags": ["agent"]},
                ],
                "edges": [{"source": "skill:a", "target": "harness:b", "weight": 0.9}],
            }
        ),
        encoding="utf-8",
    )
    communities_path.write_text(
        json.dumps(
            {
                "communities": {"7": {"members": ["skill:a", "harness:b"]}},
            }
        ),
        encoding="utf-8",
    )

    G = wv.load_graph(graph_path)
    communities = wv.load_communities(communities_path)

    assert G.graph["export_id"] == "ctx-graph-test-2-1"
    assert G.number_of_nodes() == 2
    assert communities["communities"]["7"]["members"] == ["skill:a", "harness:b"]


def test_visualizer_embeds_export_metadata_for_preview_freshness():
    import wiki_visualize as wv

    G = nx.Graph(export_id="ctx-graph-test-2-1")
    G.add_node("skill:a", label="alpha", type="skill", tags=["python"])
    G.add_node("harness:b", label="beta", type="harness", tags=["agent"])
    G.add_edge("skill:a", "harness:b", weight=0.9)
    pos = {"skill:a": (0.0, 0.0), "harness:b": (1.0, 1.0)}

    html = wv.build_html_with_filters(
        G,
        pos,
        title="Knowledge Graph",
        metadata={"export_id": "ctx-graph-test-2-1", "nodes": 2, "edges": 1},
    )

    assert '<meta name="ctx-graph-export-id" content="ctx-graph-test-2-1">' in html
    assert '"export_id": "ctx-graph-test-2-1"' in html
    assert '"nodes": 2' in html
    assert '"edges": 1' in html


def test_cli_default_output_writes_html_and_opens_browser(
    cli_graph_files: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    import webbrowser

    graph_path, communities_path = cli_graph_files
    opened: list[str] = []
    monkeypatch.chdir(tmp_path)

    def record_open(target: str) -> bool:
        opened.append(target)
        return True

    monkeypatch.setattr(webbrowser, "open", record_open)

    _run_main(
        monkeypatch,
        "--graph-json",
        graph_path,
        "--communities-json",
        communities_path,
        "--seed",
        "alpha",
    )

    output_path = tmp_path / "graph-view.html"
    assert output_path.is_file()
    assert opened == [str(output_path.resolve())]
    assert "Subgraph: 4 nodes, 3 edges" in capsys.readouterr().out


def test_cli_explicit_output_applies_filters_without_opening_browser(
    cli_graph_files: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import webbrowser

    graph_path, communities_path = cli_graph_files
    output_path = tmp_path / "filtered.html"
    opened: list[str] = []

    def record_open(target: str) -> bool:
        opened.append(target)
        return True

    monkeypatch.setattr(webbrowser, "open", record_open)

    _run_main(
        monkeypatch,
        "--graph-json",
        graph_path,
        "--communities-json",
        communities_path,
        "--tag",
        "security",
        "--top",
        2,
        "--min-weight",
        2,
        "--output",
        output_path,
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert opened == []
    assert '"view_nodes": 2' in rendered
    assert '"view_edges": 1' in rendered
    assert '"label": "alpha"' in rendered
    assert '"label": "beta"' in rendered
    assert '"label": "gamma"' not in rendered
    assert '"label": "delta"' not in rendered


def test_cli_community_filter_uses_explicit_assignments(
    cli_graph_files: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    graph_path, communities_path = cli_graph_files
    output_path = tmp_path / "community.html"

    _run_main(
        monkeypatch,
        "--graph-json",
        graph_path,
        "--communities-json",
        communities_path,
        "--community",
        7,
        "--output",
        output_path,
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert '"view_nodes": 2' in rendered
    assert '"view_edges": 1' in rendered
    assert "community 7" in rendered


def test_cli_stats_is_read_only(
    cli_graph_files: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    graph_path, communities_path = cli_graph_files
    monkeypatch.chdir(tmp_path)

    _run_main(
        monkeypatch,
        "--graph-json",
        graph_path,
        "--communities-json",
        communities_path,
        "--stats",
    )

    output = capsys.readouterr().out
    assert "Full graph: 4 nodes, 3 edges" in output
    assert "Skills: 1, Agents: 1" in output
    assert "Communities: 1" in output
    assert not (tmp_path / "graph-view.html").exists()


def test_cli_interactive_community_uses_supplied_assignments(
    cli_graph_files: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import wiki_visualize as wv

    graph_path, communities_path = cli_graph_files
    output_path = tmp_path / "interactive-community.html"
    answers = iter(["3", "7", str(output_path)])
    load_calls: list[Path | str | None] = []
    figures = []
    real_load_communities = wv.load_communities

    class FakeFigure:
        def __init__(self, data=None):
            self.data = list(data or [])
            self.layout = {}
            figures.append(self)

        def add_annotation(self, **kwargs) -> None:
            self.annotation = kwargs

        def update_layout(self, **kwargs) -> None:
            self.layout = kwargs

        def write_html(self, path: str, *, include_plotlyjs: bool) -> None:
            assert include_plotlyjs is True
            Path(path).write_text("rendered", encoding="utf-8")

    class FakePlotly:
        Figure = FakeFigure

        @staticmethod
        def Scatter(**kwargs):
            return kwargs

    def record_load_communities(path: Path | str | None = None) -> dict:
        load_calls.append(path)
        return real_load_communities(path)

    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))
    monkeypatch.setattr(wv, "load_communities", record_load_communities)
    monkeypatch.setattr(wv, "_load_plotly_graph_objects", lambda: FakePlotly)

    _run_main(
        monkeypatch,
        "--graph-json",
        graph_path,
        "--communities-json",
        communities_path,
    )

    assert load_calls == [communities_path]
    assert output_path.read_text(encoding="utf-8") == "rendered"
    assert len(figures) == 1
    node_traces = [trace for trace in figures[0].data if trace.get("mode") == "markers+text"]
    assert len(node_traces) == 2
    for trace in node_traces:
        assert trace["marker"]["color"] == [wv.COMMUNITY_COLORS[7]]
        assert trace["hovertext"][0].endswith("Community: 7")


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--top", "0"),
        ("--top", "-1"),
        ("--hops", "-1"),
        ("--min-weight", "-0.1"),
        ("--min-weight", "nan"),
        ("--community", "-1"),
    ],
)
def test_cli_rejects_out_of_bounds_numbers_before_prompting(
    option: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    def unexpected_prompt(_prompt: str) -> None:
        pytest.fail("invalid CLI input must not enter the interactive menu")

    monkeypatch.setattr(builtins, "input", unexpected_prompt)

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, option, value)

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "usage:" in error
    assert f"argument {option}:" in error


def test_cli_rejects_conflicting_primary_filters(
    cli_graph_files: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    graph_path, communities_path = cli_graph_files
    output_path = tmp_path / "conflict.html"

    with pytest.raises(SystemExit) as exc_info:
        _run_main(
            monkeypatch,
            "--graph-json",
            graph_path,
            "--communities-json",
            communities_path,
            "--seed",
            "alpha",
            "--tag",
            "security",
            "--output",
            output_path,
        )

    assert exc_info.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
    assert not output_path.exists()


def test_cli_no_match_exits_one_without_writing_output(
    cli_graph_files: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    graph_path, communities_path = cli_graph_files
    output_path = tmp_path / "empty.html"

    with pytest.raises(SystemExit) as exc_info:
        _run_main(
            monkeypatch,
            "--graph-json",
            graph_path,
            "--communities-json",
            communities_path,
            "--tag",
            "not-present",
            "--output",
            output_path,
        )

    assert exc_info.value.code == 1
    assert "no nodes matched" in capsys.readouterr().err.lower()
    assert not output_path.exists()


def test_interactive_plotly_error_is_concise(
    cli_graph_files: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    import wiki_visualize as wv

    graph_path, communities_path = cli_graph_files
    graph = wv.load_graph(graph_path)
    communities = wv.load_communities(communities_path)
    output_path = tmp_path / "unrendered.html"
    answers = iter(["3", "7", str(output_path)])

    def unavailable_plotly() -> object:
        raise RuntimeError('Plotly is optional. Install it with: pip install "claude-ctx[viz]"')

    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))
    monkeypatch.setattr(wv, "_load_plotly_graph_objects", unavailable_plotly)

    with pytest.raises(SystemExit) as exc_info:
        wv.interactive_menu(graph, communities=communities)

    assert exc_info.value.code == 1
    error = capsys.readouterr().err
    assert "Install it with" in error
    assert "Traceback" not in error
    assert not output_path.exists()
