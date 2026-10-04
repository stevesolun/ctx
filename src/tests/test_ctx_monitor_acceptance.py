"""Focused HTTP acceptance checks for dashboard compatibility routes."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from ctx.monitor import testing as mt
from ctx.monitor.services import sidecars as sidecar_service
from ctx.monitor.services import status as status_service
from ctx.telemetry import EXPORT_STATUS_SCHEMA_VERSION, SCHEMA_VERSION


@pytest.fixture()
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    claude = tmp_path / ".claude"
    (claude / "skill-quality").mkdir(parents=True)
    monkeypatch.setattr(mt, "claude_dir", lambda: claude)
    sidecar_service.reset_caches()
    return claude


def _serve(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, threading.Thread, int]:
    monkeypatch.setattr(mt, "MONITOR_TOKEN", "acceptance-token")
    server = mt.make_monitor_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, int(server.server_port)


def _get(port: int, path: str) -> tuple[int, str, str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return (
            response.status,
            response.headers.get_content_type(),
            response.read().decode("utf-8"),
        )


def test_legacy_skill_detail_renders_all_entity_types_and_missing_sidecar(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = (
        ("skill", "acceptance-skill", "A", 0.95),
        ("agent", "acceptance-agent", "B", 0.85),
        ("mcp-server", "acceptance-mcp", "C", 0.75),
        ("harness", "acceptance-harness", "D", 0.65),
    )
    for entity_type, slug, grade, score in fixtures:
        (fake_claude / "skill-quality" / f"{slug}.json").write_text(
            json.dumps(
                {
                    "slug": slug,
                    "subject_type": entity_type,
                    "grade": grade,
                    "raw_score": score,
                }
            ),
            encoding="utf-8",
        )
    sidecar_service.reset_caches()

    server, thread, port = _serve(monkeypatch)
    try:
        for entity_type, slug, grade, score in fixtures:
            status, content_type, body = _get(
                port,
                f"/skill/{slug}?type={quote(entity_type)}",
            )
            assert status == 200
            assert content_type == "text/html"
            assert f"<h1>{slug}</h1>" in body
            assert f"grade {grade}" in body
            assert f"score <strong>{score:.3f}</strong>" in body
            assert f"type {entity_type}" in body
            assert "Audit timeline (0 entries)" in body

        status, content_type, body = _get(port, "/skill/not-installed?type=skill")
        assert status == 200
        assert content_type == "text/html"
        assert "<h1>not-installed</h1>" in body
        assert "<p>No sidecar.</p>" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_status_http_and_page_keep_published_and_local_graph_counts_distinct(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_claude
    empty_artifact = {"exists": False, "size": 0, "path": "missing"}
    artifacts: dict[str, Any] = {
        name: dict(empty_artifact)
        for name in (
            "graph_json",
            "graph_packs",
            "graph_delta_json",
            "communities_json",
            "wiki_packs",
            "pack_compaction",
            "skills_sh_catalog",
        )
    }
    artifacts.update(
        {
            "graph_store": {
                "exists": True,
                "size": 4096,
                "path": "graph-store.sqlite3",
                "fresh": True,
                "nodes": 7,
                "edges": 6,
            },
            "wiki_graph_tar": {
                "exists": True,
                "size": 8192,
                "path": "wiki-graph.tar.gz",
                "counts": {"nodes": 80_000, "edges": 1_800_000},
            },
            "published_graph_stats": {
                "counts": {"nodes": 80_000, "edges": 1_800_000},
            },
            "promotion_count": 0,
            "promotions": [],
        }
    )
    monkeypatch.setattr(
        mt,
        "status_payload",
        lambda: {
            "queue": {
                "available": False,
                "db_path": "queue.sqlite3",
                "counts": {},
                "total": 0,
                "recent_jobs": [],
            },
            "telemetry": {},
            "artifacts": artifacts,
        },
    )

    server, thread, port = _serve(monkeypatch)
    try:
        page_status, page_type, page_body = _get(port, "/status")
        api_status, api_type, api_body = _get(port, "/api/status.json")
        payload = json.loads(api_body)

        assert page_status == api_status == 200
        assert page_type == "text/html"
        assert api_type == "application/json"
        assert "published graph: 80,000 nodes, 1,800,000 edges" in page_body
        assert "local store: fresh, 7 nodes, 6 edges" in page_body
        assert payload["artifacts"]["wiki_graph_tar"]["counts"] == {
            "nodes": 80_000,
            "edges": 1_800_000,
        }
        assert payload["artifacts"]["graph_store"]["nodes"] == 7
        assert payload["artifacts"]["graph_store"]["edges"] == 6
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_status_http_projects_telemetry_health_without_event_payloads(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry_dir = fake_claude / "telemetry"
    telemetry_dir.mkdir(parents=True)
    spool = telemetry_dir / "events.jsonl"
    checkpoint = telemetry_dir / "export-checkpoint.json"
    export_status = telemetry_dir / "export-status.json"
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    raw_payload_marker = "raw-telemetry-payload-must-not-render"
    spool.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "event_id": "acceptance-event-1",
                "ts": "2026-09-30T10:00:00Z",
                "event_name": "ctx.acceptance.secret",
                "source": "acceptance-test",
                "outcome": "ok",
                "privacy_mode": "local_redacted",
                "payload": {"api_key": secret, "detail": raw_payload_marker},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint.write_text(
        json.dumps({"last_event_id": "acceptance-event-1", "private": raw_payload_marker}),
        encoding="utf-8",
    )
    export_status.write_text(
        json.dumps(
            {
                "schema_version": EXPORT_STATUS_SCHEMA_VERSION,
                "status": "healthy",
                "sink": "otlp_http",
                "attempted": 3,
                "exported": 3,
                "failed": 0,
                "checkpoint_advanced": True,
                "updated_at": "2026-09-30T10:01:00Z",
                "private": secret,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        status_service,
        "_telemetry_config",
        lambda: {
            "enabled": True,
            "mode": "local_redacted",
            "path": str(spool),
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "checkpoint_path": str(checkpoint),
                "status_path": str(export_status),
            },
        },
    )

    server, thread, port = _serve(monkeypatch)
    try:
        page_status, page_type, page_body = _get(port, "/status")
        api_status, api_type, api_body = _get(port, "/api/status.json")
        payload = json.loads(api_body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    telemetry = payload["telemetry"]
    assert page_status == api_status == 200
    assert page_type == "text/html"
    assert api_type == "application/json"
    assert telemetry["enabled"] is True
    assert telemetry["mode"] == "local_redacted"
    assert telemetry["export_enabled"] is True
    assert telemetry["export_sink"] == "otlp_http"
    assert telemetry["spool"]["event_count"] == 1
    assert telemetry["spool"]["latest_event"] == {
        "ts": "2026-09-30T10:00:00Z",
        "event_name": "ctx.acceptance.secret",
        "source": "acceptance-test",
        "outcome": "ok",
    }
    assert telemetry["checkpoint"]["exists"] is True
    assert telemetry["export_status"]["status"] == "healthy"
    assert telemetry["export_status"]["checkpoint_advanced"] is True
    assert telemetry["export_status"]["attempted"] == 3
    assert telemetry["export_status"]["exported"] == 3
    assert telemetry["export_status"]["failed"] == 0
    for expected in (
        "Telemetry health",
        "enabled",
        "mode local_redacted",
        "events: 1",
        "ctx.acceptance.secret",
        "sink otlp_http",
        "enabled: yes",
        "attempted/exported/failed: 3/3/0",
        "checkpoint: yes",
    ):
        assert expected in page_body
    for public_body in (api_body, page_body):
        assert secret not in public_body
        assert raw_payload_marker not in public_body
    assert '"payload"' not in api_body


@pytest.mark.parametrize("path", ["/logs", "/events", "/api/events.stream"])
def test_non_loopback_event_reads_require_token_before_resolving_audit_data(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    secret_event = "non-loopback-private-event"
    audit_path = fake_claude / "ctx-audit.jsonl"
    audit_path.write_text(
        json.dumps(
            {
                "ts": "2026-09-30T11:00:00Z",
                "event": "skill.loaded",
                "subject": secret_event,
                "session_id": "private-session",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path_resolutions: list[Path] = []

    def tracked_claude_dir() -> Path:
        path_resolutions.append(fake_claude)
        return fake_claude

    monkeypatch.setattr(mt, "claude_dir", tracked_claude_dir)
    monkeypatch.setattr(mt, "MONITOR_TOKEN", "acceptance-token")
    server = mt.make_monitor_server("0.0.0.0", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stream = None
    try:
        url = f"http://127.0.0.1:{server.server_port}{path}"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(url, timeout=5)
        error_body = excinfo.value.read().decode("utf-8")
        assert excinfo.value.code == 403
        assert "monitor read token required" in error_body
        assert secret_event not in error_body
        assert path_resolutions == []

        request = urllib.request.Request(
            url,
            headers={"X-CTX-Monitor-Token": "acceptance-token"},
        )
        stream = urllib.request.urlopen(request, timeout=5)
        assert stream.status == 200
        if path == "/api/events.stream":
            assert stream.headers.get_content_type() == "text/event-stream"
            assert stream.readline() == b": connected\n"
            assert stream.readline() == b"\n"
            with audit_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "ts": "2026-09-30T11:01:00Z",
                            "event": "agent.loaded",
                            "subject": secret_event,
                        }
                    )
                    + "\n"
                )
            streamed_line = stream.readline().decode("utf-8")
            assert streamed_line.startswith("data: ")
            assert secret_event in streamed_line
        else:
            authorized_body = stream.read().decode("utf-8")
            assert stream.headers.get_content_type() == "text/html"
            assert secret_event in authorized_body
        assert path_resolutions
    finally:
        if stream is not None:
            stream.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
