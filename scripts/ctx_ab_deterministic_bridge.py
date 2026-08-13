"""Deterministic, loopback-only provider bridge for local A/B plumbing tests.

This bridge is test infrastructure, not production-provider evidence.  Its token
accounting rule is intentionally simple and fully reproducible: one token is
each non-overlapping group of up to four UTF-8 bytes.  Input tokens cover the
exact HTTP request body, output tokens cover the configured assistant text,
cached input is always zero, and total tokens are input plus output.

Only ``POST /v1/chat/completions`` is implemented.  A request is accepted only
when either ``X-CTX-Bridge-Approval-SHA256`` or the standard
``Authorization: Bearer`` API-key field exactly carries the lowercase SHA-256
digest supplied by the caller when constructing the bridge.  If both are
present, both must match.  The server binds only to an IP loopback literal and
independently rejects non-loopback peers.  It never opens an outbound
connection.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar, Literal


APPROVAL_DIGEST_HEADER = "X-CTX-Bridge-Approval-SHA256"
_CHAT_COMPLETIONS_PATH: Literal["/v1/chat/completions"] = "/v1/chat/completions"
_SHA256_HEXDIGEST_LENGTH = 64
_MAX_CONTENT_LENGTH_DIGITS = 20


@dataclass(frozen=True, slots=True)
class CapturedMessage:
    """The message fields relevant to deterministic pair verification."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Exact usage under the deterministic four-UTF-8-byte accounting rule."""

    input_tokens: int
    cached_input_tokens: int
    uncached_input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """Immutable HTTP-body evidence for one accepted provider request."""

    request_body_bytes: bytes
    request_body_sha256: str
    approval_digest: str
    method: Literal["POST"]
    path: Literal["/v1/chat/completions"]
    model: str
    messages: tuple[CapturedMessage, ...]
    usage: TokenUsage
    response_status: Literal[200]
    response_body_bytes: bytes
    response_body_sha256: str


def deterministic_token_count(payload: bytes) -> int:
    """Count one token per non-empty group of at most four bytes."""

    return (len(payload) + 3) // 4


def _is_canonical_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_HEXDIGEST_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_sha256(value: str) -> str:
    if not _is_canonical_sha256(value):
        raise ValueError("approval_digest must be a lowercase SHA-256 hex digest")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(constant: str) -> None:
    raise ValueError(f"non-finite JSON number: {constant}")


class _BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False

    bridge: DeterministicProviderBridge

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._active_requests: set[socket.socket | tuple[bytes, socket.socket]] = set()
        self._active_requests_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: Any,
    ) -> None:
        with self._active_requests_lock:
            self._active_requests.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._active_requests_lock:
                self._active_requests.discard(request)
            raise

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: Any,
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_requests_lock:
                self._active_requests.discard(request)

    def close_active_requests(self) -> None:
        """Interrupt request reads after the accept loop has stopped."""

        with self._active_requests_lock:
            requests = tuple(self._active_requests)
        for request in requests:
            connection = request[1] if isinstance(request, tuple) else request
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _IPv6BridgeHTTPServer(_BridgeHTTPServer):
    address_family = socket.AF_INET6


class _BridgeRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _BridgeHTTPServer

    def handle_one_request(self) -> None:
        """Bound unexpected application errors without leaking their details."""

        try:
            super().handle_one_request()
        except Exception:  # BaseHTTPRequestHandler retains its own parser limits.
            self.close_connection = True
            try:
                self.server.bridge._send_error(
                    self,
                    400,
                    "malformed_request",
                    "request could not be processed",
                )
            except Exception:
                # A peer that has already disconnected cannot receive a response.
                pass

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.server.bridge._handle_post(self)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.server.bridge._handle_other_method(self)

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.server.bridge._handle_other_method(self)

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.server.bridge._handle_other_method(self)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class DeterministicProviderBridge:
    """A context-managed local OpenAI-compatible deterministic HTTP endpoint."""

    _ERROR_TYPE: ClassVar[str] = "invalid_request_error"

    def __init__(
        self,
        *,
        approval_digest: str,
        token_budget: int,
        response_content: str = "deterministic response",
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
        max_request_bytes: int = 1_048_576,
    ) -> None:
        try:
            bind_address = ipaddress.ip_address(bind_host)
        except ValueError as error:
            raise ValueError("bind_host must be a loopback IP literal") from error
        if not bind_address.is_loopback:
            raise ValueError("bind_host must be a loopback IP literal")
        if (
            isinstance(bind_port, bool)
            or not isinstance(bind_port, int)
            or not 0 <= bind_port <= 65535
        ):
            raise ValueError("bind_port must be an integer from 0 through 65535")
        if isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget <= 0:
            raise ValueError("token_budget must be a positive integer")
        if (
            isinstance(max_request_bytes, bool)
            or not isinstance(max_request_bytes, int)
            or max_request_bytes <= 0
        ):
            raise ValueError("max_request_bytes must be a positive integer")
        if not isinstance(response_content, str):
            raise TypeError("response_content must be a string")

        self._approval_digest = _validate_sha256(approval_digest)
        self._token_budget = token_budget
        self._response_content = response_content
        self._bind_address = bind_address
        self._bind_port = bind_port
        self._max_request_bytes = max_request_bytes
        self._records: list[RequestRecord] = []
        self._records_lock = threading.Lock()
        self._lifecycle_condition = threading.Condition()
        self._lifecycle: Literal["new", "running", "closing", "closed"] = "new"
        self._server: _BridgeHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """Return the loopback URL after the bridge has started."""

        with self._lifecycle_condition:
            server = self._server
            if self._lifecycle != "running" or server is None:
                raise RuntimeError("deterministic provider bridge is not running")
        host = str(self._bind_address)
        if self._bind_address.version == 6:
            host = f"[{host}]"
        return f"http://{host}:{server.server_port}"

    @property
    def records(self) -> tuple[RequestRecord, ...]:
        """Return an immutable point-in-time snapshot of accepted requests."""

        with self._records_lock:
            return tuple(self._records)

    def start(self) -> DeterministicProviderBridge:
        """Bind and start the background server exactly once."""

        with self._lifecycle_condition:
            if self._lifecycle == "running":
                raise RuntimeError("deterministic provider bridge is already running")
            if self._lifecycle == "closing":
                raise RuntimeError("deterministic provider bridge is closing")
            if self._lifecycle == "closed":
                raise RuntimeError("deterministic provider bridge is closed")
            server_type = (
                _IPv6BridgeHTTPServer if self._bind_address.version == 6 else _BridgeHTTPServer
            )
            server = server_type(
                (str(self._bind_address), self._bind_port),
                _BridgeRequestHandler,
            )
            server.bridge = self
            thread = threading.Thread(
                target=server.serve_forever,
                name="ctx-ab-deterministic-bridge",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                try:
                    if thread.is_alive():
                        server.shutdown()
                finally:
                    server.close_active_requests()
                    server.server_close()
                    if thread.is_alive():
                        thread.join(timeout=5)
                    self._server = None
                    self._thread = None
                    self._lifecycle = "closed"
                    self._lifecycle_condition.notify_all()
                raise
            self._lifecycle = "running"
        return self

    def close(self) -> None:
        """Stop the server; repeated closes are harmless."""

        with self._lifecycle_condition:
            while self._lifecycle == "closing":
                self._lifecycle_condition.wait()
            if self._lifecycle == "closed":
                return
            if self._lifecycle == "new":
                self._lifecycle = "closed"
                self._lifecycle_condition.notify_all()
                return
            server = self._server
            thread = self._thread
            self._lifecycle = "closing"
        assert server is not None
        try:
            try:
                server.shutdown()
            finally:
                server.close_active_requests()
                server.server_close()
            if thread is not None:
                thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError("deterministic provider bridge did not stop")
        finally:
            with self._lifecycle_condition:
                self._server = None
                self._thread = None
                self._lifecycle = "closed"
                self._lifecycle_condition.notify_all()

    def __enter__(self) -> DeterministicProviderBridge:
        return self.start()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    @staticmethod
    def _peer_is_loopback(handler: _BridgeRequestHandler) -> bool:
        try:
            return ipaddress.ip_address(str(handler.client_address[0])).is_loopback
        except ValueError:
            return False

    def _handle_other_method(self, handler: _BridgeRequestHandler) -> None:
        if not self._peer_is_loopback(handler):
            self._send_error(handler, 403, "remote_peer_rejected", "remote peer rejected")
            return
        self._send_error(handler, 405, "method_not_allowed", "method not allowed")

    def _handle_post(self, handler: _BridgeRequestHandler) -> None:
        if not self._peer_is_loopback(handler):
            self._send_error(handler, 403, "remote_peer_rejected", "remote peer rejected")
            return
        if handler.path != _CHAT_COMPLETIONS_PATH:
            self._send_error(handler, 404, "unknown_path", "unknown path")
            return

        if not self._request_is_approved(handler):
            self._send_error(
                handler,
                403,
                "approval_digest_mismatch",
                "approval digest mismatch",
            )
            return

        content_type = handler.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._send_error(
                handler,
                415,
                "unsupported_media_type",
                "Content-Type must be application/json",
            )
            return
        if handler.headers.get("Transfer-Encoding") is not None:
            self._send_error(
                handler,
                400,
                "invalid_content_length",
                "transfer encoding is not supported",
            )
            return
        lengths = handler.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1:
            self._send_error(
                handler,
                400,
                "invalid_content_length",
                "one decimal Content-Length is required",
            )
            return
        raw_content_length = lengths[0]
        if (
            len(raw_content_length) > _MAX_CONTENT_LENGTH_DIGITS
            or not raw_content_length.isascii()
            or not raw_content_length.isdecimal()
        ):
            self._send_error(
                handler,
                400,
                "invalid_content_length",
                "one decimal Content-Length is required",
            )
            return
        content_length = int(raw_content_length)
        if content_length > self._max_request_bytes:
            self._send_error(handler, 413, "request_too_large", "request body is too large")
            return
        request_bytes = handler.rfile.read(content_length)
        if len(request_bytes) != content_length:
            self._send_error(handler, 400, "truncated_request", "request body is truncated")
            return

        try:
            payload = json.loads(
                request_bytes.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._send_error(handler, 400, "malformed_json", "request body is not strict JSON")
            return
        if not isinstance(payload, dict):
            self._send_error(handler, 422, "invalid_request", "request body must be an object")
            return

        parsed = self._parse_request(handler, payload)
        if parsed is None:
            return
        model, messages, request_output_budget = parsed
        usage = self._usage(request_bytes)
        if request_output_budget is not None and usage.output_tokens > request_output_budget:
            self._send_error(
                handler,
                413,
                "request_output_budget_exceeded",
                "configured output exceeds request output budget",
            )
            return
        if usage.total_tokens > self._token_budget:
            self._send_error(
                handler,
                413,
                "token_budget_exceeded",
                "request exceeds bridge token budget",
            )
            return

        response_bytes = self._completion_bytes(
            model=model,
            request_sha256=hashlib.sha256(request_bytes).hexdigest(),
            usage=usage,
        )
        record = RequestRecord(
            request_body_bytes=request_bytes,
            request_body_sha256=hashlib.sha256(request_bytes).hexdigest(),
            approval_digest=self._approval_digest,
            method="POST",
            path=_CHAT_COMPLETIONS_PATH,
            model=model,
            messages=messages,
            usage=usage,
            response_status=200,
            response_body_bytes=response_bytes,
            response_body_sha256=hashlib.sha256(response_bytes).hexdigest(),
        )
        with self._records_lock:
            self._records.append(record)
        self._send_bytes(handler, 200, response_bytes)

    def _request_is_approved(self, handler: _BridgeRequestHandler) -> bool:
        explicit_values = handler.headers.get_all(APPROVAL_DIGEST_HEADER, failobj=[])
        authorization_values = handler.headers.get_all("Authorization", failobj=[])
        if len(explicit_values) > 1 or len(authorization_values) > 1:
            return False

        submitted: list[str] = list(explicit_values)
        if authorization_values:
            authorization = authorization_values[0]
            prefix = "Bearer "
            if not authorization.startswith(prefix):
                return False
            submitted.append(authorization.removeprefix(prefix))
        return bool(submitted) and all(
            _is_canonical_sha256(value)
            and hmac.compare_digest(value.encode("ascii"), self._approval_digest.encode("ascii"))
            for value in submitted
        )

    def _parse_request(
        self,
        handler: _BridgeRequestHandler,
        payload: dict[str, Any],
    ) -> tuple[str, tuple[CapturedMessage, ...], int | None] | None:
        model = payload.get("model")
        raw_messages = payload.get("messages")
        if not isinstance(model, str) or not model.strip():
            self._send_error(handler, 422, "invalid_request", "model must be a non-empty string")
            return None
        if not isinstance(raw_messages, list):
            self._send_error(handler, 422, "invalid_request", "messages must be a list")
            return None
        messages: list[CapturedMessage] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, dict):
                self._send_error(handler, 422, "invalid_request", "message must be an object")
                return None
            role = raw_message.get("role")
            content = raw_message.get("content")
            if not isinstance(role, str) or not role.strip() or not isinstance(content, str):
                self._send_error(
                    handler,
                    422,
                    "invalid_request",
                    "message role and content must be strings",
                )
                return None
            messages.append(CapturedMessage(role=role, content=content))
        if payload.get("stream", False) is not False:
            self._send_error(
                handler,
                422,
                "streaming_not_supported",
                "streaming is not supported",
            )
            return None

        budget_fields = [
            value
            for value in (payload.get("max_tokens"), payload.get("max_completion_tokens"))
            if value is not None
        ]
        for value in budget_fields:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                self._send_error(
                    handler,
                    422,
                    "invalid_request",
                    "output token budgets must be non-negative integers",
                )
                return None
        request_output_budget = min(budget_fields) if budget_fields else None
        return model, tuple(messages), request_output_budget

    def _usage(self, request_bytes: bytes) -> TokenUsage:
        input_tokens = deterministic_token_count(request_bytes)
        output_tokens = deterministic_token_count(self._response_content.encode("utf-8"))
        return TokenUsage(
            input_tokens=input_tokens,
            cached_input_tokens=0,
            uncached_input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )

    def _completion_bytes(
        self,
        *,
        model: str,
        request_sha256: str,
        usage: TokenUsage,
    ) -> bytes:
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {"content": self._response_content, "role": "assistant"},
                }
            ],
            "created": 0,
            "id": f"chatcmpl-ctx-{request_sha256[:24]}",
            "model": model,
            "object": "chat.completion",
            "usage": {
                "cached_input_tokens": usage.cached_input_tokens,
                "completion_tokens": usage.output_tokens,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "prompt_tokens": usage.input_tokens,
                "prompt_tokens_details": {"cached_tokens": usage.cached_input_tokens},
                "total_tokens": usage.total_tokens,
                "uncached_input_tokens": usage.uncached_input_tokens,
            },
        }
        return json.dumps(
            response,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _send_error(
        self,
        handler: _BridgeRequestHandler,
        status: int,
        code: str,
        message: str,
    ) -> None:
        payload = {
            "error": {
                "code": code,
                "message": message,
                "type": self._ERROR_TYPE,
            }
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self._send_bytes(handler, status, body)

    @staticmethod
    def _send_bytes(handler: _BridgeRequestHandler, status: int, body: bytes) -> None:
        handler.close_connection = True
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)


__all__ = [
    "APPROVAL_DIGEST_HEADER",
    "CapturedMessage",
    "DeterministicProviderBridge",
    "RequestRecord",
    "TokenUsage",
    "deterministic_token_count",
]
