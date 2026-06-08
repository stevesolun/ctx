from __future__ import annotations

from pathlib import Path


def test_public_catalog_page_does_not_link_to_local_dashboard() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / "docs" / "catalog.md").read_text(encoding="utf-8")

    assert "http://127.0.0.1" not in text
    assert "http://localhost" not in text
    assert "ctxLocalWikiUrl" not in text
    assert "ctxPublicCatalogUrl" in text
    assert "../dashboard/#catalog-badge-links" in text
