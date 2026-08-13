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
- `ctx.api.recommend_related`
- `ctx.mcp.request`
- `ctx.mcp.external_tool_call`
- `ctx.core.recommend_bundle`
- `ctx.core.recommend_related`
- `ctx.runtime_lifecycle.record`
- `ctx.cli.run`
- `ctx.cli.resume`

Outcome and dimensions live in attributes such as `otel.status_code`,
`ctx.operation`, `ctx.tool.name`, `ctx.result.count`,
`ctx.selection.selected.count`, `ctx.selection.rejected.count`,
`ctx.selection.source`, `ctx.selection.selected`, `ctx.usage.attribution`, and
failure/correlation attributes such as `ctx.run.failure_stage`,
`ctx.session.previous_trace_id`, `ctx.traceparent.received`, and hashed
identifiers like `ctx.query.hash`, `ctx.slug.hash`, or `ctx.session.hash`.

Every recorded event gets a generated OpenTelemetry-compatible `trace_id` and
`span_id` when the caller does not provide one. The local envelope also keeps
`parent_span_id` when a span was parented to another ctx span or to inbound MCP
`traceparent` metadata. OTLP export maps trace and span ids to the log record
`traceId` and `spanId` fields and also includes ctx release provenance as
`ctx.version`. In installed wheels this comes from package metadata; in source
checkouts it falls back to `ctx.__version__`.

The `ctx run` session log stores the initial CLI trace id so `ctx resume` can
emit `ctx.session.previous_trace_id` without exposing raw prompts. When the
generic MCP router calls an external MCP server, it records
`ctx.mcp.external_tool_call`, injects W3C `traceparent` plus a hashed session
correlator into JSON-RPC `_meta`, and the ctx MCP server parents request spans
to valid inbound trace metadata.

## Trace Shape

`export_traces()` projects the redacted event spool into a real OTLP/HTTP traces
signal at `/v1/traces`. The payload uses
`resourceSpans[].scopeSpans[].spans[]` with hexadecimal `traceId`, `spanId`, and
optional `parentSpanId`, nanosecond start/end timestamps, span status, resource
attributes, and sanitized span attributes.

Events that share one `trace_id` and `span_id` are emitted as one OTLP span.
Trace export preserves those original ids, including `parent_span_id`, so trace
spans correlate exactly with OTLP log records and retain their parent topology.
The first event names the span and every grouped record appears under its
`events` array. Start time is derived from each record's timestamp and
`duration_ms`; end time is the latest grouped event timestamp. ctx never
invents trace context during export: records with missing or invalid trace/span
ids fail both preview and export without advancing the checkpoint.

Because the local event envelope has no explicit span-close marker, trace
export uses a deterministic maturity high-water mark. A span is eligible only
after its latest event is at least five seconds old. The delay is configured as
`traces.export.span_maturity_seconds`, is capped at 300 seconds, and can be set
to zero only when an operator explicitly accepts immediate maturity. Export
advances across a complete prefix of mature span groups; younger or crossing
groups remain in the tail with `status: pending` and a `retained_events` count.
The high-water mark is evaluated on each preview or export invocation; ctx does
not start a background timer.

If an event later reuses a span id already before the checkpoint, ctx does not
emit that span a second time: it retains the event, reports `status: degraded`,
and increments `late_span_events`.

Trace export reads the same retained event spool but has independent delivery
state:

- `~/.ctx/telemetry/events.jsonl.trace-export-checkpoint.json`
- `~/.ctx/telemetry/events.jsonl.trace-export-status.json`

Log, trace, and metric retries therefore cannot advance one another's
checkpoints.

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

Runtime lifecycle token usage emits OTel-style metric names when a host records
`ctx__mark_entity_used.token_usage`:

- `ctx.tool_usage.records` counts usage records by entity type and attribution.
- `ctx.tool_usage.input_tokens` and `ctx.tool_usage.output_tokens` count reported
  provider input and output tokens.
- `ctx.tool_usage.cached_input_tokens` and
  `ctx.tool_usage.uncached_input_tokens` split reported input usage when the
  provider exposes cache-read counts.
- `ctx.tool_usage.tokens` counts total tokens when the record has a
  non-negative `total_tokens` value.
- `ctx.tool_usage.tokens_per_record` observes the same total as a histogram.

Those metric points are recorded inside the same telemetry span as the
`ctx.runtime_lifecycle.record` event that describes the usage, so trace/span
correlation stays aligned across event and metric signals. The low-cardinality
`ctx.usage.tokens_reported` attribute distinguishes explicit zero usage from an
unavailable provider report.

Attribution is always explicit through `ctx.usage.attribution`:
`exact`, `estimated`, or `unavailable`. The built-in `ctx run` provider totals
are session-scoped and are labeled as unavailable for per-tool attribution; ctx
does not split session totals across selected tools. Exact per-tool rows require
the host or runner to provide usage for that specific entity.

## Privacy Defaults

The shipped config keeps telemetry local. The optional trace block shown here
also defaults to export disabled:

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
    "traces": {
      "enabled": true,
      "export": {
        "enabled": false,
        "span_maturity_seconds": 5,
        "sink": "otlp_http",
        "otlp": {
          "endpoint": "http://localhost:4318/v1/traces",
          "allowed_hosts": []
        }
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
`tool_input`, `stdout`, `stderr`, repo names, secrets, `paths`, dotted or
hyphenated path keys such as `ctx.repo.path` and `repo-path`, and keys that
normalize to `_path` or `_paths` suffixes. It also scans arbitrary string
payload values for local host paths such as `/Users/...`, `/home/...`, `~/...`,
and Windows drive paths, replacing each match with
`[path_hash:sha256:...]`. The only accepted modes are `local_redacted`,
`disabled`, `off`, and `none`; unknown modes fail closed instead of emitting
raw fields.

Runtime lifecycle records apply the same local-redacted posture to their
evidence-bearing strings. Top-level `reason`, `evidence`, `command`, `summary`,
`trigger`, and `status` values, plus nested security-scan text, are stored only
after secret and local path redaction.

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

Preview or export traces with the same CLI:

```bash
ctx-telemetry-export \
  --signal traces \
  --dry-run \
  --sink otlp_http \
  --otlp-endpoint https://collector.example:4318/v1/traces \
  --otlp-allowed-host collector.example \
  --json

ctx-telemetry-export \
  --signal traces \
  --sink otlp_http \
  --otlp-endpoint https://collector.example:4318/v1/traces \
  --otlp-allowed-host collector.example
```

The equivalent Python API is:

```python
from pathlib import Path

from ctx.telemetry import export_traces, preview_traces_export

config = {
    "traces": {
        "enabled": True,
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
    Path("~/.ctx/telemetry/events.jsonl"),
    config=config,
)
result = export_traces(
    Path("~/.ctx/telemetry/events.jsonl"),
    config=config,
)
```

Both trace export surfaces build and validate the real OTLP payload. They
re-sanitize every outbound string and attribute at the network boundary, hash
raw legacy session ids, and apply the same HTTPS, host allow-list, header,
timeout, and no-redirect policy as logs and metrics. `Content-Type:
application/json` and `Content-Encoding: identity` are mandatory and cannot be
overridden through configured headers. Trace export is disabled when
`traces.export.enabled` is absent or false for direct API calls. An explicit
`ctx-telemetry-export --signal traces` invocation enables the trace exporter for
that run, matching the existing event and metric CLI behavior.

Trace `attempted`, `exported`, and `failed` counts are mature OTLP spans, not
source events. `ctx.trace.event_count` records how many source events are inside
each span. A populated `200` OTLP
`partialSuccess.rejectedSpans` response is non-retryable: ctx records
`exported = attempted - rejectedSpans`, `failed = rejectedSpans`,
`status: partial_success`, a sanitized collector message when present, and
advances the checkpoint for the acknowledged request only when no malformed
pending local records exist. With malformed pending records, ctx preserves the
prior checkpoint so those records remain visible and repairable; accepted spans
may be resent after repair.

The direct OTLP client retries `429`, `502`, `503`, `504`, `URLError`, timeout,
and connection-disconnect failures up to two times by default. It honors
`Retry-After`; otherwise it uses bounded exponential backoff with jitter. Each
delay is capped at five seconds. Override these limits with
`max_retries`, `retry_backoff_seconds`, `retry_jitter_ratio`, and
`max_retry_delay_seconds` inside the signal's `export.otlp` block.
Collector response bodies are read through a 64 KiB limit and oversized
responses fail closed.

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

`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` overrides the configured traces endpoint.
`OTEL_EXPORTER_OTLP_ENDPOINT` appends `/v1/traces` for trace exports.

Successful exports advance an owner-only checkpoint file. By default it lives
next to the spool as `events.jsonl.export-checkpoint.json`, so later runs export
only new events. Use `--checkpoint /path/to/checkpoint.json` to choose another
checkpoint file, or `--all` when you intentionally want to replay the full spool.

The command exits non-zero if the selected exporter or trace preview validation
fails. Use
`--fail-on-degraded` when running from cron or CI and you also want malformed
pending records or checkpoint anomalies to fail the command. Real export
attempts also write an owner-only status file next to the spool as
`events.jsonl.export-status.json`. It records an explicit `status` of `ok`,
`noop`, `pending`, `partial_success`, `failed`, or `degraded`, plus the
sink, destination hash,
attempted/exported/failed counts, checkpoint-before/checkpoint-after ids,
whether the checkpoint advanced, malformed pending record counts, and the last
exporter error kind. Trace status also records `retained_events`,
`late_span_events`, and only a sanitized collector `error_message`.

`degraded` means the exporter delivered the well-formed events it could, but ctx
detected a condition an operator should inspect, such as malformed pending local
records or a checkpoint id that no longer appears in the spool. A fully
accepted trace request does not advance past malformed pending records; retrying
after repair may re-export already delivered spans, so downstream collectors
should deduplicate by `ctx.event_id`. The same checkpoint barrier applies to
`partial_success`: accepted spans may be resent, but malformed local evidence
cannot be stranded behind an advanced checkpoint.

Manual and continuous exporters re-sanitize legacy local records at the export
boundary before writing `local_jsonl` or sending OTLP, so older spool entries
that predate current redaction rules do not leak raw query, path, repo, stdout,
stderr, token, or secret payload values.

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

Continuous log and metric export drains the pending spool before advancing the
checkpoint, not just the record that triggered the write. Trace export advances
only through the mature high-water prefix. A fully or partially accepted request
advances to that prefix's final event only when no malformed pending records
exist. Younger trace events stay pending. With malformed pending records, ctx
keeps its prior checkpoint; a fully accepted request reports `status: degraded`
and a populated partial-success response reports `status: partial_success`. A
checkpoint anomaly is reported as degraded while a successful mature prefix can
establish a replacement checkpoint. Reopened closed-span events remain in the
tail for operator inspection.

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
    traces:
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
    },
    "traces": {
      "export": {
        "enabled": true,
        "sink": "otlp_http",
        "otlp": {
          "endpoint": "https://otel-gateway.example.com:4318/v1/traces",
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
    traces:
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
| Request traces | trace spans grouped by `service.name`, span name, status, and `ctx.source` |
| API latency | histogram metric `ctx.api.duration` by `ctx.operation` |
| CLI/runtime usage | count logs for `ctx.cli.run`, `ctx.cli.resume`, `ctx.runtime_lifecycle.record`, `ctx.mcp.external_tool_call` |
| Exporter health | status JSON fields `status`, `attempted`, `exported`, `failed`, `malformed_pending_records`, `error_kind` |
| Spool growth | `event_count`, `malformed_records`, and checkpoint age from `/api/status.json` |

Recommended enterprise alerts:

| Alert | Condition |
|---|---|
| `CtxTelemetryExporterFailed` | latest export status is `failed` for 2 consecutive runs |
| `CtxTelemetryExporterDegraded` | latest export status is `degraded` or `malformed_pending_records > 0` |
| `CtxTelemetrySilent` | telemetry is enabled but no new event appears during an expected active window |
| `CtxTelemetrySpoolGrowing` | local spool count grows while checkpoint id stays unchanged |
| `CtxTelemetryUnhandledExceptions` | new `ctx.exception.fingerprint` appears in prod |

For local dashboard checks, use:

```bash
python -m ctx_monitor serve
curl -fsS http://127.0.0.1:8765/api/status.json
```

The monitor surfaces telemetry health, spool counts, malformed counts,
checkpoint presence, exporter attempted/exported/failed counts, and exporter
`error_kind` without rendering event payloads.

## Operator Runbook

1. Confirm config posture:
   `ctx-telemetry-export --dry-run --json`.
2. Inspect dashboard health:
   `curl -fsS http://127.0.0.1:8765/api/status.json`.
3. Inspect exporter status:
   `cat ~/.ctx/telemetry/events.jsonl.export-status.json`,
   `cat ~/.ctx/telemetry/events.jsonl.trace-export-status.json`, and
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
latest event name/outcome, checkpoint presence, exporter
attempted/exported/failed counts, and exporter `error_kind`.
The monitor does not render telemetry payloads.
