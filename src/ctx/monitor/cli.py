"""CLI and blocking serve loop for ctx-monitor."""

from __future__ import annotations

import argparse
import secrets
import socket
from typing import Callable, Protocol

from ctx.monitor.security import host_allows_mutations


class ServingServer(Protocol):
    """Minimal server interface used by the blocking monitor loop."""

    def serve_forever(self) -> None: ...

    def server_close(self) -> None: ...


def monitor_display_host(host: str) -> str:
    """Return a URL host users can paste into a browser."""
    if host in {"0.0.0.0", "::"}:
        try:
            candidate = socket.gethostbyname(socket.gethostname())
        except OSError:
            candidate = ""
        if candidate and not candidate.startswith("127."):
            return candidate
        return "localhost"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def serve(
    *,
    host: str,
    port: int,
    make_server: Callable[[str, int], ServingServer],
    set_monitor_token: Callable[[str], None],
    display_host: Callable[[str], str] = monitor_display_host,
) -> None:
    """Run the monitor. Blocks until Ctrl+C."""
    server = make_server(host, port)
    monitor_token = secrets.token_urlsafe(32)
    set_monitor_token(monitor_token)
    mutations_enabled = bool(getattr(server, "_ctx_mutations_enabled", False))
    url = f"http://{display_host(host)}:{port}/"
    if not mutations_enabled:
        url = f"{url}?token={monitor_token}"
    print(f"ctx-monitor serving at {url}  (Ctrl+C to stop)", flush=True)
    if not mutations_enabled:
        print(
            "ctx-monitor: non-loopback bind; read token required and "
            "load/unload mutations disabled",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("ctx-monitor: shutdown", flush=True)
    finally:
        server.server_close()


def main(
    argv: list[str] | None = None,
    *,
    serve_func: Callable[..., None],
) -> int:
    parser = argparse.ArgumentParser(
        prog="ctx-monitor",
        description="Local HTTP dashboard for ctx skill/agent activity.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="Start the monitor web server")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind (default: 127.0.0.1)",
    )
    sp.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Explicitly allow a token-protected, read-only non-loopback bind",
    )

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        if not host_allows_mutations(args.host) and not args.allow_non_loopback:
            parser.error(
                "non-loopback --host requires explicit --allow-non-loopback",
            )
        serve_func(host=args.host, port=args.port)
    return 0
