"""Threaded stdlib HTTP server helpers for ctx-monitor."""

from __future__ import annotations

import html
import json
import secrets
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypeVar

from ctx.monitor import routes


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._ctx_shutdown = threading.Event()
        self._ctx_mutations_enabled = True
        super().__init__(*args, **kwargs)

    def shutdown(self) -> None:
        self._ctx_shutdown.set()
        super().shutdown()

    def server_close(self) -> None:
        self._ctx_shutdown.set()
        super().server_close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc_type, _, _ = sys.exc_info()
        if exc_type is not None and issubclass(
            exc_type,
            (BrokenPipeError, ConnectionAbortedError, ConnectionResetError),
        ):
            return
        super().handle_error(request, client_address)


HandlerT = TypeVar("HandlerT", bound=BaseHTTPRequestHandler)


@dataclass(frozen=True)
class MonitorHandlerDeps:
    monitor_token: Callable[[], str]
    mutations_enabled_default: Callable[[], bool]
    host_allows_mutations: Callable[[str], bool]
    request_host_name: Callable[[str], str]
    origin_host_name: Callable[[str], str]
    read_token_cookie: Callable[[str], str]
    read_token_cookie_name: str
    max_post_body_bytes: int
    audit_log_path: Callable[[], Path]
    handle_get_route: Callable[
        [BaseHTTPRequestHandler, routes.RouteMatch, dict[str, str]],
        None,
    ]
    handle_post_route: Callable[
        [BaseHTTPRequestHandler, str, Mapping[str, Any], str],
        None,
    ]


def server_shutdown_requested(server: Any) -> bool:
    event = getattr(server, "_ctx_shutdown", None)
    return bool(event is not None and event.is_set())


def build_monitor_handler(deps: MonitorHandlerDeps) -> type[BaseHTTPRequestHandler]:
    """Create the stdlib request handler bound to monitor route callbacks."""

    class MonitorHandler(BaseHTTPRequestHandler):
        # Silence the per-request access log spam. Users running ctx-monitor get
        # a clean stdout; errors still surface via log_error() below.
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _monitor_token(self) -> str:
            return deps.monitor_token()

        # CSRF defense. Dashboard mutation endpoints require same-origin POSTs
        # plus a per-process token injected into the served dashboard page.
        def _same_origin(self) -> bool:
            request_host = deps.request_host_name(self.headers.get("Host", ""))
            if not deps.host_allows_mutations(request_host):
                return False
            origin = self.headers.get("Origin") or ""
            if origin:
                return deps.origin_host_name(origin) == request_host
            # No Origin header (curl, direct tool calls) is acceptable only
            # when the mutation token below is also present.
            return True

        def _mutations_enabled(self) -> bool:
            return bool(
                getattr(
                    self.server,
                    "_ctx_mutations_enabled",
                    deps.mutations_enabled_default(),
                ),
            )

        def _mutation_authorized(self) -> bool:
            token = self.headers.get("X-CTX-Monitor-Token") or ""
            monitor_token = self._monitor_token()
            return (
                self._mutations_enabled()
                and bool(monitor_token)
                and secrets.compare_digest(token, monitor_token)
            )

        def _api_reads_enabled(self) -> bool:
            return self._mutations_enabled()

        def _read_authorized(self, qs: dict[str, str]) -> bool:
            request_host = deps.request_host_name(self.headers.get("Host", ""))
            if self._mutations_enabled():
                return deps.host_allows_mutations(request_host)
            monitor_token = self._monitor_token()
            token = (
                self.headers.get("X-CTX-Monitor-Token")
                or qs.get("token", "")
                or deps.read_token_cookie(self.headers.get("Cookie", ""))
            )
            return bool(monitor_token) and secrets.compare_digest(token, monitor_token)

        def _send_security_headers(self, *, html_response: bool = False) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            if getattr(self, "_ctx_set_read_cookie", False):
                self.send_header(
                    "Set-Cookie",
                    f"{deps.read_token_cookie_name}={self._monitor_token()}; Path=/; "
                    "HttpOnly; SameSite=Strict",
                )
            if html_response:
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
                )

        def _content_length(self) -> int | None:
            raw = self.headers.get("Content-Length")
            if raw is None:
                return 0
            try:
                length = int(raw)
            except ValueError:
                self._send_json_status(400, {"detail": "invalid Content-Length"})
                return None
            if length < 0:
                self._send_json_status(400, {"detail": "invalid Content-Length"})
                return None
            if length > deps.max_post_body_bytes:
                self._send_json_status(413, {"detail": "JSON body too large"})
                return None
            return length

        def _read_json_body(self) -> dict[str, Any] | None:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type.lower() != "application/json":
                self._send_json_status(415, {"detail": "JSON body required"})
                return None
            length = self._content_length()
            if length is None:
                return None
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json_status(400, {"detail": "invalid JSON body"})
                return None
            if not isinstance(body, dict):
                self._send_json_status(400, {"detail": "JSON object body required"})
                return None
            return body

        def _discard_small_body(self) -> None:
            raw = self.headers.get("Content-Length")
            if raw is None:
                return
            try:
                length = int(raw)
            except ValueError:
                return
            if 0 < length <= deps.max_post_body_bytes:
                self.rfile.read(length)

        def _handle_get_route(
            self,
            route: routes.RouteMatch,
            qs: dict[str, str],
        ) -> None:
            deps.handle_get_route(self, route, qs)

        def do_GET(self) -> None:  # noqa: N802 - stdlib signature
            target = routes.parse_request_target(self.path)
            path = target.path
            qs = target.query
            try:
                self._ctx_set_read_cookie = False
                read_authorized = getattr(self, "_read_authorized", lambda _qs: True)
                if not read_authorized(qs):
                    if path.startswith("/api/"):
                        self._send_json_status(
                            403,
                            {"detail": "monitor read token required on non-loopback bind"},
                        )
                    else:
                        self._send_html_status(
                            403,
                            "<h1>403</h1>"
                            "<p>monitor read token required on non-loopback bind</p>",
                        )
                    return
                query_token = qs.get("token", "")
                monitor_token = getattr(
                    self,
                    "_monitor_token",
                    deps.monitor_token,
                )()
                self._ctx_set_read_cookie = (
                    not self._mutations_enabled()
                    and bool(query_token)
                    and bool(monitor_token)
                    and secrets.compare_digest(query_token, monitor_token)
                )
                route = routes.match_get_route(path)
                if route is None:
                    self._send_404(path)
                    return
                self._handle_get_route(route, qs)
            except (BrokenPipeError, ConnectionAbortedError):
                return
            except Exception as exc:  # noqa: BLE001 - last-resort handler
                self._send_500(exc)

        def do_POST(self) -> None:  # noqa: N802 - stdlib signature
            """Mutation endpoints. Same-origin only; JSON body required."""
            path = routes.parse_request_target(self.path).path
            try:
                if not self._mutations_enabled():
                    self._discard_small_body()
                    self._send_json_status(
                        403,
                        {"detail": "monitor mutations disabled on non-loopback bind"},
                    )
                    return
                if not self._same_origin():
                    self._discard_small_body()
                    self._send_json_status(403, {"detail": "cross-origin POST denied"})
                    return
                if not self._mutation_authorized():
                    self._discard_small_body()
                    self._send_json_status(403, {"detail": "monitor token required"})
                    return
                body = self._read_json_body()
                if body is None:
                    return

                route = routes.match_post_route(path)
                if route is None:
                    self._send_404(path)
                    return

                deps.handle_post_route(self, route.name, body, path)
            except (BrokenPipeError, ConnectionAbortedError):
                return
            except Exception as exc:  # noqa: BLE001
                self._send_500(exc)

        def _send_json_status(self, status: int, obj: Any) -> None:
            raw = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(raw)

        def _send_html(self, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self._send_security_headers(html_response=True)
            self.end_headers()
            self.wfile.write(raw)

        def _send_html_status(self, status: int, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self._send_security_headers(html_response=True)
            self.end_headers()
            self.wfile.write(raw)

        def _send_json(self, obj: Any) -> None:
            raw = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(raw)

        def _send_404(self, detail: str) -> None:
            body = f"<h1>404</h1><p>{html.escape(detail)}</p>".encode()
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_security_headers(html_response=True)
            self.end_headers()
            self.wfile.write(body)

        def _send_500(self, exc: BaseException) -> None:
            self.log_error("render error: %s", exc)
            body = f"<h1>500</h1><pre>{html.escape(repr(exc))}</pre>".encode()
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_security_headers(html_response=True)
            self.end_headers()
            self.wfile.write(body)

        def _stream_audit_log(self) -> None:
            """Server-sent events: tail the audit log line-by-line."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._send_security_headers()
            self.end_headers()

            path = deps.audit_log_path()
            position = path.stat().st_size if path.exists() else 0
            last_heartbeat = time.monotonic()
            try:
                while not server_shutdown_requested(self.server):
                    if path.exists() and path.stat().st_size > position:
                        with path.open("r", encoding="utf-8") as f:
                            f.seek(position)
                            for line in f:
                                if not line.strip():
                                    continue
                                self.wfile.write(f"data: {line.rstrip()}\n\n".encode())
                                self.wfile.flush()
                            position = f.tell()
                        last_heartbeat = time.monotonic()
                    elif time.monotonic() - last_heartbeat > 25:
                        # SSE heartbeat comment: keeps proxies from timing out
                        # on idle streams and detects dead clients.
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        last_heartbeat = time.monotonic()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

    return MonitorHandler


def make_monitor_server(
    host: str,
    port: int,
    handler_cls: type[HandlerT],
    *,
    mutations_enabled: bool,
) -> MonitorServer:
    server = MonitorServer((host, port), handler_cls)
    server._ctx_mutations_enabled = mutations_enabled
    return server
