"""Opt-in live MCP compatibility checks.

These tests execute only when explicitly enabled with ``--run-live-mcp`` and
one or more trusted ``--live-mcp-config`` files. They intentionally do not
ship a default third-party server command; live MCP servers are arbitrary local
subprocesses and must be selected by the person running the test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ctx.adapters.generic.tools.mcp_router import McpClient, McpServerConfig

pytestmark = pytest.mark.integration


def _trusted_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "trusted-server",
        "command": "python",
        "trust": {
            "server_is_third_party_code": True,
            "approved_by": "test",
        },
    }
    payload.update(overrides)
    return payload


def test_server_config_inherit_env_defaults_false(tmp_path: Path) -> None:
    config = _server_config_from_payload(_trusted_payload(), tmp_path)
    assert config.inherit_env is False


@pytest.mark.parametrize("inherit_env", [False, True])
def test_server_config_accepts_boolean_inherit_env(
    inherit_env: bool,
    tmp_path: Path,
) -> None:
    config = _server_config_from_payload(
        _trusted_payload(inherit_env=inherit_env),
        tmp_path,
    )
    assert config.inherit_env is inherit_env


@pytest.mark.parametrize("inherit_env", ["false", "true", 0, 1, None])
def test_server_config_rejects_non_boolean_inherit_env(
    inherit_env: object,
    tmp_path: Path,
) -> None:
    with pytest.raises(AssertionError, match="inherit_env"):
        _server_config_from_payload(
            _trusted_payload(inherit_env=inherit_env),
            tmp_path,
        )


def test_live_mcp_servers_from_trusted_configs(
    pytestconfig: pytest.Config,
    tmp_path: Path,
) -> None:
    if not bool(pytestconfig.getoption("--run-live-mcp")):
        pytest.skip("live MCP compatibility is opt-in; pass --run-live-mcp")
    raw_paths = list(pytestconfig.getoption("--live-mcp-config") or [])
    if not raw_paths:
        pytest.fail("--run-live-mcp requires at least one --live-mcp-config PATH")

    for raw_path in raw_paths:
        payload = json.loads(Path(raw_path).read_text(encoding="utf-8-sig"))
        server_config = _server_config_from_payload(payload, tmp_path)
        expected_tools = set(_string_list(payload, "expected_tools"))
        probe = payload.get("probe")

        with McpClient(server_config) as client:
            tools = client.list_tools()
            tool_names = {tool.name for tool in tools}
            missing = expected_tools - tool_names
            assert missing == set(), (
                f"{server_config.name} did not expose expected tools {sorted(missing)}; "
                f"available={sorted(tool_names)}"
            )

            if isinstance(probe, dict):
                result = client.call_tool(
                    _required_str(probe, "tool"),
                    _expand_placeholders(
                        probe.get("arguments", {}),
                        tmp_path,
                    ),
                )
                expected_text = probe.get("expect_text_contains")
                if isinstance(expected_text, str) and expected_text:
                    assert expected_text in result


def _server_config_from_payload(
    payload: dict[str, Any],
    tmp_path: Path,
) -> McpServerConfig:
    trust = payload.get("trust")
    if not isinstance(trust, dict) or trust.get("server_is_third_party_code") is not True:
        raise AssertionError(
            "live MCP config must explicitly acknowledge third-party code execution"
        )
    if not isinstance(trust.get("approved_by"), str) or not trust["approved_by"]:
        raise AssertionError("live MCP config must include trust.approved_by")

    return McpServerConfig(
        name=_required_str(payload, "name"),
        command=_required_str(payload, "command"),
        args=tuple(_expand_placeholders(_string_list(payload, "args"), tmp_path)),
        env=dict(_expand_placeholders(_string_dict(payload, "env"), tmp_path)),
        startup_timeout=float(payload.get("startup_timeout", 30.0)),
        request_timeout=float(payload.get("request_timeout", 10.0)),
        inherit_env=_optional_bool(payload, "inherit_env", default=False),
    )


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"live MCP config field {key!r} must be a non-empty string")
    return value


def _string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AssertionError(f"live MCP config field {key!r} must be a string list")
    return list(value)


def _string_dict(payload: dict[str, Any], key: str) -> dict[str, str]:
    value = payload.get(key, {})
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in value.items()
    ):
        raise AssertionError(f"live MCP config field {key!r} must be a string map")
    return dict(value)


def _optional_bool(payload: dict[str, Any], key: str, *, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload[key]
    if not isinstance(value, bool):
        raise AssertionError(f"live MCP config field {key!r} must be a boolean")
    return value


def _expand_placeholders(value: Any, tmp_path: Path) -> Any:
    if isinstance(value, str):
        return value.replace("${tmp_path}", str(tmp_path))
    if isinstance(value, list):
        return [_expand_placeholders(item, tmp_path) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _expand_placeholders(item, tmp_path)
            for key, item in value.items()
        }
    return value
