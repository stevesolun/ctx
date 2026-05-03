"""Validate GitHub Actions dependency results for the stable CI check."""

from __future__ import annotations

import json
import os
from typing import Any


def failed_required_jobs(
    needs: dict[str, dict[str, Any]],
    *,
    event_name: str,
) -> dict[str, str | None]:
    failures: dict[str, str | None] = {}
    for name, details in sorted(needs.items()):
        result = details.get("result")
        if result == "success":
            continue
        if (
            event_name != "pull_request"
            and name == "no-test-no-merge"
            and result == "skipped"
        ):
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
