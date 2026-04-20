"""
tests/test_mcp_sources_pulsemcp.py -- Unit tests for mcp_sources.pulsemcp.

Contracts tested (Phase 2b locked spec):
  - _to_record(server_obj, meta) -> dict | None   (pure function, no network)
  - _credentials() reads env vars, raises _MissingPulsemcpCredentialsError if missing
  - _PulsemcpSource.fetch() paginates correctly via mocked HTTP
  - SOURCE singleton has expected name / homepage attributes
  - All HTTP is mocked — no real API calls
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

_FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Module now ships in Phase 2b; an import error here should fail the
# suite rather than silently skip — the parallel-write skipif guard was
# only for the brief window when the test file landed before the
# implementation. (python-reviewer Phase 2b finding.)
from mcp_sources.pulsemcp import (  # type: ignore[import-untyped]  # noqa: E402
    SOURCE,
    _MissingPulsemcpCredentialsError,
    _PulsemcpSource,
    _credentials,
    _to_record,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _page1_bytes() -> bytes:
    return (_FIXTURE_DIR / "pulsemcp_page1.json").read_bytes()


def _page2_bytes() -> bytes:
    return (_FIXTURE_DIR / "pulsemcp_page2.json").read_bytes()


def _make_server_obj(
    *,
    name: str = "io.github.example/test-server",
    description: str | None = "A test MCP server",
    repo_url: str | None = "https://github.com/example/test-server",
    repo_source: str = "github",
    packages: list[dict] | None = None,
) -> dict:
    """Build a minimal well-formed server object matching the PulseMCP API shape."""
    obj: dict[str, Any] = {
        "name": name,
        "repository": {},
        "packages": packages if packages is not None else [],
    }
    if description is not None:
        obj["description"] = description
    if repo_url is not None:
        obj["repository"] = {"url": repo_url, "source": repo_source}
    return obj


def _make_meta(
    *,
    is_official: bool = False,
    published_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    """Build a minimal _meta object matching the PulseMCP API shape."""
    return {
        "com.pulsemcp/server": {
            "isOfficial": is_official,
            "visitorsEstimateMostRecentWeek": 100,
        },
        "com.pulsemcp/server-version": {
            "publishedAt": published_at,
            "isLatest": True,
        },
    }


def _make_paginated_opener(pages: list[bytes]) -> Any:
    """Return a fake opener whose .open() returns each page in sequence."""
    state = {"i": 0}

    def _open(req: Any, timeout: float = 30.0) -> Any:
        body = pages[state["i"]]
        state["i"] += 1
        resp = MagicMock()
        resp.read.return_value = body
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    fake = MagicMock()
    fake.open.side_effect = _open
    return fake


def _make_status_opener(status: int, body: bytes = b"") -> Any:
    """Return a fake opener whose .open() returns a fixed status code."""
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    fake = MagicMock()
    fake.open.return_value = resp
    return fake


def _set_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set both required credential env vars to placeholder values."""
    monkeypatch.setenv("PULSEMCP_API_KEY", "test-key")
    monkeypatch.setenv("PULSEMCP_TENANT_ID", "test-tenant")


def _isolate_wiki(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point cfg.wiki_dir at a tmp dir so cache writes don't pollute the user's wiki.

    Without this, _fetch_page writes to the real ~/.claude/skill-wiki/raw/...
    and subsequent tests get cache hits that bypass the mocked HTTP layer.
    """
    from ctx_config import cfg  # noqa: PLC0415
    wiki = tmp_path / "skill-wiki"
    wiki.mkdir(exist_ok=True)
    monkeypatch.setattr(cfg, "wiki_dir", wiki)


# ---------------------------------------------------------------------------
# _to_record — pure function tests
# ---------------------------------------------------------------------------


class TestToRecord:
    def test_happy_path_full_object_returns_dict(self) -> None:
        server = _make_server_obj(
            packages=[{"registry_name": "npm", "name": "@example/test"}]
        )
        meta = _make_meta(is_official=True, published_at="2026-03-15T10:00:00Z")

        result = _to_record(server, meta)

        assert result is not None
        assert isinstance(result, dict)
        assert result.get("name") or result.get("slug")

    def test_missing_repository_url_yields_record_without_repo_links(self) -> None:
        # Implementation choice: a server entry without a repository URL
        # is still useful to catalog (it has name + description). Compare
        # awesome-mcp, which also accepts entries without github_url.
        # The record just lacks github_url and homepage_url.
        server = _make_server_obj(repo_url=None)
        server["repository"] = {"source": "github"}
        meta = _make_meta()

        result = _to_record(server, meta)

        assert result is not None
        assert result.get("github_url") is None
        assert result.get("homepage_url") is None
        assert result.get("name")

    def test_missing_description_returns_dict_not_none(self) -> None:
        server = _make_server_obj(description=None)
        meta = _make_meta()

        result = _to_record(server, meta)

        # A missing description should not cause the record to be dropped.
        # description key may be absent or None — both are acceptable.
        assert result is not None
        if "description" in result:
            assert result["description"] is None or isinstance(result["description"], str)

    def test_github_source_populates_github_url(self) -> None:
        server = _make_server_obj(
            repo_url="https://github.com/example/test-server",
            repo_source="github",
        )
        meta = _make_meta()

        result = _to_record(server, meta)

        assert result is not None
        assert "github.com" in (result.get("github_url") or "")
        # homepage_url should not duplicate the github URL
        homepage = result.get("homepage_url") or ""
        assert "github.com" not in homepage or not homepage

    def test_gitlab_source_populates_homepage_url_not_github_url(self) -> None:
        server = _make_server_obj(
            repo_url="https://gitlab.com/devco/gitlabmcp",
            repo_source="gitlab",
        )
        meta = _make_meta()

        result = _to_record(server, meta)

        assert result is not None
        assert result.get("github_url") is None
        assert "gitlab.com" in (result.get("homepage_url") or "")

    def test_is_official_true_adds_official_tag(self) -> None:
        server = _make_server_obj()
        meta = _make_meta(is_official=True)

        result = _to_record(server, meta)

        assert result is not None
        tags = result.get("tags") or []
        assert "official" in tags

    def test_npm_package_sets_language_typescript(self) -> None:
        server = _make_server_obj(
            packages=[{"registry_name": "npm", "name": "@example/server"}]
        )
        meta = _make_meta()

        result = _to_record(server, meta)

        assert result is not None
        assert result.get("language") == "typescript"

    def test_pypi_package_sets_language_python(self) -> None:
        server = _make_server_obj(
            packages=[{"registry_name": "pypi", "name": "example-server"}]
        )
        meta = _make_meta()

        result = _to_record(server, meta)

        assert result is not None
        assert result.get("language") == "python"

    def test_empty_packages_language_is_none(self) -> None:
        server = _make_server_obj(packages=[])
        meta = _make_meta()

        result = _to_record(server, meta)

        assert result is not None
        assert result.get("language") is None

    def test_published_at_sets_last_commit_at_iso8601(self) -> None:
        published = "2026-03-15T10:00:00Z"
        server = _make_server_obj()
        meta = _make_meta(published_at=published)

        result = _to_record(server, meta)

        assert result is not None
        last_commit = result.get("last_commit_at")
        assert last_commit is not None
        # Must be a string that starts with the date portion
        assert isinstance(last_commit, str)
        assert "2026-03-15" in last_commit

    def test_output_round_trips_through_mcp_record(self) -> None:
        from mcp_entity import McpRecord  # noqa: PLC0415

        server = _make_server_obj(
            packages=[{"registry_name": "npm", "name": "@example/server"}]
        )
        meta = _make_meta(is_official=True, published_at="2026-01-10T12:00:00Z")
        result = _to_record(server, meta)

        assert result is not None
        try:
            McpRecord.from_dict(result)
        except Exception as exc:
            pytest.fail(f"McpRecord.from_dict() raised for result {result!r}: {exc}")

    def test_fixture_page1_all_records_round_trip(self) -> None:
        """Every valid server in page1 fixture must round-trip through McpRecord."""
        from mcp_entity import McpRecord  # noqa: PLC0415

        data = _load_fixture("pulsemcp_page1.json")
        for entry in data["servers"]:
            result = _to_record(entry["server"], entry["_meta"])
            if result is None:
                continue
            try:
                McpRecord.from_dict(result)
            except Exception as exc:
                pytest.fail(
                    f"McpRecord.from_dict() raised for {entry['server']['name']!r}: {exc}"
                )


# ---------------------------------------------------------------------------
# _credentials — environment variable handling
# ---------------------------------------------------------------------------


class TestCredentials:
    def test_both_vars_set_returns_header_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PULSEMCP_API_KEY", "my-api-key")
        monkeypatch.setenv("PULSEMCP_TENANT_ID", "my-tenant")

        result = _credentials()

        assert isinstance(result, dict)
        # Must contain an auth-style header key; accept X-API-Key or Authorization
        found_key = any(
            "key" in k.lower() or "auth" in k.lower() or "tenant" in k.lower()
            for k in result
        )
        assert found_key, f"Expected auth header in credentials dict, got keys: {list(result)}"
        # Values must reflect the env var contents
        values = list(result.values())
        assert any("my-api-key" in v for v in values) or any(
            "my-tenant" in v for v in values
        ), f"Expected credential values in result, got: {result}"

    def test_only_api_key_set_raises_missing_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PULSEMCP_API_KEY", "present-key")
        monkeypatch.delenv("PULSEMCP_TENANT_ID", raising=False)

        with pytest.raises(_MissingPulsemcpCredentialsError, match="(?i)tenant"):
            _credentials()

    def test_neither_var_set_raises_with_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PULSEMCP_API_KEY", raising=False)
        monkeypatch.delenv("PULSEMCP_TENANT_ID", raising=False)

        with pytest.raises(
            _MissingPulsemcpCredentialsError,
            match=r"(?i)pulsemcp",
        ):
            _credentials()


class TestCredentialInjectionGuard:
    """Regression: refuse credentials that would inject HTTP headers.

    urllib.request.Request does NOT sanitize header values for CRLF
    on Python 3.11/3.12/3.13. A credential value containing
    ``\\r\\nX-Other: ...`` would otherwise be passed verbatim into
    every request.
    """

    def test_carriage_return_in_api_key_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PULSEMCP_API_KEY", "abc\rinjected")
        monkeypatch.setenv("PULSEMCP_TENANT_ID", "tenant")
        with pytest.raises(ValueError, match="(?i)injection"):
            _credentials()

    def test_newline_in_tenant_id_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PULSEMCP_API_KEY", "good-key")
        monkeypatch.setenv("PULSEMCP_TENANT_ID", "tenant\nX-Evil: x")
        with pytest.raises(ValueError, match="(?i)injection"):
            _credentials()

    def test_colon_in_api_key_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PULSEMCP_API_KEY", "good:bad")
        monkeypatch.setenv("PULSEMCP_TENANT_ID", "tenant")
        with pytest.raises(ValueError, match="(?i)injection"):
            _credentials()

    def test_error_message_does_not_echo_secret_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The credential value must NOT appear in the exception message —
        # it could contain the secret portion preceding the injection.
        monkeypatch.setenv("PULSEMCP_API_KEY", "supersecret\rinjected")
        monkeypatch.setenv("PULSEMCP_TENANT_ID", "tenant")
        try:
            _credentials()
        except ValueError as exc:
            assert "supersecret" not in str(exc)
            assert "injected" not in str(exc)


class TestBuildUrlCursorEncoding:
    """Regression: cursor must be URL-encoded to prevent query smuggling."""

    def test_ampersand_in_cursor_is_encoded(self) -> None:
        from mcp_sources.pulsemcp import _build_url
        url = _build_url("abc&injected=evil", page_size=100)
        # The literal & in the cursor must NOT survive as a query separator.
        assert "&injected=evil" not in url
        assert "%26injected%3Devil" in url

    def test_hash_in_cursor_is_encoded(self) -> None:
        from mcp_sources.pulsemcp import _build_url
        url = _build_url("abc#fragment", page_size=100)
        # The literal # would otherwise truncate the URL at the fragment.
        assert "#fragment" not in url
        assert "%23fragment" in url

    def test_safe_cursor_passes_through(self) -> None:
        from mcp_sources.pulsemcp import _build_url
        url = _build_url("abc123", page_size=100)
        assert "cursor=abc123" in url


# ---------------------------------------------------------------------------
# _PulsemcpSource.fetch — pagination + limit
# ---------------------------------------------------------------------------


class TestFetch:
    @pytest.fixture(autouse=True)
    def _isolated_wiki(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Auto-applied. Without it, _fetch_page writes to the user's
        # real ~/.claude/skill-wiki cache and subsequent tests get
        # cache hits that bypass the mocked HTTP layer entirely.
        _isolate_wiki(monkeypatch, tmp_path)

    def test_single_page_no_cursor_yields_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_credentials(monkeypatch)
        fake_opener = _make_paginated_opener([_page2_bytes()])
        monkeypatch.setattr("mcp_sources.base._build_opener", lambda: fake_opener)

        source = _PulsemcpSource()
        records = list(source.fetch())

        # page2 has 2 entries, both with valid repo URLs
        assert len(records) == 2

    def test_two_pages_with_cursor_yields_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_credentials(monkeypatch)
        fake_opener = _make_paginated_opener([_page1_bytes(), _page2_bytes()])
        monkeypatch.setattr("mcp_sources.base._build_opener", lambda: fake_opener)

        source = _PulsemcpSource()
        records = list(source.fetch())

        # page1: 4 entries (all have repo URLs), page2: 2 entries = 6 total
        assert len(records) == 6

    def test_limit_stops_before_second_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_credentials(monkeypatch)
        # page1 has 4 entries; with limit=3 we should stop after page1
        fake_opener = _make_paginated_opener([_page1_bytes()])
        monkeypatch.setattr("mcp_sources.base._build_opener", lambda: fake_opener)

        source = _PulsemcpSource()
        records = list(source.fetch(limit=3))

        assert len(records) == 3
        # Only one page fetch should have occurred
        assert fake_opener.open.call_count == 1

    def test_limit_spanning_two_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_credentials(monkeypatch)
        # page1 has 4 entries, page2 has 2. limit=5 → fetch both pages.
        fake_opener = _make_paginated_opener([_page1_bytes(), _page2_bytes()])
        monkeypatch.setattr("mcp_sources.base._build_opener", lambda: fake_opener)

        source = _PulsemcpSource()
        records = list(source.fetch(limit=5))

        assert len(records) == 5
        assert fake_opener.open.call_count == 2

    def test_rate_limit_429_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_credentials(monkeypatch)
        from urllib.error import HTTPError  # noqa: PLC0415

        def _open_429(req: Any, timeout: float = 30.0) -> Any:
            raise HTTPError(
                req.full_url if hasattr(req, "full_url") else str(req),
                429,
                "Too Many Requests",
                {},  # type: ignore[arg-type]
                None,
            )

        fake_opener = MagicMock()
        fake_opener.open.side_effect = _open_429
        monkeypatch.setattr("mcp_sources.base._build_opener", lambda: fake_opener)

        source = _PulsemcpSource()
        with pytest.raises((RuntimeError, Exception), match="(?i)(429|rate.?limit|retry)"):
            list(source.fetch())


# ---------------------------------------------------------------------------
# SOURCE singleton
# ---------------------------------------------------------------------------


class TestSourceSingleton:
    def test_source_name_is_pulsemcp(self) -> None:
        assert SOURCE.name == "pulsemcp"

    def test_source_homepage_is_non_empty_string(self) -> None:
        assert isinstance(SOURCE.homepage, str)
        assert SOURCE.homepage.strip() != ""

    def test_source_homepage_looks_like_url(self) -> None:
        assert SOURCE.homepage.startswith("http"), (
            f"SOURCE.homepage should start with 'http', got: {SOURCE.homepage!r}"
        )

    def test_source_satisfies_source_protocol(self) -> None:
        from mcp_sources.base import Source  # noqa: PLC0415

        assert isinstance(SOURCE, Source)
