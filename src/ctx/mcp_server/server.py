"""ctx.mcp_server.server — expose ctx-core as a standalone MCP server.

Runs as a subprocess, reads JSON-RPC 2.0 frames on stdin, writes
responses on stdout. Any MCP-aware host (Cline, Goose, Claude Agent
SDK, Claude Code itself, OpenHands, a custom harness, ...) can attach
this server and gain access to the ctx-core recommendation surface
WITHOUT using our ``ctx run`` harness loop.

Why ship this as a byproduct of H7?
    Historical Plan 001 §3 compared library (A), full harness (B),
    and hybrid (C). User picked B in the locked decisions,
    but Option A's deliverable (a standalone MCP server exposing the
    skill/graph system) is strictly additive and doubles the surface
    the project reaches. H7's CtxCoreToolbox already has the read-only
    tool wiring; this phase is a thin MCP adapter around it.

Install as a command into Claude Code:

    claude mcp add ctx-wiki -- ctx-mcp-server

Install into any MCP-aware host:

    <host-specific config pointing at the ``ctx-mcp-server`` binary>

Permissioned adapters can pass ``--allow-tools`` and ``--entity-types`` to
expose only selected ctx tools and entity types.

Tools exposed (same as ctx.adapters.generic.ctx_core_tools — that
module is the source of truth for the tool catalogue):

    ctx__recommend_bundle(query, top_k=5, selected=None, rejected=None, ...)
    ctx__recommend_related(selected, rejected=None, max_hops=2, top_n=5)
    ctx__graph_query(seeds, max_hops=2, top_n=10)
    ctx__wiki_search(query, top_n=15)
    ctx__wiki_get(slug)
    ctx__observe_dev_event(...)
    ctx__load_entity(...)
    ctx__mark_entity_used(...)
    ctx__record_validation(...)
    ctx__record_escalation(...)
    ctx__unload_entity(...)
    ctx__session_end(...)
    ctx__session_state(...)

Protocol coverage mirrors H2's client implementation — the minimal
operational subset: initialize + initialized notification +
tools/list + tools/call + shutdown. Plus the server-only extras:

    * serverInfo reported in the initialize response
    * tools capability declared
    * notifications/cancelled accepted (no-op for this server since
      tool calls are synchronous)

NOT covered (deferred until users ask):
    * resources/* (resource-aware flows)
    * prompts/* (MCP prompt templates)
    * Progress notifications from the server during long calls —
      ctx-core queries are fast (<1s) so no streaming yet
    * Logging notifications — we emit diagnostic stderr instead

Plan 001 Phase H8.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Iterable
from uuid import uuid4

from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox
from ctx.adapters.generic.providers import ToolCall
from ctx.core.entity_types import RECOMMENDABLE_ENTITY_TYPES, normalize_entity_type
from ctx.telemetry import (
    hash_identifier,
    parse_traceparent,
    record_event,
    record_exception,
    telemetry_span,
)


_logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2024-11-05"
_SERVER_NAME = "ctx-wiki"
_SERVER_VERSION = "0.1.0"


# JSON-RPC error codes per the MCP / JSON-RPC 2.0 spec.
class _ErrorCode:
    PARSE_ERROR = -32700  # invalid JSON
    INVALID_REQUEST = -32600  # not a well-formed request
    METHOD_NOT_FOUND = -32601  # method unknown
    INVALID_PARAMS = -32602  # params don't match method
    INTERNAL_ERROR = -32603  # something inside our handler exploded


@dataclass
class _ServerState:
    """Mutable state for one server instance (one stdin/stdout pair)."""

    initialized: bool = False
    toolbox: CtxCoreToolbox | None = None
    allowed_tool_names: frozenset[str] | None = None
    allowed_entity_types: frozenset[str] | None = None
    recommendation_session_id: str = field(default_factory=lambda: f"mcp-{uuid4().hex}")

    def ensure_toolbox(self) -> CtxCoreToolbox:
        if self.toolbox is None:
            self.toolbox = CtxCoreToolbox(
                recommendation_session_id=self.recommendation_session_id,
                allowed_tool_names=self.allowed_tool_names,
                allowed_entity_types=self.allowed_entity_types,
            )
        return self.toolbox


# ── Request handlers ──────────────────────────────────────────────────────


def _handle_initialize(state: _ServerState, params: dict[str, Any]) -> dict[str, Any]:
    """Respond to the client's initialize handshake.

    We accept any ``protocolVersion`` string the client sends — MCP
    versions are date-stamped so a mismatch isn't inherently fatal,
    and our tool catalogue uses only the stable tools/* API.
    """
    state.initialized = True
    return {
        "protocolVersion": _PROTOCOL_VERSION,
        "capabilities": {
            # We advertise tools but not resources / prompts / sampling.
            "tools": {},
        },
        "serverInfo": {
            "name": _SERVER_NAME,
            "version": _SERVER_VERSION,
        },
    }


def _handle_tools_list(state: _ServerState, params: dict[str, Any]) -> dict[str, Any]:
    """Return the ctx-core tool catalogue in MCP schema."""
    toolbox = state.ensure_toolbox()
    tools: list[dict[str, Any]] = []
    for td in toolbox.tool_definitions():
        tools.append(
            {
                "name": td.name,
                "description": td.description,
                "inputSchema": td.parameters,
            }
        )
    return {"tools": tools}


def _handle_tools_call(state: _ServerState, params: dict[str, Any]) -> dict[str, Any]:
    """Execute one tool call.

    MCP's tools/call response is:
      {"content": [<content block>, ...], "isError": <bool>}

    Our dispatch returns a JSON-encoded string from CtxCoreToolbox
    (shape: {"results": [...]} or {"error": "..."}). We wrap it as a
    single text content block. Tool-level errors (e.g. missing wiki
    dir) are returned as isError=True so the model sees the diagnostic.
    """
    toolbox = state.ensure_toolbox()
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise _JsonRpcError(_ErrorCode.INVALID_PARAMS, "params.name is required")

    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise _JsonRpcError(_ErrorCode.INVALID_PARAMS, "params.arguments must be an object")

    if not toolbox.owns(name) or not toolbox.allows(name):
        raise _JsonRpcError(
            _ErrorCode.METHOD_NOT_FOUND,
            f"tool not found: {name!r}. Known prefix: ctx__",
        )

    try:
        result_json = toolbox.dispatch(ToolCall(id="mcp", name=name, arguments=dict(arguments)))
    except ValueError as exc:
        # Unknown ctx-core subtool, malformed args, etc.
        return {
            "content": [{"type": "text", "text": f"error: {exc}"}],
            "isError": True,
        }
    except Exception as exc:  # noqa: BLE001
        # Bug inside ctx-core — surface as isError so the client keeps
        # running but the model sees the diagnostic.
        _logger.error("ctx-core dispatch failed for %s", name, exc_info=True)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (f"internal error in {name}: {type(exc).__name__}: {exc}"),
                }
            ],
            "isError": True,
        }

    # The dispatcher itself signals errors via {"error": ...} in the
    # JSON. Surface those as isError=True so the client sees them
    # structurally, not just as a string the model must parse.
    try:
        parsed = json.loads(result_json)
    except json.JSONDecodeError:
        parsed = None
    is_error = bool(isinstance(parsed, dict) and parsed.get("error"))

    return {
        "content": [{"type": "text", "text": result_json}],
        "isError": is_error,
    }


# ── Dispatch table ────────────────────────────────────────────────────────


_HANDLERS = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
}

# Notifications (no response expected). ping keeps the connection
# alive; cancelled is sent by clients that abort a pending request.
_NOTIFICATIONS = {
    "notifications/initialized",
    "notifications/cancelled",
    "notifications/progress",
    "ping",
}


def _duration_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _hash_json_value(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hash_identifier(encoded)


def _request_id_type(frame: dict[str, Any]) -> str | None:
    if "id" not in frame:
        return None
    return type(frame.get("id")).__name__


def _safe_tool_argument_payload(arguments: object) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {}
    payload: dict[str, Any] = {
        "ctx.arguments.keys": sorted(str(key) for key in arguments),
    }
    for key in ("top_k", "top_n", "max_hops"):
        value = arguments.get(key)
        if isinstance(value, (int, float)):
            payload[f"ctx.arguments.{key}"] = value
    query = arguments.get("query")
    if isinstance(query, str):
        payload["ctx.query.hash"] = hash_identifier(query)
        payload["ctx.query.length"] = len(query)
    seeds = arguments.get("seeds")
    if isinstance(seeds, (list, tuple)):
        payload["ctx.seeds.count"] = len(seeds)
        payload["ctx.seeds.hash"] = _hash_json_value(list(seeds))
    slug = arguments.get("slug")
    if isinstance(slug, str):
        payload["ctx.slug.hash"] = hash_identifier(slug)
    entity_type = arguments.get("entity_type")
    if isinstance(entity_type, str):
        payload["ctx.entity.type"] = entity_type
    return payload


def _safe_mcp_payload(
    method: str | None,
    params: object,
    *,
    notification: bool,
    response_emitted: bool,
    request_id_type: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "rpc.system": "jsonrpc",
        "rpc.method": method or "<parse-error>",
        "ctx.notification": notification,
        "ctx.response_emitted": response_emitted,
    }
    if request_id_type is not None:
        payload["rpc.request_id.type"] = request_id_type
    if isinstance(params, dict):
        payload["ctx.params.keys"] = sorted(str(key) for key in params)
        if method == "tools/call":
            tool_name = params.get("name")
            if isinstance(tool_name, str):
                payload["ctx.tool.name"] = tool_name
            payload.update(_safe_tool_argument_payload(params.get("arguments")))
    return payload


def _jsonrpc_metadata(frame: dict[str, Any], params: object) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for value in (frame.get("metadata"), frame.get("_meta")):
        if isinstance(value, dict):
            metadata.update(value)
    if isinstance(params, dict) and isinstance(params.get("_meta"), dict):
        metadata.update(params["_meta"])
    return metadata


def _trace_context_from_metadata(
    frame: dict[str, Any],
    params: object,
) -> tuple[str | None, str | None]:
    metadata = _jsonrpc_metadata(frame, params)
    raw_traceparent = metadata.get("traceparent")
    parsed = parse_traceparent(raw_traceparent) if isinstance(raw_traceparent, str) else None
    if parsed is None:
        return None, None
    return parsed


def _safe_metadata_payload(frame: dict[str, Any], params: object) -> dict[str, Any]:
    metadata = _jsonrpc_metadata(frame, params)
    payload: dict[str, Any] = {}
    if isinstance(metadata.get("traceparent"), str):
        payload["ctx.traceparent.received"] = parse_traceparent(metadata["traceparent"]) is not None
    session_hash = metadata.get("ctx.session.hash")
    if isinstance(session_hash, str) and session_hash.startswith("sha256:"):
        payload["ctx.session.hash"] = session_hash
    return payload


def _mcp_result_payload(method: str, result: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if method == "tools/list":
        tools = result.get("tools")
        if isinstance(tools, list):
            payload["ctx.result.count"] = len(tools)
    elif method == "tools/call":
        payload["ctx.result.is_error"] = bool(result.get("isError"))
        content = result.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                try:
                    parsed = json.loads(first["text"])
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    results = parsed.get("results")
                    if isinstance(results, list):
                        payload["ctx.result.count"] = len(results)
                    payload["ctx.result.has_error_payload"] = "error" in parsed
    return payload


def _record_mcp_request(
    *,
    payload: dict[str, Any],
    outcome: str,
    duration_ms: float,
    error_kind: str | None = None,
    exc: BaseException | None = None,
) -> None:
    payload["otel.status_code"] = "ERROR" if outcome == "error" else "OK"
    if error_kind:
        payload["error.type"] = error_kind
    try:
        if exc is not None:
            record_exception(
                "ctx.mcp.request",
                source="ctx-mcp-server",
                exc=exc,
                transport="mcp-jsonrpc",
                outcome=outcome,
                duration_ms=duration_ms,
                error_kind=error_kind,
                payload=payload,
            )
        else:
            record_event(
                "ctx.mcp.request",
                source="ctx-mcp-server",
                transport="mcp-jsonrpc",
                outcome=outcome,
                duration_ms=duration_ms,
                error_kind=error_kind,
                payload=payload,
            )
    except Exception:  # noqa: BLE001 - telemetry must never corrupt JSON-RPC.
        pass


def _csv_items(values: Iterable[str] | None) -> list[str]:
    if values is None:
        return []
    items: list[str] = []
    for value in values:
        items.extend(piece.strip() for piece in value.split(",") if piece.strip())
    return items


def _normalise_allowed_tool_names(
    tool_names: Iterable[str] | None,
) -> frozenset[str] | None:
    if tool_names is None:
        return None
    return frozenset(str(name).strip() for name in tool_names if str(name).strip())


def _normalise_allowed_entity_types(
    entity_types: Iterable[str] | None,
) -> frozenset[str] | None:
    if entity_types is None:
        return None
    normalised: list[str] = []
    for raw in entity_types:
        entity_type = normalize_entity_type(raw, allowed=RECOMMENDABLE_ENTITY_TYPES)
        if entity_type is None:
            raise ValueError(
                "allowed entity types must be one of " + ", ".join(RECOMMENDABLE_ENTITY_TYPES)
            )
        if entity_type not in normalised:
            normalised.append(entity_type)
    return frozenset(normalised)


class _JsonRpcError(Exception):
    """Raised inside a handler to emit an RPC-level error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ── Server I/O loop ───────────────────────────────────────────────────────


def run_server(
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    *,
    allowed_tool_names: Iterable[str] | None = None,
    allowed_entity_types: Iterable[str] | None = None,
) -> int:
    """Read JSON-RPC frames from ``stdin`` until EOF, write to ``stdout``.

    Both args default to ``sys.stdin.buffer`` / ``sys.stdout.buffer``,
    matching how an MCP host invokes the server as a subprocess.
    Passing explicit streams lets tests drive the server without a
    subprocess boundary.

    Returns an exit code: 0 for clean EOF, 1 for a fatal error.
    """
    in_stream: BinaryIO = stdin if stdin is not None else sys.stdin.buffer
    out_stream: BinaryIO = stdout if stdout is not None else sys.stdout.buffer
    state = _ServerState(
        allowed_tool_names=_normalise_allowed_tool_names(allowed_tool_names),
        allowed_entity_types=_normalise_allowed_entity_types(allowed_entity_types),
    )

    try:
        while True:
            line = in_stream.readline()
            if not line:
                # EOF — clean shutdown.
                return 0
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not text.strip():
                continue
            _process_line(text, state, out_stream)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        # Fatal loop-level error. Log to stderr (the host sees our
        # stderr) and exit non-zero so the host reaps the subprocess.
        _logger.error("ctx-mcp-server: fatal: %s", exc, exc_info=True)
        return 1


def _process_line(
    text: str,
    state: _ServerState,
    out_stream: BinaryIO,
) -> None:
    """Parse one JSON line, dispatch, emit response (or not, for notifications)."""
    started = time.perf_counter()
    try:
        frame = json.loads(text)
    except json.JSONDecodeError as exc:
        _write_error(
            out_stream,
            None,
            _ErrorCode.PARSE_ERROR,
            f"parse error: {exc}",
        )
        _record_mcp_request(
            payload=_safe_mcp_payload(
                None,
                None,
                notification=False,
                response_emitted=True,
                request_id_type=None,
            ),
            outcome="error",
            duration_ms=_duration_ms(started),
            error_kind="parse_error",
        )
        return

    if not isinstance(frame, dict):
        _write_error(
            out_stream,
            None,
            _ErrorCode.INVALID_REQUEST,
            "request must be a JSON object",
        )
        _record_mcp_request(
            payload=_safe_mcp_payload(
                None,
                None,
                notification=False,
                response_emitted=True,
                request_id_type=None,
            ),
            outcome="error",
            duration_ms=_duration_ms(started),
            error_kind="invalid_request",
        )
        return

    req_id = frame.get("id")
    method = frame.get("method")
    params = frame.get("params") or {}

    # Spec: an object with no "id" is a notification (no response).
    is_notification = "id" not in frame
    base_payload = _safe_mcp_payload(
        method if isinstance(method, str) else None,
        params,
        notification=is_notification,
        response_emitted=not is_notification,
        request_id_type=_request_id_type(frame),
    )
    base_payload.update(_safe_metadata_payload(frame, params))
    trace_id, parent_span_id = _trace_context_from_metadata(frame, params)

    if not isinstance(method, str) or not method:
        if not is_notification:
            _write_error(
                out_stream,
                req_id,
                _ErrorCode.INVALID_REQUEST,
                "method must be a non-empty string",
            )
            _record_mcp_request(
                payload=base_payload,
                outcome="error",
                duration_ms=_duration_ms(started),
                error_kind="invalid_request",
            )
        return

    with telemetry_span(trace_id=trace_id, parent_span_id=parent_span_id):
        if is_notification:
            # Per spec we accept (and ignore) unknown notifications rather
            # than erroring — it lets hosts send progress/ping/cancel
            # without the server breaking on unrecognised names.
            if method in _NOTIFICATIONS:
                _logger.debug("ctx-mcp-server notification: %s", method)
            else:
                _logger.debug("ctx-mcp-server unknown notification: %s", method)
            _record_mcp_request(
                payload=base_payload,
                outcome="ok",
                duration_ms=_duration_ms(started),
            )
            return

        handler = _HANDLERS.get(method)
        if handler is None:
            _write_error(
                out_stream,
                req_id,
                _ErrorCode.METHOD_NOT_FOUND,
                f"method not found: {method}",
            )
            _record_mcp_request(
                payload=base_payload,
                outcome="error",
                duration_ms=_duration_ms(started),
                error_kind="method_not_found",
            )
            return

        # Some servers reject tools/* before initialize; we are permissive
        # (no state depends on initialize actually having run) to avoid
        # a footgun for simple clients. The spec doesn't forbid this.
        try:
            result = handler(state, params if isinstance(params, dict) else {})
        except _JsonRpcError as exc:
            _write_error(out_stream, req_id, exc.code, exc.message, exc.data)
            _record_mcp_request(
                payload=base_payload,
                outcome="error",
                duration_ms=_duration_ms(started),
                error_kind=type(exc).__name__,
            )
            return
        except Exception as exc:  # noqa: BLE001
            _logger.error("handler %s raised", method, exc_info=True)
            _write_error(
                out_stream,
                req_id,
                _ErrorCode.INTERNAL_ERROR,
                f"internal error: {type(exc).__name__}",
            )
            _record_mcp_request(
                payload=base_payload,
                outcome="error",
                duration_ms=_duration_ms(started),
                error_kind=type(exc).__name__,
                exc=exc,
            )
            return

        _write_response(out_stream, req_id, result)
        base_payload.update(_mcp_result_payload(method, result))
        result_is_error = bool(base_payload.get("ctx.result.is_error"))
        _record_mcp_request(
            payload=base_payload,
            outcome="error" if result_is_error else "ok",
            duration_ms=_duration_ms(started),
            error_kind="tool_error" if result_is_error else None,
        )


# ── Frame writers ─────────────────────────────────────────────────────────


def _write_frame(stream: BinaryIO, frame: dict[str, Any]) -> None:
    payload = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
    stream.write(payload)
    try:
        stream.flush()
    except Exception:  # noqa: BLE001
        # Some test streams don't support flush; ignore.
        pass


def _write_response(stream: BinaryIO, req_id: Any, result: dict[str, Any]) -> None:
    _write_frame(
        stream,
        {"jsonrpc": "2.0", "id": req_id, "result": result},
    )


def _write_error(
    stream: BinaryIO,
    req_id: Any,
    code: int,
    message: str,
    data: Any = None,
) -> None:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _write_frame(
        stream,
        {"jsonrpc": "2.0", "id": req_id, "error": err},
    )


# ── CLI entry ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``ctx-mcp-server`` console script."""
    parser = argparse.ArgumentParser(description="Run the ctx MCP stdio server.")
    parser.add_argument(
        "--allow-tools",
        action="append",
        help="Comma-separated ctx tool names to expose. Omit to expose all ctx tools.",
    )
    parser.add_argument(
        "--entity-types",
        action="append",
        help=(
            "Comma-separated entity types allowed in read/query results. "
            "Omit to expose every recommendable entity type."
        ),
    )
    args = parser.parse_args(argv)
    allowed_entity_types = _csv_items(args.entity_types) if args.entity_types is not None else None
    try:
        _normalise_allowed_entity_types(allowed_entity_types)
    except ValueError as exc:
        parser.error(str(exc))

    # Keep logging on stderr so it doesn't corrupt the stdout JSON-RPC
    # stream. Level defaults to WARNING — hosts typically don't want
    # debug noise unless the user opts in.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(asctime)s ctx-mcp-server %(levelname)s %(message)s",
    )
    return run_server(
        allowed_tool_names=_csv_items(args.allow_tools) if args.allow_tools is not None else None,
        allowed_entity_types=allowed_entity_types,
    )


if __name__ == "__main__":
    raise SystemExit(main())
