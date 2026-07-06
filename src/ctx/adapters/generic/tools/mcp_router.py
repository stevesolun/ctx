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
import queue
import re
import signal
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from ctx.adapters.generic.providers.base import ToolDefinition
from ctx.telemetry import (
    hash_identifier,
    record_event,
    telemetry_enabled,
    telemetry_span,
    traceparent_from_span,
)
from ctx.utils._secret_scan import find_inline_secret_arg, secret_key_like

_logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "ctx-harness", "version": "0.1"}
_SAFE_PARENT_ENV_KEYS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TMP",
        "TEMP",
        "HOME",
        "USERPROFILE",
        "LANG",
    }
)
_SAFE_PARENT_ENV_PREFIXES = ("LC_",)

# Tool names combine server name and tool name; "__" is the separator
# so a server named "github" with a tool "list_repos" surfaces to the
# model as "github__list_repos". Uses a double underscore to avoid
# colliding with legitimate snake_case identifiers.
TOOL_SEPARATOR = "__"
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_SERVER_NAMES = frozenset({"ctx"})
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|"
    r"PASSWD|PRIVATE[_-]?KEY|CREDENTIAL|ACCESS[_-]?KEY|"
    r"CLIENT[_-]?SECRET|AUTHORIZATION|BEARER)[A-Z0-9_-]*)\s*([:=])\s*([^\s]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_VALUE_RE = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat|hf|sk|xox[baprs])"
    r"[_-]?[A-Za-z0-9_./+=-]{8,}\b"
)
_ENV_PLACEHOLDER_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)
_MIN_LITERAL_SECRET_LENGTH = 4


def _duration_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _current_trace_metadata(session_id: str | None) -> dict[str, str]:
    if not telemetry_enabled():
        return {}
    meta: dict[str, str] = {}
    traceparent = traceparent_from_span()
    if traceparent is not None:
        meta["traceparent"] = traceparent
    if session_id:
        meta["ctx.session.hash"] = hash_identifier(session_id)
    return meta


def _params_with_metadata(
    params: dict[str, Any] | None,
    *,
    session_id: str | None,
) -> dict[str, Any]:
    out = dict(params or {})
    meta = _current_trace_metadata(session_id)
    if not meta:
        return out
    existing = out.get("_meta")
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(meta)
    out["_meta"] = merged
    return out


def _record_mcp_client_tool_call(
    *,
    server: str,
    tool: str,
    session_id: str | None,
    outcome: str,
    duration_ms: float,
    error_kind: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "rpc.system": "jsonrpc",
        "rpc.method": "tools/call",
        "mcp.server.name": server,
        "mcp.tool.name": tool,
        "otel.status_code": "ERROR" if outcome == "error" else "OK",
    }
    try:
        record_event(
            "ctx.mcp.external_tool_call",
            source="ctx-mcp-router",
            transport="mcp-jsonrpc",
            session_id=session_id,
            outcome=outcome,
            duration_ms=duration_ms,
            error_kind=error_kind,
            payload=payload,
        )
    except Exception:  # noqa: BLE001 - telemetry must never break tool execution.
        pass


def _default_child_env() -> dict[str, str]:
    """Return parent env entries that are process plumbing, not credentials."""
    child_env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper_key = key.upper()
        if upper_key in _SAFE_PARENT_ENV_KEYS or any(
            upper_key.startswith(prefix) for prefix in _SAFE_PARENT_ENV_PREFIXES
        ):
            child_env[key] = value
    return child_env


def _validate_server_name(name: str) -> None:
    if TOOL_SEPARATOR in name or _SERVER_NAME_RE.fullmatch(name) is None:
        raise ValueError(
            "MCP server names must match [A-Za-z0-9][A-Za-z0-9_-]* "
            f"and may not contain {TOOL_SEPARATOR!r}: {name!r}"
        )
    if name.lower() in _RESERVED_SERVER_NAMES:
        raise ValueError(f"MCP server name {name!r} is reserved for built-in ctx tools")


def _validate_env_name(name: str) -> None:
    if _ENV_NAME_RE.fullmatch(name) is None:
        raise ValueError(f"invalid MCP credential env var name: {name!r}")


def _redact_sensitive_text(text: str, literal_values: Iterable[str] = ()) -> str:
    """Remove likely credential values from diagnostics before retention."""
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    text = _TOKEN_VALUE_RE.sub("[REDACTED]", text)
    for value in sorted(
        {
            value
            for value in literal_values
            if len(value) >= _MIN_LITERAL_SECRET_LENGTH and "[REDACTED]" not in value
        },
        key=len,
        reverse=True,
    ):
        text = text.replace(value, "[REDACTED]")
    return text


def _child_env_for_config(config: "McpServerConfig") -> dict[str, str]:
    env = os.environ.copy() if config.inherit_env else _default_child_env()
    for key in config.credential_env:
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(config.env)
    return env


def _stderr_redaction_values(config: "McpServerConfig", env: dict[str, str]) -> tuple[str, ...]:
    credential_keys = {key.upper() for key in config.credential_env}
    values = []
    for key, value in env.items():
        if key.upper() in credential_keys or secret_key_like(key):
            values.append(value)
    return tuple(dict.fromkeys(values))


def _env_placeholder_names(value: str) -> tuple[str, ...]:
    names: list[str] = []
    for match in _ENV_PLACEHOLDER_RE.finditer(value):
        name = match.group("braced") or match.group("bare")
        if name is not None:
            names.append(name)
    return tuple(names)


def _is_sensitive_env_reference(config: "McpServerConfig", name: str) -> bool:
    credential_keys = {key.upper() for key in config.credential_env}
    return name.upper() in credential_keys or secret_key_like(name)


def _expand_env_placeholders(value: str, env: dict[str, str]) -> str:
    def replace_match(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bare")
        if name is None:
            return match.group(0)
        return env.get(name, match.group(0))

    return _ENV_PLACEHOLDER_RE.sub(replace_match, value)


def _expand_config_args(config: "McpServerConfig", env: dict[str, str]) -> tuple[str, ...]:
    if not config.allow_argv_secret_expansion:
        for arg in config.args:
            for name in _env_placeholder_names(arg):
                if name in env and _is_sensitive_env_reference(config, name):
                    raise ValueError(
                        f"MCP server {config.name!r} argv references sensitive env var "
                        f"{name!r}; pass secrets through the child environment or set "
                        "allow_argv_secret_expansion=True only for trusted local servers"
                    )
    expanded = tuple(_expand_env_placeholders(arg, env) for arg in config.args)
    if not config.allow_argv_secret_expansion:
        secret_arg = find_inline_secret_arg(list(expanded))
        bearer_arg = next((arg for arg in expanded if _BEARER_RE.search(arg)), None)
        if secret_arg is not None or bearer_arg is not None:
            marker = secret_arg if secret_arg is not None else "Bearer"
            raise ValueError(
                f"MCP server {config.name!r} argv expands an env var into secret-looking "
                f"argument {marker!r}; pass secrets through the child environment or set "
                "allow_argv_secret_expansion=True only for trusted local servers"
            )
    return expanded


def _popen_process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _signal_process_tree(
    proc: subprocess.Popen[bytes],
    *,
    force: bool,
) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        args = ["taskkill", "/PID", str(proc.pid), "/T"]
        if force:
            args.append("/F")
        try:
            subprocess.run(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
            )
        except Exception:  # noqa: BLE001
            if force:
                proc.kill()
        return

    killpg = getattr(os, "killpg", None)
    if killpg is None:
        if force:
            proc.kill()
        else:
            proc.terminate()
        return

    sig = getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM
    try:
        killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    except Exception:  # noqa: BLE001
        if force:
            proc.kill()
        else:
            proc.terminate()


def _resolve_executable(command: str, env: dict[str, str]) -> str:
    """Resolve bare commands through PATH/PATHEXT before spawning.

    Windows does not let ``subprocess.Popen(["npx", ...])`` find
    ``npx.cmd`` reliably in all environments. ``shutil.which`` applies
    PATHEXT and gives us the actual executable path while leaving
    explicit paths untouched.
    """
    if not command.strip():
        raise ValueError("MCP command is empty")
    if os.path.isabs(command) or any(sep in command for sep in ("/", "\\")):
        return command
    return shutil.which(command, path=env.get("PATH")) or command


# ── Config ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class McpServerConfig:
    """How to spawn one MCP server.

    ``command`` + ``args`` mirror the argv-list form of subprocess.Popen,
    so there is no shell interpolation — a server config cannot inject
    shell metacharacters into the spawn call.

    ``args`` may contain ``$ENVVAR`` or ``${ENVVAR}`` placeholders. They
    expand from the final child environment immediately before spawn. Sensitive
    env vars are not expanded into argv unless ``allow_argv_secret_expansion``
    is explicitly enabled for a trusted local server.

    ``env`` is the explicit child overlay. Parent secrets are not
    inherited by default; only a small process-basics allowlist
    (PATH, temp/home, locale, Windows runtime vars) is passed through.
    ``credential_env`` names specific parent env vars to copy into the
    child without enabling full environment inheritance.
    Set ``inherit_env=True`` only for trusted local servers that need
    legacy full-environment inheritance.
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    credential_env: tuple[str, ...] = ()
    startup_timeout: float = 10.0
    request_timeout: float = 30.0
    inherit_env: bool = False
    allow_argv_secret_expansion: bool = False

    def __post_init__(self) -> None:
        _validate_server_name(self.name)
        unique_credential_env = tuple(dict.fromkeys(self.credential_env))
        object.__setattr__(self, "credential_env", unique_credential_env)
        for key in unique_credential_env:
            _validate_env_name(key)


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

    def __init__(self, config: McpServerConfig, *, session_id: str | None = None) -> None:
        self._config = config
        self._session_id = str(session_id or "").strip() or None
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._tools_cache: list[ToolDefinition] | None = None
        self._stdout_frames: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stdout_thread: threading.Thread | None = None
        # Capture stderr for diagnostics; reading runs in a background
        # thread so a chatty server doesn't block the pipe.
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._stderr_redaction_values: tuple[str, ...] = ()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the server and complete the initialize handshake."""
        if self._proc is not None:
            raise RuntimeError(f"MCP client '{self._config.name}' already started")

        self._stdout_frames = queue.Queue()
        env = _child_env_for_config(self._config)
        self._stderr_redaction_values = _stderr_redaction_values(self._config, env)

        command = _resolve_executable(self._config.command, env)
        args = _expand_config_args(self._config, env)
        try:
            self._proc = subprocess.Popen(
                [command, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                bufsize=0,  # unbuffered; we flush each write ourselves
                **_popen_process_group_kwargs(),
            )
        except OSError as exc:
            self._stderr_redaction_values = ()
            raise McpServerError(
                f"{self._config.name}: failed to start MCP command {self._config.command!r}: {exc}"
            ) from exc
        assert self._proc.stdin and self._proc.stdout and self._proc.stderr

        # Drain stderr in the background so a verbose server can't fill
        # the OS pipe buffer and deadlock us.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(self._stderr_redaction_values,),
            daemon=True,
        )
        self._stderr_thread.start()
        self._stdout_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self._stdout_thread.start()

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
            self._stderr_redaction_values = ()
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
            _signal_process_tree(proc, force=False)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                _signal_process_tree(proc, force=True)
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    _logger.error(
                        "MCP server '%s' did not die after kill",
                        self._config.name,
                    )
        except Exception as exc:  # noqa: BLE001
            _logger.debug("MCP server '%s' stop error: %s", self._config.name, exc)
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread and thread.is_alive():
                thread.join(timeout=0.2)
        self._stdout_thread = None
        self._stderr_thread = None
        self._stderr_redaction_values = ()

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
                    parameters=t.get("inputSchema")
                    or {
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
        started = time.perf_counter()
        recorded = False
        with telemetry_span():
            try:
                result = self._request(
                    "tools/call",
                    {"name": name, "arguments": arguments},
                )
                if result.get("isError"):
                    content = _flatten_content(result.get("content", []))
                    _record_mcp_client_tool_call(
                        server=self._config.name,
                        tool=name,
                        session_id=self._session_id,
                        outcome="error",
                        duration_ms=_duration_ms(started),
                        error_kind="tool_error",
                    )
                    recorded = True
                    raise McpServerError(
                        f"tool '{name}' on '{self._config.name}' reported isError: {content}"
                    )
                output = _flatten_content(result.get("content", []))
            except Exception as exc:
                if not recorded:
                    _record_mcp_client_tool_call(
                        server=self._config.name,
                        tool=name,
                        session_id=self._session_id,
                        outcome="error",
                        duration_ms=_duration_ms(started),
                        error_kind=type(exc).__name__,
                    )
                raise
            _record_mcp_client_tool_call(
                server=self._config.name,
                tool=name,
                session_id=self._session_id,
                outcome="ok",
                duration_ms=_duration_ms(started),
            )
            return output

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
            raise RuntimeError(f"MCP client '{self._config.name}' is not started")
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": _params_with_metadata(params, session_id=self._session_id),
            }
            try:
                self._write_frame(request)
            except (BrokenPipeError, OSError) as exc:
                raise McpServerError(
                    f"{self._config.name}.{method}: write failed; server pipe "
                    f"closed. stderr tail:\n{self._stderr_tail()}"
                ) from exc

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
                try:
                    frame = self._read_frame(timeout=remaining if deadline is not None else None)
                except TimeoutError as exc:
                    raise McpServerError(
                        f"{self._config.name}.{method}: timed out after "
                        f"{timeout if timeout is not None else self._config.request_timeout}s"
                    ) from exc
                if frame is None:
                    raise McpServerError(
                        f"{self._config.name} pipe closed before response to "
                        f"{method!r}. stderr tail:\n{self._stderr_tail()}"
                    )
                # Notifications have no ``id``; skip them for now.
                if "id" not in frame:
                    _logger.debug(
                        "MCP %s notification: %s",
                        self._config.name,
                        frame.get("method"),
                    )
                    continue
                if frame.get("id") != request_id:
                    # Late response to a prior request (shouldn't happen
                    # while we hold the lock, but defensive).
                    _logger.debug(
                        "MCP %s stale response id=%s (waiting for %s)",
                        self._config.name,
                        frame.get("id"),
                        request_id,
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
                self._config.name,
                method,
                exc,
            )

    def _write_frame(self, frame: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        payload = (json.dumps(frame) + "\n").encode("utf-8")
        self._proc.stdin.write(payload)
        self._proc.stdin.flush()

    def _read_frame(self, *, timeout: float | None) -> dict[str, Any] | None:
        try:
            if timeout is None:
                return self._stdout_frames.get()
            return self._stdout_frames.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc

    def _drain_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._stdout_frames.put(None)
            return
        try:
            for line in iter(proc.stdout.readline, b""):
                try:
                    self._stdout_frames.put(json.loads(line.decode("utf-8", errors="replace")))
                except json.JSONDecodeError as exc:
                    _logger.warning(
                        "MCP %s: dropping malformed frame: %s (raw=%r)",
                        self._config.name,
                        exc,
                        line,
                    )
        finally:
            self._stdout_frames.put(None)

    def _drain_stderr(self, redaction_values: tuple[str, ...]) -> None:
        """Consume stderr in the background; keep the last ~200 lines."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                self._stderr_lines.append(_redact_sensitive_text(line, redaction_values))
                # Cap memory usage on a chatty server.
                if len(self._stderr_lines) > 200:
                    del self._stderr_lines[:-200]
        except Exception:  # noqa: BLE001
            pass

    def _stderr_tail(self) -> str:
        return "\n".join(self._stderr_lines[-20:])


# ── Multi-server router ───────────────────────────────────────────────────


class McpRouter:
    """Manages a set of ``McpClient``s, routes tool calls by namespaced name.

    Tool names exposed to the model are ``<server>__<tool>`` — the
    router splits on ``TOOL_SEPARATOR`` to route to the right server.
    Server-level collisions (two servers with a tool called
    ``read_file``) are not a problem because the namespace prefix
    distinguishes them.
    """

    def __init__(
        self,
        configs: list[McpServerConfig],
        *,
        session_id: str | None = None,
    ) -> None:
        self._configs = list(configs)
        self._session_id = str(session_id or "").strip() or None
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
                    raise ValueError(f"duplicate MCP server name {cfg.name!r}")
                client = McpClient(cfg, session_id=self._session_id)
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
        seen_names: set[str] = set()
        for server_name, client in self._clients.items():
            for tool in client.list_tools():
                qualified_name = f"{server_name}{TOOL_SEPARATOR}{tool.name}"
                if qualified_name in seen_names:
                    raise ValueError(f"duplicate MCP tool name {qualified_name!r}")
                seen_names.add(qualified_name)
                out.append(
                    ToolDefinition(
                        name=qualified_name,
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
            raise ValueError(f"unknown MCP server {server!r}; known: {sorted(self._clients)}")
        return client.call_tool(tool, arguments)

    @property
    def server_names(self) -> list[str]:
        return sorted(self._clients)


# ── Helpers ───────────────────────────────────────────────────────────────


def _flatten_content(content: list[Any] | None) -> str:
    """Concatenate a MCP tool-result content array into a single string.

    Text blocks pass through verbatim. Non-text blocks (image/resource)
    are summarised with a short tag; resource URIs are deliberately
    omitted so local paths and private identifiers do not leak.
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
            parts.append("[resource omitted]")
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
