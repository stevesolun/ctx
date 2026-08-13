from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ctx.core.install_policy_store import persist_install_policy
from ctx.engine.installation import InstallConsentPolicy
from ctx.runtime.activated_skill_availability import (
    open_activated_skill_query_availability,
)
from ctx.runtime.production_catalog import open_release_pinned_query_catalog
from ctx.runtime.release_material import RELEASE_INSTALL_SKILL_ID
from ctx.runtime.release_skill_dispatcher import dispatch_release_skill_install
from ctx.runtime.release_skill_layout import open_release_skill_runtime_layout
from ctx.runtime.release_skill_lifecycle import activate_installed_release_skill


NOW = datetime(2026, 8, 2, 12, 30, tzinfo=UTC)
OCCURRED_AT = "2026-08-02T12:00:00Z"


def _layout(tmp_path: Path):  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return open_release_skill_runtime_layout(
        state_root=tmp_path / "state",
        host_context_id="codex",
        native_session_id="native-session",
        workspace=workspace,
    )


def _sqlite_state(path: Path) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if isinstance(row[0], str)
        )
        return tuple(
            (name, tuple(connection.execute(f'SELECT * FROM "{name}"').fetchall()))
            for name in tables
        )
    finally:
        connection.close()


def _regular_file_bytes(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (str(path.relative_to(root)), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_query_availability_exposes_only_reviewed_load_after_exact_activation(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    absent = open_activated_skill_query_availability(
        layout=layout,
        task="repair nested context manager state restoration",
        language="Python",
        occurred_at=OCCURRED_AT,
        trusted_utc_now=lambda: NOW,
    )
    assert not absent.has_activated_release_skill

    request = layout.install_request(
        task="repair nested context manager state restoration",
        language="Python",
        occurred_at=OCCURRED_AT,
    )
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    assert (
        dispatch_release_skill_install(
            request,
            trusted_utc_now=lambda: NOW,
        ).status
        == "installed"
    )
    activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)

    available = open_activated_skill_query_availability(
        layout=layout,
        task="repair nested context manager state restoration",
        language="Python",
        occurred_at=OCCURRED_AT,
        trusted_utc_now=lambda: NOW,
    )
    assert available.has_activated_release_skill
    assert available.activation_epoch_digest != absent.activation_epoch_digest

    catalog = open_release_pinned_query_catalog()
    prepared = catalog.prepare_query(
        task="repair nested context manager state restoration",
        language="Python",
        host_policy=available,
    )
    candidates = prepared.closure.source.retrieve(prepared.closure.observation)

    assert tuple((item.capability_id, item.actionability) for item in candidates) == (
        (RELEASE_INSTALL_SKILL_ID, "load"),
    )
    assert prepared.install_authority is None
    assert prepared.material_authority is not None

    prepared.close()
    catalog.close()


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_query_availability_keeps_install_variant_closed_when_skill_is_absent(
    tmp_path: Path,
) -> None:
    availability = open_activated_skill_query_availability(
        layout=_layout(tmp_path),
        task="repair nested context manager state restoration",
        language="Python",
        occurred_at=OCCURRED_AT,
        trusted_utc_now=lambda: NOW,
    )
    catalog = open_release_pinned_query_catalog()
    prepared = catalog.prepare_query(
        task="repair nested context manager state restoration",
        language="Python",
        host_policy=availability,
    )

    assert prepared.closure.source.retrieve(prepared.closure.observation) == ()
    assert prepared.material_authority is None
    assert prepared.install_authority is None

    prepared.close()
    catalog.close()


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_query_availability_does_not_activate_inactive_skill_for_irrelevant_prompt(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    request = layout.install_request(
        task="repair nested context manager state restoration",
        language="Python",
        occurred_at=OCCURRED_AT,
    )
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    assert (
        dispatch_release_skill_install(request, trusted_utc_now=lambda: NOW).status == "installed"
    )
    journal_before = _sqlite_state(request.journal_path)
    cas_before = _regular_file_bytes(request.skill_store_root)

    availability = open_activated_skill_query_availability(
        layout=layout,
        task="write a JavaScript button label",
        language="JavaScript",
        occurred_at=OCCURRED_AT,
        trusted_utc_now=lambda: NOW,
    )

    assert not availability.has_activated_release_skill
    assert _sqlite_state(request.journal_path) == journal_before
    assert _regular_file_bytes(request.skill_store_root) == cas_before
