from __future__ import annotations

from dataclasses import asdict
from email.message import Message
from io import BytesIO
import json
import stat
from pathlib import Path
from typing import Any

import pytest

import ctx.api as ctx_api
from ctx.cli import telemetry as telemetry_cli
import ctx.telemetry as telemetry
from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox
from ctx.mcp_server import server as mcp_server
from ctx.telemetry import (
    EXPORT_STATUS_SCHEMA_VERSION,
    METRIC_SCHEMA_VERSION,
    RETENTION_STATUS_SCHEMA_VERSION,
    SCHEMA_VERSION,
    TelemetryEvent,
    TelemetryMetric,
    enforce_telemetry_retention,
    exception_payload,
    export_events,
    export_metrics,
    export_traces,
    hash_identifier,
    plan_telemetry_retention,
    preview_traces_export,
    read_events,
    read_metrics,
    record_counter,
    record_event,
    record_exception,
    record_histogram,
)


def _redirect_real_event_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
) -> None:
    config = {"path": str(path), "export": {"enabled": False}}

    def config_get(key: str, default: Any) -> Any:
        return config if key == "telemetry" else default

    monkeypatch.setattr(telemetry, "_config_get", config_get)
    monkeypatch.setattr(telemetry, "record_event", record_event)


def test_record_event_writes_local_redacted_envelope(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    event = record_event(
        "recommendation.returned",
        source="ctx-core",
        session_id="sess-1",
        transport="python-api",
        actor="cli",
        duration_ms=12.5,
        repo="/Users/example/private-repo",
        cwd="/Users/example/private-repo/service",
        payload={
            "query": "debug failing checkout for customer acme",
            "repo_path": "/Users/example/private-repo",
            "result_count": 2,
            "token": "sk-secret-token-value",
            "ranked": [{"slug": "python-patterns", "score": 0.91}],
        },
        path=path,
        trusted_root=tmp_path,
        config={"mode": "local_redacted", "path": str(path)},
    )

    assert event is not None
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == SCHEMA_VERSION
    assert raw["event_name"] == "recommendation.returned"
    assert raw["source"] == "ctx-core"
    assert raw["session_id"] == "sess-1"
    assert raw["session_hash"].startswith("sha256:")
    assert len(raw["trace_id"]) == 32
    assert len(raw["span_id"]) == 16
    assert raw["ctx_version"]
    assert raw["privacy_mode"] == "local_redacted"
    assert raw["repo_hash"].startswith("sha256:")
    assert raw["cwd_hash"].startswith("sha256:")
    assert raw["payload"]["result_count"] == 2
    assert raw["payload"]["token"] == "[redacted]"
    assert "query" not in raw["payload"]
    assert raw["payload"]["query_hash"].startswith("sha256:")
    assert "repo_path" not in raw["payload"]
    assert raw["payload"]["repo_path_hash"].startswith("sha256:")
    assert "/Users/example/private-repo" not in path.read_text(encoding="utf-8")

    got = list(read_events(path, trusted_root=tmp_path))
    assert len(got) == 1
    assert got[0].event_id == event.event_id
    assert got[0].session_hash == event.session_hash
    assert got[0].trace_id == event.trace_id
    assert got[0].span_id == event.span_id
    assert got[0].ctx_version == event.ctx_version


def test_sanitize_payload_hashes_common_path_key_shapes() -> None:
    payload = telemetry.sanitize_payload(
        {
            "paths": ["/Users/example/private-repo/a.py"],
            "ctx.repo.path": "/Users/example/private-repo",
            "file.paths": ["/Users/example/private-repo/b.py"],
            "repo_name": "private-repo",
            "repository": "steves/private-repo",
            "ctx.repo.name": "private-repo",
            "ctx.repository": "steves/private-repo",
            "workspace": "/Users/example/private-repo",
            "workspace.root": "/Users/example/private-repo",
            "workspace_url": "https://example.test/steves/private-repo",
            "ctx.workspace.url": "https://example.test/steves/private-repo",
            "project": "/Users/example/private-repo/service",
            "project-dir": "/Users/example/private-repo/service",
            "project_url": "https://example.test/steves/private-repo/service",
            "ctx.project.url": "https://example.test/steves/private-repo/service",
            "context": "Look in /Users/example/private-repo for acme",
            "safe": "kept",
        },
        config={"mode": "local_redacted", "privacy": {"hash_salt": "test-salt"}},
    )

    assert payload["paths_hash"].startswith("sha256:")
    assert payload["ctx.repo.path_hash"].startswith("sha256:")
    assert payload["file.paths_hash"].startswith("sha256:")
    assert payload["repo_name_hash"].startswith("sha256:")
    assert payload["repository_hash"].startswith("sha256:")
    assert payload["ctx.repo.name_hash"].startswith("sha256:")
    assert payload["ctx.repository_hash"].startswith("sha256:")
    assert payload["workspace_hash"].startswith("sha256:")
    assert payload["workspace.root_hash"].startswith("sha256:")
    assert payload["workspace_url_hash"].startswith("sha256:")
    assert payload["ctx.workspace.url_hash"].startswith("sha256:")
    assert payload["project_hash"].startswith("sha256:")
    assert payload["project-dir_hash"].startswith("sha256:")
    assert payload["project_url_hash"].startswith("sha256:")
    assert payload["ctx.project.url_hash"].startswith("sha256:")
    assert payload["context_hash"].startswith("sha256:")
    assert payload["safe"] == "kept"
    for raw_key in (
        "paths",
        "ctx.repo.path",
        "file.paths",
        "repo_name",
        "repository",
        "ctx.repo.name",
        "ctx.repository",
        "workspace",
        "workspace.root",
        "workspace_url",
        "ctx.workspace.url",
        "project",
        "project-dir",
        "project_url",
        "ctx.project.url",
        "context",
    ):
        assert raw_key not in payload
    payload_json = json.dumps(payload)
    assert "/Users/example/private-repo" not in payload_json
    assert "steves/private-repo" not in payload_json
    assert "private-repo" not in payload_json
    assert "acme" not in payload_json


def test_local_redacted_payload_strings_hash_host_paths(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    export_path = tmp_path / "exported-events.jsonl"

    event = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        payload={
            "safe_note": (
                "opened /Users/example/private-repo/app.py and "
                r"C:\Users\example\private-repo\app.py"
            ),
            "result_count": 1,
        },
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )

    assert event is not None
    raw = json.loads(path.read_text(encoding="utf-8"))
    safe_note = raw["payload"]["safe_note"]
    assert safe_note.startswith("opened [path_hash:sha256:")
    assert safe_note.count("[path_hash:sha256:") == 2
    raw_text = path.read_text(encoding="utf-8")
    assert "/Users/example/private-repo" not in raw_text
    assert "private-repo" not in raw_text

    result = export_events(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "export": {
                "enabled": True,
                "sink": "local_jsonl",
                "path": str(export_path),
            },
        },
    )

    assert result.exported == 1
    exported_text = export_path.read_text(encoding="utf-8")
    assert "/Users/example/private-repo" not in exported_text
    assert "private-repo" not in exported_text
    assert exported_text.count("[path_hash:sha256:") == 2


def test_local_redacted_nested_payload_strings_hash_host_paths() -> None:
    payload = telemetry.sanitize_payload(
        {
            "safe_note": "see /home/alice/private-repo/app.py",
            "nested": {
                "items": [
                    r"C:\Users\alice\private-repo\tool.py",
                    "~/workspace/private-repo/readme.md",
                ]
            },
        },
        config={"mode": "local_redacted", "privacy": {"hash_salt": "tenant-a"}},
    )

    payload_text = json.dumps(payload)
    assert payload_text.count("[path_hash:sha256:") == 3
    assert "/home/alice/private-repo" not in payload_text
    assert "private-repo" not in payload_text


def test_telemetry_span_propagates_trace_to_nested_events(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    with telemetry.telemetry_span():
        parent = record_event(
            "ctx.api.recommend_bundle",
            source="ctx-api",
            path=path,
            trusted_root=tmp_path,
            config={"path": str(path), "export": {"enabled": False}},
        )
        with telemetry.telemetry_span():
            child = record_event(
                "ctx.core.recommend_bundle",
                source="ctx-core",
                path=path,
                trusted_root=tmp_path,
                config={"path": str(path), "export": {"enabled": False}},
            )

    assert parent is not None
    assert child is not None
    assert parent.trace_id == child.trace_id
    assert parent.span_id != child.span_id
    assert parent.parent_span_id is None
    assert child.parent_span_id == parent.span_id


def test_record_event_explicit_trace_ids_override_active_span(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    with telemetry.telemetry_span():
        event = record_event(
            "ctx.api.recommend_bundle",
            source="ctx-api",
            trace_id="1" * 32,
            span_id="2" * 16,
            parent_span_id="3" * 16,
            path=path,
            trusted_root=tmp_path,
            config={"path": str(path), "export": {"enabled": False}},
        )

    assert event is not None
    assert event.trace_id == "1" * 32
    assert event.span_id == "2" * 16
    assert event.parent_span_id == "3" * 16


def test_record_metrics_writes_local_redacted_spool(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    config = {
        "metrics": {
            "enabled": True,
            "path": str(path),
            "export": {"enabled": False},
        },
        "privacy": {"hash_salt": "tenant-a"},
    }

    with telemetry.telemetry_span():
        counter = record_counter(
            "ctx.api.requests",
            value=2,
            attributes={"query": "private acme query", "ctx.operation": "recommend"},
            source="ctx-api",
            session_id="sess-private",
            path=path,
            trusted_root=tmp_path,
            config=config,
        )
        histogram = record_histogram(
            "ctx.api.duration",
            value=42.5,
            unit="ms",
            attributes={"path": "/Users/example/private-repo", "ctx.operation": "recommend"},
            source="ctx-api",
            session_id="sess-private",
            path=path,
            trusted_root=tmp_path,
            config=config,
        )

    assert counter is not None
    assert histogram is not None
    assert counter.schema_version == METRIC_SCHEMA_VERSION
    assert counter.instrument == "counter"
    assert histogram.instrument == "histogram"
    assert counter.trace_id == histogram.trace_id
    assert counter.session_hash == histogram.session_hash
    assert counter.session_hash is not None
    assert counter.session_hash.startswith("sha256:")
    raw = path.read_text(encoding="utf-8")
    assert "private acme query" not in raw
    assert "/Users/example/private-repo" not in raw
    assert "sess-private" not in raw
    assert "query_hash" in raw
    assert "path_hash" in raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    metrics = list(read_metrics(path, trusted_root=tmp_path))
    assert [metric.name for metric in metrics] == [
        "ctx.api.requests",
        "ctx.api.duration",
    ]


def test_metrics_disabled_unless_metrics_config_present(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"

    missing = record_counter(
        "ctx.api.requests",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(tmp_path / "events.jsonl")},
    )
    disabled = record_counter(
        "ctx.api.requests",
        path=path,
        trusted_root=tmp_path,
        config={"metrics": {"enabled": False, "path": str(path)}},
    )

    assert missing is None
    assert disabled is None
    assert not path.exists()


def test_export_metrics_posts_otlp_resource_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "metrics.jsonl"
    config = {
        "metrics": {
            "enabled": True,
            "path": str(path),
            "export": {"enabled": False},
        },
        "privacy": {"hash_salt": "tenant-a"},
    }
    counter = record_counter(
        "ctx.api.requests",
        value=3,
        attributes={"query": "private acme query"},
        source="ctx-api",
        session_id="sess-raw-private",
        path=path,
        trusted_root=tmp_path,
        config=config,
    )
    histogram = record_histogram(
        "ctx.api.duration",
        value=42,
        attributes={"ctx.operation": "recommend"},
        source="ctx-api",
        session_id="sess-raw-private",
        path=path,
        trusted_root=tmp_path,
        config=config,
    )
    assert counter is not None
    assert histogram is not None
    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append((payload, settings))

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)

    result = export_metrics(
        path,
        trusted_root=tmp_path,
        config={
            "metrics": {
                "enabled": True,
                "path": str(path),
                "export": {
                    "enabled": True,
                    "sink": "otlp_http",
                    "otlp": {
                        "endpoint": "https://collector.example:4318/v1/metrics",
                        "allowed_hosts": ["collector.example"],
                        "service_name": "ctx-test",
                    },
                },
            },
            "privacy": {"hash_salt": "tenant-a"},
        },
    )

    assert result.exported == 2
    assert result.failed == 0
    assert result.status == "ok"
    assert result.checkpoint_advanced is True
    assert len(calls) == 1
    payload, settings = calls[0]
    assert settings["otlp_endpoint"] == "https://collector.example:4318/v1/metrics"
    metric_records = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    by_name = {record["name"]: record for record in metric_records}
    assert by_name["ctx.api.requests"]["sum"]["aggregationTemporality"] == 1
    assert by_name["ctx.api.requests"]["sum"]["isMonotonic"] is True
    assert by_name["ctx.api.requests"]["sum"]["dataPoints"][0]["asInt"] == "3"
    histogram_point = by_name["ctx.api.duration"]["histogram"]["dataPoints"][0]
    assert by_name["ctx.api.duration"]["histogram"]["aggregationTemporality"] == 1
    assert histogram_point["count"] == "1"
    assert histogram_point["sum"] == 42.0
    assert histogram_point["min"] == 42.0
    assert histogram_point["max"] == 42.0
    assert sum(int(count) for count in histogram_point["bucketCounts"]) == 1
    text = json.dumps(payload)
    assert "private acme query" not in text
    assert "sess-raw-private" not in text
    assert "ctx.session.hash" in text
    assert "ctx.metric.query_hash" in text


def test_export_metrics_resanitizes_legacy_attributes_for_otlp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "metrics.jsonl"
    legacy_metric = TelemetryMetric(
        schema_version=METRIC_SCHEMA_VERSION,
        metric_id="legacy-raw-metric",
        ts="2026-06-28T00:00:00Z",
        name="ctx.api.requests",
        instrument="counter",
        value=1,
        source="ctx-api",
        privacy_mode="local_redacted",
        attributes={
            "query": "private acme query",
            "safe_note": "opened /Users/example/private-repo/app.py",
        },
    )
    path.write_text(
        json.dumps(asdict(legacy_metric), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append(payload)

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)

    result = export_metrics(
        path,
        trusted_root=tmp_path,
        config={
            "metrics": {
                "enabled": True,
                "path": str(path),
                "export": {
                    "enabled": True,
                    "sink": "otlp_http",
                    "otlp": {
                        "endpoint": "https://collector.example:4318/v1/metrics",
                        "allowed_hosts": ["collector.example"],
                    },
                },
            },
            "privacy": {"hash_salt": "tenant-a"},
        },
    )

    assert result.exported == 1
    assert result.failed == 0
    payload_text = json.dumps(calls[0])
    assert "private acme query" not in payload_text
    assert "/Users/example/private-repo" not in payload_text
    assert "private-repo" not in payload_text
    assert "ctx.metric.query_hash" in payload_text
    assert "ctx.metric.safe_note" in payload_text
    assert "[path_hash:sha256:" in payload_text


def test_metrics_export_checkpoint_is_independent_from_event_checkpoint(
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "events.jsonl"
    metric_path = tmp_path / "metrics.jsonl"
    event_export_path = tmp_path / "exported-events.jsonl"
    metric_export_path = tmp_path / "exported-metrics.jsonl"
    event = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        path=event_path,
        trusted_root=tmp_path,
        config={"path": str(event_path), "export": {"enabled": False}},
    )
    metric = record_counter(
        "ctx.api.requests",
        path=metric_path,
        trusted_root=tmp_path,
        config={
            "metrics": {
                "enabled": True,
                "path": str(metric_path),
                "export": {"enabled": False},
            },
        },
    )
    assert event is not None
    assert metric is not None

    event_result = export_events(
        event_path,
        trusted_root=tmp_path,
        config={
            "path": str(event_path),
            "export": {
                "enabled": True,
                "sink": "local_jsonl",
                "path": str(event_export_path),
            },
        },
    )
    metric_result = export_metrics(
        metric_path,
        trusted_root=tmp_path,
        config={
            "metrics": {
                "enabled": True,
                "path": str(metric_path),
                "export": {
                    "enabled": True,
                    "sink": "local_jsonl",
                    "path": str(metric_export_path),
                },
            },
        },
    )

    assert event_result.checkpoint_path == str(event_path) + ".export-checkpoint.json"
    assert metric_result.checkpoint_path == str(metric_path) + ".export-checkpoint.json"
    event_checkpoint = json.loads(Path(event_result.checkpoint_path).read_text(encoding="utf-8"))
    metric_checkpoint = json.loads(Path(metric_result.checkpoint_path).read_text(encoding="utf-8"))
    assert event_checkpoint["last_event_id"] == event.event_id
    assert "last_metric_id" not in event_checkpoint
    assert metric_checkpoint["last_metric_id"] == metric.metric_id
    assert "last_event_id" not in metric_checkpoint


def test_export_metrics_degraded_on_malformed_pending_records(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "metrics.jsonl"
    export_path = tmp_path / "exported-metrics.jsonl"
    metric = record_counter(
        "ctx.api.requests",
        path=path,
        trusted_root=tmp_path,
        config={
            "metrics": {
                "enabled": True,
                "path": str(path),
                "export": {"enabled": False},
            },
        },
    )
    assert metric is not None
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json}\n")

    result = export_metrics(
        path,
        trusted_root=tmp_path,
        config={
            "metrics": {
                "enabled": True,
                "path": str(path),
                "export": {
                    "enabled": True,
                    "sink": "local_jsonl",
                    "path": str(export_path),
                },
            },
        },
    )

    assert result.attempted == 1
    assert result.exported == 1
    assert result.failed == 0
    assert result.status == "degraded"
    assert result.malformed_records == 1
    assert result.malformed_pending_records == 1
    assert result.checkpoint_advanced is False
    assert not Path(str(path) + ".export-checkpoint.json").exists()
    status = json.loads(Path(str(path) + ".export-status.json").read_text(encoding="utf-8"))
    assert status["status"] == "degraded"
    assert status["malformed_pending_records"] == 1
    assert status["checkpoint_advanced"] is False
    assert "skipping malformed metric" in capsys.readouterr().err


def test_api_core_events_share_trace_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctx.adapters.generic.ctx_core_tools as core_tools

    spans: dict[str, telemetry.TelemetrySpan] = {}

    def capture_api_event(*args: Any, **kwargs: Any) -> None:
        span = telemetry.current_telemetry_span()
        assert span is not None
        spans["api"] = span

    def capture_core_event(*args: Any, **kwargs: Any) -> None:
        span = telemetry.current_telemetry_span()
        assert span is not None
        spans["core"] = span

    monkeypatch.setattr(ctx_api, "_record_api_event", capture_api_event)
    monkeypatch.setattr(core_tools, "_record_core_tool_event", capture_core_event)
    monkeypatch.setattr(
        ctx_api,
        "_get_toolbox",
        lambda: CtxCoreToolbox(wiki_dir=tmp_path / "wiki", graph_path=tmp_path / "graph.json"),
    )

    with pytest.raises(ValueError, match="unknown ctx-core tool"):
        ctx_api._call("ctx__missing", {})

    assert set(spans) == {"api", "core"}
    assert spans["core"].trace_id == spans["api"].trace_id
    assert spans["core"].span_id != spans["api"].span_id
    assert spans["core"].parent_span_id == spans["api"].span_id
    assert spans["api"].parent_span_id is None


def test_mcp_core_events_share_trace_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctx.adapters.generic.ctx_core_tools as core_tools

    spans: dict[str, telemetry.TelemetrySpan] = {}

    def capture_mcp_event(*args: Any, **kwargs: Any) -> None:
        span = telemetry.current_telemetry_span()
        assert span is not None
        spans["mcp"] = span

    def capture_core_event(*args: Any, **kwargs: Any) -> None:
        span = telemetry.current_telemetry_span()
        assert span is not None
        spans["core"] = span

    monkeypatch.setattr(mcp_server, "_record_mcp_request", capture_mcp_event)
    monkeypatch.setattr(core_tools, "_record_core_tool_event", capture_core_event)
    out = BytesIO()
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "ctx__missing", "arguments": {}},
    }

    mcp_server._process_line(json.dumps(frame), mcp_server._ServerState(), out)

    response = json.loads(out.getvalue().decode("utf-8"))
    assert response["result"]["isError"] is True
    assert set(spans) == {"core", "mcp"}
    assert spans["core"].trace_id == spans["mcp"].trace_id
    assert spans["core"].span_id != spans["mcp"].span_id
    assert spans["core"].parent_span_id == spans["mcp"].span_id
    assert spans["mcp"].parent_span_id is None


def test_mcp_request_traceparent_parents_server_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctx.adapters.generic.ctx_core_tools as core_tools

    trace_id = "1" * 32
    client_span_id = "2" * 16
    session_hash = "sha256:" + "3" * 64
    spans: dict[str, telemetry.TelemetrySpan] = {}
    payloads: list[dict[str, Any]] = []

    def capture_mcp_event(*args: Any, **kwargs: Any) -> None:
        span = telemetry.current_telemetry_span()
        assert span is not None
        spans["mcp"] = span
        payloads.append(dict(kwargs["payload"]))

    def capture_core_event(*args: Any, **kwargs: Any) -> None:
        span = telemetry.current_telemetry_span()
        assert span is not None
        spans["core"] = span

    monkeypatch.setattr(mcp_server, "_record_mcp_request", capture_mcp_event)
    monkeypatch.setattr(core_tools, "_record_core_tool_event", capture_core_event)
    out = BytesIO()
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "ctx__missing",
            "arguments": {},
            "_meta": {
                "traceparent": f"00-{trace_id}-{client_span_id}-01",
                "ctx.session.hash": session_hash,
            },
        },
    }

    mcp_server._process_line(json.dumps(frame), mcp_server._ServerState(), out)

    response = json.loads(out.getvalue().decode("utf-8"))
    assert response["result"]["isError"] is True
    assert set(spans) == {"core", "mcp"}
    assert spans["mcp"].trace_id == trace_id
    assert spans["mcp"].parent_span_id == client_span_id
    assert spans["mcp"].span_id != client_span_id
    assert spans["core"].trace_id == trace_id
    assert spans["core"].parent_span_id == spans["mcp"].span_id
    assert payloads[0]["ctx.traceparent.received"] is True
    assert payloads[0]["ctx.session.hash"] == session_hash


def test_record_event_returns_none_when_disabled(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    event = record_event(
        "session.started",
        source="ctx-run",
        path=path,
        trusted_root=tmp_path,
        config={"enabled": False, "path": str(path)},
    )

    assert event is None
    assert not path.exists()


def test_record_event_fails_closed_for_unknown_privacy_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "events.jsonl"

    event = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        payload={"query": "private acme query"},
        path=path,
        trusted_root=tmp_path,
        config={"mode": "debug_raw", "path": str(path)},
    )

    assert event is None
    assert not path.exists()
    assert "telemetry.mode must be one of" in capsys.readouterr().err


def test_record_event_can_export_to_local_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    export_path = tmp_path / "exported-events.jsonl"

    event = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        payload={"query": "private acme query", "ctx.result.count": 1},
        path=path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "export": {
                "enabled": True,
                "sink": "local_jsonl",
                "path": str(export_path),
            },
        },
    )

    assert event is not None
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported["event_id"] == event.event_id
    assert exported["event_name"] == "ctx.api.recommend_bundle"
    assert "query" not in exported["payload"]
    assert exported["payload"]["query_hash"].startswith("sha256:")


def test_record_event_creates_owner_only_local_files(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    export_path = tmp_path / "exported-events.jsonl"

    record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        path=path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "export": {
                "enabled": True,
                "sink": "local_jsonl",
                "path": str(export_path),
            },
        },
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(export_path.stat().st_mode) == 0o600
    checkpoint_path = Path(str(path) + ".export-checkpoint.json")
    assert stat.S_IMODE(checkpoint_path.stat().st_mode) == 0o600
    status_path = Path(str(path) + ".export-status.json")
    assert stat.S_IMODE(status_path.stat().st_mode) == 0o600


def test_telemetry_export_cli_rejects_unknown_privacy_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        telemetry_cli,
        "_base_telemetry_config",
        lambda: {"mode": "debug_raw", "export": {"enabled": True, "sink": "local_jsonl"}},
    )

    rc = telemetry_cli.main(["--dry-run", "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 1
    assert "telemetry.mode must be one of" in payload["error"]


def test_export_events_posts_otlp_http_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    record_event(
        "ctx.mcp.request",
        source="ctx-mcp-server",
        outcome="error",
        session_id="sess-otlp-private",
        error_kind="method_not_found",
        payload={"rpc.method": "tools/call", "query": "private acme query"},
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append((payload, settings))

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)

    result = export_events(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/logs",
                    "allowed_hosts": ["collector.example"],
                    "headers": {"Authorization": "Bearer token"},
                    "service_name": "ctx-test",
                    "service_namespace": "ctx",
                    "deployment_environment": "test",
                },
            },
        },
    )

    assert result.exported == 1
    assert result.failed == 0
    assert result.sink == "otlp_http"
    assert len(calls) == 1
    payload, settings = calls[0]
    assert settings["otlp_endpoint"] == "https://collector.example:4318/v1/logs"
    assert settings["otlp_allowed_hosts"] == ["collector.example"]
    resource_logs = payload["resourceLogs"]
    assert isinstance(resource_logs, list)
    log_record = resource_logs[0]["scopeLogs"][0]["logRecords"][0]
    assert log_record["body"] == {"stringValue": "ctx.mcp.request"}
    assert len(log_record["traceId"]) == 32
    assert len(log_record["spanId"]) == 16
    attributes = {item["key"]: item["value"] for item in log_record["attributes"]}
    assert attributes["event.name"] == {"stringValue": "ctx.mcp.request"}
    assert attributes["ctx.outcome"] == {"stringValue": "error"}
    assert attributes["error.type"] == {"stringValue": "method_not_found"}
    assert "ctx.session_id" not in attributes
    assert attributes["ctx.session.hash"]["stringValue"].startswith("sha256:")
    assert attributes["ctx.version"]["stringValue"]
    assert "ctx.payload.query_hash" in attributes
    assert "private acme query" not in json.dumps(payload)
    assert "sess-otlp-private" not in json.dumps(payload)


def test_export_traces_posts_otlp_resource_spans_and_redacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    with telemetry.telemetry_span(
        trace_id="1" * 32,
        span_id="2" * 16,
        parent_span_id="3" * 16,
    ):
        first = record_event(
            "ctx.api.recommend_bundle",
            source="ctx-api",
            session_id="sess-trace-private",
            duration_ms=12.5,
            payload={
                "query": "private acme query",
                "safe_note": "opened /Users/example/private-repo/app.py",
            },
            path=path,
            trusted_root=tmp_path,
            config={"path": str(path), "export": {"enabled": False}},
        )
        second = record_event(
            "ctx.core.recommend_bundle",
            source="ctx-core",
            outcome="error",
            error_kind="lookup_failed",
            payload={"token": "ghp_private_trace_token"},
            path=path,
            trusted_root=tmp_path,
            config={"path": str(path), "export": {"enabled": False}},
        )
    assert first is not None
    assert second is not None
    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append((payload, settings))

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)
    result = export_traces(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "privacy": {"hash_salt": "tenant-a"},
            "traces": {
                "enabled": True,
                "export": {
                    "enabled": True,
                    "span_maturity_seconds": 0,
                    "sink": "otlp_http",
                    "otlp": {
                        "endpoint": "https://collector.example:4318/v1/traces",
                        "allowed_hosts": ["collector.example"],
                        "headers": {"Authorization": "Bearer trace-token"},
                        "service_name": "ctx-test",
                        "service_namespace": "ctx",
                        "deployment_environment": "test",
                    },
                },
            },
        },
    )

    assert result.attempted == 1
    assert result.exported == 1
    assert result.failed == 0
    assert result.status == "ok"
    assert result.checkpoint_path == str(path) + ".trace-export-checkpoint.json"
    assert result.status_path == str(path) + ".trace-export-status.json"
    assert len(calls) == 1
    payload, settings = calls[0]
    assert settings["otlp_endpoint"] == "https://collector.example:4318/v1/traces"
    assert settings["otlp_headers"] == {"Authorization": "Bearer trace-token"}
    resource_spans = payload["resourceSpans"]
    assert len(resource_spans) == 1
    resource_attributes = {
        item["key"]: item["value"] for item in resource_spans[0]["resource"]["attributes"]
    }
    assert resource_attributes["service.name"] == {"stringValue": "ctx-test"}
    scope_spans = resource_spans[0]["scopeSpans"]
    assert scope_spans[0]["scope"]["name"] == "ctx.telemetry"
    spans = scope_spans[0]["spans"]
    assert len(spans) == 1
    span = spans[0]
    assert span["traceId"] == "1" * 32
    assert span["spanId"] == "2" * 16
    assert span["parentSpanId"] == "3" * 16
    assert span["name"] == "ctx.api.recommend_bundle"
    assert span["kind"] == 1
    assert int(span["startTimeUnixNano"]) <= int(span["endTimeUnixNano"])
    assert span["status"] == {"code": 2, "message": "lookup_failed"}
    assert [event["name"] for event in span["events"]] == [
        "ctx.api.recommend_bundle",
        "ctx.core.recommend_bundle",
    ]
    attributes = {item["key"]: item["value"] for item in span["attributes"]}
    assert attributes["ctx.trace.event_count"] == {"intValue": "2"}
    assert attributes["ctx.session.hash"]["stringValue"].startswith("sha256:")
    assert "ctx.payload.query_hash" in attributes
    payload_text = json.dumps(payload)
    assert "sess-trace-private" not in payload_text
    assert "private acme query" not in payload_text
    assert "/Users/example/private-repo" not in payload_text
    assert "private-repo" not in payload_text
    assert "ghp_private_trace_token" not in payload_text


def test_trace_export_preview_retry_and_checkpoint_is_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    log_export_path = tmp_path / "exported-events.jsonl"
    event = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    assert event is not None
    trace_config = {
        "path": str(path),
        "traces": {
            "enabled": True,
            "export": {
                "enabled": True,
                "span_maturity_seconds": 0,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/traces",
                    "allowed_hosts": ["collector.example"],
                },
            },
        },
    }
    checkpoint_path = Path(str(path) + ".trace-export-checkpoint.json")
    status_path = Path(str(path) + ".trace-export-status.json")

    preview = preview_traces_export(path, trusted_root=tmp_path, config=trace_config)
    assert preview.attempted == 1
    assert preview.exported == 0
    assert preview.status == "ok"
    assert not checkpoint_path.exists()
    assert not status_path.exists()

    attempts = 0

    def flaky_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("collector unavailable")

    monkeypatch.setattr(telemetry, "_post_otlp_http", flaky_post_otlp_http)
    failed = export_traces(path, trusted_root=tmp_path, config=trace_config)
    assert failed.failed == 1
    assert failed.status == "failed"
    assert not checkpoint_path.exists()
    assert json.loads(status_path.read_text(encoding="utf-8"))["signal"] == "traces"

    exported = export_traces(path, trusted_root=tmp_path, config=trace_config)
    assert exported.exported == 1
    assert exported.checkpoint_advanced is True
    assert checkpoint_path.is_file()
    assert not Path(str(path) + ".export-checkpoint.json").exists()

    repeated = export_traces(path, trusted_root=tmp_path, config=trace_config)
    assert repeated.attempted == 0
    assert repeated.status == "noop"
    log_result = export_events(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "export": {
                "enabled": True,
                "sink": "local_jsonl",
                "path": str(log_export_path),
            },
        },
    )
    assert log_result.exported == 1
    assert Path(str(path) + ".export-checkpoint.json").is_file()


def test_export_traces_resanitizes_legacy_payload_and_hashes_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    legacy_event = TelemetryEvent(
        schema_version=SCHEMA_VERSION,
        event_id="legacy-trace-event",
        ts="2026-06-28T00:00:00Z",
        event_name="ctx.mcp.request",
        source="ctx-mcp-server",
        session_id="legacy-session-private",
        trace_id="1" * 32,
        span_id="2" * 16,
        privacy_mode="local_redacted",
        payload={
            "query": "private acme query",
            "safe_note": "opened /Users/example/private-repo/app.py",
        },
    )
    path.write_text(
        json.dumps(asdict(legacy_event), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append(payload)

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)
    result = export_traces(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "privacy": {"hash_salt": "tenant-a"},
            "traces": {
                "export": {
                    "enabled": True,
                    "span_maturity_seconds": 0,
                    "sink": "otlp_http",
                    "otlp": {
                        "endpoint": "https://collector.example:4318/v1/traces",
                        "allowed_hosts": ["collector.example"],
                    },
                },
            },
        },
    )

    assert result.exported == 1
    span = calls[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == legacy_event.trace_id
    assert span["spanId"] == legacy_event.span_id
    attributes = {item["key"]: item["value"] for item in span["attributes"]}
    assert attributes["ctx.session.hash"] == {
        "stringValue": hash_identifier("legacy-session-private", salt="tenant-a")
    }
    assert attributes["ctx.payload.query_hash"]["stringValue"].startswith("sha256:")
    assert attributes["ctx.payload.safe_note"]["stringValue"].startswith(
        "opened [path_hash:sha256:"
    )
    payload_text = json.dumps(calls[0])
    assert "legacy-session-private" not in payload_text
    assert "private acme query" not in payload_text
    assert "/Users/example/private-repo" not in payload_text
    assert "private-repo" not in payload_text


def test_export_traces_is_disabled_by_default_and_validates_env_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    event = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    assert event is not None
    disabled = export_traces(path, trusted_root=tmp_path, config={"path": str(path)})
    assert disabled.status == "noop"
    assert disabled.attempted == 0

    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "http://collector.example:4318/v1/traces",
    )
    with pytest.raises(ValueError, match="must use https"):
        export_traces(
            path,
            trusted_root=tmp_path,
            config={
                "path": str(path),
                "traces": {
                    "export": {
                        "enabled": True,
                        "sink": "otlp_http",
                        "otlp": {"allowed_hosts": ["collector.example"]},
                    },
                },
            },
        )

    calls: list[dict[str, Any]] = []

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append(settings)

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example")
    result = export_traces(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "traces": {
                "export": {
                    "enabled": True,
                    "span_maturity_seconds": 0,
                    "sink": "otlp_http",
                    "otlp": {"allowed_hosts": ["collector.example"]},
                },
            },
        },
    )
    assert result.exported == 1
    assert calls[0]["otlp_endpoint"] == "https://collector.example/v1/traces"


@pytest.mark.parametrize(
    ("trace_id", "span_id"),
    [
        (None, None),
        ("invalid", "5" * 16),
    ],
)
def test_export_traces_rejects_missing_or_invalid_context_without_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trace_id: str | None,
    span_id: str | None,
) -> None:
    path = tmp_path / "events.jsonl"
    invalid = TelemetryEvent(
        schema_version=SCHEMA_VERSION,
        event_id="invalid-trace-context",
        ts="2026-06-28T00:00:00Z",
        event_name="ctx.mcp.request",
        source="ctx-mcp-server",
        trace_id=trace_id,
        span_id=span_id,
        privacy_mode="local_redacted",
    )
    path.write_text(json.dumps(asdict(invalid)) + "\n", encoding="utf-8")
    called = False

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)
    config = {
        "path": str(path),
        "traces": {
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/traces",
                    "allowed_hosts": ["collector.example"],
                },
            },
        },
    }
    preview = preview_traces_export(
        path,
        trusted_root=tmp_path,
        config=config,
    )
    assert preview.attempted == 1
    assert preview.failed == 1
    assert preview.status == "failed"
    assert preview.error_kind == "ValueError"

    result = export_traces(
        path,
        trusted_root=tmp_path,
        config=config,
    )

    assert result.failed == 1
    assert result.error_kind == "ValueError"
    assert called is False
    assert not Path(str(path) + ".trace-export-checkpoint.json").exists()


def test_trace_preview_rejects_fresh_invalid_context_before_maturity_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    invalid = TelemetryEvent(
        schema_version=SCHEMA_VERSION,
        event_id="fresh-invalid-trace-context",
        ts=telemetry._now_iso(),
        event_name="ctx.api.failed",
        source="ctx-api",
        trace_id=None,
        span_id=None,
        privacy_mode="local_redacted",
    )
    path.write_text(json.dumps(asdict(invalid)) + "\n", encoding="utf-8")
    called = False

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)
    config = {
        "path": str(path),
        "traces": {
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/traces",
                    "allowed_hosts": ["collector.example"],
                },
            },
        },
    }

    preview = preview_traces_export(path, trusted_root=tmp_path, config=config)
    result = export_traces(path, trusted_root=tmp_path, config=config)

    assert preview.status == "failed"
    assert preview.attempted == 1
    assert preview.failed == 1
    assert preview.error_kind == "ValueError"
    assert preview.retained_events == 1
    assert result.status == "failed"
    assert result.attempted == 1
    assert result.failed == 1
    assert result.error_kind == "ValueError"
    assert result.checkpoint_advanced is False
    assert called is False
    assert not Path(str(path) + ".trace-export-checkpoint.json").exists()


def test_trace_export_partial_success_advances_checkpoint_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    record_config = {"path": str(path), "export": {"enabled": False}}
    with telemetry.telemetry_span(trace_id="1" * 32, span_id="2" * 16):
        first = record_event(
            "ctx.api.recommend_bundle",
            source="ctx-api",
            path=path,
            trusted_root=tmp_path,
            config=record_config,
        )
    with telemetry.telemetry_span(trace_id="1" * 32, span_id="3" * 16):
        second = record_event(
            "ctx.core.recommend_bundle",
            source="ctx-core",
            path=path,
            trusted_root=tmp_path,
            config=record_config,
        )
    assert first is not None
    assert second is not None
    attempts = 0

    class PartialSuccessResponse:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> PartialSuccessResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return self.status

        def read(self, amount: int = -1) -> bytes:
            return json.dumps(
                {
                    "partialSuccess": {
                        "rejectedSpans": "1",
                        "errorMessage": "ghp_private_collector_detail",
                    }
                }
            ).encode()

    class PartialSuccessOpener:
        def open(self, request: object, timeout: float) -> PartialSuccessResponse:
            nonlocal attempts
            attempts += 1
            return PartialSuccessResponse()

    monkeypatch.setattr(telemetry, "build_opener", lambda *args: PartialSuccessOpener())
    result = export_traces(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "traces": {
                "export": {
                    "enabled": True,
                    "span_maturity_seconds": 0,
                    "sink": "otlp_http",
                    "otlp": {
                        "endpoint": "https://collector.example:4318/v1/traces",
                        "allowed_hosts": ["collector.example"],
                    },
                },
            },
        },
    )

    checkpoint_path = Path(str(path) + ".trace-export-checkpoint.json")
    status_path = Path(str(path) + ".trace-export-status.json")
    assert result.attempted == 2
    assert result.exported == 1
    assert result.failed == 1
    assert result.status == "partial_success"
    assert result.error_kind == "otlp_partial_success"
    assert result.error_message == "[redacted]"
    assert attempts == 1
    assert result.checkpoint_after_event_id == second.event_id
    assert checkpoint_path.exists()
    status_text = status_path.read_text(encoding="utf-8")
    assert "ghp_private_collector_detail" not in status_text
    status = json.loads(status_text)
    assert status["status"] == "partial_success"
    assert status["attempted"] == 2
    assert status["exported"] == 1
    assert status["failed"] == 1
    assert status["error_kind"] == "otlp_partial_success"
    assert status["error_message"] == "[redacted]"
    assert status["checkpoint_advanced"] is True


def test_trace_partial_success_never_checkpoints_past_malformed_pending_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    record_config = {"path": str(path), "export": {"enabled": False}}
    with telemetry.telemetry_span(trace_id="1" * 32, span_id="2" * 16):
        first = record_event(
            "ctx.api.recommend_bundle",
            source="ctx-api",
            path=path,
            trusted_root=tmp_path,
            config=record_config,
        )
    with telemetry.telemetry_span(trace_id="1" * 32, span_id="3" * 16):
        second = record_event(
            "ctx.core.recommend_bundle",
            source="ctx-core",
            path=path,
            trusted_root=tmp_path,
            config=record_config,
        )
    assert first is not None
    assert second is not None
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{malformed pending record}\n")
    calls = 0

    def partial_success(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> telemetry._OTLPResponse:
        nonlocal calls
        calls += 1
        return telemetry._OTLPResponse(rejected_records=1)

    monkeypatch.setattr(telemetry, "_post_otlp_http", partial_success)
    config = {
        "path": str(path),
        "traces": {
            "export": {
                "enabled": True,
                "span_maturity_seconds": 0,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/traces",
                    "allowed_hosts": ["collector.example"],
                },
            },
        },
    }

    result = export_traces(path, trusted_root=tmp_path, config=config)

    assert calls == 1
    assert result.status == "partial_success"
    assert result.attempted == 2
    assert result.exported == 1
    assert result.failed == 1
    assert result.malformed_pending_records == 1
    assert result.checkpoint_after_event_id is None
    assert result.checkpoint_advanced is False
    assert not Path(str(path) + ".trace-export-checkpoint.json").exists()

    preview = preview_traces_export(path, trusted_root=tmp_path, config=config)
    assert preview.attempted == 2
    assert preview.malformed_pending_records == 1
    assert preview.status == "degraded"
    assert preview.checkpoint_before_event_id is None


def test_trace_export_sanitizes_all_outbound_string_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    secrets = {
        "source": "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "actor": "sk-abcdefghijklmnopqrstuvwxyz123456",
        "error": "ghp_zyxwvutsrqponmlkjihgfedcba654321",
        "service": "sk-1234567890abcdefghijklmnopqrstuvwxyz",
    }
    event = TelemetryEvent(
        schema_version=SCHEMA_VERSION,
        event_id="legacy-secret-envelope",
        ts="2026-06-28T00:00:00Z",
        event_name="ctx.api.failed",
        source=secrets["source"],
        outcome="error",
        actor=secrets["actor"],
        error_kind=secrets["error"],
        trace_id="1" * 32,
        span_id="2" * 16,
        privacy_mode="local_redacted",
    )
    path.write_text(json.dumps(asdict(event)) + "\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        telemetry,
        "_post_otlp_http",
        lambda payload, settings: calls.append(payload),
    )

    result = export_traces(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "privacy": {"hash_salt": "tenant-a"},
            "traces": {
                "export": {
                    "enabled": True,
                    "sink": "otlp_http",
                    "otlp": {
                        "endpoint": "https://collector.example:4318/v1/traces",
                        "allowed_hosts": ["collector.example"],
                        "service_name": secrets["service"],
                    },
                },
            },
        },
    )

    assert result.exported == 1
    payload_text = json.dumps(calls[0])
    for value in secrets.values():
        assert value not in payload_text
    assert payload_text.count("[redacted]") >= 4


def test_log_export_sanitizes_legacy_envelope_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    secrets = {
        "source": "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "actor": "sk-abcdefghijklmnopqrstuvwxyz123456",
        "error": "ghp_zyxwvutsrqponmlkjihgfedcba654321",
        "outcome": "sk-1234567890abcdefghijklmnopqrstuvwxyz",
        "service": "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
        "trace_id": "ghp_111111111111111111111111111111111111",
        "span_id": "sk-222222222222222222222222222222222222",
    }
    event = TelemetryEvent(
        schema_version=SCHEMA_VERSION,
        event_id="legacy-secret-log-envelope",
        ts="2026-06-28T00:00:00Z",
        event_name="ctx.api.failed",
        source=secrets["source"],
        outcome=secrets["outcome"],
        actor=secrets["actor"],
        error_kind=secrets["error"],
        trace_id=secrets["trace_id"],
        span_id=secrets["span_id"],
        privacy_mode="local_redacted",
    )
    path.write_text(json.dumps(asdict(event)) + "\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        telemetry,
        "_post_otlp_http",
        lambda payload, settings: calls.append(payload),
    )

    result = export_events(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "privacy": {"hash_salt": "tenant-a"},
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/logs",
                    "allowed_hosts": ["collector.example"],
                    "service_name": secrets["service"],
                },
            },
        },
    )

    assert result.exported == 1
    record = calls[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    attributes = {
        item["key"]: item["value"]["stringValue"]
        for item in record["attributes"]
        if "stringValue" in item["value"]
    }
    assert "traceId" not in record
    assert "spanId" not in record
    assert attributes["ctx.trace_id"] == "[redacted]"
    assert attributes["ctx.span_id"] == "[redacted]"
    payload_text = json.dumps(calls[0])
    for value in secrets.values():
        assert value not in payload_text
    assert payload_text.count("[redacted]") >= len(secrets)


def test_metric_export_sanitizes_legacy_envelope_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "metrics.jsonl"
    secrets = {
        "source": "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "unit": "sk-abcdefghijklmnopqrstuvwxyz123456",
        "service": "ghp_zyxwvutsrqponmlkjihgfedcba654321",
    }
    metric = TelemetryMetric(
        schema_version=METRIC_SCHEMA_VERSION,
        metric_id="legacy-secret-metric-envelope",
        ts="2026-06-28T00:00:00Z",
        name="ctx.api.requests",
        instrument="counter",
        value=1,
        unit=secrets["unit"],
        source=secrets["source"],
        privacy_mode="local_redacted",
    )
    path.write_text(json.dumps(asdict(metric)) + "\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        telemetry,
        "_post_otlp_http",
        lambda payload, settings: calls.append(payload),
    )

    result = export_metrics(
        path,
        trusted_root=tmp_path,
        config={
            "privacy": {"hash_salt": "tenant-a"},
            "metrics": {
                "enabled": True,
                "path": str(path),
                "export": {
                    "enabled": True,
                    "sink": "otlp_http",
                    "otlp": {
                        "endpoint": "https://collector.example:4318/v1/metrics",
                        "allowed_hosts": ["collector.example"],
                        "service_name": secrets["service"],
                    },
                },
            },
        },
    )

    assert result.exported == 1
    payload_text = json.dumps(calls[0])
    for value in secrets.values():
        assert value not in payload_text
    assert payload_text.count("[redacted]") >= len(secrets)


def test_trace_maturity_retains_tail_and_never_reopens_checkpointed_span(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    config = {
        "path": str(path),
        "traces": {
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/traces",
                    "allowed_hosts": ["collector.example"],
                },
            },
        },
    }
    mature_config = {
        "path": str(path),
        "traces": {
            "export": {
                "enabled": True,
                "span_maturity_seconds": 0,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/traces",
                    "allowed_hosts": ["collector.example"],
                },
            },
        },
    }
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        telemetry,
        "_post_otlp_http",
        lambda payload, settings: calls.append(payload),
    )
    record_config = {
        "path": str(path),
        "export": {"enabled": False},
    }
    first = record_event(
        "ctx.api.started",
        source="ctx-api",
        trace_id="1" * 32,
        span_id="2" * 16,
        path=path,
        trusted_root=tmp_path,
        config=record_config,
    )
    assert first is not None
    waiting = export_traces(path, trusted_root=tmp_path, config=config)
    first_result = export_traces(path, trusted_root=tmp_path, config=mature_config)
    second = record_event(
        "ctx.api.completed",
        source="ctx-api",
        trace_id="1" * 32,
        span_id="2" * 16,
        path=path,
        trusted_root=tmp_path,
        config=record_config,
    )
    assert second is not None
    late_result = export_traces(path, trusted_root=tmp_path, config=mature_config)

    assert waiting.status == "pending"
    assert waiting.attempted == 0
    assert waiting.retained_events == 1
    assert waiting.checkpoint_advanced is False
    assert first_result.exported == 1
    assert first_result.checkpoint_after_event_id == first.event_id
    assert late_result.status == "degraded"
    assert late_result.attempted == 0
    assert late_result.retained_events == 1
    assert late_result.late_span_events == 1
    assert late_result.checkpoint_after_event_id == first.event_id
    assert late_result.checkpoint_advanced is False
    assert len(calls) == 1
    span = calls[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == "1" * 32
    assert span["spanId"] == "2" * 16
    assert [event["name"] for event in span["events"]] == ["ctx.api.started"]


def test_trace_ids_match_logs_and_parent_topology_across_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    parent = TelemetryEvent(
        schema_version=SCHEMA_VERSION,
        event_id="parent-event",
        ts="2026-01-01T00:00:00Z",
        event_name="ctx.api.parent",
        source="ctx-api",
        trace_id="1" * 32,
        span_id="2" * 16,
        privacy_mode="local_redacted",
    )
    child = TelemetryEvent(
        schema_version=SCHEMA_VERSION,
        event_id="child-event",
        ts=telemetry._now_iso(),
        event_name="ctx.core.child",
        source="ctx-core",
        trace_id="1" * 32,
        span_id="3" * 16,
        parent_span_id="2" * 16,
        privacy_mode="local_redacted",
    )
    path.write_text(
        "\n".join(json.dumps(asdict(event)) for event in (parent, child)) + "\n",
        encoding="utf-8",
    )
    trace_payloads: list[dict[str, Any]] = []
    monkeypatch.setattr(
        telemetry,
        "_post_otlp_http",
        lambda payload, settings: trace_payloads.append(payload),
    )
    base_export = {
        "enabled": True,
        "sink": "otlp_http",
        "otlp": {
            "endpoint": "https://collector.example:4318/v1/traces",
            "allowed_hosts": ["collector.example"],
        },
    }
    first = export_traces(
        path,
        trusted_root=tmp_path,
        config={"path": str(path), "traces": {"export": base_export}},
    )
    second_export = dict(base_export)
    second_export["span_maturity_seconds"] = 0
    second = export_traces(
        path,
        trusted_root=tmp_path,
        config={"path": str(path), "traces": {"export": second_export}},
    )

    assert first.exported == 1
    assert first.retained_events == 1
    assert first.checkpoint_after_event_id == parent.event_id
    assert second.exported == 1
    assert second.checkpoint_after_event_id == child.event_id
    trace_spans = [
        payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for payload in trace_payloads
    ]
    assert trace_spans[0]["traceId"] == parent.trace_id
    assert trace_spans[0]["spanId"] == parent.span_id
    assert trace_spans[1]["traceId"] == child.trace_id
    assert trace_spans[1]["spanId"] == child.span_id
    assert trace_spans[1]["parentSpanId"] == parent.span_id

    log_payloads: list[dict[str, Any]] = []
    monkeypatch.setattr(
        telemetry,
        "_post_otlp_http",
        lambda payload, settings: log_payloads.append(payload),
    )
    log_result = export_events(
        path,
        trusted_root=tmp_path,
        include_exported=True,
        config={
            "path": str(path),
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/logs",
                    "allowed_hosts": ["collector.example"],
                },
            },
        },
    )
    assert log_result.exported == 2
    log_records = log_payloads[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    by_name = {record["body"]["stringValue"]: record for record in log_records}
    assert by_name[parent.event_name]["traceId"] == trace_spans[0]["traceId"]
    assert by_name[parent.event_name]["spanId"] == trace_spans[0]["spanId"]
    assert by_name[child.event_name]["traceId"] == trace_spans[1]["traceId"]
    assert by_name[child.event_name]["spanId"] == trace_spans[1]["spanId"]


def test_otlp_http_retries_retryable_status_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    class SuccessResponse:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> SuccessResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return self.status

        def read(self, amount: int = -1) -> bytes:
            return b"{}"

    class RetryOpener:
        def open(self, request: object, timeout: float) -> SuccessResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                headers = Message()
                headers["Retry-After"] = "0"
                raise telemetry.HTTPError(
                    "https://collector.example/v1/traces",
                    503,
                    "private collector detail",
                    headers,
                    None,
                )
            return SuccessResponse()

    monkeypatch.setattr(telemetry, "build_opener", lambda *args: RetryOpener())
    monkeypatch.setattr(telemetry.time, "sleep", sleeps.append)
    telemetry._post_otlp_http(
        {"resourceSpans": []},
        {
            "otlp_endpoint": "https://collector.example/v1/traces",
            "otlp_headers": {},
            "otlp_timeout_seconds": 1.0,
            "otlp_max_retries": 1,
            "otlp_retry_backoff_seconds": 10.0,
            "otlp_max_retry_delay_seconds": 1.0,
        },
    )

    assert attempts == 2
    assert sleeps == [0.0]


@pytest.mark.parametrize(
    "failure",
    [
        telemetry.URLError("collector disconnected"),
        telemetry.RemoteDisconnected("collector closed connection"),
    ],
)
def test_otlp_http_retries_transport_disconnects_with_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    class SuccessResponse:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> SuccessResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return self.status

        def read(self, amount: int = -1) -> bytes:
            return b"{}"

    class RetryOpener:
        def open(self, request: object, timeout: float) -> SuccessResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise failure
            return SuccessResponse()

    monkeypatch.setattr(telemetry, "build_opener", lambda *args: RetryOpener())
    monkeypatch.setattr(telemetry.time, "sleep", sleeps.append)
    response = telemetry._post_otlp_http(
        {"resourceSpans": []},
        {
            "mode": "local_redacted",
            "hash_salt": "tenant-a",
            "max_payload_value_chars": 1024,
            "otlp_endpoint": "https://collector.example/v1/traces",
            "otlp_headers": {},
            "otlp_timeout_seconds": 1.0,
            "otlp_max_retries": 1,
            "otlp_retry_backoff_seconds": 0.125,
            "otlp_max_retry_delay_seconds": 1.0,
            "otlp_retry_jitter_ratio": 0.0,
        },
    )

    assert response.rejected_records == 0
    assert attempts == 2
    assert sleeps == [0.125]


def test_otlp_http_mandatory_content_headers_cannot_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Any] = []

    class SuccessResponse:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> SuccessResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return self.status

        def read(self, amount: int = -1) -> bytes:
            return b"{}"

    class CapturingOpener:
        def open(self, request: Any, timeout: float) -> SuccessResponse:
            requests.append(request)
            return SuccessResponse()

    monkeypatch.setattr(telemetry, "build_opener", lambda *args: CapturingOpener())
    telemetry._post_otlp_http(
        {"resourceSpans": []},
        {
            "otlp_endpoint": "https://collector.example/v1/traces",
            "otlp_headers": {
                "content-type": "text/plain",
                "CONTENT-ENCODING": "gzip",
                "Authorization": "Bearer allowed",
            },
            "otlp_timeout_seconds": 1.0,
            "otlp_max_retries": 0,
        },
    )

    request = requests[0]
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Content-encoding") == "identity"
    assert request.get_header("Authorization") == "Bearer allowed"


def test_otlp_http_rejects_oversized_response_with_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_amounts: list[int] = []

    class OversizedResponse:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> OversizedResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return self.status

        def read(self, amount: int = -1) -> bytes:
            read_amounts.append(amount)
            return b"x" * amount

    class OversizedOpener:
        def open(self, request: object, timeout: float) -> OversizedResponse:
            return OversizedResponse()

    monkeypatch.setattr(telemetry, "build_opener", lambda *args: OversizedOpener())
    with pytest.raises(RuntimeError, match="response exceeded the size limit"):
        telemetry._post_otlp_http(
            {"resourceSpans": []},
            {
                "otlp_endpoint": "https://collector.example/v1/traces",
                "otlp_headers": {},
                "otlp_timeout_seconds": 1.0,
                "otlp_max_retries": 0,
            },
        )

    assert read_amounts == [telemetry._MAX_OTLP_RESPONSE_BYTES + 1]


def test_export_events_hashes_legacy_session_id_for_otlp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "event_id": "legacy-event",
                "ts": "2026-06-28T00:00:00Z",
                "event_name": "ctx.mcp.request",
                "source": "ctx-mcp-server",
                "outcome": "ok",
                "session_id": "legacy-session-private",
                "privacy_mode": "local_redacted",
                "payload": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append(payload)

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)

    result = export_events(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "privacy": {"hash_salt": "tenant-a"},
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/logs",
                    "allowed_hosts": ["collector.example"],
                },
            },
        },
    )

    assert result.exported == 1
    assert result.failed == 0
    payload = calls[0]
    log_record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    attributes = {item["key"]: item["value"] for item in log_record["attributes"]}
    assert "ctx.session_id" not in attributes
    assert attributes["ctx.session.hash"] == {
        "stringValue": hash_identifier("legacy-session-private", salt="tenant-a")
    }
    assert "legacy-session-private" not in json.dumps(payload)


def test_export_events_resanitizes_legacy_payload_for_otlp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    legacy_event = TelemetryEvent(
        schema_version=SCHEMA_VERSION,
        event_id="legacy-raw-payload",
        ts="2026-06-28T00:00:00Z",
        event_name="ctx.mcp.request",
        source="ctx-mcp-server",
        outcome="ok",
        privacy_mode="local_redacted",
        payload={
            "query": "private acme query",
            "path": "/Users/example/private-repo/app.py",
            "safe_note": "opened /Users/example/private-repo/app.py",
        },
    )
    path.write_text(
        json.dumps(asdict(legacy_event), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append(payload)

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)

    result = export_events(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "privacy": {"hash_salt": "tenant-a"},
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/logs",
                    "allowed_hosts": ["collector.example"],
                },
            },
        },
    )

    assert result.exported == 1
    assert result.failed == 0
    payload = calls[0]
    log_record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    attributes = {item["key"]: item["value"] for item in log_record["attributes"]}
    assert "ctx.payload.query" not in attributes
    assert "ctx.payload.path" not in attributes
    assert attributes["ctx.payload.query_hash"]["stringValue"].startswith("sha256:")
    assert attributes["ctx.payload.path_hash"]["stringValue"].startswith("sha256:")
    safe_note = attributes["ctx.payload.safe_note"]["stringValue"]
    assert safe_note.startswith("opened [path_hash:sha256:")
    payload_text = json.dumps(payload)
    assert "private acme query" not in payload_text
    assert "/Users/example/private-repo" not in payload_text
    assert "private-repo" not in payload_text


@pytest.mark.parametrize(
    ("endpoint", "match"),
    [
        ("http://collector.example:4318/v1/logs", "must use https"),
        ("https://collector.example:4318/v1/logs", "allowed_hosts"),
        ("https://user:pass@collector.example/v1/logs", "must not include userinfo"),
        ("https://collector.example/v1/logs?token=x", "must not include query"),
        ("https://collector.example/v1/logs#fragment", "must not include query"),
        ("https://collector.example:bad/v1/logs", "invalid port"),
        ("ftp://collector.example/v1/logs", "must use http or https"),
        ("/v1/logs", "must use http or https"),
        ("https:///v1/logs", "must include a host"),
        ("https://169.254.169.254/v1/logs", "host is not allowed"),
        ("https://10.0.0.1/v1/logs", "host is not allowed"),
    ],
)
def test_export_events_rejects_unsafe_otlp_endpoints(
    tmp_path: Path,
    endpoint: str,
    match: str,
) -> None:
    path = tmp_path / "events.jsonl"
    record_event(
        "ctx.mcp.request",
        source="ctx-mcp-server",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )

    with pytest.raises(ValueError, match=match):
        export_events(
            path,
            trusted_root=tmp_path,
            config={
                "path": str(path),
                "export": {
                    "enabled": True,
                    "sink": "otlp_http",
                    "otlp": {"endpoint": endpoint},
                },
            },
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:4318/v1/logs",
        "http://127.0.0.1:4318/v1/logs",
        "http://[::1]:4318/v1/logs",
    ],
)
def test_export_events_allows_loopback_http_otlp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    path = tmp_path / "events.jsonl"
    record_event(
        "ctx.mcp.request",
        source="ctx-mcp-server",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append((payload, settings))

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)

    result = export_events(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {"endpoint": endpoint},
            },
        },
    )

    assert result.exported == 1
    assert result.failed == 0
    assert calls[0][1]["otlp_endpoint"] == endpoint


def test_export_events_applies_otlp_policy_to_env_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    record_event(
        "ctx.mcp.request",
        source="ctx-mcp-server",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://collector.example/v1/logs")

    with pytest.raises(ValueError, match="must use https"):
        export_events(
            path,
            trusted_root=tmp_path,
            config={
                "path": str(path),
                "export": {
                    "enabled": True,
                    "sink": "otlp_http",
                    "otlp": {
                        "endpoint": "https://collector.example/v1/logs",
                        "allowed_hosts": ["collector.example"],
                    },
                },
            },
        )


def test_export_events_appends_logs_path_for_otlp_base_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    record_event(
        "ctx.mcp.request",
        source="ctx-mcp-server",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append((payload, settings))

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example")

    result = export_events(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {"allowed_hosts": ["collector.example"]},
            },
        },
    )

    assert result.exported == 1
    assert result.failed == 0
    assert calls[0][1]["otlp_endpoint"] == "https://collector.example/v1/logs"


def test_local_jsonl_export_ignores_unused_invalid_otlp_endpoint(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    export_path = tmp_path / "exported-events.jsonl"
    record_event(
        "ctx.mcp.request",
        source="ctx-mcp-server",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )

    result = export_events(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "export": {
                "enabled": True,
                "sink": "local_jsonl",
                "path": str(export_path),
                "otlp": {"endpoint": "ftp://collector.example/v1/logs"},
            },
        },
    )

    assert result.exported == 1
    assert result.failed == 0
    assert export_path.is_file()


def test_otlp_redirect_handler_rejects_redirects() -> None:
    handler = telemetry._NoRedirectHandler()
    redirect_request: Any = handler.redirect_request

    assert redirect_request(None, None, 302, "Found", {}, "https://collector.example") is None


def test_export_events_checkpoint_skips_already_exported_events(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    export_path = tmp_path / "exported-events.jsonl"
    checkpoint_path = tmp_path / "checkpoint.json"
    config = {
        "path": str(path),
        "export": {
            "enabled": True,
            "sink": "local_jsonl",
            "path": str(export_path),
            "checkpoint_path": str(checkpoint_path),
        },
    }
    for name in ("ctx.api.recommend_bundle", "ctx.mcp.request"):
        record_event(
            name,
            source="ctx-test",
            path=path,
            trusted_root=tmp_path,
            config={"path": str(path), "export": {"enabled": False}},
        )

    first = export_events(path, trusted_root=tmp_path, config=config)

    assert first.attempted == 2
    assert first.exported == 2
    assert first.status == "ok"
    assert first.checkpoint_advanced is True
    assert first.last_event_id is not None
    assert checkpoint_path.is_file()
    assert len(export_path.read_text(encoding="utf-8").splitlines()) == 2

    second = export_events(path, trusted_root=tmp_path, config=config)

    assert second.attempted == 0
    assert second.exported == 0
    assert second.status == "noop"
    assert second.checkpoint_advanced is False
    assert second.checkpoint_before_event_id == first.last_event_id
    assert second.checkpoint_after_event_id == first.last_event_id
    assert second.last_event_id == first.last_event_id
    assert len(export_path.read_text(encoding="utf-8").splitlines()) == 2
    status_path = Path(str(path) + ".export-status.json")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["schema_version"] == EXPORT_STATUS_SCHEMA_VERSION
    assert status["status"] == "noop"
    assert status["checkpoint_advanced"] is False
    assert status["checkpoint_before_event_id"] == first.last_event_id
    assert status["checkpoint_after_event_id"] == first.last_event_id

    record_event(
        "ctx.cli.run",
        source="ctx-test",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    third = export_events(path, trusted_root=tmp_path, config=config)

    assert third.attempted == 1
    assert third.exported == 1
    assert third.last_event_id != first.last_event_id
    assert len(export_path.read_text(encoding="utf-8").splitlines()) == 3

    replay = export_events(
        path,
        trusted_root=tmp_path,
        config=config,
        include_exported=True,
    )

    assert replay.attempted == 3
    assert replay.exported == 3
    assert len(export_path.read_text(encoding="utf-8").splitlines()) == 6


def test_record_event_continuous_export_drains_backlog_before_checkpoint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    export_path = tmp_path / "exported-events.jsonl"
    checkpoint_path = tmp_path / "checkpoint.json"
    first = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-test",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    config = {
        "path": str(path),
        "export": {
            "enabled": True,
            "sink": "local_jsonl",
            "path": str(export_path),
            "checkpoint_path": str(checkpoint_path),
        },
    }
    second = record_event(
        "ctx.mcp.request",
        source="ctx-test",
        path=path,
        trusted_root=tmp_path,
        config=config,
    )

    assert first is not None
    assert second is not None
    assert checkpoint_path.exists()
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["last_event_id"] == second.event_id
    status = json.loads(Path(str(path) + ".export-status.json").read_text(encoding="utf-8"))
    assert status["attempted"] == 2
    assert status["exported"] == 2
    assert status["checkpoint_advanced"] is True
    assert status["checkpoint_after_event_id"] == second.event_id
    exported_ids = [
        json.loads(line)["event_id"]
        for line in export_path.read_text(encoding="utf-8").splitlines()
    ]
    assert exported_ids == [first.event_id, second.event_id]

    later = export_events(path, trusted_root=tmp_path, config=config)

    assert later.attempted == 0
    assert later.exported == 0
    assert later.checkpoint_after_event_id == second.event_id


def test_record_metric_continuous_export_drains_backlog_before_checkpoint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "metrics.jsonl"
    export_path = tmp_path / "exported-metrics.jsonl"
    checkpoint_path = tmp_path / "metric-checkpoint.json"
    first = record_counter(
        "ctx.api.requests",
        value=1,
        path=path,
        trusted_root=tmp_path,
        config={
            "metrics": {
                "enabled": True,
                "path": str(path),
                "export": {"enabled": False},
            },
        },
    )
    config = {
        "metrics": {
            "enabled": True,
            "path": str(path),
            "export": {
                "enabled": True,
                "sink": "local_jsonl",
                "path": str(export_path),
                "checkpoint_path": str(checkpoint_path),
            },
        },
    }
    second = record_counter(
        "ctx.api.requests",
        value=2,
        path=path,
        trusted_root=tmp_path,
        config=config,
    )

    assert first is not None
    assert second is not None
    assert checkpoint_path.exists()
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["last_metric_id"] == second.metric_id
    status = json.loads(Path(str(path) + ".export-status.json").read_text(encoding="utf-8"))
    assert status["attempted"] == 2
    assert status["exported"] == 2
    assert status["checkpoint_advanced"] is True
    assert status["checkpoint_after_metric_id"] == second.metric_id
    exported_ids = [
        json.loads(line)["metric_id"]
        for line in export_path.read_text(encoding="utf-8").splitlines()
    ]
    assert exported_ids == [first.metric_id, second.metric_id]

    later = export_metrics(path, trusted_root=tmp_path, config=config)

    assert later.attempted == 0
    assert later.exported == 0
    assert later.checkpoint_after_metric_id == second.metric_id


def test_export_events_ignores_checkpoint_for_different_destination(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    export_path_a = tmp_path / "exported-a.jsonl"
    export_path_b = tmp_path / "exported-b.jsonl"
    checkpoint_path = tmp_path / "checkpoint.json"
    record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    config_a = {
        "path": str(path),
        "export": {
            "enabled": True,
            "sink": "local_jsonl",
            "path": str(export_path_a),
            "checkpoint_path": str(checkpoint_path),
        },
    }
    config_b = {
        "path": str(path),
        "export": {
            "enabled": True,
            "sink": "local_jsonl",
            "path": str(export_path_b),
            "checkpoint_path": str(checkpoint_path),
        },
    }

    first = export_events(path, trusted_root=tmp_path, config=config_a)
    second = export_events(path, trusted_root=tmp_path, config=config_b)

    assert first.exported == 1
    assert second.attempted == 1
    assert second.exported == 1
    assert second.checkpoint_before_event_id is None
    assert second.checkpoint_advanced is True
    assert len(export_path_b.read_text(encoding="utf-8").splitlines()) == 1


def test_export_events_writes_status_with_malformed_count(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "events.jsonl"
    export_path = tmp_path / "exported-events.jsonl"
    event = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    assert event is not None
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json}\n")
    status_path = Path(str(path) + ".export-status.json")

    result = export_events(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "export": {
                "enabled": True,
                "sink": "local_jsonl",
                "path": str(export_path),
            },
        },
    )

    assert result.attempted == 1
    assert result.exported == 1
    assert result.failed == 0
    assert result.status == "degraded"
    assert result.malformed_records == 1
    assert result.malformed_pending_records == 1
    assert result.malformed_first_line == 2
    assert result.malformed_last_line == 2
    assert result.checkpoint_advanced is False
    assert result.last_event_id is None
    assert result.last_success_event_id == event.event_id
    assert result.status_path == str(status_path)
    assert not Path(str(path) + ".export-checkpoint.json").exists()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["schema_version"] == EXPORT_STATUS_SCHEMA_VERSION
    assert status["status"] == "degraded"
    assert status["attempted"] == 1
    assert status["exported"] == 1
    assert status["failed"] == 0
    assert status["malformed_records"] == 1
    assert status["malformed_total_records"] == 1
    assert status["malformed_pending_records"] == 1
    assert status["malformed_first_line"] == 2
    assert status["malformed_last_line"] == 2
    assert status["checkpoint_advanced"] is False
    assert status["checkpoint_before_event_id"] is None
    assert status["checkpoint_after_event_id"] is None
    assert status["last_success_event_id"] == event.event_id
    assert status["destination_hash"].startswith("sha256:")
    assert "skipping malformed event" in capsys.readouterr().err


def test_export_events_writes_failure_status_without_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    event = record_event(
        "ctx.mcp.request",
        source="ctx-mcp-server",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    assert event is not None
    checkpoint_path = Path(str(path) + ".export-checkpoint.json")
    status_path = Path(str(path) + ".export-status.json")

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        raise RuntimeError("collector unavailable")

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)

    result = export_events(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {"endpoint": "http://127.0.0.1:4318/v1/logs"},
            },
        },
    )

    assert result.attempted == 1
    assert result.exported == 0
    assert result.failed == 1
    assert result.status == "failed"
    assert result.error_kind == "RuntimeError"
    assert result.checkpoint_advanced is False
    assert result.malformed_pending_records == 0
    assert result.status_path == str(status_path)
    assert not checkpoint_path.exists()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["schema_version"] == EXPORT_STATUS_SCHEMA_VERSION
    assert status["status"] == "failed"
    assert status["attempted"] == 1
    assert status["exported"] == 0
    assert status["failed"] == 1
    assert status["error_kind"] == "RuntimeError"
    assert status["last_event_id"] is None
    assert status["checkpoint_advanced"] is False
    assert status["malformed_pending_records"] == 0
    assert status["destination_hash"].startswith("sha256:")


def test_telemetry_export_cli_writes_local_jsonl(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "events.jsonl"
    export_path = tmp_path / "exported-events.jsonl"
    record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        payload={"query": "private acme query", "ctx.result.count": 1},
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )

    rc = telemetry_cli.main(
        [
            "--path",
            str(path),
            "--trusted-root",
            str(tmp_path),
            "--sink",
            "local_jsonl",
            "--output",
            str(export_path),
            "--json",
        ]
    )

    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["attempted"] == 1
    assert summary["error_kind"] is None
    assert summary["exported"] == 1
    assert summary["failed"] == 0
    assert summary["sink"] == "local_jsonl"
    assert summary["status"] == "ok"
    assert summary["checkpoint_path"] == str(path) + ".export-checkpoint.json"
    assert summary["checkpoint_before_event_id"] is None
    assert summary["checkpoint_after_event_id"] == summary["last_event_id"]
    assert summary["checkpoint_advanced"] is True
    assert summary["checkpoint_found"] is False
    assert summary["malformed_records"] == 0
    assert summary["malformed_pending_records"] == 0
    assert summary["destination_hash"].startswith("sha256:")
    assert summary["last_success_event_id"] == summary["last_event_id"]
    assert summary["status_path"] == str(path) + ".export-status.json"
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert summary["last_event_id"] == exported["event_id"]
    assert exported["event_name"] == "ctx.api.recommend_bundle"
    assert "query" not in exported["payload"]
    assert "private acme query" not in json.dumps(exported)


def test_telemetry_export_cli_writes_metric_local_jsonl(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "metrics.jsonl"
    export_path = tmp_path / "exported-metrics.jsonl"
    metric = record_counter(
        "ctx.api.requests",
        attributes={"ctx.source": "api", "query": "private acme query"},
        path=path,
        trusted_root=tmp_path,
        config={"metrics": {"enabled": True, "path": str(path)}},
    )
    assert metric is not None

    rc = telemetry_cli.main(
        [
            "--signal",
            "metrics",
            "--path",
            str(path),
            "--trusted-root",
            str(tmp_path),
            "--sink",
            "local_jsonl",
            "--output",
            str(export_path),
            "--json",
        ]
    )

    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["signal"] == "metrics"
    assert summary["attempted"] == 1
    assert summary["exported"] == 1
    assert summary["failed"] == 0
    assert summary["status"] == "ok"
    assert summary["checkpoint_path"] == str(path) + ".export-checkpoint.json"
    assert summary["checkpoint_after_metric_id"] == summary["last_metric_id"]
    assert summary["last_success_metric_id"] == summary["last_metric_id"]
    assert summary["status_path"] == str(path) + ".export-status.json"
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported["schema_version"] == METRIC_SCHEMA_VERSION
    assert exported["metric_id"] == summary["last_metric_id"]
    assert "query" not in exported["attributes"]
    assert "private acme query" not in json.dumps(exported)


def test_telemetry_export_cli_previews_and_exports_traces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "events.jsonl"
    event = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        session_id="private-session",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    assert event is not None
    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append((payload, settings))

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)
    monkeypatch.setattr(
        telemetry_cli,
        "_base_telemetry_config",
        lambda: {
            "path": str(path),
            "privacy": {"hash_salt": "tenant-a"},
            "traces": {
                "enabled": True,
                "export": {"enabled": False, "span_maturity_seconds": 0},
            },
        },
    )
    common_args = [
        "--signal",
        "traces",
        "--path",
        str(path),
        "--trusted-root",
        str(tmp_path),
        "--sink",
        "otlp_http",
        "--otlp-endpoint",
        "http://127.0.0.1:4318/v1/traces",
        "--json",
    ]

    assert telemetry_cli.main([*common_args, "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["signal"] == "traces"
    assert preview["attempted"] == 1
    assert preview["exported"] == 0
    assert preview["checkpoint_advanced"] is False
    assert preview["checkpoint_path"] == str(path) + ".trace-export-checkpoint.json"
    assert calls == []

    assert telemetry_cli.main(common_args) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["signal"] == "traces"
    assert summary["attempted"] == 1
    assert summary["exported"] == 1
    assert summary["failed"] == 0
    assert summary["checkpoint_after_event_id"] == event.event_id
    assert summary["checkpoint_path"] == str(path) + ".trace-export-checkpoint.json"
    assert summary["status_path"] == str(path) + ".trace-export-status.json"
    assert len(calls) == 1
    payload, settings = calls[0]
    assert settings["otlp_endpoint"] == "http://127.0.0.1:4318/v1/traces"
    assert payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert "private-session" not in json.dumps(payload)


def test_telemetry_export_cli_trace_preview_fails_invalid_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "events.jsonl"
    invalid = TelemetryEvent(
        schema_version=SCHEMA_VERSION,
        event_id="invalid-preview-trace",
        ts="2026-06-28T00:00:00Z",
        event_name="ctx.api.failed",
        source="ctx-api",
        trace_id="invalid",
        span_id="2" * 16,
        privacy_mode="local_redacted",
    )
    path.write_text(json.dumps(asdict(invalid)) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        telemetry_cli,
        "_base_telemetry_config",
        lambda: {
            "path": str(path),
            "traces": {"enabled": True, "export": {"enabled": False}},
        },
    )

    rc = telemetry_cli.main(
        [
            "--signal",
            "traces",
            "--path",
            str(path),
            "--trusted-root",
            str(tmp_path),
            "--sink",
            "otlp_http",
            "--otlp-endpoint",
            "http://127.0.0.1:4318/v1/traces",
            "--dry-run",
            "--json",
        ]
    )

    summary = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert summary["attempted"] == 1
    assert summary["exported"] == 0
    assert summary["failed"] == 1
    assert summary["status"] == "failed"
    assert summary["error_kind"] == "ValueError"
    assert summary["checkpoint_advanced"] is False


def test_telemetry_export_cli_allows_remote_otlp_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "events.jsonl"
    record_event(
        "ctx.mcp.request",
        source="ctx-mcp-server",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    calls: list[dict[str, Any]] = []

    def fake_post_otlp_http(
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append(settings)

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)

    rc = telemetry_cli.main(
        [
            "--path",
            str(path),
            "--trusted-root",
            str(tmp_path),
            "--sink",
            "otlp_http",
            "--otlp-endpoint",
            "https://collector.example:4318/v1/logs",
            "--otlp-allowed-host",
            "collector.example",
            "--json",
        ]
    )

    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["exported"] == 1
    assert summary["failed"] == 0
    assert summary["sink"] == "otlp_http"
    assert summary["status"] == "ok"
    assert len(calls) == 1
    assert calls[0]["otlp_endpoint"] == "https://collector.example:4318/v1/logs"
    assert calls[0]["otlp_allowed_hosts"] == ["collector.example"]


def test_telemetry_export_cli_can_fail_on_degraded_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "events.jsonl"
    export_path = tmp_path / "exported-events.jsonl"
    record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not-json\n")

    rc = telemetry_cli.main(
        [
            "--path",
            str(path),
            "--trusted-root",
            str(tmp_path),
            "--sink",
            "local_jsonl",
            "--output",
            str(export_path),
            "--fail-on-degraded",
            "--json",
        ]
    )

    assert rc == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["exported"] == 1
    assert summary["failed"] == 0
    assert summary["status"] == "degraded"
    assert summary["malformed_pending_records"] == 1
    assert summary["checkpoint_advanced"] is False


def test_telemetry_export_cli_dry_run_counts_events(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "events.jsonl"
    event = record_event(
        "ctx.cli.run",
        source="ctx-cli",
        path=path,
        trusted_root=tmp_path,
        config={"path": str(path), "export": {"enabled": False}},
    )
    assert event is not None

    rc = telemetry_cli.main(
        [
            "--path",
            str(path),
            "--trusted-root",
            str(tmp_path),
            "--sink",
            "local_jsonl",
            "--dry-run",
            "--json",
        ]
    )

    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["attempted"] == 1
    assert summary["dry_run"] is True
    assert summary["exported"] == 0
    assert summary["failed"] == 0
    assert summary["sink"] == "local_jsonl"
    assert summary["status"] == "ok"
    assert summary["checkpoint_advanced"] is False
    assert summary["last_event_id"] == event.event_id
    assert summary["malformed_records"] == 0
    assert summary["malformed_pending_records"] == 0
    assert summary["destination_hash"].startswith("sha256:")
    assert summary["status_path"] == str(path) + ".export-status.json"
    assert not Path(summary["status_path"]).exists()


def test_hash_identifier_is_stable_and_saltable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTX_TELEMETRY_HASH_SALT", "tenant-a")

    assert hash_identifier("repo") == hash_identifier("repo")
    assert hash_identifier("repo") != hash_identifier("other")
    assert hash_identifier("repo", salt="tenant-a") != hash_identifier("repo", salt="tenant-b")


def test_hash_identifier_uses_env_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTX_TELEMETRY_HASH_SALT", "tenant-a")
    tenant_a = hash_identifier("repo")

    monkeypatch.setenv("CTX_TELEMETRY_HASH_SALT", "tenant-b")

    assert hash_identifier("repo") != tenant_a
    assert hash_identifier("repo", salt="explicit") == hash_identifier("repo", salt="explicit")


def test_hash_identifier_generates_owner_only_local_salt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    salt_path = tmp_path / "hash-salt"
    monkeypatch.delenv("CTX_TELEMETRY_HASH_SALT", raising=False)
    monkeypatch.setattr(
        telemetry,
        "_config_get",
        lambda key, default: (
            {"privacy": {"hash_salt_path": str(salt_path)}} if key == "telemetry" else default
        ),
    )

    first = hash_identifier("repo")

    assert salt_path.is_file()
    assert salt_path.read_text(encoding="utf-8").strip()
    assert hash_identifier("repo") == first
    assert stat.S_IMODE(salt_path.stat().st_mode) == 0o600


def test_record_event_hashes_with_configured_salt(tmp_path: Path) -> None:
    path_a = tmp_path / "tenant-a.jsonl"
    path_b = tmp_path / "tenant-b.jsonl"

    event_a = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        repo="/Users/example/private-repo",
        payload={"query": "private acme query"},
        path=path_a,
        trusted_root=tmp_path,
        config={"path": str(path_a), "privacy": {"hash_salt": "tenant-a"}},
    )
    event_b = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        repo="/Users/example/private-repo",
        payload={"query": "private acme query"},
        path=path_b,
        trusted_root=tmp_path,
        config={"path": str(path_b), "privacy": {"hash_salt": "tenant-b"}},
    )

    assert event_a is not None
    assert event_b is not None
    assert event_a.repo_hash != event_b.repo_hash
    assert event_a.payload["query_hash"] != event_b.payload["query_hash"]


def test_nested_payload_hashing_uses_configured_salt(tmp_path: Path) -> None:
    path_a = tmp_path / "tenant-a.jsonl"
    path_b = tmp_path / "tenant-b.jsonl"

    event_a = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        payload={"nested": {"query": "private nested query"}},
        path=path_a,
        trusted_root=tmp_path,
        config={"path": str(path_a), "privacy": {"hash_salt": "tenant-a"}},
    )
    event_b = record_event(
        "ctx.api.recommend_bundle",
        source="ctx-api",
        payload={"nested": {"query": "private nested query"}},
        path=path_b,
        trusted_root=tmp_path,
        config={"path": str(path_b), "privacy": {"hash_salt": "tenant-b"}},
    )

    assert event_a is not None
    assert event_b is not None
    assert event_a.payload["nested"]["query_hash"].startswith("sha256:")
    assert event_b.payload["nested"]["query_hash"].startswith("sha256:")
    assert event_a.payload["nested"]["query_hash"] != event_b.payload["nested"]["query_hash"]
    assert "private nested query" not in path_a.read_text(encoding="utf-8")
    assert "private nested query" not in path_b.read_text(encoding="utf-8")


def test_record_exception_hashes_message_and_stack_for_otlp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    config = {"path": str(path), "privacy": {"hash_salt": "tenant-a"}}
    monkeypatch.setattr(telemetry, "record_event", record_event)

    try:
        raise RuntimeError("private acme failure at /Users/example/private-repo")
    except RuntimeError as exc:
        payload = exception_payload(exc, config=config)
        event = record_exception(
            "ctx.api.recommend_bundle",
            source="ctx-api",
            exc=exc,
            payload={"query": "private acme query"},
            path=path,
            trusted_root=tmp_path,
            config=config,
        )

    assert event is not None
    assert payload["ctx.exception.message_hash"].startswith("sha256:")
    assert payload["ctx.exception.stack_hash"].startswith("sha256:")
    assert event.payload["ctx.exception.message_hash"] == payload["ctx.exception.message_hash"]
    assert event.payload["ctx.exception.stack_hash"] == payload["ctx.exception.stack_hash"]
    local_text = path.read_text(encoding="utf-8")
    assert "private acme failure" not in local_text
    assert "/Users/example/private-repo" not in local_text
    assert "private acme query" not in local_text

    calls: list[dict[str, Any]] = []

    def fake_post_otlp_http(
        otlp_payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        calls.append(otlp_payload)

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post_otlp_http)
    result = export_events(
        path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "privacy": {"hash_salt": "tenant-a"},
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/logs",
                    "allowed_hosts": ["collector.example"],
                },
            },
        },
    )

    assert result.exported == 1
    otlp_text = json.dumps(calls[0])
    assert "private acme failure" not in otlp_text
    assert "/Users/example/private-repo" not in otlp_text
    assert "private acme query" not in otlp_text
    assert "ctx.payload.ctx.exception.message_hash" in otlp_text
    assert "ctx.payload.ctx.exception.stack_hash" in otlp_text


def test_api_and_core_exceptions_record_hashed_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctx.adapters.generic.ctx_core_tools as core_tools

    path = tmp_path / "events.jsonl"
    _redirect_real_event_telemetry(monkeypatch, path)

    toolbox = CtxCoreToolbox(wiki_dir=tmp_path / "wiki", graph_path=tmp_path / "graph.json")

    def fail_recommend(args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("private core failure for /Users/example/private-repo")

    monkeypatch.setattr(toolbox, "_dispatch_recommend", fail_recommend)
    with pytest.raises(RuntimeError):
        toolbox.dispatch(
            core_tools.ToolCall(
                id="core",
                name="ctx__recommend_bundle",
                arguments={"query": "private core query"},
            )
        )

    class FailingToolbox:
        def dispatch(self, call: Any) -> str:
            raise RuntimeError("private api failure for /Users/example/private-repo")

    monkeypatch.setattr(ctx_api, "_get_toolbox", lambda: FailingToolbox())
    with pytest.raises(RuntimeError):
        ctx_api._call("ctx__recommend_bundle", {"query": "private api query"})

    events = list(read_events(path, trusted_root=tmp_path))
    by_source = {event.source: event for event in events}
    assert {"ctx-core", "ctx-api"} <= set(by_source)
    for event in by_source.values():
        assert event.payload["ctx.exception.message_hash"].startswith("sha256:")
        assert event.payload["ctx.exception.stack_hash"].startswith("sha256:")
        assert event.payload["ctx.exception.escaped"] is True
    raw = path.read_text(encoding="utf-8")
    assert "private core failure" not in raw
    assert "private api failure" not in raw
    assert "private core query" not in raw
    assert "private api query" not in raw
    assert "/Users/example/private-repo" not in raw


def test_mcp_handler_exception_records_hashed_payload_and_sanitized_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    _redirect_real_event_telemetry(monkeypatch, path)

    def boom(state: Any, params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("private mcp failure for /Users/example/private-repo")

    monkeypatch.setitem(mcp_server._HANDLERS, "boom", boom)
    out = BytesIO()
    frame = {"jsonrpc": "2.0", "id": 1, "method": "boom", "params": {}}

    mcp_server._process_line(json.dumps(frame), mcp_server._ServerState(), out)

    response = json.loads(out.getvalue().decode("utf-8"))
    assert response["error"]["code"] == -32603
    assert response["error"]["message"] == "internal error: RuntimeError"
    assert "private mcp failure" not in json.dumps(response)
    event = next(read_events(path, trusted_root=tmp_path))
    assert event.event_name == "ctx.mcp.request"
    assert event.payload["ctx.exception.message_hash"].startswith("sha256:")
    assert event.payload["ctx.exception.stack_hash"].startswith("sha256:")
    raw = path.read_text(encoding="utf-8")
    assert "private mcp failure" not in raw
    assert "/Users/example/private-repo" not in raw


def _write_event_record(path: Path, event_id: str, ts: str) -> None:
    event = TelemetryEvent(
        schema_version=SCHEMA_VERSION,
        event_id=event_id,
        ts=ts,
        event_name="ctx.api.recommend_bundle",
        source="ctx-api",
        payload={"ctx.result.count": 1},
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(event), separators=(",", ":")) + "\n")


def _write_metric_record(path: Path, metric_id: str, ts: str) -> None:
    metric = TelemetryMetric(
        schema_version=METRIC_SCHEMA_VERSION,
        metric_id=metric_id,
        ts=ts,
        name="ctx.api.duration",
        instrument="histogram",
        value=42.0,
        unit="ms",
        attributes={"ctx.source": "api"},
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(metric), separators=(",", ":")) + "\n")


def test_plan_telemetry_retention_does_not_mutate_spool(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    status_path = tmp_path / "retention-status.json"
    for index in range(4):
        _write_event_record(path, f"event-{index}", f"2026-01-0{index + 1}T00:00:00Z")
    before = path.read_text(encoding="utf-8")

    results = plan_telemetry_retention(
        signal="events",
        event_path=path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "retention": {
                "enabled": True,
                "status_path": str(status_path),
                "min_keep_records": 1,
                "events": {"max_records": 2},
            },
        },
    )

    assert len(results) == 1
    result = results[0]
    assert result.signal == "events"
    assert result.status == "planned"
    assert result.dry_run is True
    assert result.scanned_records == 4
    assert result.retained_records == 2
    assert result.dropped_records == 2
    assert result.status_path == str(status_path)
    assert path.read_text(encoding="utf-8") == before
    assert not status_path.exists()


def test_enforce_telemetry_retention_prunes_events_and_preserves_malformed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    status_path = tmp_path / "retention-status.json"
    for index in range(3):
        _write_event_record(path, f"event-{index}", f"2026-01-0{index + 1}T00:00:00Z")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not-json\n")

    results = enforce_telemetry_retention(
        signal="events",
        event_path=path,
        trusted_root=tmp_path,
        config={
            "path": str(path),
            "retention": {
                "enabled": True,
                "status_path": str(status_path),
                "min_keep_records": 1,
                "drop_malformed": False,
                "events": {"max_records": 2},
            },
        },
    )

    assert len(results) == 1
    result = results[0]
    assert result.status == "pruned"
    assert result.dry_run is False
    assert result.scanned_records == 3
    assert result.retained_records == 2
    assert result.dropped_records == 1
    assert result.malformed_records == 1
    assert result.malformed_dropped_records == 0
    assert [event.event_id for event in read_events(path, trusted_root=tmp_path)] == [
        "event-1",
        "event-2",
    ]
    assert "not-json" in path.read_text(encoding="utf-8")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["schema_version"] == RETENTION_STATUS_SCHEMA_VERSION
    assert status["results"][0]["signal"] == "events"
    assert status["results"][0]["status"] == "pruned"
    assert stat.S_IMODE(status_path.stat().st_mode) == 0o600


def test_enforce_telemetry_retention_prunes_metrics_and_can_drop_malformed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "metrics.jsonl"
    status_path = tmp_path / "retention-status.json"
    _write_metric_record(path, "metric-1", "2026-01-01T00:00:00Z")
    _write_metric_record(path, "metric-2", "2026-01-02T00:00:00Z")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not-json\n")

    results = enforce_telemetry_retention(
        signal="metrics",
        metrics_path=path,
        trusted_root=tmp_path,
        drop_malformed=True,
        config={
            "metrics": {"enabled": True, "path": str(path)},
            "retention": {
                "enabled": True,
                "status_path": str(status_path),
                "min_keep_records": 0,
                "metrics": {"max_records": 1},
            },
        },
    )

    assert len(results) == 1
    result = results[0]
    assert result.signal == "metrics"
    assert result.status == "pruned"
    assert result.retained_records == 1
    assert result.dropped_records == 1
    assert result.malformed_records == 1
    assert result.malformed_dropped_records == 1
    assert [metric.metric_id for metric in read_metrics(path, trusted_root=tmp_path)] == [
        "metric-2"
    ]
    assert "not-json" not in path.read_text(encoding="utf-8")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["results"][0]["signal"] == "metrics"


def test_telemetry_retention_cli_plans_then_enforces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "events.jsonl"
    status_path = tmp_path / "retention-status.json"
    _write_event_record(path, "event-1", "2026-01-01T00:00:00Z")
    _write_event_record(path, "event-2", "2026-01-02T00:00:00Z")
    monkeypatch.setattr(
        telemetry_cli,
        "_base_telemetry_config",
        lambda: {
            "path": str(path),
            "retention": {
                "enabled": True,
                "status_path": str(status_path),
                "min_keep_records": 0,
                "events": {"max_records": 1},
            },
        },
    )

    plan_rc = telemetry_cli.retention_main(
        [
            "plan",
            "--signal",
            "events",
            "--event-path",
            str(path),
            "--trusted-root",
            str(tmp_path),
            "--json",
        ]
    )

    assert plan_rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True
    assert plan["results"][0]["status"] == "planned"
    assert [event.event_id for event in read_events(path, trusted_root=tmp_path)] == [
        "event-1",
        "event-2",
    ]

    enforce_rc = telemetry_cli.retention_main(
        [
            "enforce",
            "--signal",
            "events",
            "--event-path",
            str(path),
            "--trusted-root",
            str(tmp_path),
            "--json",
        ]
    )

    assert enforce_rc == 0
    enforced = json.loads(capsys.readouterr().out)
    assert enforced["dry_run"] is False
    assert enforced["results"][0]["status"] == "pruned"
    assert enforced["results"][0]["dropped_records"] == 1
    assert [event.event_id for event in read_events(path, trusted_root=tmp_path)] == ["event-2"]
    assert json.loads(status_path.read_text(encoding="utf-8"))["schema_version"] == (
        RETENTION_STATUS_SCHEMA_VERSION
    )


def test_event_rejects_invalid_schema_and_negative_duration() -> None:
    with pytest.raises(ValueError, match="unsupported telemetry schema"):
        TelemetryEvent(
            schema_version="wrong",
            event_id="e1",
            ts="2026-06-28T00:00:00Z",
            event_name="session.started",
            source="ctx-run",
        )
    with pytest.raises(ValueError, match="duration_ms"):
        TelemetryEvent(
            schema_version=SCHEMA_VERSION,
            event_id="e1",
            ts="2026-06-28T00:00:00Z",
            event_name="session.started",
            source="ctx-run",
            duration_ms=-1,
        )
    with pytest.raises(ValueError, match="privacy_mode"):
        TelemetryEvent(
            schema_version=SCHEMA_VERSION,
            event_id="e1",
            ts="2026-06-28T00:00:00Z",
            event_name="session.started",
            source="ctx-run",
            privacy_mode="debug_raw",
        )
    with pytest.raises(ValueError, match="session_hash"):
        TelemetryEvent(
            schema_version=SCHEMA_VERSION,
            event_id="e1",
            ts="2026-06-28T00:00:00Z",
            event_name="session.started",
            source="ctx-run",
            session_hash="sess-raw",
        )


def test_path_containment_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        record_event(
            "session.started",
            source="ctx-run",
            path=tmp_path / ".." / "events.jsonl",
            trusted_root=tmp_path,
            config={"path": str(tmp_path / "events.jsonl")},
        )


def test_default_config_declares_local_only_export_disabled() -> None:
    for path in (Path("src/config.json"), Path("src/ctx/config.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        telemetry = data["telemetry"]
        assert telemetry["enabled"] is True
        assert telemetry["mode"] == "local_redacted"
        assert telemetry["path"] == "~/.ctx/telemetry/events.jsonl"
        assert telemetry["export"]["enabled"] is False
        assert telemetry["export"]["sink"] == "otlp_http"
        assert telemetry["export"]["path"] == "~/.ctx/telemetry/exported-events.jsonl"
        assert telemetry["export"]["otlp"]["endpoint"] == "http://localhost:4318/v1/logs"
        assert telemetry["export"]["otlp"]["allowed_hosts"] == []
        assert telemetry["metrics"]["enabled"] is True
        assert telemetry["metrics"]["path"] == "~/.ctx/telemetry/metrics.jsonl"
        assert telemetry["metrics"]["export"]["enabled"] is False
        assert telemetry["metrics"]["export"]["sink"] == "otlp_http"
        assert telemetry["metrics"]["export"]["path"] == "~/.ctx/telemetry/exported-metrics.jsonl"
        assert telemetry["metrics"]["export"]["otlp"]["endpoint"] == (
            "http://localhost:4318/v1/metrics"
        )
        assert telemetry["metrics"]["export"]["otlp"]["allowed_hosts"] == []
        assert telemetry["privacy"]["store_raw_inputs"] is False
        assert telemetry["privacy"]["hash_identifiers"] is True
        assert telemetry["privacy"]["hash_salt_env"] == "CTX_TELEMETRY_HASH_SALT"
        assert telemetry["privacy"]["hash_salt_path"] == "~/.ctx/telemetry/hash-salt"
        assert telemetry["retention"]["enabled"] is True
        assert telemetry["retention"]["status_path"] == ("~/.ctx/telemetry/retention-status.json")
        assert telemetry["retention"]["min_keep_records"] == 1000
        assert telemetry["retention"]["drop_malformed"] is False
        assert telemetry["retention"]["events"]["max_age_days"] == 90
        assert telemetry["retention"]["events"]["max_records"] == 100000
        assert telemetry["retention"]["metrics"]["max_age_days"] == 30
        assert telemetry["retention"]["metrics"]["max_records"] == 200000
