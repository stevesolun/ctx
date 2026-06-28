from __future__ import annotations

from dataclasses import asdict
from io import BytesIO
import json
import os
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
    hash_identifier,
    plan_telemetry_retention,
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

    got = list(read_events(path, trusted_root=tmp_path))
    assert len(got) == 1
    assert got[0].event_id == event.event_id
    assert got[0].session_hash == event.session_hash
    assert got[0].trace_id == event.trace_id
    assert got[0].span_id == event.span_id
    assert got[0].ctx_version == event.ctx_version


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
    if os.name != "nt":
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
    if os.name == "nt":
        pytest.skip("POSIX mode bits are not portable on Windows")
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
    attributes = {
        item["key"]: item["value"]
        for item in log_record["attributes"]
    }
    assert attributes["event.name"] == {"stringValue": "ctx.mcp.request"}
    assert attributes["ctx.outcome"] == {"stringValue": "error"}
    assert attributes["error.type"] == {"stringValue": "method_not_found"}
    assert "ctx.session_id" not in attributes
    assert attributes["ctx.session.hash"]["stringValue"].startswith("sha256:")
    assert attributes["ctx.version"]["stringValue"]
    assert "ctx.payload.query_hash" in attributes
    assert "private acme query" not in json.dumps(payload)
    assert "sess-otlp-private" not in json.dumps(payload)


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
    attributes = {
        item["key"]: item["value"]
        for item in log_record["attributes"]
    }
    assert "ctx.session_id" not in attributes
    assert attributes["ctx.session.hash"] == {
        "stringValue": hash_identifier("legacy-session-private", salt="tenant-a")
    }
    assert "legacy-session-private" not in json.dumps(payload)


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
    if os.name != "nt":
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
    if os.name != "nt":
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
    assert [event.event_id for event in read_events(path, trusted_root=tmp_path)] == [
        "event-2"
    ]
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
        assert telemetry["retention"]["status_path"] == (
            "~/.ctx/telemetry/retention-status.json"
        )
        assert telemetry["retention"]["min_keep_records"] == 1000
        assert telemetry["retention"]["drop_malformed"] is False
        assert telemetry["retention"]["events"]["max_age_days"] == 90
        assert telemetry["retention"]["events"]["max_records"] == 100000
        assert telemetry["retention"]["metrics"]["max_age_days"] == 30
        assert telemetry["retention"]["metrics"]["max_records"] == 200000
