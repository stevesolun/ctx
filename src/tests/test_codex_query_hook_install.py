from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from ctx.adapters.codex import install_query_hook as installer


def test_codex_query_hook_has_exact_current_shape_without_trust_state() -> None:
    hooks = installer.make_query_hooks()

    assert hooks == {
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": installer._module_cmd("ctx.adapters.codex.hook_handler"),
                        "timeout": 10,
                        "additionalContextLimit": 0,
                    }
                ]
            }
        ]
    }
    encoded = json.dumps(hooks)
    assert "matcher" not in encoded
    assert "trusted_hash" not in encoded
    assert "enabled" not in encoded
    assert sys.executable in hooks["UserPromptSubmit"][0]["hooks"][0]["command"]  # type: ignore[index]


def test_codex_query_hook_preserves_unrelated_configuration_and_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "codex" / "hooks.json"
    path.parent.mkdir()
    original = {
        "description": "keep this valid Codex description",
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "existing-stop", "timeout": 7}]}],
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "existing-prompt", "timeout": 3}]}
            ],
        },
    }
    path.write_text(json.dumps(original), encoding="utf-8")

    first = installer.install_query_hook(path)
    first_bytes = path.read_bytes()
    first_mtime = path.stat().st_mtime_ns
    second = installer.install_query_hook(path)

    assert first == second
    assert path.read_bytes() == first_bytes
    assert path.stat().st_mtime_ns == first_mtime
    assert first["description"] == original["description"]
    hooks = first["hooks"]
    original_hooks = original["hooks"]
    assert isinstance(hooks, dict)
    assert isinstance(original_hooks, dict)
    assert hooks["Stop"] == original_hooks["Stop"]
    prompt_commands = [
        hook["command"] for group in hooks["UserPromptSubmit"] for hook in group["hooks"]
    ]
    assert prompt_commands == [
        "existing-prompt",
        installer._module_cmd("ctx.adapters.codex.hook_handler"),
    ]


def test_codex_query_hook_replaces_prior_interpreter_and_repairs_ctx_handler(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "http",
                                    "command": ("/old/python -m ctx.adapters.codex.hook_handler"),
                                    "timeout": 999_999,
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    installed = installer.install_query_hook(path)

    assert installed["hooks"] == installer.make_query_hooks()


def test_codex_query_hook_rejects_unknown_root_fields_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "hooks.json"
    raw = b'{"vendor":{"keep":true},"hooks":{}}'
    path.write_bytes(raw)

    with pytest.raises(ValueError, match="unsupported root"):
        installer.install_query_hook(path)

    assert path.read_bytes() == raw


@pytest.mark.parametrize(
    "handler",
    [
        {"type": "http", "command": "external"},
        {"type": "command", "timeout": -1, "command": "external"},
        {"type": "command", "async": "yes", "command": "external"},
        {"type": "command", "additionalContextLimit": True, "command": "external"},
    ],
)
def test_codex_query_hook_rejects_invalid_handler_metadata_without_writing(
    tmp_path: Path,
    handler: dict[str, object],
) -> None:
    path = tmp_path / "hooks.json"
    raw = json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [handler]}]}}).encode()
    path.write_bytes(raw)

    with pytest.raises(ValueError):
        installer.install_query_hook(path)

    assert path.read_bytes() == raw


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"[]",
        b'{"hooks":{},"hooks":{}}',
        b'{"hooks":[]}',
        b'{"hooks":{"Stop":[42]}}',
        b'{"hooks":{"Stop":[{"hooks":[42]}]}}',
    ],
)
def test_codex_query_hook_rejects_invalid_configuration_without_writing(
    tmp_path: Path,
    raw: bytes,
) -> None:
    path = tmp_path / "hooks.json"
    path.write_bytes(raw)

    with pytest.raises(ValueError):
        installer.install_query_hook(path)

    assert path.read_bytes() == raw


def test_codex_query_hook_rejects_symlink_and_hardlink_targets(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    symlink = tmp_path / "symlink.json"
    try:
        symlink.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    with pytest.raises(ValueError):
        installer.install_query_hook(symlink)
    assert target.read_text() == "{}"

    dangling = tmp_path / "dangling.json"
    try:
        dangling.symlink_to(tmp_path / "missing.json")
    except OSError as error:
        pytest.skip(f"dangling symlinks unavailable: {error}")
    with pytest.raises(ValueError):
        installer.install_query_hook(dangling)
    assert dangling.is_symlink()

    hardlink = tmp_path / "hardlink.json"
    try:
        os.link(target, hardlink)
    except OSError as error:
        pytest.skip(f"hardlinks unavailable: {error}")
    with pytest.raises(ValueError):
        installer.install_query_hook(hardlink)
    assert target.read_text() == "{}"


def test_codex_query_hook_rejects_oversized_file_before_reading(tmp_path: Path) -> None:
    path = tmp_path / "hooks.json"
    with path.open("wb") as stream:
        stream.truncate(2 * 1024 * 1024 + 1)

    with pytest.raises(ValueError, match="size bound"):
        installer.install_query_hook(path)

    assert path.stat().st_size == 2 * 1024 * 1024 + 1


def test_codex_query_hook_does_not_write_output_over_the_input_size_bound(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hooks.json"
    prefix = b'{"description":"'
    suffix = b'"}'
    raw = prefix + (b"x" * (2 * 1024 * 1024 - len(prefix) - len(suffix))) + suffix
    assert len(raw) == 2 * 1024 * 1024
    path.write_bytes(raw)

    with pytest.raises(ValueError, match="updated.*size bound"):
        installer.install_query_hook(path)

    assert path.read_bytes() == raw


def test_codex_query_hook_path_respects_absolute_codex_home(tmp_path: Path) -> None:
    assert installer.default_hooks_path({"CODEX_HOME": str(tmp_path)}) == (tmp_path / "hooks.json")
    with pytest.raises(ValueError):
        installer.default_hooks_path({"CODEX_HOME": "relative"})


def test_codex_query_hook_cli_reports_registration_without_claiming_trust(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "hooks.json"

    assert installer.main(["--hooks-path", str(path)]) == 0

    output = capsys.readouterr().out
    assert "registered" in output
    assert "Review and approve" in output
    assert "trusted" not in path.read_text()
