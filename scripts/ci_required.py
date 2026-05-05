"""Validate GitHub Actions dependency results for the stable CI check."""

from __future__ import annotations

import json
import os
from typing import Any

CHEAP_PR_SKIPPABLE_JOBS = {
    "clean-host-contract",
    "contract-compat",
    "e2e-canary",
    "no-test-no-merge",
    "package-build",
    "package-smoke",
    "similarity-integration",
    "static",
    "unit-linux",
}
REQUIRED_JOBS = {
    "browser-security",
    "classify",
    "clean-host-contract",
    "contract-compat",
    "docs-check",
    "e2e-canary",
    "graph-check",
    "no-test-no-merge",
    "package-build",
    "package-smoke",
    "similarity-integration",
    "static",
    "test",
    "unit-linux",
}


def _job_output(
    needs: dict[str, dict[str, Any]],
    job_name: str,
    output_name: str,
) -> str | None:
    outputs = needs.get(job_name, {}).get("outputs", {})
    if not isinstance(outputs, dict):
        return None
    output = outputs.get(output_name)
    return output if isinstance(output, str) else None


def failed_required_jobs(
    needs: dict[str, dict[str, Any]],
    *,
    event_name: str,
) -> dict[str, str | None]:
    failures: dict[str, str | None] = {}
    for name in sorted(REQUIRED_JOBS - set(needs)):
        failures[name] = "missing"
    docs_only_pr = (
        event_name == "pull_request"
        and _job_output(needs, "classify", "docs_only") == "true"
    )
    graph_only_pr = (
        event_name == "pull_request"
        and _job_output(needs, "classify", "graph_only") == "true"
    )
    cheap_pr = docs_only_pr or graph_only_pr
    for name, details in sorted(needs.items()):
        result = details.get("result")
        if result == "success":
            continue
        if (
            event_name != "pull_request"
            and name in {"docs-check", "graph-check", "no-test-no-merge"}
            and result == "skipped"
        ):
            continue
        if cheap_pr and name in CHEAP_PR_SKIPPABLE_JOBS and result == "skipped":
            continue
        if (
            event_name == "pull_request"
            and name == "docs-check"
            and result == "skipped"
            and not docs_only_pr
        ):
            continue
        if (
            event_name == "pull_request"
            and name == "graph-check"
            and result == "skipped"
            and not graph_only_pr
        ):
            continue
        if (
            event_name == "pull_request"
            and name == "browser-security"
            and result == "skipped"
            and _job_output(needs, "classify", "browser_changed") == "false"
        ):
            continue
        if (
            event_name == "pull_request"
            and name == "similarity-integration"
            and result == "skipped"
            and _job_output(needs, "classify", "similarity_changed") == "false"
        ):
            continue
        if event_name == "pull_request" and name == "test" and result == "skipped":
            continue
        failures[name] = result
    return failures


def main() -> int:
    event_name = os.environ["EVENT_NAME"]
    needs = json.loads(os.environ["NEEDS_JSON"])
    bad = failed_required_jobs(needs, event_name=event_name)

    for name, details in sorted(needs.items()):
        print(f"{name}: {details.get('result')}")

    if bad:
        for name, result in bad.items():
            print(f"::error::{name} finished with {result}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
