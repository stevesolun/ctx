# Enterprise Telemetry

ctx records privacy-first operational telemetry for API, MCP, CLI, core-tool, and
runtime lifecycle boundaries. The default mode is local and redacted: events are
written to `~/.ctx/telemetry/events.jsonl`, metric points are written separately
to `~/.ctx/telemetry/metrics.jsonl`, raw prompts and queries are hashed or
removed, and network export is disabled unless an operator explicitly enables it
or runs an export.

## Event Shape

Events use the `ctx.telemetry.v1` envelope and OpenTelemetry-style naming:

- `ctx.api.recommend_bundle`
- `ctx.mcp.request`
- `ctx.core.recommend_bundle`
- `ctx.runtime_lifecycle.record`
- `ctx.cli.run`
- `ctx.cli.resume`

Outcome and dimensions live in attributes such as `otel.status_code`,
`ctx.operation`, `ctx.tool.name`, `ctx.result.count`, and hashed identifiers like
`ctx.query.hash`, `ctx.slug.hash`, or `ctx.session.hash`.

Every recorded event gets a generated OpenTelemetry-compatible `trace_id` and
`span_id` when the caller does not provide one. OTLP export maps those to the
log record `traceId` and `spanId` fields and also includes ctx release
provenance as `ctx.version`.

## Metric Shape

Metrics use the `ctx.telemetry.metrics.v1` envelope and the same local redaction
rules as events. `record_counter()` writes monotonic counter points and
`record_histogram()` writes histogram observations. OTLP export maps counters to
delta `sum` metrics and observations to delta `histogram` metrics under
`resourceMetrics`.

Metrics use a separate spool, checkpoint, and status file:

- `~/.ctx/telemetry/metrics.jsonl`
- `~/.ctx/telemetry/metrics.jsonl.export-checkpoint.json`
- `~/.ctx/telemetry/metrics.jsonl.export-status.json`

Metric checkpointing is independent from event/log checkpointing, so replaying
or repairing one signal does not advance the other.

## Privacy Defaults

The shipped config keeps telemetry local:

```json
{
  "telemetry": {
    "enabled": true,
    "mode": "local_redacted",
    "path": "~/.ctx/telemetry/events.jsonl",
    "export": {
      "enabled": false,
      "sink": "otlp_http",
      "otlp": {
        "endpoint": "http://localhost:4318/v1/logs",
        "allowed_hosts": []
      }
    },
    "metrics": {
      "enabled": true,
      "path": "~/.ctx/telemetry/metrics.jsonl",
      "export": {
        "enabled": false,
        "sink": "otlp_http",
        "path": "~/.ctx/telemetry/exported-metrics.jsonl",
        "otlp": {
          "endpoint": "http://localhost:4318/v1/metrics",
          "allowed_hosts": []
        }
      }
    },
    "privacy": {
      "store_raw_inputs": false,
      "hash_identifiers": true,
      "hash_salt_env": "CTX_TELEMETRY_HASH_SALT",
      "hash_salt_path": "~/.ctx/telemetry/hash-salt"
    },
    "retention": {
      "enabled": true,
      "status_path": "~/.ctx/telemetry/retention-status.json",
      "min_keep_records": 1000,
      "drop_malformed": false,
      "events": {
        "max_age_days": 90,
        "max_records": 100000
      },
      "metrics": {
        "max_age_days": 30,
        "max_records": 200000
      }
    }
  }
}
```

`local_redacted` removes or hashes raw input fields such as `query`, `prompt`,
`tool_input`, `stdout`, `stderr`, paths, repo names, and secrets. The only
accepted modes are `local_redacted`, `disabled`, `off`, and `none`; unknown modes
fail closed instead of emitting raw fields.

Local JSONL records retain the top-level `session_id` field for compatibility
with existing local-only workflows, but they also include a salted
`session_hash`. Remote OTLP export sends `ctx.session.hash` and never sends the
raw session id, including when exporting legacy local records that predate
`session_hash`. Manual `local_jsonl` export preserves the same local envelope,
so treat exported JSONL as local-sensitive if session ids are present.

Identifier hashes are salted by default. ctx first checks the
`CTX_TELEMETRY_HASH_SALT` environment variable, then any configured
`privacy.hash_salt`, then an owner-only local salt file at
`~/.ctx/telemetry/hash-salt`. Set `CTX_TELEMETRY_HASH_SALT` per tenant or
deployment when multiple hosts need to correlate the same redacted identifiers.
Do not commit a literal `privacy.hash_salt` into shared source control.

## Manual Export

Use `ctx-telemetry-export` to export an existing local spool without changing
the default privacy posture.

Export to another JSONL file:

```bash
ctx-telemetry-export \
  --sink local_jsonl \
  --output /var/log/ctx/exported-events.jsonl
```

Export to an OpenTelemetry Collector logs endpoint:

```bash
ctx-telemetry-export \
  --sink otlp_http \
  --otlp-endpoint https://collector.example:4318/v1/logs \
  --otlp-allowed-host collector.example
```

Remote OTLP endpoints must use `https://` and their host must be listed in
`telemetry.export.otlp.allowed_hosts`, or provided for a one-off command with
`--otlp-allowed-host`. Plain `http://` is accepted only for loopback collectors
(`localhost`, `127.0.0.1`, or `[::1]`). Literal metadata/link-local, multicast,
unspecified, and reserved IP endpoints are rejected even when a command-line or
`OTEL_EXPORTER_OTLP_*` endpoint override is used. Redirects are refused instead
of revalidating a second network target.

Preview the event count without exporting:

```bash
ctx-telemetry-export --dry-run --json
```

Export metrics with the same CLI:

```bash
ctx-telemetry-export \
  --signal metrics \
  --sink otlp_http \
  --otlp-endpoint https://collector.example:4318/v1/metrics \
  --otlp-allowed-host collector.example
```

Export metrics from Python:

```python
from pathlib import Path

from ctx.telemetry import export_metrics, record_counter, record_histogram

record_counter("ctx.api.requests", attributes={"ctx.source": "api"})
record_histogram("ctx.api.duration", value=42.0, unit="ms")

result = export_metrics(
    Path("~/.ctx/telemetry/metrics.jsonl"),
    config={
        "metrics": {
            "enabled": True,
            "export": {
                "enabled": True,
                "sink": "otlp_http",
                "otlp": {
                    "endpoint": "https://collector.example:4318/v1/metrics",
                    "allowed_hosts": ["collector.example"],
                },
            },
        }
    },
)
```

Metrics export uses its own checkpoint and status files next to
`metrics.jsonl`, and the `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` environment
variable overrides the configured metrics endpoint. `OTEL_EXPORTER_OTLP_ENDPOINT`
is also supported and automatically appends `/v1/metrics` for metric exports.

Successful exports advance an owner-only checkpoint file. By default it lives
next to the spool as `events.jsonl.export-checkpoint.json`, so later runs export
only new events. Use `--checkpoint /path/to/checkpoint.json` to choose another
checkpoint file, or `--all` when you intentionally want to replay the full spool.

The command exits non-zero if the selected exporter fails. Use
`--fail-on-degraded` when running from cron or CI and you also want malformed
pending records or checkpoint anomalies to fail the command. Real export
attempts also write an owner-only status file next to the spool as
`events.jsonl.export-status.json`. It records an explicit `status` of `ok`,
`noop`, `failed`, or `degraded`, plus the sink, destination hash,
attempted/exported/failed counts, checkpoint-before/checkpoint-after ids,
whether the checkpoint advanced, malformed pending record counts, and the last
exporter error kind.

`degraded` means the exporter delivered the well-formed events it could, but ctx
detected a condition an operator should inspect, such as malformed pending local
records or a checkpoint id that no longer appears in the spool. ctx does not
advance the checkpoint past malformed pending records; retrying after repair may
re-export already delivered events, so downstream collectors should deduplicate
by `event_id`.

## Retention

Retention is explicit operator action, never a background deletion. Plan first:

```bash
ctx-telemetry-retention plan --signal all --json
```

Then enforce:

```bash
ctx-telemetry-retention enforce --signal all --json
```

Malformed JSONL records are preserved by default so an operator can inspect and
repair them. Use `--drop-malformed` only when the malformed lines have already
been preserved elsewhere. Every enforcement run writes an owner-only status file
at `~/.ctx/telemetry/retention-status.json`.

## Continuous Export

To export every new event as it is recorded, enable the exporter in config. User
overrides live at `~/.claude/skill-system-config.json`.

```json
{
  "telemetry": {
    "export": {
      "enabled": true,
      "sink": "otlp_http",
      "otlp": {
        "endpoint": "https://collector.example:4318/v1/logs",
        "allowed_hosts": ["collector.example"],
        "service_name": "ctx",
        "service_namespace": "ctx",
        "deployment_environment": "prod"
      }
    }
  }
}
```

`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` overrides the configured logs endpoint.
`OTEL_EXPORTER_OTLP_ENDPOINT` is also supported and automatically appends
`/v1/logs`.

## Collector Examples

Use an OpenTelemetry Collector between ctx and the observability backend. That
keeps ctx vendor-neutral and gives enterprise operators one place to add
batching, retry, TLS, auth, routing, and backend-specific exporters.

Local loopback smoke config:

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:4318

processors:
  batch:

exporters:
  debug:
    verbosity: detailed

service:
  pipelines:
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
```

Run it, then point ctx at the local Collector:

```bash
otelcol --config=file:ctx-otel-local.yaml

ctx-telemetry-export \
  --sink otlp_http \
  --otlp-endpoint http://127.0.0.1:4318/v1/logs \
  --otlp-allowed-host 127.0.0.1
```

For production, keep ctx pointed at a tenant-approved HTTPS Collector and
allow-list only that host:

```json
{
  "telemetry": {
    "export": {
      "enabled": true,
      "sink": "otlp_http",
      "otlp": {
        "endpoint": "https://otel-gateway.example.com:4318/v1/logs",
        "allowed_hosts": ["otel-gateway.example.com"],
        "service_name": "ctx",
        "service_namespace": "ctx",
        "deployment_environment": "prod"
      }
    },
    "metrics": {
      "export": {
        "enabled": true,
        "sink": "otlp_http",
        "otlp": {
          "endpoint": "https://otel-gateway.example.com:4318/v1/metrics",
          "allowed_hosts": ["otel-gateway.example.com"]
        }
      }
    }
  }
}
```

Production Collector sketch:

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318

processors:
  memory_limiter:
    check_interval: 5s
    limit_mib: 512
  batch:

exporters:
  otlphttp/vendor:
    endpoint: https://observability-vendor.example.com/otlp
    headers:
      Authorization: ${env:OTEL_VENDOR_AUTH_HEADER}

service:
  pipelines:
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [otlphttp/vendor]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [otlphttp/vendor]
```

Keep the Collector's public listener and upstream exporter TLS/auth policy under
the enterprise network owner. ctx still rejects non-loopback plaintext OTLP
endpoints before it sends data.

## Dashboards And Alerts

Build the first production dashboard from the stable ctx attributes instead of
raw message text:

| Panel | Query dimensions |
|---|---|
| Request volume | count logs grouped by `event.name`, `ctx.source`, `ctx.operation` |
| Error rate | count logs where `otel.status_code = ERROR`, grouped by `ctx.source` |
| Exception fingerprints | count logs grouped by `ctx.exception.fingerprint`, `ctx.exception.type` |
| API latency | histogram metric `ctx.api.duration` by `ctx.operation` |
| CLI/runtime usage | count logs for `ctx.cli.run`, `ctx.cli.resume`, `ctx.runtime_lifecycle.record` |
| Exporter health | status JSON fields `status`, `malformed_pending_count`, `last_error_kind` |
| Spool growth | `event_count`, `malformed_count`, and checkpoint age from `/api/status.json` |

Recommended enterprise alerts:

| Alert | Condition |
|---|---|
| `CtxTelemetryExporterFailed` | latest export status is `failed` for 2 consecutive runs |
| `CtxTelemetryExporterDegraded` | latest export status is `degraded` or `malformed_pending_count > 0` |
| `CtxTelemetrySilent` | telemetry is enabled but no new event appears during an expected active window |
| `CtxTelemetrySpoolGrowing` | local spool count grows while checkpoint id stays unchanged |
| `CtxTelemetryUnhandledExceptions` | new `ctx.exception.fingerprint` appears in prod |

For local dashboard checks, use:

```bash
ctx-monitor serve
curl -fsS http://127.0.0.1:8765/api/status.json
```

The monitor surfaces telemetry health, spool counts, malformed counts,
checkpoint presence, and exporter status without rendering event payloads.

## Operator Runbook

1. Confirm config posture:
   `ctx-telemetry-export --dry-run --json`.
2. Inspect dashboard health:
   `curl -fsS http://127.0.0.1:8765/api/status.json`.
3. Inspect exporter status:
   `cat ~/.ctx/telemetry/events.jsonl.export-status.json` and
   `cat ~/.ctx/telemetry/metrics.jsonl.export-status.json`.
4. Repair malformed local records by preserving the original file, removing only
   invalid JSON lines, and rerunning export with `--fail-on-degraded`.
5. Replay intentionally with `--all` only after confirming the downstream
   backend deduplicates by `event_id`.
6. Rotate tenant salts only during a planned privacy reset; rotation breaks
   cross-run correlation for hashed identifiers and exception fingerprints.

## Verification

For a local smoke test, run:

```bash
ctx-telemetry-export --dry-run --json
ctx-telemetry-export --sink local_jsonl --output /tmp/ctx-telemetry-export.jsonl --json
ctx-telemetry-export --all --sink local_jsonl --output /tmp/ctx-telemetry-replay.jsonl --json
```

The exported JSONL should contain the same event ids as the local spool and no
raw prompt, query, path, repo, stdout, stderr, token, or secret values.

Inspect the durable exporter status after a real run:

```bash
cat ~/.ctx/telemetry/events.jsonl.export-status.json
```

The ctx monitor also surfaces the same local health summary on `/status` and
`/api/status.json`: capture enabled/mode, spool event and malformed counts, the
latest event name/outcome, checkpoint presence, and the last exporter status.
The monitor does not render telemetry payloads.
