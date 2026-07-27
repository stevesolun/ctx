"""Privacy regressions for ctx-monitor config payloads."""

from __future__ import annotations

import json
from pathlib import Path

from ctx.monitor.services import config as config_service


def test_effective_config_payload_redacts_nested_and_inline_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sentinel = "ctx-monitor-secret-sentinel-7x9"
    monkeypatch.setattr(
        config_service,
        "read_default_config_raw",
        lambda: {
            "resolver": {"recommendation_top_k": 5},
            "service_token": sentinel,
        },
    )
    user_path = tmp_path / "skill-system-config.json"
    raw_user = {
        "api_token": sentinel,
        "refresh_token_value": sentinel,
        "idTokenHint": sentinel,
        "access_tokens": [sentinel],
        "nested": {"password": sentinel, "enabled": True},
        "label": "OPENAI_API_KEY=abc123",
        "metadata": f"refresh_token_value={sentinel}",
        "quoted_assignment": f'"OPENAI_API_KEY" = "{sentinel}"',
        "spaced_assignment": f"CLIENT_SECRET = '{sentinel}'",
        "http_header": f"Authorization: Bearer {sentinel}",
        "request_line": f"Bearer {sentinel}",
        "argv": [
            "runner",
            "--api-key",
            sentinel,
            "--id-token-hint",
            sentinel,
            "--mode",
            "safe",
        ],
        "credential_store": {"opaque": sentinel},
        "webhookSecret": sentinel,
        "resolver": {"recommendation_top_k": 3},
    }
    user_path.write_text(json.dumps(raw_user), encoding="utf-8")

    payload = config_service.effective_config_payload(user_path)

    assert payload["defaults"]["service_token"] == "[redacted]"
    assert payload["user"]["api_token"] == "[redacted]"
    assert payload["user"]["refresh_token_value"] == "[redacted]"
    assert payload["user"]["idTokenHint"] == "[redacted]"
    assert payload["user"]["access_tokens"] == "[redacted]"
    assert payload["user"]["nested"]["password"] == "[redacted]"
    assert payload["user"]["label"] == "OPENAI_API_KEY=[redacted]"
    assert payload["user"]["metadata"] == "refresh_token_value=[redacted]"
    assert payload["user"]["quoted_assignment"] == '"OPENAI_API_KEY" = "[redacted]"'
    assert payload["user"]["spaced_assignment"] == "CLIENT_SECRET = '[redacted]'"
    assert payload["user"]["http_header"] == "Authorization: Bearer [redacted]"
    assert payload["user"]["request_line"] == "Bearer [redacted]"
    assert payload["user"]["argv"] == [
        "runner",
        "--api-key",
        "[redacted]",
        "--id-token-hint",
        "[redacted]",
        "--mode",
        "safe",
    ]
    assert payload["user"]["credential_store"] == "[redacted]"
    assert payload["user"]["webhookSecret"] == "[redacted]"
    assert payload["effective"]["api_token"] == "[redacted]"
    assert payload["effective"]["service_token"] == "[redacted]"
    assert payload["effective"]["resolver"]["recommendation_top_k"] == 3
    assert payload["user"]["nested"]["enabled"] is True
    assert set(payload) == {"defaults", "user", "effective", "path"}
    assert sentinel not in json.dumps(payload, sort_keys=True)
    assert json.loads(user_path.read_text(encoding="utf-8")) == raw_user


def test_effective_config_payload_preserves_nonsecret_token_config(tmp_path: Path) -> None:
    defaults = config_service.read_default_config_raw()
    user_path = tmp_path / "skill-system-config.json"
    raw_user = {
        "graph": {
            "edge_weights": {"slug_tokens": 0.25},
            "token_edges": {
                "dense_token_threshold": 17,
                "shared_token_saturation": "private-value",
            },
        },
        "slug_tokens": "private-value",
        "token_budget": "private-value",
        "other": {"dense_token_threshold": "private-value"},
    }
    user_path.write_text(json.dumps(raw_user), encoding="utf-8")

    payload = config_service.effective_config_payload(user_path)

    assert (
        payload["defaults"]["graph"]["edge_weights"]["slug_tokens"]
        == defaults["graph"]["edge_weights"]["slug_tokens"]
    )
    assert payload["defaults"]["graph"]["token_edges"] == defaults["graph"]["token_edges"]
    assert payload["user"]["graph"]["edge_weights"]["slug_tokens"] == 0.25
    assert payload["user"]["graph"]["token_edges"]["dense_token_threshold"] == 17
    assert payload["user"]["graph"]["token_edges"]["shared_token_saturation"] == "[redacted]"
    assert payload["user"]["slug_tokens"] == "[redacted]"
    assert payload["user"]["token_budget"] == "[redacted]"
    assert payload["user"]["other"]["dense_token_threshold"] == "[redacted]"
    assert payload["effective"]["graph"]["edge_weights"]["slug_tokens"] == 0.25
    assert payload["effective"]["graph"]["token_edges"]["dense_token_threshold"] == 17
    assert "private-value" not in json.dumps(payload, sort_keys=True)
