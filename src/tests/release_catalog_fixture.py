"""Explicit, test-only resources for the retired release-pinned catalog.

These project-authored bytes retain the historical integrity/consent tests;
they are not a production catalog or a fallback for a user's empty graph.
Only tests requesting this fixture (or spawned workers entering its context)
see them. The production resource reader still validates names, size and pins.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import io
from pathlib import Path
import tarfile

import pytest

from ctx.runtime import production_catalog


RELEASE_CATALOG_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "release_catalog"


def _fixture_resources(package: str) -> Path:
    if package != "ctx.assets":
        raise AssertionError(f"unexpected historical fixture package: {package}")
    return RELEASE_CATALOG_FIXTURE_ROOT


@contextmanager
def historical_catalog_resources() -> Iterator[None]:
    """Inject test-owned resource location, without replacing any validation."""

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(production_catalog, "files", _fixture_resources)
        yield


@pytest.fixture
def historical_release_catalog() -> Iterator[None]:
    """Opt in per legacy test/module; never autouse across production tests."""

    with historical_catalog_resources():
        yield


@pytest.fixture
def historical_benchmark_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    """Small explicit inputs for historical freeze/hash tests, never live data."""

    archive = tmp_path / "synthetic-catalog.tar.gz"
    body = b"# Synthetic test knowledge\nNo production tool content.\n"
    entry = tarfile.TarInfo("entities/skills/synthetic-fixture.md")
    entry.size = len(body)
    with tarfile.open(archive, "w:gz") as stream:
        stream.addfile(entry, io.BytesIO(body))
    return archive, RELEASE_CATALOG_FIXTURE_ROOT / "runtime-availability.json"
