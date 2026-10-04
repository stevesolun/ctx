"""Subprocess regressions for expected CLI input and filesystem failures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def _run_console_script(
    name: str,
    *args: str,
    home: Path,
) -> subprocess.CompletedProcess[str]:
    script = Path(sys.executable).parent / name
    assert script.is_file(), f"console script is not installed: {script}"
    env = {**os.environ, "HOME": str(home)}
    env.pop("CTX_TELEMETRY_HASH_SALT", None)
    return subprocess.run(
        [str(script), *args],
        check=False,
        capture_output=True,
        env=env,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("json_output", [False, True])
def test_source_registry_reports_invalid_json_without_traceback(
    tmp_path: Path,
    json_output: bool,
) -> None:
    args = ["--registry", os.devnull]
    if json_output:
        args.append("--json")

    completed = _run_console_script(
        "ctx-source-registry",
        *args,
        home=tmp_path,
    )

    assert completed.returncode == 1
    assert "Traceback" not in completed.stdout + completed.stderr
    if json_output:
        assert completed.stderr == ""
        payload = json.loads(completed.stdout)
        assert set(payload) == {"error", "failed"}
        assert payload["failed"] == 1
        assert "Expecting value" in payload["error"]
    else:
        assert completed.stdout == ""
        assert completed.stderr.startswith("Source registry validation failed: Expecting value")
        assert len(completed.stderr.splitlines()) == 1


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize(
    ("registry_payload", "expected_error"),
    [
        ([None], "source registry item 0 must be an object"),
        ({"source": []}, "source registry object must contain a sources list"),
    ],
    ids=("non-object-item", "misspelled-sources-key"),
)
def test_source_registry_rejects_invalid_structures_without_traceback(
    tmp_path: Path,
    registry_payload: object,
    expected_error: str,
    json_output: bool,
) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(registry_payload), encoding="utf-8")
    args = ["--registry", str(registry)]
    if json_output:
        args.append("--json")

    completed = _run_console_script("ctx-source-registry", *args, home=tmp_path)

    assert completed.returncode == 1
    assert "Traceback" not in completed.stdout + completed.stderr
    if json_output:
        assert completed.stderr == ""
        assert json.loads(completed.stdout) == {
            "error": expected_error,
            "failed": 1,
        }
    else:
        assert completed.stdout == ""
        assert completed.stderr == f"Source registry validation failed: {expected_error}\n"


@pytest.mark.parametrize(
    ("entrypoint", "args", "expected_keys", "error_prefix"),
    [
        (
            "ctx-telemetry-export",
            ("--path", f"{os.devnull}/events", "--dry-run"),
            {"attempted", "error", "exported", "failed"},
            "Telemetry export failed:",
        ),
        (
            "ctx-telemetry-retention",
            ("plan", "--signal", "events", "--event-path", f"{os.devnull}/events"),
            {"error", "failed"},
            "Telemetry retention failed:",
        ),
    ],
)
@pytest.mark.parametrize("json_output", [False, True])
def test_telemetry_clis_report_invalid_paths_without_traceback(
    tmp_path: Path,
    entrypoint: str,
    args: tuple[str, ...],
    expected_keys: set[str],
    error_prefix: str,
    json_output: bool,
) -> None:
    command_args = [*args]
    if json_output:
        command_args.append("--json")

    completed = _run_console_script(entrypoint, *command_args, home=tmp_path)

    assert completed.returncode == 1
    assert "Traceback" not in completed.stdout + completed.stderr
    if json_output:
        assert completed.stderr == ""
        payload = json.loads(completed.stdout)
        assert set(payload) == expected_keys
        assert payload["failed"] == 1
        assert os.devnull in payload["error"]
    else:
        assert completed.stdout == ""
        assert completed.stderr.startswith(error_prefix)
        assert os.devnull in completed.stderr
        assert len(completed.stderr.splitlines()) == 1
