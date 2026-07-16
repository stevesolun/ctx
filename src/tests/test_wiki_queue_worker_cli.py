"""CLI contract tests for the durable wiki queue worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ctx.core.wiki import wiki_queue_worker


def test_once_forwards_limit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, Any] = {}

    def fake_drain_queue(wiki_path: Path, **kwargs: Any) -> list[object]:
        observed["wiki_path"] = wiki_path
        observed.update(kwargs)
        return []

    monkeypatch.setattr(wiki_queue_worker, "drain_queue", fake_drain_queue)

    wiki_queue_worker.main(["--wiki", str(tmp_path), "--worker-id", "test-worker", "--once"])

    assert observed["wiki_path"] == tmp_path
    assert observed["worker_id"] == "test-worker"
    assert observed["limit"] == 1
    assert capsys.readouterr().out == "No ready wiki queue jobs.\n"


def test_once_and_limit_are_mutually_exclusive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        wiki_queue_worker.main(["--once", "--limit", "1"])

    assert exc_info.value.code == 2
    assert "use either --once or --limit, not both" in capsys.readouterr().err
