from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, cast


def _load_builder() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2] / "imported-skills" / "mattpocock" / "build_manifest.py"
    )
    spec = importlib.util.spec_from_file_location("mattpocock_build_manifest", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upstream_revision_prefers_revision_file(tmp_path: Path) -> None:
    builder = cast(Any, _load_builder())
    builder.ROOT = tmp_path
    builder.MANIFEST_PATH = tmp_path / "MANIFEST.json"
    builder.UPSTREAM_REVISION_PATH = tmp_path / "UPSTREAM_REVISION"
    builder.UPSTREAM_REVISION_PATH.write_text("abc123\n", encoding="utf-8")

    assert builder.upstream_revision() == "abc123"


def test_upstream_revision_falls_back_to_existing_manifest(tmp_path: Path) -> None:
    builder = cast(Any, _load_builder())
    builder.ROOT = tmp_path
    builder.MANIFEST_PATH = tmp_path / "MANIFEST.json"
    builder.UPSTREAM_REVISION_PATH = tmp_path / "UPSTREAM_REVISION"
    builder.MANIFEST_PATH.write_text(
        '{"upstream_revision": "manifest-revision"}',
        encoding="utf-8",
    )

    assert builder.upstream_revision() == "manifest-revision"
