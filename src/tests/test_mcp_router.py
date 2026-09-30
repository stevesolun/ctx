"""
test_mcp_router.py -- McpClient + McpRouter tests against a real subprocess.

These tests spawn the fake MCP server in src/tests/fixtures/fake_mcp_server.py
as an actual subprocess and round-trip real JSON-RPC frames. That is
slower than mocking stdio, but the whole POINT of the router is to
talk JSON-RPC to a real child — mocking the subprocess surface would
verify nothing load-bearing. Each test starts + stops its own server,
so there's no cross-test state.
"""

from __future__ import annotations

import json
import io
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import pytest

import ctx.telemetry as telemetry
from ctx.adapters.generic.loop import _collect_tools
from ctx.adapters.generic.providers import ToolDefinition
from ctx.adapters.generic.tools import (
    McpClient,
    McpRouter,
    McpServerConfig,
    McpServerError,
    mcp_router,
    running_router,
)


_FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
_INITIALIZE_RESULT_SERVER = """
import json
import sys

request = json.loads(sys.stdin.readline())
result = json.loads(sys.argv[1])
response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
print(json.dumps(response), flush=True)
sys.stdin.read()
"""


def _make_config(
    name: str = "fake",
    *,
    extra_env: dict[str, str] | None = None,
    credential_env: tuple[str, ...] = (),
    startup_timeout: float = 5.0,
    request_timeout: float = 5.0,
    inherit_env: bool = False,
) -> McpServerConfig:
    """Return a config that launches the fake server via the test Python."""
    return McpServerConfig(
        name=name,
        command=sys.executable,
        args=(str(_FIXTURE),),
        env=dict(extra_env or {}),
        credential_env=credential_env,
        startup_timeout=startup_timeout,
        request_timeout=request_timeout,
        inherit_env=inherit_env,
    )


def _make_initialize_result_config(result: object) -> McpServerConfig:
    """Return a real child server that emits one chosen initialize result."""
    return McpServerConfig(
        name="malformed",
        command=sys.executable,
        args=("-c", _INITIALIZE_RESULT_SERVER, json.dumps(result)),
        startup_timeout=2.0,
        request_timeout=2.0,
    )


def _capture_popen(
    monkeypatch: pytest.MonkeyPatch,
) -> list[subprocess.Popen[bytes]]:
    procs: list[subprocess.Popen[bytes]] = []
    real_popen = mcp_router.subprocess.Popen

    def capturing_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        proc = real_popen(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(mcp_router.subprocess, "Popen", capturing_popen)
    return procs


def _assert_exited(proc: subprocess.Popen[bytes]) -> None:
    proc.wait(timeout=2.0)
    assert proc.poll() is not None


def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before timeout")


# ── McpClient basics ─────────────────────────────────────────────────────────


class TestClientLifecycle:
    def test_processes_start_in_a_posix_session(self) -> None:
        assert mcp_router._popen_process_group_kwargs() == {"start_new_session": True}

    @pytest.mark.parametrize(
        ("force", "expected_signal"),
        [(False, mcp_router.signal.SIGTERM), (True, mcp_router.signal.SIGKILL)],
    )
    def test_stop_signals_the_posix_process_group(
        self,
        monkeypatch: pytest.MonkeyPatch,
        force: bool,
        expected_signal: int,
    ) -> None:
        calls: list[tuple[int, int]] = []

        class RunningProcess:
            pid = 4312

            @staticmethod
            def poll() -> None:
                return None

            @staticmethod
            def kill() -> None:
                pytest.fail("process fallback should not be used")

            @staticmethod
            def terminate() -> None:
                pytest.fail("process fallback should not be used")

        monkeypatch.setattr(
            mcp_router.os,
            "killpg",
            lambda pid, sig: calls.append((pid, sig)),
        )

        mcp_router._signal_process_tree(RunningProcess(), force=force)  # type: ignore[arg-type]

        assert calls == [(4312, expected_signal)]

    def test_start_and_stop(self) -> None:
        client = McpClient(_make_config())
        client.start()
        try:
            assert client.negotiated_protocol_version == "2024-11-05"
            # Trivial health: list_tools succeeds → handshake completed.
            tools = client.list_tools()
            assert len(tools) >= 2  # echo + add
        finally:
            client.stop()

    def test_start_offers_and_stores_newest_supported_protocol(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = McpClient(_make_config())
        initialize_params: list[dict[str, Any]] = []

        def fake_request(
            method: str,
            params: dict[str, Any] | None,
            *,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            assert method == "initialize"
            assert timeout == client._config.startup_timeout
            assert params is not None
            initialize_params.append(params)
            return {"protocolVersion": "2025-11-25"}

        monkeypatch.setattr(client, "_request", fake_request)

        try:
            client.start()
            assert initialize_params[0]["protocolVersion"] == "2025-11-25"
            assert client.negotiated_protocol_version == "2025-11-25"
        finally:
            client.stop()

    @pytest.mark.parametrize(
        ("initialize_result", "error_pattern"),
        [
            ({}, "missing protocolVersion"),
            ({"protocolVersion": 20251125}, "non-string protocolVersion"),
            ({"protocolVersion": "2026-07-28"}, "unsupported protocolVersion"),
        ],
    )
    def test_start_rejects_invalid_or_unsupported_protocol_before_tool_use(
        self,
        monkeypatch: pytest.MonkeyPatch,
        initialize_result: dict[str, Any],
        error_pattern: str,
    ) -> None:
        procs = _capture_popen(monkeypatch)
        client = McpClient(_make_config())
        methods: list[str] = []
        notifications: list[str] = []

        def fake_request(
            method: str,
            _params: dict[str, Any] | None,
            *,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            assert timeout == client._config.startup_timeout
            methods.append(method)
            return initialize_result

        monkeypatch.setattr(client, "_request", fake_request)
        monkeypatch.setattr(
            client,
            "_notify",
            lambda method, _params: notifications.append(method),
        )

        try:
            with pytest.raises(McpServerError, match=error_pattern):
                client.start()
        finally:
            client.stop()

        assert methods == ["initialize"]
        assert notifications == []
        assert client.negotiated_protocol_version is None
        assert len(procs) == 1
        _assert_exited(procs[0])

    @pytest.mark.parametrize(
        ("initialize_result", "result_kind"),
        [(7, "int"), (["not-an-object"], "list"), (None, "null")],
        ids=("scalar", "list", "null"),
    )
    def test_start_rejects_non_object_initialize_result_and_reaps_child(
        self,
        monkeypatch: pytest.MonkeyPatch,
        initialize_result: object,
        result_kind: str,
    ) -> None:
        procs = _capture_popen(monkeypatch)
        client = McpClient(_make_initialize_result_config(initialize_result))

        with pytest.raises(
            McpServerError,
            match=rf"malformed\.initialize: result must be a JSON object; received {result_kind}",
        ):
            client.start()

        assert client.negotiated_protocol_version is None
        assert client.stop() is True
        assert len(procs) == 1
        _assert_exited(procs[0])

    @pytest.mark.parametrize("value_kind", ["string", "list", "object"])
    @pytest.mark.parametrize("secret", ["opaque-credential-value", "opaque\nvalue'with\\escapes"])
    def test_rejected_protocol_version_diagnostic_redacts_credentials(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        value_kind: str,
        secret: str,
    ) -> None:
        monkeypatch.setenv("MCP_TEST_AUTH", secret)
        protocol_version: object = secret
        if value_kind == "list":
            protocol_version = [secret]
        elif value_kind == "object":
            protocol_version = {"credential": secret}
        procs = _capture_popen(monkeypatch)
        config = McpServerConfig(
            name="malformed",
            command=sys.executable,
            args=(
                "-c",
                _INITIALIZE_RESULT_SERVER,
                json.dumps({"protocolVersion": protocol_version}),
            ),
            credential_env=("MCP_TEST_AUTH",),
            startup_timeout=2.0,
        )
        client = McpClient(config)

        with pytest.raises(McpServerError, match="protocolVersion") as exc:
            client.start()

        assert client._proc is None
        assert client._stderr_redaction_values == ()
        assert client.negotiated_protocol_version is None
        assert len(procs) == 1
        _assert_exited(procs[0])
        diagnostic = "".join(traceback.format_exception(exc.value)) + caplog.text
        assert secret not in diagnostic
        assert repr(secret)[1:-1] not in diagnostic

    def test_context_manager(self) -> None:
        with McpClient(_make_config()) as client:
            tools = client.list_tools()
            names = {t.name for t in tools}
            assert {"echo", "add"} <= names

    @pytest.mark.parametrize(
        ("frame", "error_pattern"),
        [
            (7, "frame must be a JSON object"),
            ([1, 2], "frame must be a JSON object"),
            ("private-payload", "frame must be a JSON object"),
            (None, "frame must be a JSON object"),
            (False, "frame must be a JSON object"),
            ({"jsonrpc": "1.0", "id": 0, "result": {}}, "jsonrpc must be '2.0'"),
            ({"id": 0, "result": {}}, "jsonrpc must be '2.0'"),
            ({"jsonrpc": "2.0", "result": {}}, "missing response id"),
            ({"jsonrpc": "2.0", "method": 7}, "invalid notification"),
            ({"jsonrpc": "2.0", "id": False, "result": {}}, "invalid response id"),
            ({"jsonrpc": "2.0", "id": 0}, "exactly one of result or error"),
            (
                {"jsonrpc": "2.0", "id": 0, "result": {}, "error": {}},
                "exactly one of result or error",
            ),
            ({"jsonrpc": "2.0", "id": 0, "error": 7}, "invalid error object"),
            (
                {"jsonrpc": "2.0", "id": 0, "error": {"code": True, "message": "error"}},
                "invalid error object",
            ),
            ({"jsonrpc": "2.0", "id": 0, "error": {"code": -32603}}, "invalid error object"),
        ],
    )
    def test_invalid_response_frames_raise_protocol_error_and_reap_child(
        self, monkeypatch: pytest.MonkeyPatch, frame: object, error_pattern: str
    ) -> None:
        procs = _capture_popen(monkeypatch)
        config = McpServerConfig(
            name="malformed",
            command=sys.executable,
            args=(
                "-c",
                "import sys; sys.stdin.readline(); print(sys.argv[1], flush=True); sys.stdin.read()",
                json.dumps(frame),
            ),
            startup_timeout=2.0,
        )
        client = McpClient(config)
        try:
            with pytest.raises(McpServerError, match=error_pattern) as exc:
                client.start()
            assert "private-payload" not in str(exc.value)
            assert client.negotiated_protocol_version is None
            assert len(procs) == 1
            _assert_exited(procs[0])
        finally:
            client.stop()

    def test_double_start_rejected(self) -> None:
        client = McpClient(_make_config())
        client.start()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                client.start()
        finally:
            client.stop()

    def test_malformed_frame_diagnostic_does_not_log_raw_server_payload(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        private = "MCP-PRIVATE-FRAME sk-ABCDEFGHIJKLMNOPQRSTUVWX"
        server = f"print({private!r}, flush=True)\n" + _INITIALIZE_RESULT_SERVER
        config = McpServerConfig(
            name="malformed",
            command=sys.executable,
            args=("-c", server, json.dumps({"protocolVersion": "2025-11-25"})),
            startup_timeout=2.0,
        )
        with McpClient(config) as client:
            assert client.negotiated_protocol_version == "2025-11-25"
        assert "dropping malformed frame" in caplog.text
        assert "MCP-PRIVATE-FRAME" not in caplog.text
        assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in caplog.text

    def test_notification_and_stale_id_diagnostics_redact_secret_shapes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWX"
        frames = [
            {"jsonrpc": "2.0", "method": secret},
            {"jsonrpc": "2.0", "id": secret, "result": {}},
        ]
        server = "".join(f"print({json.dumps(frame)!r}, flush=True)\n" for frame in frames)
        server += _INITIALIZE_RESULT_SERVER
        config = McpServerConfig(
            name="malformed",
            command=sys.executable,
            args=("-c", server, json.dumps({"protocolVersion": "2025-11-25"})),
            startup_timeout=2.0,
        )
        with caplog.at_level("DEBUG", logger=mcp_router.__name__):
            with McpClient(config) as client:
                assert client.negotiated_protocol_version == "2025-11-25"
        assert "notification" in caplog.text
        assert "stale response" in caplog.text
        assert secret not in caplog.text

    @pytest.mark.parametrize(
        ("method", "result", "error_pattern"),
        [
            ("tools/list", 7, "result must be a JSON object"),
            ("tools/list", [], "result must be a JSON object"),
            ("tools/list", None, "result must be a JSON object"),
            ("tools/list", {"tools": 7}, "tools must be an array"),
            ("tools/list", {"tools": [7]}, "tool must be a JSON object"),
            (
                "tools/list",
                {"tools": [{"name": "echo", "inputSchema": 7}]},
                "inputSchema must be a JSON object",
            ),
            ("tools/call", 7, "result must be a JSON object"),
            ("tools/call", "payload", "result must be a JSON object"),
            ("tools/call", None, "result must be a JSON object"),
            ("tools/call", {"isError": "false", "content": []}, "isError must be a boolean"),
            ("tools/call", {"content": 7}, "content must be an array"),
        ],
    )
    def test_invalid_tool_results_raise_protocol_errors_and_close_cleanly(
        self, monkeypatch: pytest.MonkeyPatch, method: str, result: object, error_pattern: str
    ) -> None:
        procs = _capture_popen(monkeypatch)
        server = """
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    result = ({'protocolVersion': '2025-11-25'} if request['method'] == 'initialize'
              else json.loads(sys.argv[1]))
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
"""
        config = McpServerConfig(
            name="malformed",
            command=sys.executable,
            args=("-c", server, json.dumps(result)),
            startup_timeout=2.0,
            request_timeout=2.0,
        )
        with McpClient(config) as client:
            with pytest.raises(McpServerError, match=error_pattern):
                if method == "tools/list":
                    client.list_tools()
                else:
                    client.call_tool("echo", {})
        assert len(procs) == 1
        _assert_exited(procs[0])

    def test_stop_before_start_is_noop(self) -> None:
        client = McpClient(_make_config())
        client.stop()  # must not raise

    def test_bare_command_resolved_through_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        resolved = tmp_path / "npx"
        resolved.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            mcp_router.shutil,
            "which",
            lambda command, path=None: str(resolved) if command == "npx" else None,
        )

        assert mcp_router._resolve_executable("npx", {"PATH": str(tmp_path)}) == str(resolved)

    def test_idempotent_stop(self) -> None:
        client = McpClient(_make_config())
        client.start()
        client.stop()
        client.stop()  # second call is a no-op


class TestClientToolOperations:
    def test_list_tools_shape(self) -> None:
        with McpClient(_make_config()) as client:
            tools = client.list_tools()
            echo = next(t for t in tools if t.name == "echo")
            assert echo.description == "Echo the input text verbatim."
            assert echo.parameters["required"] == ["text"]

    def test_list_tools_cached(self) -> None:
        """Second call must not fire a fresh tools/list RPC."""
        with McpClient(_make_config()) as client:
            first = client.list_tools()
            second = client.list_tools()
            assert first == second
            # Cache check: same object identity on the list elements
            assert first[0] is second[0]

    def test_call_echo_round_trip(self) -> None:
        with McpClient(_make_config()) as client:
            result = client.call_tool("echo", {"text": "hello, world"})
            assert result == "hello, world"

    def test_call_add_integer_args(self) -> None:
        with McpClient(_make_config()) as client:
            result = client.call_tool("add", {"a": 3, "b": 4})
            assert result == "7"

    def test_call_unknown_tool_raises(self) -> None:
        with McpClient(_make_config()) as client:
            with pytest.raises(McpServerError, match="code=-32601"):
                client.call_tool("nope", {})

    def test_tool_reports_error(self) -> None:
        with McpClient(_make_config(extra_env={"FAKE_MCP_TOOL_ERROR": "1"})) as c:
            with pytest.raises(McpServerError, match="isError"):
                c.call_tool("echo", {"text": "x"})

    def test_call_tool_records_privacy_safe_telemetry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[tuple[str, dict[str, Any], telemetry.TelemetrySpan | None]] = []

        def fake_record_event(event_name: str, **kwargs: Any) -> None:
            events.append((event_name, kwargs, telemetry.current_telemetry_span()))

        def fake_request(
            method: str,
            params: dict[str, Any] | None,
            *,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            assert method == "tools/call"
            assert params == {
                "name": "private-tool",
                "arguments": {"text": "private acme query"},
            }
            assert timeout is None
            return {"content": [{"type": "text", "text": "ok"}]}

        monkeypatch.setattr(mcp_router, "record_event", fake_record_event)
        client = McpClient(_make_config("private-server"), session_id="sess-mcp")
        monkeypatch.setattr(client, "_request", fake_request)

        with telemetry.telemetry_span(trace_id="1" * 32, span_id="2" * 16):
            assert (
                client.call_tool(
                    "private-tool",
                    {"text": "private acme query"},
                    capability_epoch=23,
                )
                == "ok"
            )

        assert len(events) == 1
        event_name, kwargs, span = events[0]
        assert event_name == "ctx.mcp.external_tool_call"
        assert kwargs["source"] == "ctx-mcp-router"
        assert kwargs["transport"] == "mcp-jsonrpc"
        assert kwargs["session_id"] == "sess-mcp"
        assert kwargs["outcome"] == "ok"
        assert kwargs["duration_ms"] >= 0
        assert kwargs["payload"] == {
            "rpc.system": "jsonrpc",
            "rpc.method": "tools/call",
            "ctx.mcp.server.hash": telemetry.hash_identifier("private-server"),
            "ctx.mcp.tool.hash": telemetry.hash_identifier("private-server__private-tool"),
            "ctx.mcp.capability.epoch": 23,
            "otel.status_code": "OK",
        }
        serialized = json.dumps(kwargs, sort_keys=True)
        assert "private acme query" not in serialized
        assert "private-server" not in serialized
        assert "private-tool" not in serialized
        assert span is not None
        assert span.trace_id == "1" * 32
        assert span.parent_span_id == "2" * 16
        assert span.span_id != "2" * 16


class TestClientRobustness:
    @pytest.mark.parametrize("error_at", ["initialize", "tools/list", "tools/call", "isError"])
    @pytest.mark.parametrize("secret", ["opaque-credential-value", "opaque\nvalue'with\\escapes"])
    def test_server_error_diagnostic_redacts_credentials(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        error_at: str,
        secret: str,
    ) -> None:
        procs = _capture_popen(monkeypatch)
        server = """
import json, os, sys
error_at = sys.argv[1]
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    response = {'jsonrpc': '2.0', 'id': request['id']}
    message = 'request refused for ' + os.environ['MCP_API_KEY'] + '; retry later'
    if request['method'] == error_at:
        response['error'] = {'code': -32603, 'message': message}
    elif request['method'] == 'initialize':
        response['result'] = {'protocolVersion': '2025-11-25'}
    else:
        response['result'] = {'isError': True, 'content': [{'type': 'text', 'text': message}]}
    print(json.dumps(response), flush=True)
"""
        client = McpClient(
            McpServerConfig(
                name="error-server",
                command=sys.executable,
                args=("-c", server, error_at),
                env={"MCP_API_KEY": secret},
                startup_timeout=2.0,
                request_timeout=2.0,
            )
        )
        try:
            with pytest.raises(McpServerError, match="request refused for") as exc:
                client.start()
                assert client.negotiated_protocol_version == "2025-11-25"
                if error_at == "tools/list":
                    client.list_tools()
                else:
                    client.call_tool("echo", {})
            if error_at == "initialize":
                assert client._proc is None
                assert client._stderr_redaction_values == ()
        finally:
            client.stop()

        assert client._proc is None
        assert client._stderr_redaction_values == ()
        assert len(procs) == 1
        _assert_exited(procs[0])
        diagnostic = "".join(traceback.format_exception(exc.value)) + caplog.text
        assert secret not in diagnostic
        assert repr(secret)[1:-1] not in diagnostic
        assert "[REDACTED]" in str(exc.value)
        assert "retry later" in str(exc.value)
        assert ("isError" if error_at == "isError" else "code=-32603") in str(exc.value)

    @pytest.mark.parametrize(
        ("raw", "forbidden"),
        [
            (
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
                ("abcdefghijklmnopqrstuvwxyz",),
            ),
            ("HF_TOKEN=hf_abcdefghijklmnop", ("hf_abcdefghijklmnop",)),
            ("OPENAI_API_KEY=sk-proj-abcdefghijklmnop", ("sk-proj-abcdefghijklmnop",)),
            ("SLACK_BOT_TOKEN=xoxb-1234567890-secret", ("xoxb-1234567890-secret",)),
            ("db_password: supersecretpassword", ("supersecretpassword",)),
            ("PRIVATE_KEY=supersecretprivatekey", ("supersecretprivatekey",)),
            ("ACCESS_KEY=supersecretaccesskey", ("supersecretaccesskey",)),
            ("SERVICE_PASSWD=supersecretpasswd", ("supersecretpasswd",)),
            ("BEARER=supersecretbearer", ("supersecretbearer",)),
        ],
    )
    def test_redact_sensitive_text_matrix(
        self,
        raw: str,
        forbidden: tuple[str, ...],
    ) -> None:
        redacted = mcp_router._redact_sensitive_text(raw)

        assert "[REDACTED]" in redacted
        for value in forbidden:
            assert value not in redacted

    def test_init_failure_surfaces_and_reaps_child(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When initialize errors, start() must clean up — no zombie child."""
        procs = _capture_popen(monkeypatch)
        client = McpClient(_make_config(extra_env={"FAKE_MCP_FAIL_INIT": "1"}))
        with pytest.raises(McpServerError, match="init-forbidden"):
            client.start()
        assert len(procs) == 1
        _assert_exited(procs[0])
        assert client._proc is None
        client.stop()

    def test_startup_timeout_when_initialize_stays_silent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs = _capture_popen(monkeypatch)
        client = McpClient(
            _make_config(
                extra_env={"FAKE_MCP_IGNORE_INIT": "1"},
                startup_timeout=0.2,
            )
        )
        started = time.monotonic()
        with pytest.raises(
            McpServerError,
            match="fake.initialize: timed out after 0.2s",
        ):
            client.start()
        assert time.monotonic() - started < 1.5
        assert len(procs) == 1
        _assert_exited(procs[0])
        assert client._proc is None
        client.stop()

    def test_server_crash_during_call(self) -> None:
        secret = "GITHUB_TOKEN=ghp_supersecret123456789"
        cfg = _make_config(
            extra_env={
                "FAKE_MCP_CRASH_ON_TOOL": "1",
                "FAKE_MCP_STDERR_LINE": secret,
            }
        )
        with McpClient(cfg) as c:
            with pytest.raises(McpServerError, match="pipe closed") as excinfo:
                c.call_tool("echo", {"text": "x"})
        message = str(excinfo.value)
        assert "ghp_supersecret" not in message
        assert "[REDACTED]" in message

    def test_killed_child_after_tool_listing_raises_mcp_error(self) -> None:
        client = McpClient(_make_config())
        client.start()
        try:
            client.list_tools()
            proc = client._proc
            assert proc is not None
            proc.kill()
            proc.wait(timeout=2.0)

            with pytest.raises(McpServerError, match="write failed"):
                client.call_tool("echo", {"text": "x"})
        finally:
            client.stop()

    def test_server_notification_is_skipped(self) -> None:
        """The client ignores notifications that interleave with a response."""
        cfg = _make_config(extra_env={"FAKE_MCP_EMIT_NOTIFICATION": "1"})
        with McpClient(cfg) as client:
            result = client.call_tool("echo", {"text": "hi"})
            assert result == "hi"

    def test_stale_response_id_is_ignored_until_matching_response(self) -> None:
        cfg = _make_config(extra_env={"FAKE_MCP_EMIT_STALE_RESPONSE": "1"})
        with McpClient(cfg) as client:
            result = client.call_tool("echo", {"text": "fresh"})
            assert result == "fresh"

    def test_only_stale_response_id_times_out(self) -> None:
        cfg = _make_config(
            extra_env={"FAKE_MCP_ONLY_STALE_RESPONSE": "1"},
            request_timeout=0.2,
        )
        with McpClient(cfg) as client:
            started = time.monotonic()
            with pytest.raises(McpServerError, match="timed out after 0.2s"):
                client.call_tool("echo", {"text": "x"})
            assert time.monotonic() - started < 1.5

    def test_stderr_captured_on_startup(self) -> None:
        secret = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
        cfg = _make_config(
            extra_env={
                "FAKE_MCP_NOISY_STDERR": "1",
                "FAKE_MCP_STDERR_LINE": secret,
            }
        )
        with McpClient(cfg) as client:
            client.list_tools()  # let the server run
            _wait_until(lambda: "fake-mcp-server: starting up" in client._stderr_tail())
            _wait_until(lambda: "[REDACTED]" in client._stderr_tail())
            stderr_tail = client._stderr_tail()
        assert "fake-mcp-server: starting up" in stderr_tail
        assert "abcdefghijklmnopqrstuvwxyz" not in stderr_tail
        assert "Authorization:" in stderr_tail
        assert "[REDACTED]" in stderr_tail

    def test_stderr_redacts_credential_env_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        secret = "argv-only-secret"
        monkeypatch.setenv("MCP_API_KEY", secret)
        cfg = _make_config(
            extra_env={
                "FAKE_MCP_NOISY_STDERR": "1",
                "FAKE_MCP_STDERR_LINE": secret,
            },
            credential_env=("MCP_API_KEY",),
        )

        with McpClient(cfg) as client:
            client.list_tools()
            _wait_until(lambda: "[REDACTED]" in client._stderr_tail())
            stderr_tail = client._stderr_tail()

        assert secret not in stderr_tail
        assert "[REDACTED]" in stderr_tail

    def test_stderr_drain_uses_thread_redaction_snapshot(self) -> None:
        secret = "late-secret"
        client = McpClient(_make_config())
        client._proc = type("Proc", (), {"stderr": io.BytesIO(f"{secret}\n".encode())})()  # type: ignore[assignment]
        client._stderr_redaction_values = ()

        client._drain_stderr((secret,))

        assert client._stderr_lines == ["[REDACTED]"]

    def test_request_before_start_raises(self) -> None:
        client = McpClient(_make_config())
        with pytest.raises(RuntimeError, match="not started"):
            client.list_tools()

    def test_request_timeout_when_server_stays_silent(self) -> None:
        cfg = _make_config(
            extra_env={"FAKE_MCP_IGNORE_TOOL": "1"},
            request_timeout=0.2,
        )
        with McpClient(cfg) as client:
            started = time.monotonic()
            with pytest.raises(McpServerError, match="timed out after 0.2s"):
                client.call_tool("echo", {"text": "x"})
            assert time.monotonic() - started < 1.5

    def test_request_injects_traceparent_and_session_hash(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        frames: list[dict[str, Any]] = []
        client = McpClient(_make_config(), session_id="sess-private")
        client._proc = type("Proc", (), {"stdin": object(), "stdout": object()})()  # type: ignore[assignment]

        def fake_read_frame(*, timeout: float | None) -> dict[str, Any]:
            assert timeout is None or timeout > 0
            return {"jsonrpc": "2.0", "id": 0, "result": {"ok": True}}

        monkeypatch.setattr(client, "_write_frame", frames.append)
        monkeypatch.setattr(client, "_read_frame", fake_read_frame)

        with telemetry.telemetry_span(trace_id="1" * 32, span_id="2" * 16):
            assert client._request("tools/call", {"name": "echo", "arguments": {}}) == {"ok": True}

        assert len(frames) == 1
        meta = frames[0]["params"]["_meta"]
        assert meta["traceparent"] == f"00-{'1' * 32}-{'2' * 16}-01"
        assert meta["ctx.session.hash"].startswith("sha256:")
        assert "sess-private" not in json.dumps(frames[0], sort_keys=True)

    def test_request_skips_trace_metadata_when_telemetry_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        frames: list[dict[str, Any]] = []
        client = McpClient(_make_config(), session_id="sess-private")
        client._proc = type("Proc", (), {"stdin": object(), "stdout": object()})()  # type: ignore[assignment]

        def fake_read_frame(*, timeout: float | None) -> dict[str, Any]:
            assert timeout is None or timeout > 0
            return {"jsonrpc": "2.0", "id": 0, "result": {"ok": True}}

        monkeypatch.setattr(mcp_router, "telemetry_enabled", lambda: False)
        monkeypatch.setattr(client, "_write_frame", frames.append)
        monkeypatch.setattr(client, "_read_frame", fake_read_frame)

        with telemetry.telemetry_span(trace_id="1" * 32, span_id="2" * 16):
            assert client._request("tools/call", {"name": "echo", "arguments": {}}) == {"ok": True}

        assert len(frames) == 1
        assert "_meta" not in frames[0]["params"]
        assert "traceparent" not in json.dumps(frames[0], sort_keys=True)
        assert "sess-private" not in json.dumps(frames[0], sort_keys=True)

    def test_parent_env_is_not_inherited_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CTX_SECRET_SHOULD_NOT_LEAK", "leaked")
        with McpClient(_make_config()) as client:
            assert client.call_tool("echo_env", {"name": "CTX_SECRET_SHOULD_NOT_LEAK"}) == ""
        with McpClient(_make_config(credential_env=("CTX_SECRET_SHOULD_NOT_LEAK",))) as client:
            assert client.call_tool("echo_env", {"name": "CTX_SECRET_SHOULD_NOT_LEAK"}) == "leaked"

    def test_explicit_env_overlay_is_passed(self) -> None:
        cfg = _make_config(extra_env={"CTX_ALLOWED_FOR_TEST": "visible"})
        with McpClient(cfg) as client:
            assert client.call_tool("echo_env", {"name": "CTX_ALLOWED_FOR_TEST"}) == "visible"

    def test_full_env_inheritance_requires_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CTX_LEGACY_INHERIT_TEST", "visible")
        with McpClient(_make_config(inherit_env=True)) as client:
            assert client.call_tool("echo_env", {"name": "CTX_LEGACY_INHERIT_TEST"}) == "visible"

    def test_env_placeholders_are_literal_by_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MCP_API_KEY", "argv-only-secret")
        cfg = McpServerConfig(
            name="argvserver",
            command="server",
            args=("--api-key", "${MCP_API_KEY}", "--header=Bearer $MCP_API_KEY"),
            credential_env=("MCP_API_KEY",),
        )

        assert mcp_router._expand_config_args(cfg, mcp_router._child_env_for_config(cfg)) == (
            "--api-key",
            "${MCP_API_KEY}",
            "--header=Bearer $MCP_API_KEY",
        )

    def test_sensitive_env_placeholders_are_not_expanded_into_argv_by_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MCP_API_KEY", "argv-only-secret")
        cfg = McpServerConfig(
            name="argvserver",
            command="server",
            args=("--api-key", "${MCP_API_KEY}", "--header=Bearer $MCP_API_KEY"),
            credential_env=("MCP_API_KEY",),
            expand_argv_env=True,
        )

        with pytest.raises(ValueError, match="sensitive env var 'MCP_API_KEY'"):
            mcp_router._expand_config_args(cfg, mcp_router._child_env_for_config(cfg))

    @pytest.mark.parametrize(
        "env_name",
        ("MCP_PRIVATE_KEY", "MCP_ACCESS_KEY", "SERVICE_PASSWD", "MCP_BEARER"),
    )
    def test_sensitive_env_placeholder_key_markers_are_rejected_by_default(
        self,
        env_name: str,
    ) -> None:
        cfg = McpServerConfig(
            name="argvserver",
            command="server",
            args=(f"--auth=${{{env_name}}}",),
            env={env_name: "argv-only-secret"},
            expand_argv_env=True,
        )
        env = mcp_router._child_env_for_config(cfg)

        with pytest.raises(ValueError, match=env_name):
            mcp_router._expand_config_args(cfg, env)
        assert "argv-only-secret" in mcp_router._stderr_redaction_values(cfg, env)

    def test_non_secret_env_placeholders_expand_when_enabled(self) -> None:
        cfg = McpServerConfig(
            name="argvserver",
            command="server",
            args=("--port=${MCP_PORT}",),
            env={"MCP_PORT": "8123"},
            expand_argv_env=True,
        )

        assert mcp_router._expand_config_args(cfg, mcp_router._child_env_for_config(cfg)) == (
            "--port=8123",
        )

    def test_argv_secret_expansion_requires_explicit_opt_in(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MCP_API_KEY", "argv-only-secret")
        cfg = McpServerConfig(
            name="argvserver",
            command="server",
            args=("--api-key", "${MCP_API_KEY}"),
            credential_env=("MCP_API_KEY",),
            allow_argv_secret_expansion=True,
            expand_argv_env=True,
        )

        assert mcp_router._expand_config_args(cfg, mcp_router._child_env_for_config(cfg)) == (
            "--api-key",
            "argv-only-secret",
        )


# ── McpRouter ─────────────────────────────────────────────────────────────────


class TestRouter:
    def test_single_server(self) -> None:
        with running_router([_make_config("fake")]) as router:
            tools = router.list_tools()
            names = {t.name for t in tools}
            assert {"fake__echo", "fake__add"} <= names

    def test_namespaced_call(self) -> None:
        with running_router([_make_config("fake")]) as router:
            result = router.call("fake__echo", {"text": "round-trip"})
            assert result == "round-trip"

    def test_call_rejects_unpublished_tool_before_dispatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        dispatched: list[str] = []

        class BoundaryClient:
            def __init__(
                self,
                config: McpServerConfig,
                *,
                session_id: str | None = None,
            ) -> None:
                pass

            def start(self) -> None:
                pass

            def list_tools(self) -> list[ToolDefinition]:
                return [
                    ToolDefinition(
                        name="public",
                        description="Published tool",
                        parameters={},
                    )
                ]

            def call_tool(
                self,
                name: str,
                arguments: dict[str, Any],
                *,
                capability_epoch: int | None = None,
            ) -> str:
                assert capability_epoch is None
                dispatched.append(name)
                return "dispatched"

            def stop(self) -> bool:
                return True

        monkeypatch.setattr(mcp_router, "McpClient", BoundaryClient)
        router = McpRouter([_make_config("fake")], lazy=True)
        router.start()
        try:
            assert router.server_names == []
            assert [tool.name for tool in router.activate(["fake"])] == ["fake__public"]

            with pytest.raises(ValueError, match=r"unknown MCP tool 'fake__hidden'"):
                router.call("fake__hidden", {})
            assert dispatched == []

            assert router.call("fake__public", {}) == "dispatched"
            assert dispatched == ["public"]
        finally:
            router.stop()

    def test_multi_server_union(self) -> None:
        cfgs = [
            _make_config("a"),
            _make_config("b", extra_env={"FAKE_MCP_EXTRA_TOOL": "special"}),
        ]
        with running_router(cfgs) as router:
            tools = router.list_tools()
            names = {t.name for t in tools}
            assert "a__echo" in names
            assert "a__add" in names
            assert "b__echo" in names
            assert "b__special" in names

    def test_multi_server_routing(self) -> None:
        """Each server must get the call meant for it, and only it."""
        cfgs = [_make_config("a"), _make_config("b")]
        with running_router(cfgs) as router:
            a_result = router.call("a__echo", {"text": "from-a"})
            b_result = router.call("b__echo", {"text": "from-b"})
            assert a_result == "from-a"
            assert b_result == "from-b"

    def test_unknown_server(self) -> None:
        with running_router([_make_config("fake")]) as router:
            with pytest.raises(ValueError, match="unknown MCP server"):
                router.call("ghost__echo", {})

    def test_malformed_qualified_name(self) -> None:
        with running_router([_make_config("fake")]) as router:
            with pytest.raises(ValueError, match="expected"):
                router.call("no_separator_here", {})

    def test_duplicate_server_name_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs = _capture_popen(monkeypatch)
        router = McpRouter([_make_config("dup"), _make_config("dup")])
        with pytest.raises(ValueError, match="duplicate MCP server"):
            router.start()
        # Atomic rollback — the first (already-started) server must be
        # reaped so we don't leak child processes.
        assert len(procs) == 1
        _assert_exited(procs[0])
        assert router.server_names == []

    def test_server_name_cannot_contain_router_separator(self) -> None:
        with pytest.raises(ValueError, match="may not contain"):
            McpServerConfig(name="foo__bar", command=sys.executable)
        with pytest.raises(ValueError, match="reserved"):
            McpServerConfig(name="ctx", command=sys.executable)
        with running_router(
            [_make_config("fake", extra_env={"FAKE_MCP_EXTRA_TOOL": "echo"})]
        ) as router:
            with pytest.raises(ValueError, match="duplicate MCP tool name"):
                router.list_tools()

    def test_stopped_router_rejects_calls(self) -> None:
        router = McpRouter([_make_config("fake")])
        with pytest.raises(RuntimeError, match="not started"):
            router.list_tools()
        with pytest.raises(RuntimeError, match="not started"):
            router.call("fake__echo", {})

    def test_double_start_is_idempotent(self) -> None:
        router = McpRouter([_make_config("fake")])
        router.start()
        try:
            router.start()  # must not spawn a second server
            assert router.server_names == ["fake"]
        finally:
            router.stop()

    def test_lazy_router_activates_exact_server_and_reaps_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs = _capture_popen(monkeypatch)
        events: list[dict[str, Any]] = []

        def capture_event(event_name: str, **kwargs: Any) -> None:
            events.append({"event_name": event_name, **kwargs})

        monkeypatch.setattr(mcp_router, "record_event", capture_event)
        router = McpRouter(
            [_make_config("alpha"), _make_config("beta")],
            session_id="lazy-session",
            lazy=True,
        )
        router.start()
        try:
            assert router.lazy is True
            assert router.configured_server_names == ["alpha", "beta"]
            assert router.server_names == []
            assert router.list_tools() == []
            assert procs == []

            tools = router.activate(["beta"], capability_epoch=17)
            assert len(procs) == 1
            assert router.server_names == ["beta"]
            assert {tool.name for tool in tools} >= {"beta__echo", "beta__add"}
            assert all(tool.name.startswith("beta__") for tool in tools)
            assert router.call("beta__echo", {"text": "leased"}) == "leased"

            router.deactivate(["beta"])
            assert router.server_names == []
            _assert_exited(procs[0])
        finally:
            router.stop()

        transitions = [
            event
            for event in events
            if event["event_name"] in {"ctx.mcp.activation", "ctx.mcp.deactivation"}
        ]
        assert [
            (event["event_name"], event["payload"]["ctx.mcp.phase"]) for event in transitions
        ] == [
            ("ctx.mcp.activation", "requested"),
            ("ctx.mcp.activation", "applied"),
            ("ctx.mcp.deactivation", "requested"),
            ("ctx.mcp.deactivation", "applied"),
        ]
        assert transitions[1]["payload"]["ctx.mcp.tool.count"] == len(tools)
        assert transitions[1]["payload"]["ctx.mcp.server.hashes"] == [
            telemetry.hash_identifier("beta")
        ]
        assert all(
            transition["payload"]["ctx.mcp.capability.epoch"] == 17 for transition in transitions
        )
        assert transitions[1]["payload"]["ctx.mcp.process.started.count"] == 1
        deactivation = transitions[-1]["payload"]
        assert deactivation["ctx.mcp.process.reap.attempted.count"] == 1
        assert deactivation["ctx.mcp.process.reap.succeeded.count"] == 1
        assert deactivation["ctx.mcp.process.reap.failed.count"] == 0
        assert deactivation["ctx.mcp.process.reap.outcome"] == "complete"
        assert deactivation["ctx.mcp.process.lifetime.observed.count"] == 1
        assert deactivation["ctx.mcp.process.lifetime_ms.max"] >= 0
        assert deactivation["ctx.mcp.process.lifetime_ms.total"] >= 0
        external_call = next(
            event for event in events if event["event_name"] == "ctx.mcp.external_tool_call"
        )
        assert external_call["payload"]["ctx.mcp.server.hash"] == telemetry.hash_identifier("beta")
        assert external_call["payload"]["ctx.mcp.tool.hash"] == telemetry.hash_identifier(
            "beta__echo"
        )
        assert external_call["payload"]["ctx.mcp.capability.epoch"] == 17
        assert "beta" not in json.dumps(transitions)

    def test_lazy_router_rejects_unknown_grant_before_spawning(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs = _capture_popen(monkeypatch)
        router = McpRouter([_make_config("known")], lazy=True)
        router.start()
        try:
            with pytest.raises(ValueError, match="unknown MCP server grant"):
                router.activate(["known", "missing"])
            assert procs == []
            assert router.server_names == []
        finally:
            router.stop()

    def test_lazy_router_reaps_failed_activation_with_epoch_telemetry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs = _capture_popen(monkeypatch)
        events: list[dict[str, Any]] = []

        def capture_event(event_name: str, **kwargs: Any) -> None:
            events.append({"event_name": event_name, **kwargs})

        monkeypatch.setattr(mcp_router, "record_event", capture_event)
        router = McpRouter(
            [
                _make_config(
                    "private-failing",
                    extra_env={"FAKE_MCP_FAIL_INIT": "1"},
                )
            ],
            lazy=True,
        )
        router.start()
        try:
            with pytest.raises(McpServerError):
                router.activate(["private-failing"], capability_epoch=29)
            assert router.server_names == []
            assert len(procs) == 1
            _assert_exited(procs[0])
        finally:
            router.stop()

        failed = next(
            event
            for event in events
            if event["event_name"] == "ctx.mcp.activation"
            and event["payload"]["ctx.mcp.phase"] == "failed"
        )
        payload = failed["payload"]
        assert payload["ctx.mcp.capability.epoch"] == 29
        assert payload["ctx.mcp.process.started.count"] == 1
        assert payload["ctx.mcp.process.reap.attempted.count"] == 1
        assert payload["ctx.mcp.process.reap.succeeded.count"] == 1
        assert payload["ctx.mcp.process.reap.failed.count"] == 0
        assert payload["ctx.mcp.process.reap.outcome"] == "complete"
        assert payload["ctx.mcp.cleanup.complete.count"] == 1
        assert payload["ctx.mcp.cleanup.incomplete.count"] == 0
        assert payload["ctx.mcp.process.lifetime.observed.count"] == 1
        assert "private-failing" not in json.dumps(failed)

    def test_lazy_router_retains_failed_shutdown_for_final_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instances: list[Any] = []
        events: list[dict[str, Any]] = []

        def capture_event(event_name: str, **kwargs: Any) -> None:
            events.append({"event_name": event_name, **kwargs})

        class FlakyClient:
            def __init__(self, config: McpServerConfig, *, session_id: str | None = None) -> None:
                self.stop_calls = 0
                instances.append(self)

            def start(self) -> None:
                pass

            def list_tools(self) -> list[Any]:
                return []

            def stop(self) -> bool:
                self.stop_calls += 1
                return self.stop_calls >= 2

            @property
            def process_observed_age_ms(self) -> float:
                return 12.5

            @property
            def process_exited(self) -> bool:
                return self.stop_calls >= 2

        monkeypatch.setattr(mcp_router, "record_event", capture_event)
        monkeypatch.setattr(mcp_router, "McpClient", FlakyClient)
        router = McpRouter([_make_config("flaky")], lazy=True)
        router.start()
        router.activate(["flaky"], capability_epoch=31)

        with pytest.raises(McpServerError, match="did not fully stop"):
            router.deactivate(["flaky"])
        assert router.server_names == []
        assert instances[0].stop_calls == 1
        failed = next(
            event
            for event in events
            if event["event_name"] == "ctx.mcp.deactivation"
            and event["payload"]["ctx.mcp.phase"] == "failed"
        )
        assert failed["payload"]["ctx.mcp.capability.epoch"] == 31
        assert failed["payload"]["ctx.mcp.process.reap.attempted.count"] == 1
        assert failed["payload"]["ctx.mcp.process.reap.succeeded.count"] == 0
        assert failed["payload"]["ctx.mcp.process.reap.failed.count"] == 1
        assert failed["payload"]["ctx.mcp.process.reap.outcome"] == "incomplete"
        assert failed["payload"]["ctx.mcp.cleanup.complete.count"] == 0
        assert failed["payload"]["ctx.mcp.cleanup.incomplete.count"] == 1
        assert failed["payload"]["ctx.mcp.process.lifetime.observed.count"] == 0
        assert failed["payload"]["ctx.mcp.process.unreaped_age.observed.count"] == 1
        assert failed["payload"]["ctx.mcp.process.unreaped_age_ms.max"] == 12.5
        assert "ctx.mcp.process.lifetime_ms.max" not in failed["payload"]
        assert "ctx.mcp.process.lifetime_ms.total" not in failed["payload"]
        assert "flaky" not in json.dumps(failed)

        router.stop()
        assert instances[0].stop_calls == 2
        recovered = next(
            event
            for event in events
            if event["event_name"] == "ctx.mcp.deactivation"
            and event["payload"]["ctx.mcp.phase"] == "recovered"
        )
        recovered_payload = recovered["payload"]
        assert recovered["outcome"] == "ok"
        assert recovered["error_kind"] is None
        assert recovered_payload["ctx.mcp.capability.epoch"] == 31
        assert recovered_payload["ctx.mcp.server.hashes"] == [telemetry.hash_identifier("flaky")]
        assert recovered_payload["ctx.mcp.process.reap.attempted.count"] == 1
        assert recovered_payload["ctx.mcp.process.reap.succeeded.count"] == 1
        assert recovered_payload["ctx.mcp.process.reap.failed.count"] == 0
        assert recovered_payload["ctx.mcp.process.reap.outcome"] == "complete"
        assert recovered_payload["ctx.mcp.cleanup.complete.count"] == 1
        assert recovered_payload["ctx.mcp.cleanup.incomplete.count"] == 0
        assert recovered_payload["ctx.mcp.process.lifetime.observed.count"] == 1
        assert recovered_payload["ctx.mcp.process.unreaped_age.observed.count"] == 0
        assert recovered_payload["ctx.mcp.process.lifetime_ms.max"] == 12.5
        assert recovered_payload["ctx.mcp.process.lifetime_ms.total"] == 12.5
        assert "ctx.mcp.process.unreaped_age_ms.max" not in recovered_payload
        assert "flaky" not in json.dumps(recovered)

    def test_recovery_does_not_double_count_lifetime_after_reader_cleanup(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instances: list[Any] = []
        events: list[dict[str, Any]] = []

        def capture_event(event_name: str, **kwargs: Any) -> None:
            events.append({"event_name": event_name, **kwargs})

        class ReaderCleanupClient:
            def __init__(self, config: McpServerConfig, *, session_id: str | None = None) -> None:
                self.stop_calls = 0
                instances.append(self)

            def start(self) -> None:
                pass

            def list_tools(self) -> list[Any]:
                return []

            def stop(self) -> bool:
                self.stop_calls += 1
                return self.stop_calls >= 3

            @property
            def process_observed_age_ms(self) -> float:
                return 12.5

            @property
            def process_exited(self) -> bool:
                return True

        monkeypatch.setattr(mcp_router, "record_event", capture_event)
        monkeypatch.setattr(mcp_router, "McpClient", ReaderCleanupClient)
        router = McpRouter([_make_config("private-reader")], lazy=True)
        router.start()
        router.activate(["private-reader"], capability_epoch=41)

        with pytest.raises(McpServerError, match="did not fully stop"):
            router.deactivate(["private-reader"])
        router.stop()
        router.stop()

        assert instances[0].stop_calls == 3
        transitions = [
            event
            for event in events
            if event["event_name"] == "ctx.mcp.deactivation"
            and event["payload"]["ctx.mcp.phase"] in {"failed", "recovery_failed", "recovered"}
        ]
        assert [event["payload"]["ctx.mcp.phase"] for event in transitions] == [
            "failed",
            "recovery_failed",
            "recovered",
        ]
        failed_payload, retry_failed_payload, recovered_payload = [
            event["payload"] for event in transitions
        ]
        assert failed_payload["ctx.mcp.process.reap.outcome"] == "complete"
        assert failed_payload["ctx.mcp.cleanup.incomplete.count"] == 1
        assert failed_payload["ctx.mcp.process.lifetime.observed.count"] == 1
        assert failed_payload["ctx.mcp.process.lifetime_ms.total"] == 12.5
        assert retry_failed_payload["ctx.mcp.process.reap.succeeded.count"] == 1
        assert retry_failed_payload["ctx.mcp.cleanup.incomplete.count"] == 1
        assert retry_failed_payload["ctx.mcp.process.lifetime.observed.count"] == 0
        assert "ctx.mcp.process.lifetime_ms.max" not in retry_failed_payload
        assert "ctx.mcp.process.lifetime_ms.total" not in retry_failed_payload
        assert recovered_payload["ctx.mcp.capability.epoch"] == 41
        assert recovered_payload["ctx.mcp.server.hashes"] == [
            telemetry.hash_identifier("private-reader")
        ]
        assert recovered_payload["ctx.mcp.process.reap.succeeded.count"] == 1
        assert recovered_payload["ctx.mcp.cleanup.complete.count"] == 1
        assert recovered_payload["ctx.mcp.process.lifetime.observed.count"] == 0
        assert "ctx.mcp.process.lifetime_ms.max" not in recovered_payload
        assert "ctx.mcp.process.lifetime_ms.total" not in recovered_payload
        assert (
            sum(
                event["payload"]["ctx.mcp.process.lifetime.observed.count"] for event in transitions
            )
            == 1
        )
        assert (
            sum(
                event["payload"].get("ctx.mcp.process.lifetime_ms.total", 0.0)
                for event in transitions
            )
            == 12.5
        )
        assert "private-reader" not in json.dumps(transitions)

    def test_lazy_router_reserves_dormant_server_namespace(self) -> None:
        router = McpRouter([_make_config("owner")], lazy=True)
        router.start()
        try:
            caller_tool = ToolDefinition(
                name="owner__local",
                description="caller tool",
                parameters={},
            )
            with pytest.raises(ValueError, match="caller tool namespace"):
                _collect_tools(router, [caller_tool])
            assert router.server_names == []
        finally:
            router.stop()

    def test_tool_descriptions_unchanged_by_namespacing(self) -> None:
        """The server prefix goes on the NAME only — descriptions stay clean."""
        with running_router([_make_config("fake")]) as router:
            tools = router.list_tools()
            echo = next(t for t in tools if t.name == "fake__echo")
            # Description is the server-local one, no "fake__" prefix.
            assert echo.description == "Echo the input text verbatim."

    def test_server_names_sorted(self) -> None:
        cfgs = [_make_config("beta"), _make_config("alpha"), _make_config("gamma")]
        with running_router(cfgs) as router:
            assert router.server_names == ["alpha", "beta", "gamma"]

    def test_atomic_startup_on_second_config_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If one server fails to start, already-spawned ones must be reaped."""
        procs = _capture_popen(monkeypatch)
        cfgs = [
            _make_config("ok"),
            _make_config("bad", extra_env={"FAKE_MCP_FAIL_INIT": "1"}),
        ]
        router = McpRouter(cfgs)
        with pytest.raises(McpServerError):
            router.start()
        assert len(procs) == 2
        for proc in procs:
            _assert_exited(proc)
        assert router.server_names == []


# ── _flatten_content ────────────────────────────────────────────────────────


class TestFlattenContent:
    def test_empty(self) -> None:
        from ctx.adapters.generic.tools.mcp_router import _flatten_content

        assert _flatten_content([]) == ""
        assert _flatten_content(None) == ""

    def test_single_text(self) -> None:
        from ctx.adapters.generic.tools.mcp_router import _flatten_content

        assert _flatten_content([{"type": "text", "text": "hi"}]) == "hi"

    def test_multi_text_concatenated(self) -> None:
        from ctx.adapters.generic.tools.mcp_router import _flatten_content

        assert (
            _flatten_content(
                [
                    {"type": "text", "text": "a"},
                    {"type": "text", "text": "b"},
                    {"type": "text", "text": "c"},
                ]
            )
            == "abc"
        )

    def test_image_block_summarised(self) -> None:
        from ctx.adapters.generic.tools.mcp_router import _flatten_content

        out = _flatten_content([{"type": "image", "mimeType": "image/png"}])
        assert "[image/png image omitted]" in out

    def test_resource_block_summarised(self) -> None:
        from ctx.adapters.generic.tools.mcp_router import _flatten_content

        out = _flatten_content(
            [{"type": "resource", "resource": {"uri": "file:///Users/alice/private/x.md"}}]
        )
        assert "[resource omitted]" in out
        assert "file:///Users/alice/private/x.md" not in out

    def test_unknown_type(self) -> None:
        from ctx.adapters.generic.tools.mcp_router import _flatten_content

        out = _flatten_content([{"type": "fancy", "blob": "opaque"}])
        assert "[fancy block omitted]" in out

    def test_non_dict_block(self) -> None:
        from ctx.adapters.generic.tools.mcp_router import _flatten_content

        assert _flatten_content(["just a string"]) == "just a string"


# ── Config dataclass ────────────────────────────────────────────────────────


class TestConfig:
    def test_frozen(self) -> None:
        cfg = _make_config("x")
        with pytest.raises(Exception):  # FrozenInstanceError
            cfg.name = "y"  # type: ignore[misc]

    def test_default_env_is_fresh_dict_per_instance(self) -> None:
        a = McpServerConfig(name="a", command="python")
        b = McpServerConfig(name="b", command="python")
        # dataclass field(default_factory=dict) must not share state.
        assert a.env is not b.env
