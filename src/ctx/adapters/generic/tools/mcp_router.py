"""ctx.adapters.generic.tools.mcp_router — sync MCP client + router.

Spawns one or more MCP servers as child processes, speaks JSON-RPC 2.0
to each over stdio, and routes tool calls from the harness to the
right server by namespaced tool name.

Why roll our own sync client instead of using the `mcp` SDK?
  The official SDK is async-first. Our harness v1 runs a synchronous
  while-loop (mirror of ``litellm.completion`` being sync), so wrapping
  async code in ``asyncio.run_coroutine_threadsafe`` + a background
  event loop per connection would balloon complexity without buying
  anything for the solo-agent v1. We speak the minimal MCP subset
  (initialize + tools/list + tools/call + shutdown) in ~250 LOC of
  straightforward JSON-RPC — worth it for the simpler v1 surface.
  If/when H11 brings the Evaluator agent and parallel tool execution,
  swapping in the async SDK is a localised refactor behind the same
  ``McpRouter.call()`` contract.

Protocol coverage:
  * initialize / initialized notification
  * tools/list
  * tools/call
  * shutdown (notification, best-effort)

NOT covered (yet — add when harness needs them):
  * resources/* (resource-aware flows)
  * prompts/* (MCP prompt templates)
  * Notifications from the server (e.g. progress, log messages)
  * sampling/createMessage (server-driven model calls — rare)

MCP spec reference: https://spec.modelcontextprotocol.io/

Plan 001 Phase H2.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from ctx.adapters.generic.providers.base import ToolDefinition

_logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "ctx-harness", "version": "0.1"}

# Tool names combine server name and tool name; "__" is the separator
# so a server named "github" with a tool "list_repos" surfaces to the
# model as "github__list_repos". Uses a double underscore to avoid
# colliding with legitimate snake_case identifiers.
TOOL_SEPARATOR = "__"


# ── Config ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class McpServerConfig:
    """How to spawn one MCP server.

    ``command`` + ``args`` mirror the argv-list form of subprocess.Popen,
    so there is no shell interpolation — a server config cannot inject
    shell metacharacters into the spawn call.

    ``env`` overlays onto the parent's environment; unset keys fall
    through. Pass ``env={}`` to inherit verbatim.
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    startup_timeout: float = 10.0
    request_timeout: float = 30.0


class McpServerError(RuntimeError):
    """Raised when the MCP server returns a protocol error or crashes."""


# ── Per-server client ─────────────────────────────────────────────────────


class McpClient:
    """Sync JSON-RPC 2.0 client for a single MCP server over stdio.

    Lifecycle: ``start()`` spawns the server and exchanges initialize
    handshake. ``stop()`` sends a shutdown notification and reaps the
    child. The client is single-request-at-a-time — concurrent
    ``call_tool`` calls on the same client are serialized by a lock.
    """

    def __init__(self, config: McpServerConfig) -> None:
        self._config = config
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._tools_cache: list[ToolDefinition] | None = None
        # Capture stderr for diagnostics; reading runs in a background
        # thread so a chatty server doesn't block the pipe.
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the server and complete the initialize handshake."""
        if self._proc is not None:
            raise RuntimeError(f"MCP client '{self._config.name}' already started")

        env = os.environ.copy()
        env.update(self._config.env)

        self._proc = subprocess.Popen(
            [self._config.command, *self._config.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,  # unbuffered; we flush each write ourselves
        )
        assert self._proc.stdin and self._proc.stdout and self._proc.stderr

        # Drain stderr in the background so a verbose server can't fill
        # the OS pipe buffer and deadlock us.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True
        )
        self._stderr_thread.start()

        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
                timeout=self._config.startup_timeout,
            )
        except Exception:
            self.stop()
            raise

        # Initialized notification — per spec the server expects this
        # before accepting operational requests.
        self._notify("notifications/initialized", {})

    def stop(self) -> None:
        """Best-effort shutdown. Never raises."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            # Close stdin to signal the server to exit cleanly.
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                _logger.error(
                    "MCP server '%s' did not die after kill",
                    self._config.name,
                )
        except Exception as exc:  # noqa: BLE001
            _logger.debug(
                "MCP server '%s' stop error: %s", self._config.name, exc
            )

    def __enter__(self) -> "McpClient":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # ── MCP operations ────────────────────────────────────────────────────

    def list_tools(self) -> list[ToolDefinition]:
        """Return the server's tool catalog (cached on first call).

        The ToolDefinition.name returned here is the server-local name
        (e.g. "read_file") — the router prepends the server name +
        TOOL_SEPARATOR before exposing it to the model.
        """
        if self._tools_cache is not None:
            return list(self._tools_cache)
        result = self._request("tools/list", {})
        raw_tools = result.get("tools", [])
        tools: list[ToolDefinition] = []
        for t in raw_tools:
            name = t.get("name")
            if not isinstance(name, str) or not name:
                continue
            tools.append(
                ToolDefinition(
                    name=name,
                    description=str(t.get("description", "")),
                    parameters=t.get("inputSchema") or {
                        "type": "object",
                        "properties": {},
                    },
                )
            )
        self._tools_cache = tools
        return list(tools)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke a tool on this server. Returns the concatenated text output.

        MCP tool responses return a content array (text + image + resource
        blocks). We concatenate the text blocks into a single string since
        the harness's v1 tool-result shape is a string. Non-text content
        is summarised as a short placeholder — the harness can grow
        multi-modal tool results in a later phase.

        Raises ``McpServerError`` when the server reports an error.
        """
        result = self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        if result.get("isError"):
            content = _flatten_content(result.get("content", []))
            raise McpServerError(
                f"tool '{name}' on '{self._config.name}' reported isError: {content}"
            )
        return _flatten_content(result.get("content", []))

    # ── JSON-RPC plumbing ─────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request, wait for the matching response."""
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError(
                f"MCP client '{self._config.name}' is not started"
            )
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
            self._write_frame(request)

            deadline = None
            if timeout is not None:
                deadline = time.monotonic() + timeout
            elif self._config.request_timeout > 0:
                deadline = time.monotonic() + self._config.request_timeout

            # Read frames until we see our response id. MCP servers may
            # emit notifications interleaved with responses; skip those
            # (log + continue) rather than treating them as errors.
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise McpServerError(
                            f"{self._config.name}.{method}: timed out after "
                            f"{timeout if timeout is not None else self._config.request_timeout}s"
                        )
                frame = self._read_frame()
                if frame is None:
                    stderr_tail = "\n".join(self._stderr_lines[-20:])
                    raise McpServerError(
                        f"{self._config.name} pipe closed before response to "
                        f"{method!r}. stderr tail:\n{stderr_tail}"
                    )
                # Notifications have no ``id``; skip them for now.
                if "id" not in frame:
                    _logger.debug(
                        "MCP %s notification: %s",
                        self._config.name, frame.get("method"),
                    )
                    continue
                if frame.get("id") != request_id:
                    # Late response to a prior request (shouldn't happen
                    # while we hold the lock, but defensive).
                    _logger.debug(
                        "MCP %s stale response id=%s (waiting for %s)",
                        self._config.name, frame.get("id"), request_id,
                    )
                    continue
                if "error" in frame:
                    err = frame["error"]
                    raise McpServerError(
                        f"{self._config.name}.{method}: "
                        f"code={err.get('code')} message={err.get('message')!r}"
                    )
                return frame.get("result") or {}

    def _notify(self, method: str, params: dict[str, Any] | None) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        frame = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        try:
            self._write_frame(frame)
        except (BrokenPipeError, OSError) as exc:
            _logger.debug(
                "MCP %s: notify %s failed: %s",
                self._config.name, method, exc,
            )

    def _write_frame(self, frame: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        payload = (json.dumps(frame) + "\n").encode("utf-8")
        self._proc.stdin.write(payload)
        self._proc.stdin.flush()

    def _read_frame(self) -> dict[str, Any] | None:
        assert self._proc is not None and self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            _logger.warning(
                "MCP %s: dropping malformed frame: %s (raw=%r)",
                self._config.name, exc, line,
            )
            return self._read_frame()

    def _drain_stderr(self) -> None:
        """Consume stderr in the background; keep the last ~200 lines."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                self._stderr_lines.append(line)
                # Cap memory usage on a chatty server.
                if len(self._stderr_lines) > 200:
                    del self._stderr_lines[:-200]
        except Exception:  # noqa: BLE001
            pass


# ── Multi-server router ───────────────────────────────────────────────────


class McpRouter:
    """Manages a set of ``McpClient``s, routes tool calls by namespaced name.

    Tool names exposed to the model are ``<server>__<tool>`` — the
    router splits on ``TOOL_SEPARATOR`` to route to the right server.
    Server-level collisions (two servers with a tool called
    ``read_file``) are not a problem because the namespace prefix
    distinguishes them.
    """

    def __init__(self, configs: list[McpServerConfig]) -> None:
        self._configs = list(configs)
        self._clients: dict[str, McpClient] = {}
        self._started = False

    def start(self) -> None:
        """Spawn every configured server; roll back all on any failure."""
        if self._started:
            return
        spawned: list[str] = []
        try:
            for cfg in self._configs:
                if cfg.name in self._clients:
                    raise ValueError(
                        f"duplicate MCP server name {cfg.name!r}"
                    )
                client = McpClient(cfg)
                client.start()
                self._clients[cfg.name] = client
                spawned.append(cfg.name)
        except Exception:
            # Atomic startup — tear down any already-started servers so
            # we don't leak child processes when a later config fails.
            for name in spawned:
                try:
                    self._clients[name].stop()
                except Exception:  # noqa: BLE001
                    pass
            self._clients.clear()
            raise
        self._started = True

    def stop(self) -> None:
        for client in list(self._clients.values()):
            try:
                client.stop()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()
        self._started = False

    def __enter__(self) -> "McpRouter":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def list_tools(self) -> list[ToolDefinition]:
        """Return the namespaced union of every server's tools.

        ``<server>__<tool>`` is the qualified name the model sees.
        Server-local descriptions are preserved; the server prefix
        becomes part of the tool name only, not the description —
        that way the model's reasoning about what a tool does isn't
        cluttered with routing metadata.
        """
        if not self._started:
            raise RuntimeError("router not started; call start() first")
        out: list[ToolDefinition] = []
        for server_name, client in self._clients.items():
            for tool in client.list_tools():
                out.append(
                    ToolDefinition(
                        name=f"{server_name}{TOOL_SEPARATOR}{tool.name}",
                        description=tool.description,
                        parameters=tool.parameters,
                    )
                )
        return out

    def call(self, qualified_name: str, arguments: dict[str, Any]) -> str:
        """Dispatch a tool call to the owning server."""
        if not self._started:
            raise RuntimeError("router not started; call start() first")
        if TOOL_SEPARATOR not in qualified_name:
            raise ValueError(
                f"expected '<server>{TOOL_SEPARATOR}<tool>' name, got {qualified_name!r}"
            )
        server, tool = qualified_name.split(TOOL_SEPARATOR, 1)
        client = self._clients.get(server)
        if client is None:
            raise ValueError(
                f"unknown MCP server {server!r}; known: {sorted(self._clients)}"
            )
        return client.call_tool(tool, arguments)

    @property
    def server_names(self) -> list[str]:
        return sorted(self._clients)


# ── Helpers ───────────────────────────────────────────────────────────────


def _flatten_content(content: list[dict[str, Any]] | None) -> str:
    """Concatenate a MCP tool-result content array into a single string.

    Text blocks pass through verbatim. Non-text blocks (image/resource)
    are summarised with a short tag; the harness loop can grow
    multi-modal tool results in a later phase.
    """
    if not content:
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text", "")))
        elif btype == "image":
            mime = block.get("mimeType", "image/*")
            parts.append(f"[{mime} image omitted]")
        elif btype == "resource":
            uri = block.get("resource", {}).get("uri", "<no-uri>")
            parts.append(f"[resource: {uri}]")
        else:
            parts.append(f"[{btype or 'unknown'} block omitted]")
    return "".join(parts)


@contextmanager
def running_router(configs: list[McpServerConfig]) -> Iterator[McpRouter]:
    """Context-manager sugar around ``McpRouter(configs)``."""
    router = McpRouter(configs)
    router.start()
    try:
        yield router
    finally:
        router.stop()
