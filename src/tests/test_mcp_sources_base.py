"""
tests/test_mcp_sources_base.py -- Unit tests for mcp_sources.base helpers.

Contracts tested (Phase 2a locked spec):
  - cache_path(source_name, basename) -> Path under cfg.wiki_dir
  - write_cache / read_cache round-trip
  - read_cache returns None when file missing
  - write_cache is atomic (.tmp not left behind)
  - fetch_text rejects disallowed hosts
  - fetch_text rejects non-https scheme
  - fetch_text accepts allowed host (urlopen mocked)
  - fetch_text honors timeout param
  - Source protocol: runtime_checkable isinstance check
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcp_sources.base import (  # noqa: E402
    ALLOWED_HOSTS,
    Source,
    cache_path,
    fetch_text,
    read_cache,
    write_cache,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_wiki(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point cfg.wiki_dir at a fresh tmp directory."""
    from ctx_config import cfg  # noqa: PLC0415

    wiki = tmp_path / "skill-wiki"
    wiki.mkdir()
    monkeypatch.setattr(cfg, "wiki_dir", wiki)
    return wiki


def _make_fake_opener(
    body: bytes,
    *,
    status: int = 200,
    timeout_capture: list[float] | None = None,
) -> Any:
    """Build a mock OpenerDirector matching what mcp_sources.base._build_opener returns.

    The implementation calls ``opener.open(request, timeout=timeout)`` then
    uses ``with`` on the response and reads its body. Returns an object
    that satisfies that protocol. Optionally captures the timeout arg
    each call for assertions.
    """
    fake_response = MagicMock()
    fake_response.read.return_value = body
    fake_response.status = status
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    fake_opener = MagicMock()

    def _open(request: Any, timeout: float = 30.0) -> Any:
        if timeout_capture is not None:
            timeout_capture.append(timeout)
        return fake_response

    fake_opener.open.side_effect = _open
    return fake_opener


def _allowed_url() -> str:
    """Return a URL whose host is in ALLOWED_HOSTS."""
    return "https://raw.githubusercontent.com/example/repo/main/README.md"


# ---------------------------------------------------------------------------
# cache_path
# ---------------------------------------------------------------------------


class TestCachePath:
    def test_returns_path_under_wiki_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        wiki = _patch_wiki(monkeypatch, tmp_path)
        result = cache_path("awesome-mcp", "README.md")
        assert isinstance(result, Path)
        assert str(wiki) in str(result)

    def test_includes_source_name_in_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_wiki(monkeypatch, tmp_path)
        result = cache_path("awesome-mcp", "README.md")
        assert "awesome-mcp" in str(result)

    def test_includes_basename_in_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_wiki(monkeypatch, tmp_path)
        result = cache_path("awesome-mcp", "README.md")
        assert result.name == "README.md"


# ---------------------------------------------------------------------------
# write_cache / read_cache
# ---------------------------------------------------------------------------


class TestCacheRoundTrip:
    def test_write_then_read_returns_same_text(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_wiki(monkeypatch, tmp_path)
        content = "hello cache\nline two\n"
        write_cache("awesome-mcp", "test.md", content)
        result = read_cache("awesome-mcp", "test.md")
        assert result == content

    def test_read_cache_returns_none_when_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_wiki(monkeypatch, tmp_path)
        result = read_cache("awesome-mcp", "nonexistent.md")
        assert result is None

    def test_write_cache_returns_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_wiki(monkeypatch, tmp_path)
        result = write_cache("awesome-mcp", "out.md", "content")
        assert isinstance(result, Path)
        assert result.exists()

    def test_write_cache_is_atomic_no_tmp_leftover(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        wiki = _patch_wiki(monkeypatch, tmp_path)
        write_cache("awesome-mcp", "atomic.md", "data")
        # No .tmp file should remain anywhere under wiki after successful write
        tmp_files = list(wiki.rglob("*.tmp"))
        assert tmp_files == [], f"Leftover .tmp files: {tmp_files}"


# ---------------------------------------------------------------------------
# fetch_text
# ---------------------------------------------------------------------------


class TestFetchTextSecurity:
    def test_rejects_disallowed_host(self) -> None:
        # Use https so the scheme check passes and the host check is
        # what raises (the implementation is now https-only).
        with pytest.raises(Exception, match="(?i)(not allowed|disallowed|forbidden|host)"):
            fetch_text("https://evil.com/data.json")

    def test_rejects_non_https_scheme(self) -> None:
        # Use a known-allowed host with http instead of https
        with pytest.raises(Exception, match="(?i)(https|scheme|not allowed|forbidden)"):
            fetch_text("http://raw.githubusercontent.com/example/repo/main/README.md")

    def test_rejects_ftp_scheme(self) -> None:
        with pytest.raises(Exception, match="(?i)(https|scheme|not allowed|forbidden)"):
            fetch_text("ftp://raw.githubusercontent.com/example/repo/main/README.md")

    def test_caps_response_body_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: an allowlisted host that streams more than
        # MAX_RESPONSE_BYTES must raise rather than silently accept.
        from mcp_sources.base import MAX_RESPONSE_BYTES  # noqa: PLC0415

        oversized = b"x" * (MAX_RESPONSE_BYTES + 100)
        fake_opener = _make_fake_opener(oversized, status=200)
        monkeypatch.setattr(
            "mcp_sources.base._build_opener", lambda: fake_opener,
        )
        with pytest.raises(Exception, match="(?i)(too large|exceeded)"):
            fetch_text(_allowed_url())


class TestCachePathTraversalGuard:
    """Path traversal regressions in cache_path() input validation."""

    def test_source_name_with_slash_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_wiki(monkeypatch, tmp_path)
        with pytest.raises(ValueError, match="(?i)(path|separator|refusing)"):
            cache_path("../etc", "passwd")

    def test_basename_with_dotdot_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_wiki(monkeypatch, tmp_path)
        with pytest.raises(ValueError, match="(?i)(path|separator|refusing|dot)"):
            cache_path("awesome-mcp", "..")

    def test_basename_with_backslash_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_wiki(monkeypatch, tmp_path)
        with pytest.raises(ValueError):
            cache_path("awesome-mcp", "..\\..\\windows")

    def test_empty_source_name_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_wiki(monkeypatch, tmp_path)
        with pytest.raises(ValueError, match="(?i)(non-empty|empty)"):
            cache_path("", "README.md")


class TestFetchTextHappyPath:
    def test_returns_text_from_allowed_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_body = b"# Hello from mock\n"

        # The implementation builds its own opener (build_opener with a
        # NoRedirectHandler), so we monkeypatch _build_opener rather
        # than urllib.request.urlopen — patching urlopen would miss the
        # actual call site and let real HTTP through.
        fake_opener = _make_fake_opener(fake_body, status=200)
        monkeypatch.setattr(
            "mcp_sources.base._build_opener", lambda: fake_opener,
        )

        result = fetch_text(_allowed_url())
        assert result == fake_body.decode()

    def test_passes_timeout_to_urlopen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        received: list[float] = []
        fake_body = b"data"

        fake_opener = _make_fake_opener(fake_body, status=200, timeout_capture=received)
        monkeypatch.setattr(
            "mcp_sources.base._build_opener", lambda: fake_opener,
        )

        fetch_text(_allowed_url(), timeout=5.0)
        assert received == [5.0]


# ---------------------------------------------------------------------------
# ALLOWED_HOSTS
# ---------------------------------------------------------------------------


class TestAllowedHosts:
    def test_allowed_hosts_is_frozenset(self) -> None:
        assert isinstance(ALLOWED_HOSTS, frozenset)

    def test_raw_githubusercontent_in_allowed_hosts(self) -> None:
        assert "raw.githubusercontent.com" in ALLOWED_HOSTS

    def test_github_com_in_allowed_hosts(self) -> None:
        assert "github.com" in ALLOWED_HOSTS


# ---------------------------------------------------------------------------
# Source protocol
# ---------------------------------------------------------------------------


class TestSourceProtocol:
    def test_conforming_class_satisfies_source_protocol(self) -> None:
        class _FakeSource:
            name: str = "fake"
            homepage: str = "https://example.com"

            def fetch(
                self, *, limit: int | None = None, refresh: bool = False
            ) -> Iterator[dict]:  # type: ignore[override]
                return iter([])

        instance = _FakeSource()
        # Source must be decorated with @runtime_checkable for this to work
        assert isinstance(instance, Source)

    def test_non_conforming_class_fails_source_protocol(self) -> None:
        class _BadSource:
            pass

        instance = _BadSource()
        assert not isinstance(instance, Source)
