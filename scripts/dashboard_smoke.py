from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from dataclasses import dataclass


@dataclass(frozen=True)
class CheckSpec:
    name: str
    path: str
    marker: str


@dataclass
class CheckResult:
    name: str
    path: str
    status: int
    elapsed: float
    ok: bool
    reason: str
    bytes_read: int


DEFAULT_CHECKS = (
    CheckSpec("home", "/", "ctx monitor"),
    CheckSpec("loaded", "/loaded", "Loaded"),
    CheckSpec("skills", "/skills", "Skills"),
    CheckSpec("wiki", "/wiki", "Wiki"),
    CheckSpec("graph", "/graph", "Knowledge graph"),
    CheckSpec("manage", "/manage", "Manage catalog"),
    CheckSpec("harness", "/harness", "Harness Setup"),
    CheckSpec("docs", "/docs", "Docs"),
    CheckSpec("config", "/config", "Config"),
    CheckSpec("status", "/status", "Status"),
    CheckSpec("kpi", "/kpi", "KPIs"),
    CheckSpec("runtime", "/runtime", "Runtime"),
    CheckSpec("sessions", "/sessions", "Sessions"),
    CheckSpec("logs", "/logs", "Logs"),
    CheckSpec("events", "/events", "Live events"),
    CheckSpec("live", "/live", "Live events"),
    CheckSpec("catalog-page", "/catalog?type=skill&q=code", "Catalog"),
    CheckSpec("graph-api-cold", "/api/graph/github.json?type=mcp-server&limit=20", "nodes"),
    CheckSpec("catalog-search", "/api/entities/search.json?q=code%20review&type=skill&limit=10", "results"),
    CheckSpec("entity-detail", "/api/entity/github.json?type=mcp-server", "frontmatter"),
)

WARM_CHECKS = (
    CheckSpec("graph-api-warm", "/api/graph/github.json?type=mcp-server&limit=20", "nodes"),
    CheckSpec("graph-warm", "/graph", "Knowledge graph"),
    CheckSpec("wiki-detail-warm", "/wiki/github?type=mcp-server", "github"),
    CheckSpec("kpi-warm", "/kpi", "KPIs"),
)


def _join_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _fetch(base_url: str, spec: CheckSpec, *, timeout: float) -> CheckResult:
    started = time.perf_counter()
    status = 0
    body = ""
    try:
        with urllib.request.urlopen(_join_url(base_url, spec.path), timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", errors="replace")
    except OSError as exc:
        elapsed = time.perf_counter() - started
        return CheckResult(spec.name, spec.path, status, elapsed, False, str(exc), 0)
    elapsed = time.perf_counter() - started
    ok = status == 200 and spec.marker in body
    if status != 200:
        reason = f"status {status}"
    elif spec.marker not in body:
        reason = f"missing marker {spec.marker!r}"
    else:
        reason = "ok"
    return CheckResult(spec.name, spec.path, status, elapsed, ok, reason, len(body))


def run_smoke(
    base_url: str,
    *,
    timeout: float,
    include_warm: bool = False,
) -> list[CheckResult]:
    specs = list(DEFAULT_CHECKS)
    if include_warm:
        specs.extend(WARM_CHECKS)
    return [_fetch(base_url, spec, timeout=timeout) for spec in specs]


def apply_latency_thresholds(results: list[CheckResult], thresholds: dict[str, float]) -> None:
    for result in results:
        threshold = thresholds.get(result.name)
        if threshold is None or result.elapsed <= threshold:
            continue
        result.ok = False
        result.reason = f"slow: {result.elapsed:.2f}s > {threshold:.2f}s"


def results_to_jsonl(results: list[CheckResult]) -> str:
    rows = []
    for result in results:
        row = asdict(result)
        row["bytes"] = row.pop("bytes_read")
        rows.append(json.dumps(row, sort_keys=True))
    return "\n".join(rows)


def _parse_thresholds(raw: list[str]) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for item in raw:
        name, sep, value = item.partition("=")
        if not sep:
            raise argparse.ArgumentTypeError(f"threshold must be name=seconds: {item}")
        try:
            thresholds[name] = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid threshold seconds: {item}") from exc
    return thresholds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smoke-test a running ctx-monitor dashboard using stdlib HTTP checks.",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--warm", action="store_true", help="Repeat key routes after caches are warm.")
    parser.add_argument(
        "--fail-on-slow",
        action="append",
        default=[],
        metavar="NAME=SECONDS",
        help="Fail when a named check exceeds a latency threshold. Repeatable.",
    )
    parser.add_argument("--jsonl", action="store_true", help="Emit machine-readable JSONL.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    results = run_smoke(args.base_url, timeout=args.timeout, include_warm=args.warm)
    apply_latency_thresholds(results, _parse_thresholds(args.fail_on_slow))
    if args.jsonl:
        print(results_to_jsonl(results))
    else:
        for result in results:
            status = "PASS" if result.ok else "FAIL"
            print(
                f"{status:4} {result.name:16} status={result.status:<3} "
                f"elapsed={result.elapsed:6.2f}s bytes={result.bytes_read:<7} {result.reason}"
            )
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
