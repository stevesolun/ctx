"""Contract tests for the zero-spend CTX Fit live-prerequisite CI lane."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from scripts.ci_required import CHEAP_PR_SKIPPABLE_JOBS, REQUIRED_JOBS


WORKFLOW_PATH = Path(".github/workflows/test.yml")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_linux_live_prerequisite_lane_keeps_the_base_unit_install_small() -> None:
    """Paid-run dependencies belong in their own smoke lane, not every unit shard."""

    jobs = _workflow()["jobs"]
    unit_install = _step(jobs["unit-linux"], "Install dependencies")["run"]
    live_install = _step(jobs["fit-live-prerequisites-linux"], "Install CTX Fit live dependencies")[
        "run"
    ]

    assert 'python -m pip install ".[dev]"' in unit_install
    assert "harness" not in unit_install
    assert 'python -m pip install ".[dev,harness]"' in live_install
    assert "bubblewrap" in live_install


def test_linux_live_prerequisite_lane_is_pinned_bounded_and_zero_spend() -> None:
    job = _workflow()["jobs"]["fit-live-prerequisites-linux"]
    checkout = _step(job, "Checkout")
    setup_python = _step(job, "Set up Python 3.11")
    setup_node = _step(job, "Set up Node.js 24")
    adversarial = _step(job, "Run adversarial Linux sandbox checks")["run"]
    probe = _step(job, "Verify the zero-spend live-driver prerequisites")
    command = probe["run"]

    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 10
    assert job["needs"] == "classify"
    assert checkout["uses"].startswith("actions/checkout@")
    assert setup_python["uses"].startswith("actions/setup-python@")
    assert setup_node["uses"].startswith("actions/setup-node@")
    for action in (checkout["uses"], setup_python["uses"], setup_node["uses"]):
        assert len(action.rsplit("@", 1)[1]) == 40

    assert "test_repository_process_can_write_inside_but_not_beside_its_workspace" in adversarial
    assert "test_repository_process_cannot_read_an_ambient_temporary_secret" in adversarial
    assert "test_network_disabled_process_cannot_connect_to_tcp" in adversarial
    assert "test_repository_process_cannot_signal_an_ambient_host_process" in adversarial
    assert "--junitxml=.ctx-fit-linux-sandbox.xml" in adversarial
    assert "if skipped:" in adversarial

    # Exercise CTX's actual Linux boundary and driver constructor. The returned
    # driver must never be invoked: doing so is the first provider/model call.
    assert "sandboxed_command(" in command
    assert "subprocess.run(" in command
    assert "build_agent_driver()" in command
    assert "_constructed_driver(" not in command
    assert "completion(" not in command
    assert command.count('"npx"') == 1
    assert command.count('"node"') == 1
    assert 'subprocess.run(["npx"' not in command

    # Secrets are not mapped into the job, and common ambient provider keys are
    # explicitly blanked so this smoke cannot spend even if repository settings
    # change later.
    for key in (
        "ANTHROPIC_API_KEY",
        "AZURE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        assert probe["env"][key] == ""
        assert f'"{key}"' in command


def test_linux_live_prerequisite_lane_is_part_of_the_stable_required_gate() -> None:
    job_name = "fit-live-prerequisites-linux"

    assert job_name in REQUIRED_JOBS
    assert job_name in CHEAP_PR_SKIPPABLE_JOBS
    assert job_name in _workflow()["jobs"]["ci-required"]["needs"]
