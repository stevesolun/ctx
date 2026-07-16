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
        "nested": {"password": sentinel, "enabled": True},
        "label": "OPENAI_API_KEY=abc123",
        "quoted_assignment": f'"OPENAI_API_KEY" = "{sentinel}"',
        "spaced_assignment": f"CLIENT_SECRET = '{sentinel}'",
        "http_header": f"Authorization: Bearer {sentinel}",
        "request_line": f"Bearer {sentinel}",
        "argv": ["runner", "--api-key", sentinel, "--mode", "safe"],
        "credential_store": {"opaque": sentinel},
        "webhookSecret": sentinel,
        "resolver": {"recommendation_top_k": 3},
    }
    user_path.write_text(json.dumps(raw_user), encoding="utf-8")

    payload = config_service.effective_config_payload(user_path)

    assert payload["defaults"]["service_token"] == "[redacted]"
    assert payload["user"]["api_token"] == "[redacted]"
    assert payload["user"]["nested"]["password"] == "[redacted]"
    assert payload["user"]["label"] == "OPENAI_API_KEY=[redacted]"
    assert payload["user"]["quoted_assignment"] == '"OPENAI_API_KEY" = "[redacted]"'
    assert payload["user"]["spaced_assignment"] == "CLIENT_SECRET = '[redacted]'"
    assert payload["user"]["http_header"] == "Authorization: Bearer [redacted]"
    assert payload["user"]["request_line"] == "Bearer [redacted]"
    assert payload["user"]["argv"] == [
        "runner",
        "--api-key",
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
        "slug_tokens": 0.25,
        "token_edges": {"dense_token_threshold": 17},
        "tokenizer": "cl100k_base",
        "token_budget": 8192,
    }
    user_path.write_text(json.dumps(raw_user), encoding="utf-8")

    payload = config_service.effective_config_payload(user_path)

    assert (
        payload["defaults"]["graph"]["edge_weights"]["slug_tokens"]
        == defaults["graph"]["edge_weights"]["slug_tokens"]
    )
    assert payload["defaults"]["graph"]["token_edges"] == defaults["graph"]["token_edges"]
    assert payload["user"] == raw_user
    assert payload["effective"]["tokenizer"] == "cl100k_base"
    assert payload["effective"]["token_budget"] == 8192
