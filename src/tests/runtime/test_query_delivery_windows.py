from __future__ import annotations

import os
from pathlib import Path

import pytest

from ctx.runtime.query_decision import QueryHostDescriptor
from ctx.runtime.query_delivery import QueryDeliveryController, SensitiveQueryInput


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows contract")


@pytest.mark.parametrize("mode", ["shadow", "recommend", "activate"])
def test_native_windows_query_delivery_fails_closed_before_state(
    tmp_path: Path,
    mode: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    controller = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode=mode,
        state_root=state_root,
        environment={},
    )

    report = controller.issue(
        SensitiveQueryInput(
            native_session_id="windows-session",
            logical_prompt_id="turn-1",
            workspace=workspace,
            prompt="Review the implementation",
            language="python",
        )
    )

    assert report.status == "failed"
    assert report.emission_permit is None
    assert not state_root.exists()


def test_native_windows_legacy_mode_remains_a_no_state_noop(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    controller = QueryDeliveryController(
        host=QueryHostDescriptor.claude_code(),
        mode="legacy",
        state_root=state_root,
        environment={},
    )

    report = controller.issue(
        SensitiveQueryInput(
            native_session_id="windows-session",
            logical_prompt_id="turn-1",
            workspace=tmp_path,
            prompt="Review the implementation",
            language="python",
        )
    )

    assert report.status == "legacy"
    assert report.emission_permit is None
    assert not state_root.exists()
