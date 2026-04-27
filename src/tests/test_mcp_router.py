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

import sys
import time
from pathlib import Path

import pytest

from ctx.adapters.generic.tools import (
    McpClient,
    McpRouter,
    McpServerConfig,
    McpServerError,
    running_router,
)


_FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _make_config(
    name: str = "fake",
    *,
    extra_env: dict[str, str] | None = None,
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
        startup_timeout=startup_timeout,
        request_timeout=request_timeout,
        inherit_env=inherit_env,
    )


# ── McpClient basics ─────────────────────────────────────────────────────────


class TestClientLifecycle:
    def test_start_and_stop(self) -> None:
        client = McpClient(_make_config())
        client.start()
        try:
            # Trivial health: list_tools succeeds → handshake completed.
            tools = client.list_tools()
            assert len(tools) >= 2  # echo + add
        finally:
            client.stop()

    def test_context_manager(self) -> None:
        with McpClient(_make_config()) as client:
            tools = client.list_tools()
            names = {t.name for t in tools}
            assert {"echo", "add"} <= names

    def test_double_start_rejected(self) -> None:
        client = McpClient(_make_config())
        client.start()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                client.start()
        finally:
            client.stop()

    def test_stop_before_start_is_noop(self) -> None:
        client = McpClient(_make_config())
        client.stop()  # must not raise

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


class TestClientRobustness:
    def test_init_failure_surfaces_and_reaps_child(self) -> None:
        """When initialize errors, start() must clean up — no zombie child."""
        client = McpClient(_make_config(extra_env={"FAKE_MCP_FAIL_INIT": "1"}))
        with pytest.raises(McpServerError, match="init-forbidden"):
            client.start()
        # After failed start, _proc is reset to None so a follow-up stop()
        # is a safe no-op and no resource leaks.
        client.stop()

    def test_server_crash_during_call(self) -> None:
        with McpClient(_make_config(extra_env={"FAKE_MCP_CRASH_ON_TOOL": "1"})) as c:
            with pytest.raises(McpServerError, match="pipe closed"):
                c.call_tool("echo", {"text": "x"})

    def test_server_notification_is_skipped(self) -> None:
        """The client ignores notifications that interleave with a response."""
        cfg = _make_config(extra_env={"FAKE_MCP_EMIT_NOTIFICATION": "1"})
        with McpClient(cfg) as client:
            result = client.call_tool("echo", {"text": "hi"})
            assert result == "hi"

    def test_stderr_captured_on_startup(self) -> None:
        cfg = _make_config(extra_env={"FAKE_MCP_NOISY_STDERR": "1"})
        with McpClient(cfg) as client:
            client.list_tools()  # let the server run
        # Can't poke at internal state much — just confirm no hang.
        # The drain thread is a daemon; if it deadlocked the pipe,
        # list_tools above would have timed out.

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

    def test_parent_env_is_not_inherited_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CTX_SECRET_SHOULD_NOT_LEAK", "leaked")
        with McpClient(_make_config()) as client:
            assert client.call_tool(
                "echo_env", {"name": "CTX_SECRET_SHOULD_NOT_LEAK"}
            ) == ""

    def test_explicit_env_overlay_is_passed(self) -> None:
        cfg = _make_config(extra_env={"CTX_ALLOWED_FOR_TEST": "visible"})
        with McpClient(cfg) as client:
            assert client.call_tool(
                "echo_env", {"name": "CTX_ALLOWED_FOR_TEST"}
            ) == "visible"

    def test_full_env_inheritance_requires_opt_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CTX_LEGACY_INHERIT_TEST", "visible")
        with McpClient(_make_config(inherit_env=True)) as client:
            assert client.call_tool(
                "echo_env", {"name": "CTX_LEGACY_INHERIT_TEST"}
            ) == "visible"


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

    def test_duplicate_server_name_rejected(self) -> None:
        router = McpRouter([_make_config("dup"), _make_config("dup")])
        with pytest.raises(ValueError, match="duplicate MCP server"):
            router.start()
        # Atomic rollback — the first (already-started) server must be
        # reaped so we don't leak child processes.
        assert router.server_names == []

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

    def test_atomic_startup_on_second_config_failure(self) -> None:
        """If one server fails to start, already-spawned ones must be reaped."""
        cfgs = [
            _make_config("ok"),
            _make_config("bad", extra_env={"FAKE_MCP_FAIL_INIT": "1"}),
        ]
        router = McpRouter(cfgs)
        with pytest.raises(McpServerError):
            router.start()
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

        assert _flatten_content(
            [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b"},
                {"type": "text", "text": "c"},
            ]
        ) == "abc"

    def test_image_block_summarised(self) -> None:
        from ctx.adapters.generic.tools.mcp_router import _flatten_content

        out = _flatten_content([{"type": "image", "mimeType": "image/png"}])
        assert "[image/png image omitted]" in out

    def test_resource_block_summarised(self) -> None:
        from ctx.adapters.generic.tools.mcp_router import _flatten_content

        out = _flatten_content([
            {"type": "resource", "resource": {"uri": "file:///x.md"}}
        ])
        assert "[resource: file:///x.md]" in out

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
