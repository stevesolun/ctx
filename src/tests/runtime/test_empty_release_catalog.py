"""The real BYO package must never recover the retired catalog from tests."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

from ctx.runtime.production_catalog import (
    RELEASE_QUERY_CATALOG_ROOT_RESOURCE,
    ReleaseCatalogError,
    open_release_pinned_query_catalog,
)
from ctx.runtime.query_decision import QueryDecisionFailure, QueryHostDescriptor
from ctx.runtime.query_session import prepare_query_delivery, prepare_ctx_run_query_decision
from tests.release_catalog_fixture import historical_catalog_resources


def test_real_package_has_no_release_catalog_or_fallback() -> None:
    # Deliberately no historical_release_catalog fixture in this module.
    assert not files("ctx.assets").joinpath(RELEASE_QUERY_CATALOG_ROOT_RESOURCE).is_file()
    with pytest.raises(ReleaseCatalogError, match="resource is unavailable"):
        open_release_pinned_query_catalog()


def test_historical_fixture_is_scoped_and_does_not_restore_production_catalog() -> None:
    with historical_catalog_resources():
        catalog = open_release_pinned_query_catalog()
        catalog.close()
    with pytest.raises(ReleaseCatalogError, match="resource is unavailable"):
        open_release_pinned_query_catalog()


@pytest.mark.parametrize("explicit_delivery", [False, True])
def test_missing_release_catalog_fails_before_any_engine_journal(
    tmp_path: Path,
    explicit_delivery: bool,
) -> None:
    journal = tmp_path / "state" / "engine.sqlite3"
    audit = tmp_path / "state" / "benefit.sqlite3"
    arguments = {
        "task": "Fix the Python tests",
        "language": "Python",
        "session_id": "missing-release",
        "workspace": tmp_path,
        "journal_path": journal,
        "benefit_audit_path": audit,
    }
    if explicit_delivery:
        result = prepare_query_delivery(
            **arguments,  # type: ignore[arg-type]
            host=QueryHostDescriptor.codex("activate"),
            host_invocation_digest="a" * 64,
        )
    else:
        result = prepare_ctx_run_query_decision(**arguments)  # type: ignore[arg-type]

    assert isinstance(result, QueryDecisionFailure)
    assert result.failure_code == "catalog-open-failed"
    assert result.recommendation_count == 0
    assert result.recommendation_context is None
    assert not journal.parent.exists()
