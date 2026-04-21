"""
tests/test_mcp_ingest.py -- Phase 6c resume-safe ingest tests.

Contracts pinned:
  - Fresh run processes every record; checkpoint records each outcome.
  - Resume skips slugs in ``processed`` without re-invoking add_mcp.
  - Resume skips slugs in ``failures`` UNLESS ``--retry-failures``.
  - Retry clears the failure entry before re-attempting; success moves
    the entry to ``processed``, failure re-records under ``failures``.
  - Parse errors (bad dict -> McpRecord.from_dict raises) land as
    failures keyed by the raw slug, without calling add_mcp.
  - Rejections (IntakeRejected) land in ``processed`` annotated with
    ``rejected:<codes>`` — they are a valid outcome, not a failure.
  - Flush cadence: checkpoint persisted every N records + at end.
  - SIGINT simulation: graceful exit flushes the partial checkpoint.
  - load_checkpoint fail-open on missing / corrupt / wrong-version /
    wrong-source / wrong-shape.
  - _checkpoint_path rejects source names with path separators.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import mcp_ingest  # noqa: E402
from intake_gate import IntakeDecision, IntakeFinding  # noqa: E402
from intake_pipeline import IntakeRejected  # noqa: E402


# ── Helpers ─────────────────────────────────────────────────────────────────


def _record_dict(slug: str, **overrides: Any) -> dict[str, Any]:
    """Minimal McpRecord-shaped dict. Overrides win over defaults."""
    base: dict[str, Any] = {
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "description": f"{slug} mcp server",
        "sources": ["test-source"],
        "tags": [],
        "transports": [],
    }
    base.update(overrides)
    return base


class _FakeAddMcp:
    """Stand-in for ``mcp_add.add_mcp`` that records invocations.

    Default behaviour: return ``is_new_page=True`` for every call.
    Control overrides per-slug via the ``behaviour`` dict:
      - "merge": returns is_new_page=False
      - "reject": raises IntakeRejected with a single 'TEST' finding
      - "error": raises RuntimeError
    """

    def __init__(self, behaviour: dict[str, str] | None = None) -> None:
        self.behaviour = behaviour or {}
        self.calls: list[str] = []

    def __call__(self, *, record: Any, wiki_path: Path, dry_run: bool) -> dict[str, Any]:
        self.calls.append(record.slug)
        mode = self.behaviour.get(record.slug, "add")
        if mode == "reject":
            decision = IntakeDecision(
                allow=False,
                findings=(
                    IntakeFinding(code="TEST", severity="fail", message="synthetic reject"),
                ),
            )
            raise IntakeRejected(decision)
        if mode == "error":
            raise RuntimeError(f"synthetic error for {record.slug}")
        return {
            "slug": record.slug,
            "is_new_page": mode != "merge",
            "merged_sources": ["test-source"],
            "path": str(wiki_path / "entities/mcp-servers" / f"{record.slug}.md"),
        }


@pytest.fixture
def fake_add(monkeypatch: pytest.MonkeyPatch) -> _FakeAddMcp:
    """Patch mcp_ingest.add_mcp with a controllable fake."""
    fake = _FakeAddMcp()
    monkeypatch.setattr(mcp_ingest, "add_mcp", fake)
    return fake


# ── load_checkpoint / save_checkpoint ──────────────────────────────────────


class TestCheckpointPersistence:
    def test_missing_returns_empty(self, tmp_path: Path) -> None:
        cp = mcp_ingest.load_checkpoint(tmp_path, "awesome-mcp")
        assert cp["source"] == "awesome-mcp"
        assert cp["processed"] == {}
        assert cp["failures"] == {}
        assert cp["total_seen"] == 0

    def test_round_trip(self, tmp_path: Path) -> None:
        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        cp["processed"]["s1"] = {"result": "added", "at": "2026-04-21T00:00:00Z"}
        cp["failures"]["s2"] = {"error": "boom", "at": "2026-04-21T00:00:01Z"}
        cp["total_seen"] = 2
        mcp_ingest.save_checkpoint(tmp_path, cp)

        reloaded = mcp_ingest.load_checkpoint(tmp_path, "src")
        assert reloaded["processed"] == {"s1": {"result": "added", "at": "2026-04-21T00:00:00Z"}}
        assert reloaded["failures"] == {"s2": {"error": "boom", "at": "2026-04-21T00:00:01Z"}}
        assert reloaded["total_seen"] == 2

    def test_corrupt_json_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / mcp_ingest.CHECKPOINT_SUBDIR / "src.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        assert cp["processed"] == {}
        assert cp["failures"] == {}

    def test_wrong_version_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / mcp_ingest.CHECKPOINT_SUBDIR / "src.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 99, "source": "src", "processed": {"s1": {}}, "failures": {}}),
            encoding="utf-8",
        )
        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        assert cp["processed"] == {}

    def test_wrong_source_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / mcp_ingest.CHECKPOINT_SUBDIR / "src.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": mcp_ingest.CHECKPOINT_VERSION,
                    "source": "different-source",
                    "processed": {"s1": {"result": "added", "at": "x"}},
                    "failures": {},
                }
            ),
            encoding="utf-8",
        )
        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        assert cp["processed"] == {}

    def test_wrong_shape_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / mcp_ingest.CHECKPOINT_SUBDIR / "src.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]", encoding="utf-8")
        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        assert cp["processed"] == {}

    def test_path_separator_in_source_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            mcp_ingest._checkpoint_path(tmp_path, "evil/../escape")
        with pytest.raises(ValueError):
            mcp_ingest._checkpoint_path(tmp_path, ".hidden")


# ── Fresh run ───────────────────────────────────────────────────────────────


class TestFreshRun:
    def test_all_records_processed(
        self, tmp_path: Path, fake_add: _FakeAddMcp
    ) -> None:
        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        records = [_record_dict(f"slug-{i}") for i in range(3)]
        mcp_ingest.ingest_records(
            records, source="src", wiki_path=tmp_path,
            checkpoint=cp, report_progress=False,
        )
        assert fake_add.calls == ["slug-0", "slug-1", "slug-2"]
        assert set(cp["processed"]) == {"slug-0", "slug-1", "slug-2"}
        assert cp["failures"] == {}
        assert cp["total_seen"] == 3

    def test_checkpoint_persisted_at_end(
        self, tmp_path: Path, fake_add: _FakeAddMcp
    ) -> None:
        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        mcp_ingest.ingest_records(
            [_record_dict("only-one")],
            source="src", wiki_path=tmp_path,
            checkpoint=cp, report_progress=False,
        )
        reloaded = mcp_ingest.load_checkpoint(tmp_path, "src")
        assert "only-one" in reloaded["processed"]

    def test_merge_vs_add_recorded_in_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeAddMcp(behaviour={"slug-1": "merge"})
        monkeypatch.setattr(mcp_ingest, "add_mcp", fake)
        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        mcp_ingest.ingest_records(
            [_record_dict("slug-0"), _record_dict("slug-1")],
            source="src", wiki_path=tmp_path,
            checkpoint=cp, report_progress=False,
        )
        assert cp["processed"]["slug-0"]["result"] == "added"
        assert cp["processed"]["slug-1"]["result"] == "merged"


# ── Resume ──────────────────────────────────────────────────────────────────


class TestResume:
    def test_processed_slugs_skipped(
        self, tmp_path: Path, fake_add: _FakeAddMcp
    ) -> None:
        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        cp["processed"]["slug-1"] = {"result": "added", "at": "prior"}
        mcp_ingest.save_checkpoint(tmp_path, cp)

        # Fresh checkpoint object for the run (matches what CLI does).
        cp2 = mcp_ingest.load_checkpoint(tmp_path, "src")
        mcp_ingest.ingest_records(
            [_record_dict("slug-0"), _record_dict("slug-1"), _record_dict("slug-2")],
            source="src", wiki_path=tmp_path,
            checkpoint=cp2, report_progress=False,
        )
        # slug-1 must NOT have been passed to add_mcp.
        assert fake_add.calls == ["slug-0", "slug-2"]
        assert set(cp2["processed"]) == {"slug-0", "slug-1", "slug-2"}

    def test_failures_skipped_without_retry_flag(
        self, tmp_path: Path, fake_add: _FakeAddMcp
    ) -> None:
        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        cp["failures"]["slug-bad"] = {"error": "prior", "at": "earlier"}
        mcp_ingest.save_checkpoint(tmp_path, cp)

        cp2 = mcp_ingest.load_checkpoint(tmp_path, "src")
        mcp_ingest.ingest_records(
            [_record_dict("slug-bad"), _record_dict("slug-ok")],
            source="src", wiki_path=tmp_path,
            checkpoint=cp2, report_progress=False,
        )
        assert fake_add.calls == ["slug-ok"]
        # Failure stays recorded.
        assert "slug-bad" in cp2["failures"]

    def test_retry_failures_reattempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Prior run failed on slug-bad; this run's fake succeeds for it.
        fake = _FakeAddMcp()  # default = success for everyone
        monkeypatch.setattr(mcp_ingest, "add_mcp", fake)

        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        cp["failures"]["slug-bad"] = {"error": "prior", "at": "earlier"}
        mcp_ingest.save_checkpoint(tmp_path, cp)

        cp2 = mcp_ingest.load_checkpoint(tmp_path, "src")
        mcp_ingest.ingest_records(
            [_record_dict("slug-bad")],
            source="src", wiki_path=tmp_path,
            checkpoint=cp2, retry_failures=True, report_progress=False,
        )
        assert fake.calls == ["slug-bad"]
        assert "slug-bad" in cp2["processed"]
        assert "slug-bad" not in cp2["failures"]

    def test_retry_that_fails_again_stays_in_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeAddMcp(behaviour={"slug-bad": "error"})
        monkeypatch.setattr(mcp_ingest, "add_mcp", fake)

        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        cp["failures"]["slug-bad"] = {"error": "first attempt", "at": "earlier"}

        mcp_ingest.ingest_records(
            [_record_dict("slug-bad")],
            source="src", wiki_path=tmp_path,
            checkpoint=cp, retry_failures=True, report_progress=False,
        )
        assert "slug-bad" in cp["failures"]
        # Error message reflects the NEW attempt, not the prior one.
        assert "synthetic error" in cp["failures"]["slug-bad"]["error"]
        assert "slug-bad" not in cp["processed"]


# ── Outcome classification ─────────────────────────────────────────────────


class TestOutcomes:
    def test_parse_error_lands_in_failures(self, tmp_path: Path, fake_add: _FakeAddMcp) -> None:
        # Empty dict -> McpRecord.from_dict raises ValueError for
        # missing slug AND name.
        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        bad_record: dict[str, Any] = {}
        mcp_ingest.ingest_records(
            [bad_record, _record_dict("ok")],
            source="src", wiki_path=tmp_path,
            checkpoint=cp, report_progress=False,
        )
        # Fake was only called for the valid record.
        assert fake_add.calls == ["ok"]
        # Failure keyed by the raw slug placeholder.
        assert any("parse" in entry["error"] for entry in cp["failures"].values())

    def test_rejection_recorded_as_processed_not_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeAddMcp(behaviour={"slug-rj": "reject"})
        monkeypatch.setattr(mcp_ingest, "add_mcp", fake)

        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        mcp_ingest.ingest_records(
            [_record_dict("slug-rj")],
            source="src", wiki_path=tmp_path,
            checkpoint=cp, report_progress=False,
        )
        assert "slug-rj" in cp["processed"]
        assert cp["processed"]["slug-rj"]["result"].startswith("rejected:")
        assert "slug-rj" not in cp["failures"]

    def test_runtime_error_lands_in_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeAddMcp(behaviour={"slug-err": "error"})
        monkeypatch.setattr(mcp_ingest, "add_mcp", fake)

        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        mcp_ingest.ingest_records(
            [_record_dict("slug-err"), _record_dict("slug-ok")],
            source="src", wiki_path=tmp_path,
            checkpoint=cp, report_progress=False,
        )
        # Both slugs attempted — error doesn't abort the batch.
        assert fake.calls == ["slug-err", "slug-ok"]
        assert "slug-err" in cp["failures"]
        assert "slug-ok" in cp["processed"]


# ── Flush cadence ───────────────────────────────────────────────────────────


class TestFlushCadence:
    def test_flush_every_n_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Count save_checkpoint invocations to verify flush cadence.
        save_calls: list[int] = []
        original = mcp_ingest.save_checkpoint

        def _counting_save(*a, **kw):  # type: ignore[no-untyped-def]
            save_calls.append(len(a))
            return original(*a, **kw)

        monkeypatch.setattr(mcp_ingest, "save_checkpoint", _counting_save)
        fake = _FakeAddMcp()
        monkeypatch.setattr(mcp_ingest, "add_mcp", fake)

        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        records = [_record_dict(f"slug-{i}") for i in range(7)]
        mcp_ingest.ingest_records(
            records, source="src", wiki_path=tmp_path,
            checkpoint=cp, flush_every=3, report_progress=False,
        )
        # 7 records, flush_every=3 -> flushes at i=3 and i=6 (2 cadence
        # flushes) + 1 final flush = 3 saves total.
        assert len(save_calls) == 3


# ── SIGINT simulation ──────────────────────────────────────────────────────


class TestGracefulExit:
    def test_sigint_mid_run_flushes_and_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graceful = mcp_ingest._GracefulExit()
        fake = _FakeAddMcp()
        monkeypatch.setattr(mcp_ingest, "add_mcp", fake)

        def _flip_after_second(*a, **kw):  # type: ignore[no-untyped-def]
            # First call: record slug-0 (doesn't flip).
            # Second call: record slug-1 then set the flag so the loop
            # exits BEFORE slug-2.
            result = _FakeAddMcp.__call__(fake, *a, **kw)
            if fake.calls == ["slug-0", "slug-1"]:
                graceful.requested = True
            return result

        monkeypatch.setattr(mcp_ingest, "add_mcp", _flip_after_second)

        cp = mcp_ingest.load_checkpoint(tmp_path, "src")
        mcp_ingest.ingest_records(
            [_record_dict(f"slug-{i}") for i in range(5)],
            source="src", wiki_path=tmp_path,
            checkpoint=cp, graceful=graceful, report_progress=False,
        )
        # slug-0 and slug-1 processed, rest skipped.
        assert fake.calls == ["slug-0", "slug-1"]
        assert set(cp["processed"]) == {"slug-0", "slug-1"}
        # Final flush captured the interrupted state.
        reloaded = mcp_ingest.load_checkpoint(tmp_path, "src")
        assert set(reloaded["processed"]) == {"slug-0", "slug-1"}
