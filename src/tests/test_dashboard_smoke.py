from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from networkx import Graph
from networkx.readwrite import node_link_data

import ctx_config
from ctx import dashboard_docs
from ctx.monitor import testing as mt
from ctx.monitor.services import graph as graph_service
from ctx.monitor.services import kpi as kpi_service
from ctx.monitor.services import sidecars as sidecar_service
from ctx.monitor.services import status as status_service
from ctx.monitor.services import wiki as wiki_service
from scripts import dashboard_smoke as smoke


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _urlopen_for(bodies: dict[str, str]):
    def fake_urlopen(url: str, timeout: float) -> FakeResponse:
        path = url.replace("http://127.0.0.1:8765", "")
        return FakeResponse(200, bodies[path])

    return fake_urlopen


def test_run_smoke_checks_dashboard_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = {spec.path: spec.marker for spec in smoke.DEFAULT_CHECKS}
    seen: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> FakeResponse:
        path = url.replace("http://127.0.0.1:8765", "")
        seen.append(path)
        return FakeResponse(200, bodies[path])

    times: Iterator[float] = iter(range(0, len(smoke.DEFAULT_CHECKS) * 2))
    monkeypatch.setattr(smoke.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(smoke.time, "perf_counter", lambda: next(times))

    results = smoke.run_smoke("http://127.0.0.1:8765", timeout=5)

    assert seen == [spec.path for spec in smoke.DEFAULT_CHECKS]
    assert all(result.ok for result in results)


def test_run_smoke_fails_when_marker_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = {spec.path: spec.marker for spec in smoke.DEFAULT_CHECKS}
    bodies["/manage"] = "wrong page"
    times: Iterator[float] = iter(range(0, len(smoke.DEFAULT_CHECKS) * 2))
    monkeypatch.setattr(smoke.urllib.request, "urlopen", _urlopen_for(bodies))
    monkeypatch.setattr(smoke.time, "perf_counter", lambda: next(times))

    results = smoke.run_smoke("http://127.0.0.1:8765", timeout=5)

    failed = [result for result in results if not result.ok]
    assert [result.name for result in failed] == ["manage"]
    assert failed[0].reason == "missing marker 'Manage catalog'"


def test_apply_latency_thresholds_marks_slow_warm_graph() -> None:
    results = [
        smoke.CheckResult(
            name="graph-api-warm",
            path="/api/graph/github.json?type=mcp-server&limit=20",
            status=200,
            elapsed=1.2,
            ok=True,
            reason="ok",
            bytes_read=123,
        ),
    ]

    smoke.apply_latency_thresholds(results, {"graph-api-warm": 0.5})

    assert not results[0].ok
    assert results[0].reason == "slow: 1.20s > 0.50s"


def test_emit_jsonl_outputs_structured_rows() -> None:
    result = smoke.CheckResult(
        name="home",
        path="/",
        status=200,
        elapsed=0.1,
        ok=True,
        reason="ok",
        bytes_read=50,
    )

    rows = smoke.results_to_jsonl([result]).splitlines()

    assert json.loads(rows[0]) == {
        "name": "home",
        "path": "/",
        "status": 200,
        "elapsed": 0.1,
        "ok": True,
        "reason": "ok",
        "bytes": 50,
    }


def test_main_smokes_a_real_isolated_monitor_with_warm_thresholds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claude = tmp_path / ".claude"
    graph_dir = claude / "skill-wiki" / "graphify-out"
    github_page = claude / "skill-wiki" / "entities" / "mcp-servers" / "g" / "github.md"
    sidecars = claude / "skill-quality"
    graph_dir.mkdir(parents=True)
    github_page.parent.mkdir(parents=True)
    sidecars.mkdir(parents=True)
    github_page.write_text(
        "---\ntitle: GitHub\ntype: mcp-server\ndescription: Repository tools.\n---\n"
        "# GitHub\n\nRepository tools.\n",
        encoding="utf-8",
    )
    (sidecars / "sample.json").write_text(
        json.dumps({"slug": "sample", "grade": "A", "raw_score": 0.9}),
        encoding="utf-8",
    )
    graph = Graph()
    graph.add_node(
        "mcp-server:github",
        label="GitHub",
        type="mcp-server",
        tags=["git", "repository"],
    )
    (graph_dir / "graph.json").write_text(
        json.dumps(node_link_data(graph, edges="edges")),
        encoding="utf-8",
    )

    monkeypatch.setattr(mt, "claude_dir", lambda: claude)
    monkeypatch.setattr(mt, "runtime_lifecycle_path", lambda: claude / "runtime-events.jsonl")
    monkeypatch.setattr(mt, "dashboard_graph_index_archives", lambda: [])
    monkeypatch.setattr(
        ctx_config,
        "cfg",
        SimpleNamespace(skills_dir=sidecars, agents_dir=sidecars),
    )
    monkeypatch.setattr(
        status_service,
        "_telemetry_config",
        lambda: {"path": str(claude / "telemetry" / "events.jsonl")},
    )
    graph_service.reset_caches()
    sidecar_service.reset_caches()
    kpi_service.reset_cache()
    wiki_service.reset_caches()
    dashboard_docs.reset_docs_render_cache()

    server = mt.make_monitor_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        exit_code = smoke.main(
            [
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "--timeout",
                "10",
                "--warm",
                "--jsonl",
                "--fail-on-slow",
                "graph-api-warm=5",
                "--fail-on-slow",
                "kpi-warm=5",
            ]
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert exit_code == 0
    assert len(rows) == len(smoke.DEFAULT_CHECKS) + len(smoke.WARM_CHECKS)
    assert all(row["status"] == 200 and row["ok"] for row in rows)
    assert all(row["elapsed"] >= 0 for row in rows)
    assert {row["name"] for row in rows} >= {"graph-api-cold", "graph-api-warm", "kpi-warm"}
