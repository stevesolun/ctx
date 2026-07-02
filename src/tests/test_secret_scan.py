from __future__ import annotations

from ctx.utils._secret_scan import (
    find_inline_secret,
    find_inline_secret_arg,
    redact_secret_text,
)


def test_find_inline_secret_flags_nested_secret_value() -> None:
    config = {
        "mcpServers": {
            "github": {
                "env": {
                    "GITHUB_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz123456",
                },
            },
        },
    }

    assert find_inline_secret(config) == "mcpServers.github.env.GITHUB_TOKEN"


def test_find_inline_secret_allows_placeholders() -> None:
    assert find_inline_secret({"env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}) is None
    assert find_inline_secret({"env": {"API_KEY": "%API_KEY%"}}) is None
    assert find_inline_secret({"env": {"SECRET": "<secret>"}}) is None


def test_find_inline_secret_arg_flags_inline_values() -> None:
    assert (
        find_inline_secret_arg(
            ["node", "server.js", "--api-key", "sk-abcdefghijklmnopqrstuvwxyz123456"],
        )
        == "--api-key"
    )


def test_find_inline_secret_arg_does_not_return_bare_secret_value() -> None:
    assert (
        find_inline_secret_arg(["node", "server.js", "hf_abcdefghijklmnopqrstuvwxyz"])
        == "[secret-value]"
    )


def test_find_inline_secret_arg_allows_env_references() -> None:
    assert find_inline_secret_arg(["GITHUB_TOKEN=${GITHUB_TOKEN}"]) is None


def test_redact_secret_text_masks_assignment_and_token_shapes() -> None:
    text = (
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456 "
        "token=ghp_abcdefghijklmnopqrstuvwxyz123456"
    )

    assert redact_secret_text(text) == ("OPENAI_API_KEY=[redacted] token=[redacted]")
