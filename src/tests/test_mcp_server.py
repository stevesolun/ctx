"""
test_mcp_server.py -- ctx.mcp_server JSON-RPC protocol tests.

Drives the server's ``run_server`` directly with BytesIO streams
so we can assert on the byte-level frame output without a subprocess
boundary. The end-to-end subprocess-round-trip test spawns the server
via the same fixture pattern used by test_mcp_router — confirms the
H2 client can talk to the H8 server (round-trip closes the loop).

Coverage:
  * initialize handshake shape
  * tools/list returns the ctx-core catalogue
  * tools/call success + error paths (ctx-core JSON error surfaces
    as isError=True)
  * parse errors, invalid requests, unknown methods
  * notifications accepted without a response (initialized, ping,
    cancelled, unknown)
  * EOF exit
  * ctx-core dispatch errors → internal error response, server stays up
  * Subprocess round-trip via McpClient (the H2 client) against the
    real spawned server
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from ctx.adapters.generic.tools import (
    McpClient,
    McpServerConfig,
    McpServerError,
)
from ctx.mcp_server.server import (
    _ErrorCode,
    _HANDLERS,
    _NOTIFICATIONS,
    _ServerState,
    _handle_initialize,
    _handle_tools_call,
    _handle_tools_list,
    _write_error,
    _write_frame,
    _write_response,
    run_server,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _encode_request(
    req_id: Any,
    method: str,
    params: dict[str, Any] | None = None,
) -> bytes:
    frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        frame["id"] = req_id
    if params is not None:
        frame["params"] = params
    return (json.dumps(frame) + "\n").encode("utf-8")


def _encode_notification(method: str, params: dict[str, Any] | None = None) -> bytes:
    return _encode_request(req_id=None, method=method, params=params)


def _drive(input_bytes: bytes) -> list[dict[str, Any]]:
    """Run the server against an in-memory stream, return parsed output frames."""
    in_stream = io.BytesIO(input_bytes)
    out_stream = io.BytesIO()
    rc = run_server(stdin=in_stream, stdout=out_stream)
    assert rc == 0, f"server exited {rc}"
    raw = out_stream.getvalue().decode("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


# ── Server-level loop behaviour ─────────────────────────────────────────────


class TestRunServerLifecycle:
    def test_clean_eof_exits_zero(self) -> None:
        frames = _drive(b"")
        assert frames == []  # no input → no output

    def test_blank_lines_skipped(self) -> None:
        # Three blank lines then EOF must not emit anything.
        frames = _drive(b"\n\n\n")
        assert frames == []

    def test_invalid_json_yields_parse_error(self) -> None:
        frames = _drive(b"{not valid json\n")
        assert len(frames) == 1
        assert frames[0]["error"]["code"] == _ErrorCode.PARSE_ERROR
        # id is null when the request couldn't be parsed.
        assert frames[0]["id"] is None

    def test_non_object_request_rejected(self) -> None:
        frames = _drive(b"[1, 2, 3]\n")
        assert frames[0]["error"]["code"] == _ErrorCode.INVALID_REQUEST

    def test_unknown_method_yields_method_not_found(self) -> None:
        frames = _drive(_encode_request(1, "unknown/method"))
        assert frames[0]["error"]["code"] == _ErrorCode.METHOD_NOT_FOUND
        assert frames[0]["id"] == 1


# ── Initialize handshake ────────────────────────────────────────────────────


class TestInitialize:
    def test_happy_path(self) -> None:
        frames = _drive(
            _encode_request(
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            )
        )
        response = frames[0]
        assert response["id"] == 1
        assert "error" not in response
        result = response["result"]
        assert "protocolVersion" in result
        assert "capabilities" in result
        assert result["serverInfo"]["name"] == "ctx-wiki"
        assert "version" in result["serverInfo"]

    def test_tools_capability_declared(self) -> None:
        frames = _drive(_encode_request(1, "initialize", {}))
        result = frames[0]["result"]
        assert "tools" in result["capabilities"]

    def test_state_flag_set(self) -> None:
        state = _ServerState()
        _handle_initialize(state, {})
        assert state.initialized is True


# ── Notifications ───────────────────────────────────────────────────────────


class TestNotifications:
    def test_initialized_notification_is_silent(self) -> None:
        frames = _drive(_encode_notification("notifications/initialized"))
        assert frames == []

    def test_ping_notification_is_silent(self) -> None:
        frames = _drive(_encode_notification("ping"))
        assert frames == []

    def test_cancelled_notification_is_silent(self) -> None:
        frames = _drive(_encode_notification("notifications/cancelled", {"requestId": 1}))
        assert frames == []

    def test_unknown_notification_is_silent(self) -> None:
        # Spec: unknown notifications are ignored (no error response).
        frames = _drive(_encode_notification("future/unknown-notification"))
        assert frames == []

    def test_notifications_have_no_response_for_known_methods(self) -> None:
        # A "method":"initialize" with no "id" is a notification by spec,
        # even though initialize is usually a request. We should NOT
        # emit a response for it.
        frames = _drive(_encode_notification("initialize", {}))
        assert frames == []


# ── tools/list ──────────────────────────────────────────────────────────────


class TestToolsList:
    def test_returns_ctx_core_catalogue(self) -> None:
        frames = _drive(_encode_request(1, "tools/list"))
        response = frames[0]
        assert "error" not in response
        tool_names = {t["name"] for t in response["result"]["tools"]}
        assert tool_names == {
            "ctx__recommend_bundle",
            "ctx__graph_query",
            "ctx__wiki_search",
            "ctx__wiki_get",
        }

    def test_tool_shape_is_mcp_standard(self) -> None:
        frames = _drive(_encode_request(1, "tools/list"))
        tool = frames[0]["result"]["tools"][0]
        # MCP expects: name, description, inputSchema (not "parameters")
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        assert tool["inputSchema"]["type"] == "object"


# ── tools/call ──────────────────────────────────────────────────────────────


class TestToolsCall:
    def test_missing_name_yields_invalid_params(self) -> None:
        frames = _drive(_encode_request(1, "tools/call", {"arguments": {}}))
        assert frames[0]["error"]["code"] == _ErrorCode.INVALID_PARAMS

    def test_non_dict_arguments_yields_invalid_params(self) -> None:
        frames = _drive(
            _encode_request(
                1, "tools/call",
                {"name": "ctx__wiki_search", "arguments": "not a dict"},
            )
        )
        assert frames[0]["error"]["code"] == _ErrorCode.INVALID_PARAMS

    def test_non_ctx_tool_yields_method_not_found(self) -> None:
        frames = _drive(
            _encode_request(
                1, "tools/call",
                {"name": "fs__read_file", "arguments": {}},
            )
        )
        assert frames[0]["error"]["code"] == _ErrorCode.METHOD_NOT_FOUND

    def test_unknown_ctx_tool_returns_is_error_true(self) -> None:
        """An unknown ctx__* subtool is a data-level error, not RPC-level."""
        frames = _drive(
            _encode_request(
                1, "tools/call",
                {"name": "ctx__does_not_exist", "arguments": {}},
            )
        )
        # toolbox.dispatch raises ValueError → handler maps to
        # {content: ..., isError: true}.
        result = frames[0]["result"]
        assert result["isError"] is True
        assert "unknown" in result["content"][0]["text"].lower()

    def test_recommend_bundle_empty_query_surfaces_as_error(self) -> None:
        frames = _drive(
            _encode_request(
                1, "tools/call",
                {
                    "name": "ctx__recommend_bundle",
                    "arguments": {"query": ""},
                },
            )
        )
        result = frames[0]["result"]
        # ctx-core returns {"error": "..."} → handler flips isError.
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert "error" in payload

    def test_wiki_get_invalid_slug_surfaces_as_error(self) -> None:
        frames = _drive(
            _encode_request(
                1, "tools/call",
                {
                    "name": "ctx__wiki_get",
                    "arguments": {"slug": "../../etc/passwd"},
                },
            )
        )
        result = frames[0]["result"]
        assert result["isError"] is True


# ── Handler unit tests (direct, no I/O loop) ────────────────────────────────


class TestHandlersDirect:
    def test_tools_list_direct(self) -> None:
        state = _ServerState()
        result = _handle_tools_list(state, {})
        assert len(result["tools"]) == 4

    def test_tools_call_rejects_missing_name(self) -> None:
        from ctx.mcp_server.server import _JsonRpcError

        state = _ServerState()
        with pytest.raises(_JsonRpcError) as ei:
            _handle_tools_call(state, {})
        assert ei.value.code == _ErrorCode.INVALID_PARAMS


# ── Frame writers ───────────────────────────────────────────────────────────


class TestFrameWriters:
    def test_write_frame_appends_newline(self) -> None:
        stream = io.BytesIO()
        _write_frame(stream, {"jsonrpc": "2.0", "id": 1, "result": {}})
        raw = stream.getvalue()
        assert raw.endswith(b"\n")
        assert json.loads(raw.decode("utf-8")) == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {},
        }

    def test_write_response_shape(self) -> None:
        stream = io.BytesIO()
        _write_response(stream, 42, {"tools": []})
        frame = json.loads(stream.getvalue().decode("utf-8"))
        assert frame == {"jsonrpc": "2.0", "id": 42, "result": {"tools": []}}

    def test_write_error_shape(self) -> None:
        stream = io.BytesIO()
        _write_error(stream, 42, _ErrorCode.INVALID_PARAMS, "bad", {"hint": "x"})
        frame = json.loads(stream.getvalue().decode("utf-8"))
        assert frame == {
            "jsonrpc": "2.0",
            "id": 42,
            "error": {
                "code": _ErrorCode.INVALID_PARAMS,
                "message": "bad",
                "data": {"hint": "x"},
            },
        }

    def test_write_error_omits_data_when_none(self) -> None:
        stream = io.BytesIO()
        _write_error(stream, 1, _ErrorCode.PARSE_ERROR, "x")
        frame = json.loads(stream.getvalue().decode("utf-8"))
        assert "data" not in frame["error"]


# ── Multi-turn session ──────────────────────────────────────────────────────


class TestMultiRequestSession:
    def test_initialize_then_tools_list(self) -> None:
        input_bytes = (
            _encode_request(1, "initialize", {})
            + _encode_notification("notifications/initialized")
            + _encode_request(2, "tools/list")
        )
        frames = _drive(input_bytes)
        # Initialize response + tools/list response; notification is silent.
        assert len(frames) == 2
        assert frames[0]["id"] == 1
        assert frames[1]["id"] == 2
        assert "tools" in frames[1]["result"]


# ── Subprocess round-trip via the H2 client ─────────────────────────────────


class TestRoundTripWithH2Client:
    """Spawn ctx-mcp-server as a real subprocess; talk to it with McpClient.

    This is the canary that proves H2 + H8 agree on the wire format.
    If MCP spec evolution ever breaks the handshake or tool schema,
    this test catches it before any real user does.
    """

    def test_client_server_handshake_and_tools_list(self, tmp_path: Path) -> None:
        # Point the server at a synthetic wiki/graph so ctx__* tools
        # have something to operate on (happy-path call after list).
        wiki = self._build_synthetic_wiki(tmp_path)
        graph_path = self._build_synthetic_graph(tmp_path)
        cfg = McpServerConfig(
            name="ctx",
            command=sys.executable,
            args=(
                "-m", "ctx.mcp_server.server",
            ),
            env={
                "CTX_WIKI_DIR": str(wiki),
                "CTX_GRAPH_PATH": str(graph_path),
            },
            startup_timeout=10.0,
            request_timeout=10.0,
        )
        with McpClient(cfg) as client:
            tools = client.list_tools()
            names = {t.name for t in tools}
            assert names == {
                "ctx__recommend_bundle",
                "ctx__graph_query",
                "ctx__wiki_search",
                "ctx__wiki_get",
            }

    def test_client_calls_tool_and_reads_response(self, tmp_path: Path) -> None:
        wiki = self._build_synthetic_wiki(tmp_path)
        graph_path = self._build_synthetic_graph(tmp_path)
        cfg = McpServerConfig(
            name="ctx",
            command=sys.executable,
            args=("-m", "ctx.mcp_server.server"),
            env={
                "CTX_WIKI_DIR": str(wiki),
                "CTX_GRAPH_PATH": str(graph_path),
            },
            startup_timeout=10.0,
            request_timeout=10.0,
        )
        with McpClient(cfg) as client:
            # An empty-query recommend_bundle produces isError from
            # ctx-core; client raises McpServerError.
            with pytest.raises(McpServerError):
                client.call_tool(
                    "ctx__recommend_bundle", {"query": ""},
                )

    # ── synthetic-corpus helpers (copied from test_harness_ctx_core) ───

    @staticmethod
    def _build_synthetic_graph(tmp_path: Path) -> Path:
        import networkx as nx

        G = nx.Graph()
        G.add_node(
            "skill:python-patterns", label="python-patterns", type="skill",
            tags=["python", "patterns"],
        )
        out_dir = tmp_path / "graphify-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "graph.json"
        data = nx.node_link_data(G, edges="edges")
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    @staticmethod
    def _build_synthetic_wiki(tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        skills = wiki / "entities" / "skills"
        skills.mkdir(parents=True)
        (skills / "python-patterns.md").write_text(
            "---\nname: python-patterns\ntitle: Python Patterns\n"
            "tags: [python, patterns]\nstatus: cataloged\n---\n# body\n",
            encoding="utf-8",
        )
        return wiki


# ── Dispatch-table + notifications-set sanity ───────────────────────────────


class TestProtocolTables:
    def test_handlers_has_three_entries(self) -> None:
        assert set(_HANDLERS.keys()) == {
            "initialize", "tools/list", "tools/call",
        }

    def test_notifications_set_covers_common(self) -> None:
        for n in (
            "notifications/initialized",
            "notifications/cancelled",
            "notifications/progress",
            "ping",
        ):
            assert n in _NOTIFICATIONS
