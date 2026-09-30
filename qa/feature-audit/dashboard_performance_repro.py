from __future__ import annotations

import contextlib
import io
import json
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import ctx_config
from ctx.monitor import testing as mt
from ctx.monitor.services import graph as graph_service
from ctx.monitor.services import graph_artifacts
from ctx.monitor.services import kpi as kpi_service
from ctx.monitor.services import sidecars as sidecar_service
from ctx.monitor.services import status as status_service
from ctx.monitor.services import wiki as wiki_service
from scripts import dashboard_smoke as smoke

REPO = Path(__file__).resolve().parents[2]
ARCHIVE = REPO / "graph" / "wiki-graph-runtime.tar.gz"


def timed_fetch(base_url: str, path: str) -> dict[str, object]:
    started = time.perf_counter()
    with urllib.request.urlopen(base_url + path, timeout=30) as response:
        payload = response.read()
        status = response.status
    return {
        "path": path,
        "status": status,
        "elapsed": round(time.perf_counter() - started, 6),
        "bytes": len(payload),
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ctx-dashboard-perf-") as raw_tmp:
        root = Path(raw_tmp) / ".claude"
        wiki = root / "skill-wiki"
        graph_dir = wiki / "graphify-out"
        graph_dir.mkdir(parents=True)
        with tarfile.open(ARCHIVE, "r:gz") as tar:
            manifest = next(
                member
                for member in tar.getmembers()
                if member.name.lstrip("./") == "graphify-out/graph-export-manifest.json"
            )
            source = tar.extractfile(manifest)
            assert source is not None
            (graph_dir / "graph-export-manifest.json").write_bytes(source.read())
            source.close()

        github_page = wiki / "entities" / "mcp-servers" / "g" / "github.md"
        github_page.parent.mkdir(parents=True)
        github_page.write_text(
            "---\ntitle: GitHub\ntype: mcp-server\n---\n# GitHub\n",
            encoding="utf-8",
        )
        sidecars = root / "skill-quality"
        sidecars.mkdir(parents=True)
        sidecar_count = 10_000
        for index in range(sidecar_count):
            (sidecars / f"skill-{index:05d}.json").write_text(
                json.dumps(
                    {
                        "slug": f"skill-{index:05d}",
                        "subject_type": "skill",
                        "grade": "ABCDE"[index % 5],
                        "raw_score": (index % 100) / 100,
                        "category": "synthetic-bounded-kpi",
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )

        mt.claude_dir = lambda: root
        mt.runtime_lifecycle_path = lambda: root / "runtime-events.jsonl"
        mt.dashboard_graph_index_archives = lambda: [ARCHIVE]
        mt.packaged_graph_export_id = lambda: graph_service.archive_graph_export_id(ARCHIVE)
        ctx_config.cfg = SimpleNamespace(skills_dir=sidecars, agents_dir=sidecars)
        status_service._telemetry_config = lambda: {
            "path": str(root / "telemetry" / "events.jsonl")
        }
        graph_service.reset_caches()
        graph_artifacts.reset_caches()
        sidecar_service.reset_caches()
        kpi_service.reset_cache()
        wiki_service.reset_caches()

        server = mt.make_monitor_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            cold_graph = timed_fetch(
                base_url,
                "/api/graph/github.json?type=mcp-server&limit=20",
            )
            warm_graph = timed_fetch(
                base_url,
                "/api/graph/github.json?type=mcp-server&limit=20",
            )
            cold_kpi = timed_fetch(base_url, "/api/kpi.json")
            warm_kpi = timed_fetch(base_url, "/api/kpi.json")
            warm_kpi_page = timed_fetch(base_url, "/kpi")
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture):
                smoke_exit = smoke.main(
                    [
                        "--base-url",
                        base_url,
                        "--timeout",
                        "10",
                        "--warm",
                        "--jsonl",
                        "--fail-on-slow",
                        "graph-api-warm=1.0",
                        "--fail-on-slow",
                        "graph-warm=1.0",
                        "--fail-on-slow",
                        "wiki-detail-warm=1.0",
                        "--fail-on-slow",
                        "kpi-warm=3.0",
                    ]
                )
            smoke_rows = [json.loads(line) for line in capture.getvalue().splitlines()]
            result = {
                "fixture": {
                    "graph_archive_bytes": ARCHIVE.stat().st_size,
                    "graph_declared_nodes": 79_958,
                    "graph_declared_edges": 1_778_069,
                    "kpi_sidecars": sidecar_count,
                    "kpi_source": "synthetic bounded JSON sidecars; not full-catalog latency",
                },
                "thresholds_seconds": {
                    "graph_first_extraction": 5.0,
                    "graph_warm": 1.0,
                    "kpi_api_bounded_cold": 3.0,
                    "kpi_page_and_api_warm": 3.0,
                },
                "measurements": {
                    "graph_cold_first_request": cold_graph,
                    "graph_warm_request": warm_graph,
                    "kpi_api_cold": cold_kpi,
                    "kpi_api_warm": warm_kpi,
                    "kpi_page_warm": warm_kpi_page,
                    "extracted_index_bytes": (graph_dir / "dashboard-neighborhoods.sqlite3")
                    .stat()
                    .st_size,
                },
                "smoke": {
                    "exit_code": smoke_exit,
                    "checks": len(smoke_rows),
                    "failed": [row for row in smoke_rows if not row["ok"]],
                },
            }
            print(json.dumps(result, sort_keys=True))
            return 0 if smoke_exit == 0 else 1
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
