#!/usr/bin/env python3
"""
mcp_ingest.py -- Resume-safe MCP ingest orchestrator.

Phase 6c wraps the ``fetch -> add`` pipeline with a durable checkpoint
so a 12k+ run that crashes at record 9,500 restarts at record 9,501,
not at 0. The orchestrator is otherwise a thin shim: each record goes
through ``mcp_add.add_mcp`` unchanged, and the checkpoint is advisory
(the filesystem + canonical index remain authoritative).

Why not async/threaded
----------------------
The hot cost per record is the intake gate's embedding model call
(~100ms). That is synchronous and sentence-transformers-backed; running
it across threads would contend on the embedding cache JSON writes and
force us to isolate per-thread caches. The proper optimization is
batch inference inside ``intake_pipeline`` — a separate change. Until
then, sequential is simpler, correct, and resume-safe, which is the
reliability win that actually matters for large ingests.

Usage
-----
    # Stream from a source, checkpointed per-source:
    ctx-mcp-fetch --source awesome-mcp | ctx-mcp-ingest --source awesome-mcp

    # Replay a JSONL file (idempotent re-runs skip already-processed):
    ctx-mcp-ingest --source pulsemcp --from-jsonl records.jsonl

    # Retry only the failures from the prior run:
    ctx-mcp-ingest --source pulsemcp --retry-failures --from-stdin

    # Inspect progress:
    ctx-mcp-ingest --source pulsemcp --status

Checkpoint
----------
Location: ``<wiki>/.ingest-checkpoint/<source>.json``

Schema v1::

    {
      "version": 1,
      "source": "pulsemcp",
      "started_at": "2026-04-21T06:00:00Z",
      "updated_at": "2026-04-21T06:12:34Z",
      "total_seen": 12975,
      "processed": {
        "<slug>": {"result": "added"|"merged"|"rejected", "at": "..."}
      },
      "failures": {
        "<slug>": {"error": "...", "at": "..."}
      }
    }

``processed`` slugs skip entirely on resume. ``failures`` are kept
separate so ``--retry-failures`` can target just them without re-doing
the 9k successful records.

Interrupts
----------
SIGINT/SIGTERM trigger a final checkpoint flush before exit. The
in-flight record is NOT aborted mid-write — Python's signal handling
is cooperative, so the interrupt is observed between records. In the
worst case the checkpoint is one record behind disk state; the next
run's resume will re-attempt that slug and either merge (if already
written) or add (if the crash happened before write).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TypedDict

from _fs_utils import atomic_write_json
from ctx_config import cfg
from intake_pipeline import IntakeRejected
from mcp_add import add_mcp, _MCP_ENTITY_SUBDIR
from mcp_entity import McpRecord
from wiki_sync import ensure_wiki

__all__ = [
    "CHECKPOINT_SUBDIR",
    "CHECKPOINT_VERSION",
    "IngestCheckpoint",
    "load_checkpoint",
    "save_checkpoint",
    "ingest_records",
]

CHECKPOINT_SUBDIR = ".ingest-checkpoint"
CHECKPOINT_VERSION = 1
DEFAULT_FLUSH_EVERY = 10


class _ProcessedEntry(TypedDict):
    result: str  # "added" | "merged" | "rejected"
    at: str


class _FailureEntry(TypedDict):
    error: str
    at: str


class IngestCheckpoint(TypedDict):
    """On-disk checkpoint. ``source`` pins the checkpoint to one catalog."""

    version: int
    source: str
    started_at: str
    updated_at: str
    total_seen: int
    processed: dict[str, _ProcessedEntry]
    failures: dict[str, _FailureEntry]


# ── Checkpoint persistence ───────────────────────────────────────────────────


def _now_iso() -> str:
    """UTC ISO-8601 timestamp with seconds precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _checkpoint_path(wiki_path: Path, source: str) -> Path:
    """Return the sidecar path for ``source``'s checkpoint.

    Source name safety delegates to the shared validator in
    ``_safe_name``. See its docstring for the full rule set — most
    notably it rejects Windows drive-relative (``C:evil``) which the
    older ad-hoc check missed. Security-auditor H-3.
    """
    from _safe_name import validate_source_name  # noqa: PLC0415
    validate_source_name(source, field="source")
    return wiki_path / CHECKPOINT_SUBDIR / f"{source}.json"


def _empty_checkpoint(source: str) -> IngestCheckpoint:
    """Return a fresh checkpoint seeded with ``source`` and now-timestamps."""
    now = _now_iso()
    return {
        "version": CHECKPOINT_VERSION,
        "source": source,
        "started_at": now,
        "updated_at": now,
        "total_seen": 0,
        "processed": {},
        "failures": {},
    }


def load_checkpoint(wiki_path: Path, source: str) -> IngestCheckpoint:
    """Load ``source``'s checkpoint. Return an empty one on any failure.

    Missing file, corrupt JSON, version mismatch, or wrong shape all
    collapse to "fresh run". This is intentional: the filesystem +
    canonical index are authoritative, so a lost checkpoint just means
    the next run re-attempts records. Those will hit the existing
    entity paths and take the merge-not-add branch — cheap and correct.
    """
    path = _checkpoint_path(wiki_path, source)
    if not path.is_file():
        return _empty_checkpoint(source)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_checkpoint(source)
    if not isinstance(data, dict) or data.get("version") != CHECKPOINT_VERSION:
        return _empty_checkpoint(source)
    if data.get("source") != source:
        # Defensive: someone hand-copied a checkpoint or the filename
        # was renamed. Treat as mismatch rather than silently conflating.
        return _empty_checkpoint(source)
    processed = data.get("processed") or {}
    failures = data.get("failures") or {}
    if not isinstance(processed, dict) or not isinstance(failures, dict):
        return _empty_checkpoint(source)
    return {
        "version": CHECKPOINT_VERSION,
        "source": source,
        "started_at": str(data.get("started_at") or _now_iso()),
        "updated_at": str(data.get("updated_at") or _now_iso()),
        "total_seen": int(data.get("total_seen") or 0),
        "processed": processed,
        "failures": failures,
    }


def save_checkpoint(wiki_path: Path, checkpoint: IngestCheckpoint) -> None:
    """Atomically persist ``checkpoint`` to the sidecar. Bumps ``updated_at``."""
    checkpoint["updated_at"] = _now_iso()
    atomic_write_json(_checkpoint_path(wiki_path, checkpoint["source"]), checkpoint)


# ── Core loop ────────────────────────────────────────────────────────────────


class _GracefulExit:
    """SIGINT/SIGTERM observer. ``requested`` flips to True between records.

    We intentionally don't raise from the handler — Python's signal
    handling is cooperative, and raising mid-IO can corrupt partial
    writes. The record loop checks ``requested`` between iterations
    and flushes cleanly.
    """

    def __init__(self) -> None:
        self.requested = False
        self._prev_int = signal.getsignal(signal.SIGINT)
        self._prev_term = signal.getsignal(signal.SIGTERM)

    def install(self) -> None:
        signal.signal(signal.SIGINT, self._handle)
        # SIGTERM isn't deliverable on Windows the same way (Python only
        # surfaces SIGBREAK/SIGINT there), but signal.signal on an
        # unsupported signal is a no-op on Windows so this is safe.
        try:
            signal.signal(signal.SIGTERM, self._handle)
        except (ValueError, OSError):
            pass

    def uninstall(self) -> None:
        signal.signal(signal.SIGINT, self._prev_int)
        try:
            signal.signal(signal.SIGTERM, self._prev_term)
        except (ValueError, OSError):
            pass

    def _handle(self, signum: int, frame: object) -> None:  # noqa: ARG002
        self.requested = True


def ingest_records(
    records: Iterable[dict[str, Any]],
    *,
    source: str,
    wiki_path: Path,
    checkpoint: IngestCheckpoint,
    dry_run: bool = False,
    retry_failures: bool = False,
    flush_every: int = DEFAULT_FLUSH_EVERY,
    graceful: _GracefulExit | None = None,
    report_progress: bool = True,
) -> IngestCheckpoint:
    """Run ``records`` through ``add_mcp``, updating ``checkpoint`` in place.

    Returns the same checkpoint object (mutated) for caller convenience.
    Writes to disk every ``flush_every`` records and on graceful exit.

    Skip rules:
      - slug in ``checkpoint['processed']`` -> skip (already done)
      - slug in ``checkpoint['failures']`` AND not retry_failures -> skip
      - slug in ``checkpoint['failures']`` AND retry_failures -> retry
        (the failure entry is removed before the attempt; success or
        new failure overwrites it)
    """
    added = merged = rejected = errored = skipped = 0
    seen_this_run = 0

    def _progress(i: int, slug: str, status: str) -> None:
        if report_progress:
            print(f"  [{i}] [{status}] {slug}", flush=True)

    for raw in records:
        checkpoint["total_seen"] += 1
        seen_this_run += 1

        raw_slug = raw.get("slug") or "<unknown>"

        # Parse first so checkpoint key matches record.slug exactly.
        # Malformed records go to failures keyed by the raw slug we have.
        try:
            record = McpRecord.from_dict(raw)
        except Exception as exc:  # noqa: BLE001 — one bad record must not kill the run
            errored += 1
            checkpoint["failures"][str(raw_slug)] = {
                "error": f"parse: {exc}",
                "at": _now_iso(),
            }
            _progress(seen_this_run, str(raw_slug), "parse-error")
            if seen_this_run % flush_every == 0:
                save_checkpoint(wiki_path, checkpoint)
            if graceful and graceful.requested:
                break
            continue

        slug = record.slug

        # Resume decisions.
        if slug in checkpoint["processed"]:
            skipped += 1
            _progress(seen_this_run, slug, "skip-processed")
            if graceful and graceful.requested:
                break
            continue
        if slug in checkpoint["failures"]:
            if not retry_failures:
                skipped += 1
                _progress(seen_this_run, slug, "skip-prior-failure")
                if graceful and graceful.requested:
                    break
                continue
            # Retry: clear the old failure so a new attempt's outcome
            # is the recorded one, whether success or a new failure.
            del checkpoint["failures"][slug]

        # Attempt.
        try:
            result = add_mcp(record=record, wiki_path=wiki_path, dry_run=dry_run)
            outcome = "added" if result["is_new_page"] else "merged"
            if outcome == "added":
                added += 1
            else:
                merged += 1
            checkpoint["processed"][slug] = {"result": outcome, "at": _now_iso()}
            _progress(seen_this_run, slug, outcome)
        except IntakeRejected as exc:
            rejected += 1
            codes = ", ".join(f.code for f in exc.decision.failures) or "unknown"
            # Rejections are *not* failures — they're a valid outcome.
            # Record under processed so resumes don't reprocess, but
            # annotate with the reason.
            checkpoint["processed"][slug] = {
                "result": f"rejected:{codes}",
                "at": _now_iso(),
            }
            _progress(seen_this_run, slug, f"rejected:{codes}")
        except Exception as exc:  # noqa: BLE001 — batch must continue
            errored += 1
            checkpoint["failures"][slug] = {
                "error": f"{type(exc).__name__}: {exc}",
                "at": _now_iso(),
            }
            _progress(seen_this_run, slug, "error")

        if seen_this_run % flush_every == 0:
            save_checkpoint(wiki_path, checkpoint)

        if graceful and graceful.requested:
            break

    # Final flush — captures the tail end of the run, SIGINT or not.
    save_checkpoint(wiki_path, checkpoint)

    if report_progress:
        tail = " (interrupted)" if graceful and graceful.requested else ""
        print(
            f"\nIngest summary{tail}: "
            f"{added} added, {merged} merged, {rejected} rejected, "
            f"{errored} errors, {skipped} skipped",
            flush=True,
        )

    return checkpoint


# ── CLI ──────────────────────────────────────────────────────────────────────


def _iter_records_from(
    args: argparse.Namespace,
) -> Iterable[dict[str, Any]]:
    """Yield record dicts from whichever input arg was supplied.

    We don't pre-read the whole stream — that would defeat the point
    of streaming from ``ctx-mcp-fetch | ctx-mcp-ingest``. JSONL readers
    yield per line; the JSON-object reader is a single-record degenerate.
    """
    if args.from_json:
        path = Path(os.path.expanduser(args.from_json))
        yield json.loads(path.read_text(encoding="utf-8"))
        return
    if args.from_jsonl:
        path = Path(os.path.expanduser(args.from_jsonl))
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"Warning: line {lineno} bad JSON: {exc}", file=sys.stderr)
        return
    # Default: stdin.
    for lineno, raw in enumerate(sys.stdin, 1):
        line = raw.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"Warning: stdin line {lineno} bad JSON: {exc}", file=sys.stderr)


def _print_status(wiki_path: Path, source: str) -> None:
    """Dump a human-readable summary of the current checkpoint."""
    cp = load_checkpoint(wiki_path, source)
    print(f"source:       {cp['source']}")
    print(f"started_at:   {cp['started_at']}")
    print(f"updated_at:   {cp['updated_at']}")
    print(f"total_seen:   {cp['total_seen']}")
    print(f"processed:    {len(cp['processed'])}")
    print(f"failures:     {len(cp['failures'])}")
    if cp["failures"]:
        print("\nLast 10 failures:")
        # Dict iteration is insertion-ordered (Python 3.7+) so the tail
        # is genuinely the most-recently-recorded failures.
        for slug, info in list(cp["failures"].items())[-10:]:
            print(f"  {slug}: {info['error']}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx-mcp-ingest",
        description=(
            "Resume-safe MCP ingest. Checkpoints per source so a "
            "crashed or interrupted run picks up where it left off."
        ),
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Source name (e.g. 'awesome-mcp', 'pulsemcp'). Pins the checkpoint.",
    )
    inp = parser.add_mutually_exclusive_group()
    inp.add_argument("--from-json", metavar="PATH", help="Single JSON object file")
    inp.add_argument("--from-jsonl", metavar="PATH", help="JSONL file, one record per line")
    inp.add_argument(
        "--from-stdin",
        action="store_true",
        help="Read JSONL records from stdin (default if no other input given)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print checkpoint summary for --source and exit",
    )
    parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="Re-attempt slugs recorded in the checkpoint's failures map",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the checkpoint file for --source before starting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and route records but skip writes and embeddings",
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=DEFAULT_FLUSH_EVERY,
        help=f"Flush checkpoint every N records (default: {DEFAULT_FLUSH_EVERY})",
    )
    parser.add_argument(
        "--wiki",
        default=str(cfg.wiki_dir),
        help="Wiki root path",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-record progress lines",
    )
    return parser


def _force_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8.

    Mirror of the same helper in mcp_fetch. Needed here because non-ASCII
    slugs, descriptions, or error messages would crash Windows' default
    cp1252 console the moment a record with CJK / emoji / accented text
    flows through the per-record progress printer.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main() -> None:
    """Entry point for the ``ctx-mcp-ingest`` console script."""
    _force_utf8_stdio()
    parser = _build_parser()
    args = parser.parse_args()

    wiki_path = Path(os.path.expanduser(args.wiki))
    ensure_wiki(str(wiki_path))

    if args.status:
        _print_status(wiki_path, args.source)
        sys.exit(0)

    if args.flush_every <= 0:
        print("Error: --flush-every must be a positive integer", file=sys.stderr)
        sys.exit(2)

    if args.reset:
        try:
            _checkpoint_path(wiki_path, args.source).unlink()
            print(f"Reset checkpoint for source {args.source!r}.", file=sys.stderr)
        except FileNotFoundError:
            pass

    checkpoint = load_checkpoint(wiki_path, args.source)

    graceful = _GracefulExit()
    graceful.install()
    try:
        ingest_records(
            _iter_records_from(args),
            source=args.source,
            wiki_path=wiki_path,
            checkpoint=checkpoint,
            dry_run=args.dry_run,
            retry_failures=args.retry_failures,
            flush_every=args.flush_every,
            graceful=graceful,
            report_progress=not args.quiet,
        )
    finally:
        graceful.uninstall()

    # Non-zero exit when failures remain uncleared — lets CI tie
    # "ingest green" to "no outstanding error records".
    sys.exit(1 if checkpoint["failures"] else 0)


# _MCP_ENTITY_SUBDIR re-exported for tests that want to look at disk
# state without re-importing mcp_add.
_MCP_ENTITY_DIR_NAME = _MCP_ENTITY_SUBDIR


if __name__ == "__main__":
    main()
