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
        "TMP",
        "TEMP",
        "HOME",
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
    capability_epoch: int | None,
    outcome: str,
    duration_ms: float,
    error_kind: str | None = None,
) -> None:
    try:
        payload: dict[str, Any] = {
            "rpc.system": "jsonrpc",
            "rpc.method": "tools/call",
            "ctx.mcp.server.hash": hash_identifier(server),
            "ctx.mcp.tool.hash": hash_identifier(f"{server}{TOOL_SEPARATOR}{tool}"),
            "otel.status_code": "ERROR" if outcome == "error" else "OK",
        }
        if capability_epoch is not None:
            payload["ctx.mcp.capability.epoch"] = capability_epoch
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


def _record_mcp_transition(
    event_name: str,
    *,
    phase: str,
    server_names: Iterable[str],
    session_id: str | None,
    duration_ms: float = 0.0,
    tool_count: int = 0,
    outcome: str = "ok",
    error_kind: str | None = None,
    capability_epoch: int | None = None,
    process_started_count: int | None = None,
    process_stop_observations: Iterable[tuple[bool, bool | None, float | None]] | None = None,
) -> None:
    names = tuple(dict.fromkeys(server_names))
    stop_observations = (
        None if process_stop_observations is None else tuple(process_stop_observations)
    )
    try:
        payload: dict[str, Any] = {
            "ctx.mcp.phase": phase,
            "ctx.mcp.server.count": len(names),
            "ctx.mcp.server.hashes": [hash_identifier(name) for name in names],
            "ctx.mcp.tool.count": tool_count,
            "otel.status_code": "ERROR" if outcome == "error" else "OK",
        }
        if capability_epoch is not None:
            payload["ctx.mcp.capability.epoch"] = capability_epoch
        if process_started_count is not None:
            payload["ctx.mcp.process.started.count"] = process_started_count
        if stop_observations is not None:
            process_observations = [
                observation for observation in stop_observations if observation[1] is not None
            ]
            reaped_count = sum(
                1
                for _cleanup_complete, process_exited, _observed_age in process_observations
                if process_exited
            )
            cleanup_complete_count = sum(
                1
                for cleanup_complete, _process_exited, _observed_age in process_observations
                if cleanup_complete
            )
            completed_lifetimes = [
                max(0.0, observed_age)
                for _cleanup_complete, process_exited, observed_age in process_observations
                if process_exited and observed_age is not None
            ]
            unreaped_ages = [
                max(0.0, observed_age)
                for _cleanup_complete, process_exited, observed_age in process_observations
                if not process_exited and observed_age is not None
            ]
            payload.update(
                {
                    "ctx.mcp.process.reap.attempted.count": len(process_observations),
                    "ctx.mcp.process.reap.succeeded.count": reaped_count,
                    "ctx.mcp.process.reap.failed.count": (len(process_observations) - reaped_count),
                    "ctx.mcp.process.reap.outcome": (
                        "not_applicable"
                        if not process_observations
                        else "complete"
                        if reaped_count == len(process_observations)
                        else "incomplete"
                    ),
                    "ctx.mcp.cleanup.complete.count": cleanup_complete_count,
                    "ctx.mcp.cleanup.incomplete.count": (
                        len(process_observations) - cleanup_complete_count
                    ),
                    "ctx.mcp.process.lifetime.observed.count": len(completed_lifetimes),
                    "ctx.mcp.process.unreaped_age.observed.count": len(unreaped_ages),
                }
            )
            if completed_lifetimes:
                payload["ctx.mcp.process.lifetime_ms.max"] = max(completed_lifetimes)
                payload["ctx.mcp.process.lifetime_ms.total"] = sum(completed_lifetimes)
            if unreaped_ages:
                payload["ctx.mcp.process.unreaped_age_ms.max"] = max(unreaped_ages)
        record_event(
            event_name,
            source="ctx-mcp-router",
            transport="mcp-jsonrpc",
            session_id=session_id,
            outcome=outcome,
            duration_ms=duration_ms,
            error_kind=error_kind,
            payload=payload,
        )
    except Exception:  # noqa: BLE001 - telemetry must never break MCP lifecycle.
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
    if not config.expand_argv_env:
        return config.args
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
    return {"start_new_session": True}


def _signal_process_tree(
    proc: subprocess.Popen[bytes],
    *,
    force: bool,
) -> None:
    if proc.poll() is not None:
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    except Exception:  # noqa: BLE001
        if force:
            proc.kill()
        else:
            proc.terminate()


def _resolve_executable(command: str, env: dict[str, str]) -> str:
    """Resolve bare commands through PATH before spawning."""
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

    ``args`` are literal by default. Set ``expand_argv_env=True`` to expand
    ``$ENVVAR`` or ``${ENVVAR}`` placeholders from the final child environment
    immediately before spawn. Sensitive env vars are not expanded into argv
    unless ``allow_argv_secret_expansion`` is explicitly enabled for a trusted
    local server.

    ``env`` is the explicit child overlay. Parent secrets are not
    inherited by default; only a small process-basics allowlist
    (PATH, temp/home, and locale) is passed through.
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
    expand_argv_env: bool = False

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
        self._process_started_at: float | None = None
        self._last_process_lifetime_ms: float | None = None
        self._last_process_exited: bool | None = None

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
        self._process_started_at = None
        self._last_process_lifetime_ms = None
        self._last_process_exited = None
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
            self._process_started_at = time.perf_counter()
            self._last_process_exited = False
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

    def stop(self) -> bool:
        """Best-effort shutdown. Never raises."""
        proc = self._proc
        if proc is None:
            self._stderr_redaction_values = ()
            return True
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
        process_exited = proc.poll() is not None
        self._last_process_exited = process_exited
        if process_exited and self._process_started_at is not None:
            self._last_process_lifetime_ms = _duration_ms(self._process_started_at)
            self._process_started_at = None
        reaped = process_exited and all(
            thread is None or not thread.is_alive()
            for thread in (self._stdout_thread, self._stderr_thread)
        )
        if reaped:
            self._proc = None
            self._stdout_thread = None
            self._stderr_thread = None
            self._stderr_redaction_values = ()
        return reaped

    @property
    def process_observed_age_ms(self) -> float | None:
        """Return the final lifetime if reaped, otherwise the current process age."""
        if self._process_started_at is not None:
            return max(0.0, _duration_ms(self._process_started_at))
        return self._last_process_lifetime_ms

    @property
    def process_exited(self) -> bool | None:
        """Return observed process exit state, or ``None`` if no process started."""
        return self._last_process_exited

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

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        capability_epoch: int | None = None,
    ) -> str:
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
                        capability_epoch=capability_epoch,
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
                        capability_epoch=capability_epoch,
                        outcome="error",
                        duration_ms=_duration_ms(started),
                        error_kind=type(exc).__name__,
                    )
                raise
            _record_mcp_client_tool_call(
                server=self._config.name,
                tool=name,
                session_id=self._session_id,
                capability_epoch=capability_epoch,
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
        lazy: bool = False,
    ) -> None:
        self._configs = list(configs)
        self._session_id = str(session_id or "").strip() or None
        self._clients: dict[str, McpClient] = {}
        self._retiring_clients: list[McpClient] = []
        self._retiring_context: dict[McpClient, tuple[str, int | None, bool]] = {}
        self._capability_epochs: dict[str, int | None] = {}
        self._started = False
        self._lazy = bool(lazy)

    def start(self) -> None:
        """Spawn every configured server; roll back all on any failure."""
        if self._started:
            return
        if self._lazy:
            self._started = True
            return
        spawned: list[str] = []
        try:
            for cfg in self._configs:
                if cfg.name in self._clients:
                    raise ValueError(f"duplicate MCP server name {cfg.name!r}")
                client = McpClient(cfg, session_id=self._session_id)
                try:
                    client.start()
                except Exception:
                    self._stop_or_retain(client)
                    raise
                self._clients[cfg.name] = client
                spawned.append(cfg.name)
        except Exception:
            # Atomic startup — tear down any already-started servers so
            # we don't leak child processes when a later config fails.
            for name in spawned:
                client = self._clients.pop(name)
                self._stop_or_retain(client)
            raise
        self._started = True

    def activate(
        self,
        server_names: Iterable[str],
        *,
        capability_epoch: int | None = None,
    ) -> list[ToolDefinition]:
        """Start only the exact granted servers and return their schemas."""
        if not self._started:
            raise RuntimeError("router not started; call start() first")
        if capability_epoch is not None and (
            isinstance(capability_epoch, bool)
            or not isinstance(capability_epoch, int)
            or capability_epoch < 0
        ):
            raise ValueError("capability_epoch must be a non-negative integer or None")
        names = tuple(dict.fromkeys(server_names))
        started = time.perf_counter()
        _record_mcp_transition(
            "ctx.mcp.activation",
            phase="requested",
            server_names=names,
            session_id=self._session_id,
            capability_epoch=capability_epoch,
        )
        spawned: list[str] = []
        cleanup_observations: list[tuple[bool, bool | None, float | None]] = []
        try:
            if self._lazy:
                configs: list[McpServerConfig] = []
                for name in names:
                    matches = [config for config in self._configs if config.name == name]
                    if not matches:
                        raise ValueError(f"unknown MCP server grant {name!r}")
                    if len(matches) > 1:
                        raise ValueError(f"duplicate MCP server name {name!r}")
                    configs.append(matches[0])
                for config in configs:
                    if config.name in self._clients:
                        continue
                    client = McpClient(config, session_id=self._session_id)
                    try:
                        client.start()
                    except Exception:
                        cleanup_observations.append(self._stop_or_retain(client))
                        raise
                    self._clients[config.name] = client
                    spawned.append(config.name)
            tools = self._qualified_tools(names)
        except Exception as exc:
            for name in spawned:
                rollback_client = self._clients.pop(name) if name in self._clients else None
                if rollback_client is not None:
                    cleanup_observations.append(self._stop_or_retain(rollback_client))
            _record_mcp_transition(
                "ctx.mcp.activation",
                phase="failed",
                server_names=names,
                session_id=self._session_id,
                duration_ms=_duration_ms(started),
                outcome="error",
                error_kind=type(exc).__name__,
                capability_epoch=capability_epoch,
                process_started_count=sum(
                    1
                    for _cleanup_complete, process_exited, _observed_age in cleanup_observations
                    if process_exited is not None
                ),
                process_stop_observations=cleanup_observations,
            )
            raise
        for name in names:
            self._capability_epochs[name] = capability_epoch
        _record_mcp_transition(
            "ctx.mcp.activation",
            phase="applied",
            server_names=names,
            session_id=self._session_id,
            duration_ms=_duration_ms(started),
            tool_count=len(tools),
            capability_epoch=capability_epoch,
            process_started_count=len(spawned),
        )
        return tools

    def deactivate(self, server_names: Iterable[str] | None = None) -> None:
        """Revoke exact server routes, then stop and verify their clients."""
        names = tuple(dict.fromkeys(tuple(self._clients) if server_names is None else server_names))
        server_epochs = {
            name: self._capability_epochs[name] for name in names if name in self._capability_epochs
        }
        epochs = set(server_epochs.values())
        capability_epoch = next(iter(epochs)) if len(epochs) == 1 else None
        started = time.perf_counter()
        _record_mcp_transition(
            "ctx.mcp.deactivation",
            phase="requested",
            server_names=names,
            session_id=self._session_id,
            capability_epoch=capability_epoch,
        )
        reaped = True
        stop_observations: list[tuple[bool, bool | None, float | None]] = []
        for name in names:
            self._capability_epochs.pop(name, None)
            client = self._clients.pop(name, None)
            if client is not None:
                observation = self._stop_or_retain(client)
                stop_observations.append(observation)
                if not observation[0]:
                    lifetime_emitted = observation[1] is True and observation[2] is not None
                    self._retiring_context[client] = (
                        name,
                        server_epochs.get(name),
                        lifetime_emitted,
                    )
                reaped = observation[0] and reaped
        if not reaped:
            _record_mcp_transition(
                "ctx.mcp.deactivation",
                phase="failed",
                server_names=names,
                session_id=self._session_id,
                duration_ms=_duration_ms(started),
                outcome="error",
                error_kind="McpProcessNotReaped",
                capability_epoch=capability_epoch,
                process_stop_observations=stop_observations,
            )
            raise McpServerError("one or more MCP servers did not fully stop")
        _record_mcp_transition(
            "ctx.mcp.deactivation",
            phase="applied",
            server_names=names,
            session_id=self._session_id,
            duration_ms=_duration_ms(started),
            capability_epoch=capability_epoch,
            process_stop_observations=stop_observations,
        )

    def stop(self) -> None:
        clients = [*self._clients.values(), *self._retiring_clients]
        self._clients.clear()
        self._retiring_clients = []
        self._capability_epochs.clear()
        for client in clients:
            recovery_context = self._retiring_context.get(client)
            retry_started = time.perf_counter()
            observation = self._stop_or_retain(client)
            if recovery_context is None:
                continue
            server_name, capability_epoch, lifetime_emitted = recovery_context
            recovered = observation[0]
            telemetry_observation = (
                observation[0],
                observation[1],
                None if lifetime_emitted else observation[2],
            )
            _record_mcp_transition(
                "ctx.mcp.deactivation",
                phase="recovered" if recovered else "recovery_failed",
                server_names=(server_name,),
                session_id=self._session_id,
                duration_ms=_duration_ms(retry_started),
                outcome="ok" if recovered else "error",
                error_kind=None if recovered else "McpProcessNotReaped",
                capability_epoch=capability_epoch,
                process_stop_observations=(telemetry_observation,),
            )
            if recovered:
                self._retiring_context.pop(client, None)
            elif observation[1] is True and observation[2] is not None:
                self._retiring_context[client] = (
                    server_name,
                    capability_epoch,
                    True,
                )
        self._started = False

    def _stop_or_retain(
        self,
        client: McpClient,
    ) -> tuple[bool, bool | None, float | None]:
        try:
            reaped = client.stop()
        except Exception:  # noqa: BLE001 - retain ownership for a later retry.
            reaped = False
        if not reaped and all(item is not client for item in self._retiring_clients):
            self._retiring_clients.append(client)
        process_exited_value = getattr(client, "process_exited", None)
        process_exited = process_exited_value if isinstance(process_exited_value, bool) else None
        observed_age = getattr(client, "process_observed_age_ms", None)
        observed_age_ms = (
            float(observed_age)
            if isinstance(observed_age, (int, float)) and not isinstance(observed_age, bool)
            else None
        )
        return reaped, process_exited, observed_age_ms

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
        return self._qualified_tools(tuple(self._clients))

    def _qualified_tools(self, server_names: Iterable[str]) -> list[ToolDefinition]:
        out: list[ToolDefinition] = []
        seen_names: set[str] = set()
        for server_name in server_names:
            client = self._clients.get(server_name)
            if client is None:
                raise ValueError(
                    f"unknown MCP server {server_name!r}; active: {sorted(self._clients)}"
                )
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
        published_tools = {definition.name for definition in client.list_tools()}
        if tool not in published_tools:
            raise ValueError(
                f"unknown MCP tool {qualified_name!r}; published: {sorted(published_tools)}"
            )
        return client.call_tool(
            tool,
            arguments,
            capability_epoch=self._capability_epochs.get(server),
        )

    @property
    def server_names(self) -> list[str]:
        return sorted(self._clients)

    @property
    def configured_server_names(self) -> list[str]:
        return sorted({config.name for config in self._configs})

    @property
    def lazy(self) -> bool:
        return self._lazy


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
