"""CLI helpers for operating ctx enterprise telemetry."""

from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from ctx.telemetry import (
    enforce_telemetry_retention,
    export_events,
    export_metrics,
    plan_telemetry_retention,
    preview_export,
    preview_metrics_export,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx-telemetry-export",
        description="Export the local ctx telemetry spool to a configured enterprise sink.",
    )
    parser.add_argument(
        "--signal",
        choices=("events", "metrics"),
        default="events",
        help="Telemetry signal to export.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Telemetry spool to read (default: configured event or metric spool).",
    )
    parser.add_argument(
        "--sink",
        choices=("local_jsonl", "otlp_http"),
        default=None,
        help="Export sink for this run (default: telemetry.export.sink from config).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output file for --sink local_jsonl.",
    )
    parser.add_argument(
        "--otlp-endpoint",
        default=None,
        help="OTLP/HTTP logs or metrics endpoint for --sink otlp_http.",
    )
    parser.add_argument(
        "--otlp-allowed-host",
        action="append",
        default=[],
        help=(
            "Allow one remote OTLP endpoint host for this run; repeat for "
            "multiple hosts."
        ),
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="Export checkpoint file (default: <spool>.export-checkpoint.json).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Replay the full spool instead of only events after the checkpoint.",
    )
    parser.add_argument(
        "--trusted-root",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and count telemetry records without exporting them.",
    )
    parser.add_argument(
        "--fail-on-degraded",
        action="store_true",
        help="Exit non-zero when export status is degraded, even if no sink failed.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def _build_retention_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx-telemetry-retention",
        description="Plan or enforce local ctx telemetry retention.",
    )
    parser.add_argument("command", choices=("plan", "enforce"))
    parser.add_argument(
        "--signal",
        choices=("all", "events", "metrics"),
        default="all",
        help="Telemetry signal to process.",
    )
    parser.add_argument(
        "--event-path",
        type=Path,
        default=None,
        help="Event spool to process (default: telemetry.path from config).",
    )
    parser.add_argument(
        "--metrics-path",
        type=Path,
        default=None,
        help="Metric spool to process (default: telemetry.metrics.path from config).",
    )
    parser.add_argument(
        "--trusted-root",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--drop-malformed",
        action="store_true",
        help="Drop malformed JSONL records instead of preserving them.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def _base_telemetry_config() -> dict[str, Any]:
    try:
        from ctx_config import cfg  # noqa: PLC0415

        raw = cfg.get("telemetry", {})
    except Exception:  # noqa: BLE001 - CLI must still work in minimal installs.
        raw = {}
    return copy.deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}


def _effective_config(args: argparse.Namespace) -> dict[str, Any]:
    config = _base_telemetry_config()
    if args.signal == "metrics":
        metrics = config.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}
            config["metrics"] = metrics
        metrics["enabled"] = True
        export = metrics.get("export")
        if not isinstance(export, dict):
            export = {}
            metrics["export"] = export
        if args.path is not None:
            metrics["path"] = str(args.path)
    else:
        export = config.get("export")
        if args.path is not None:
            config["path"] = str(args.path)
    if not isinstance(export, dict):
        export = {}
        if args.signal == "metrics":
            config["metrics"]["export"] = export
        else:
            config["export"] = export
    export["enabled"] = True
    if args.sink is not None:
        export["sink"] = args.sink
    if args.output is not None:
        export["path"] = str(args.output)
    if args.checkpoint is not None:
        export["checkpoint_path"] = str(args.checkpoint)
    if args.otlp_endpoint is not None:
        otlp = export.get("otlp")
        if not isinstance(otlp, dict):
            otlp = {}
            export["otlp"] = otlp
        otlp["endpoint"] = args.otlp_endpoint
    if args.otlp_allowed_host:
        otlp = export.get("otlp")
        if not isinstance(otlp, dict):
            otlp = {}
            export["otlp"] = otlp
        existing = otlp.get("allowed_hosts")
        allowed_hosts = list(existing) if isinstance(existing, list) else []
        allowed_hosts.extend(str(host) for host in args.otlp_allowed_host)
        otlp["allowed_hosts"] = allowed_hosts
    return config


def _print_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _exit_code(*, failed: int, status: str, fail_on_degraded: bool) -> int:
    if failed != 0:
        return 1
    if fail_on_degraded and status == "degraded":
        return 1
    return 0


def _export_summary(result: Any, *, signal: str, dry_run: bool) -> dict[str, Any]:
    payload = {
        "signal": signal,
        "attempted": result.attempted,
        "exported": 0 if dry_run else result.exported,
        "failed": 0 if dry_run else result.failed,
        "sink": result.sink,
        "status": result.status,
        "dry_run": dry_run,
        "checkpoint_path": result.checkpoint_path,
        "checkpoint_advanced": result.checkpoint_advanced,
        "checkpoint_found": result.checkpoint_found,
        "malformed_records": result.malformed_records,
        "malformed_pending_records": result.malformed_pending_records,
        "malformed_first_line": result.malformed_first_line,
        "malformed_last_line": result.malformed_last_line,
        "destination_hash": result.destination_hash,
        "last_success_at": result.last_success_at,
        "status_path": result.status_path,
    }
    if signal == "metrics":
        payload.update(
            {
                "last_metric_id": result.last_metric_id,
                "checkpoint_before_metric_id": result.checkpoint_before_metric_id,
                "checkpoint_after_metric_id": result.checkpoint_after_metric_id,
                "last_success_metric_id": result.last_success_metric_id,
                "error_kind": None if dry_run else result.error_kind,
            }
        )
    else:
        payload.update(
            {
                "last_event_id": result.last_event_id,
                "checkpoint_before_event_id": result.checkpoint_before_event_id,
                "checkpoint_after_event_id": result.checkpoint_after_event_id,
                "last_success_event_id": result.last_success_event_id,
                "error_kind": None if dry_run else result.error_kind,
            }
        )
    return payload


def _print_export_human(payload: Mapping[str, Any], *, dry_run: bool) -> None:
    noun = "metric(s)" if payload["signal"] == "metrics" else "event(s)"
    if dry_run:
        print(
            f"Would export {payload['attempted']} telemetry {noun} "
            f"to {payload['sink']}."
        )
    else:
        print(
            "Exported "
            f"{payload['exported']}/{payload['attempted']} telemetry {noun} "
            f"to {payload['sink']}."
        )
    if payload.get("error_kind"):
        print(f"Telemetry export failed: {payload['error_kind']}", file=sys.stderr)
    print(f"Export status: {payload['status']}")
    if payload["malformed_pending_records"]:
        print(
            "Skipped "
            f"{payload['malformed_pending_records']} pending malformed telemetry "
            "record(s)."
        )
    if payload.get("status_path"):
        print(f"Export status path: {payload['status_path']}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = _effective_config(args)

    try:
        if args.dry_run:
            preview_result: Any
            if args.signal == "metrics":
                preview_result = preview_metrics_export(
                    args.path,
                    trusted_root=args.trusted_root,
                    config=config,
                    include_exported=args.all,
                )
            else:
                preview_result = preview_export(
                    args.path,
                    trusted_root=args.trusted_root,
                    config=config,
                    include_exported=args.all,
                )
            payload = _export_summary(preview_result, signal=args.signal, dry_run=True)
            if args.json:
                _print_json(payload)
            else:
                _print_export_human(payload, dry_run=True)
            return _exit_code(
                failed=0,
                status=str(payload["status"]),
                fail_on_degraded=args.fail_on_degraded,
            )

        export_result: Any
        if args.signal == "metrics":
            export_result = export_metrics(
                args.path,
                trusted_root=args.trusted_root,
                config=config,
                include_exported=args.all,
            )
        else:
            export_result = export_events(
                args.path,
                trusted_root=args.trusted_root,
                config=config,
                include_exported=args.all,
            )
    except ValueError as exc:
        if args.json:
            _print_json({"attempted": 0, "exported": 0, "failed": 1, "error": str(exc)})
        else:
            print(f"Telemetry export failed: {exc}", file=sys.stderr)
        return 1
    payload = _export_summary(export_result, signal=args.signal, dry_run=False)
    if args.json:
        _print_json(payload)
    else:
        _print_export_human(payload, dry_run=False)
    return _exit_code(
        failed=int(payload["failed"]),
        status=str(payload["status"]),
        fail_on_degraded=args.fail_on_degraded,
    )


def retention_main(argv: list[str] | None = None) -> int:
    args = _build_retention_parser().parse_args(argv)
    config = _base_telemetry_config()
    drop_malformed = True if args.drop_malformed else None
    try:
        if args.command == "plan":
            results = plan_telemetry_retention(
                signal=args.signal,
                event_path=args.event_path,
                metrics_path=args.metrics_path,
                trusted_root=args.trusted_root,
                config=config,
                drop_malformed=drop_malformed,
            )
        else:
            results = enforce_telemetry_retention(
                signal=args.signal,
                event_path=args.event_path,
                metrics_path=args.metrics_path,
                trusted_root=args.trusted_root,
                config=config,
                drop_malformed=drop_malformed,
            )
    except ValueError as exc:
        if args.json:
            _print_json({"failed": 1, "error": str(exc)})
        else:
            print(f"Telemetry retention failed: {exc}", file=sys.stderr)
        return 1
    payload = {
        "dry_run": args.command == "plan",
        "results": [asdict(result) for result in results],
    }
    if args.json:
        _print_json(payload)
    else:
        for result in results:
            print(
                f"{result.signal}: {result.status}; "
                f"retained={result.retained_records} "
                f"dropped={result.dropped_records} "
                f"malformed_dropped={result.malformed_dropped_records}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
