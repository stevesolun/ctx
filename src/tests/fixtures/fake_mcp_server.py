"""fake_mcp_server.py — minimal MCP server stub used by test_mcp_router.

Runs as a standalone subprocess launched by the tests. Reads JSON-RPC
lines on stdin, writes responses on stdout. Designed to be small and
scriptable: its behaviour is configurable via env vars so a single
script covers initialize + tools/list + tools/call + failure modes.

Env vars (all optional):
  FAKE_MCP_FAIL_INIT=1          - respond with an error to initialize
  FAKE_MCP_CRASH_ON_TOOL=1      - crash (exit 1) on any tools/call
  FAKE_MCP_TOOL_ERROR=1         - return isError=True on any tools/call
  FAKE_MCP_IGNORE_TOOL=1        - accept tools/call but never answer
  FAKE_MCP_NOISY_STDERR=1       - write a warning line to stderr on startup
  FAKE_MCP_EMIT_NOTIFICATION=1  - emit a progress notification before each
                                  tools/call response
  FAKE_MCP_EXTRA_TOOL=<name>    - add a second tool with this name (for
                                  testing multi-tool list_tools)

Tool catalog:
  echo(text: str) -> str
      returns the input text verbatim. Used to confirm args round-trip.
  add(a: int, b: int) -> str
      returns str(a+b). Integer-args coverage.
"""

from __future__ import annotations

import json
import os
import sys


_PROTOCOL_VERSION = "2024-11-05"


def _echo_tool(args: dict) -> dict:
    text = str(args.get("text", ""))
    return {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }


def _add_tool(args: dict) -> dict:
    a = int(args.get("a", 0))
    b = int(args.get("b", 0))
    return {
        "content": [{"type": "text", "text": str(a + b)}],
        "isError": False,
    }


def _env_tool(args: dict) -> dict:
    name = str(args.get("name", ""))
    return {
        "content": [{"type": "text", "text": os.environ.get(name, "")}],
        "isError": False,
    }


TOOLS = {"echo": _echo_tool, "add": _add_tool, "echo_env": _env_tool}


def _tool_defs() -> list[dict]:
    defs = [
        {
            "name": "echo",
            "description": "Echo the input text verbatim.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
        {
            "name": "add",
            "description": "Return the sum of two integers.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
        },
        {
            "name": "echo_env",
            "description": "Return the value of an environment variable.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    ]
    extra = os.environ.get("FAKE_MCP_EXTRA_TOOL")
    if extra:
        defs.append(
            {
                "name": extra,
                "description": f"Extra tool {extra} (test-only).",
                "inputSchema": {"type": "object", "properties": {}},
            }
        )
    return defs


def _emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def main() -> None:
    if os.environ.get("FAKE_MCP_NOISY_STDERR") == "1":
        sys.stderr.write("fake-mcp-server: starting up\n")
        sys.stderr.flush()

    while True:
        line = sys.stdin.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method", "")
        req_id = req.get("id")
        params = req.get("params") or {}

        if method == "initialize":
            if os.environ.get("FAKE_MCP_FAIL_INIT") == "1":
                _emit({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": "init-forbidden"},
                })
                continue
            _emit({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-mcp", "version": "0.1"},
                },
            })
            continue

        if method == "notifications/initialized":
            continue

        if method == "tools/list":
            _emit({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": _tool_defs()},
            })
            continue

        if method == "tools/call":
            if os.environ.get("FAKE_MCP_CRASH_ON_TOOL") == "1":
                sys.exit(1)
            if os.environ.get("FAKE_MCP_IGNORE_TOOL") == "1":
                continue

            name = params.get("name", "")
            args = params.get("arguments") or {}

            if os.environ.get("FAKE_MCP_EMIT_NOTIFICATION") == "1":
                _emit({
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {"progress": 0.5},
                })

            if os.environ.get("FAKE_MCP_TOOL_ERROR") == "1":
                _emit({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": "forced error"}],
                        "isError": True,
                    },
                })
                continue

            handler = TOOLS.get(name)
            if handler is None:
                _emit({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"unknown tool {name!r}",
                    },
                })
                continue
            _emit({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": handler(args),
            })
            continue

        # Unknown method.
        _emit({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        })


if __name__ == "__main__":
    main()
