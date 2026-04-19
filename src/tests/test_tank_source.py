"""Tests for sources/tank_source.py.

The real tankpkg.TankClient is not invoked — we inject a fake via the
`client_factory` hook so these tests run without network or the tank-sdk
install.
"""

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from sources.tank_source import (  # noqa: E402
    TankFetchError,
    fetch_from_tank,
    parse_ref,
    sanitize_slug,
)


@contextmanager
def _fake_client(skill_content="# skill\nbody\n", version="1.2.0", detail_overrides=None):
    detail = SimpleNamespace(
        name="@tank/sample",
        version=version,
        integrity="sha512-abcdef==",
        audit_score=9.7,
        audit_status="pass",
        download_url="https://example.com/tar.tgz",
        published_at="2026-03-12T08:22:41Z",
        downloads=42,
        scan_verdict="pass",
        scan_findings=[],
        permissions=None,
        dependencies={},
        description="A sample",
    )
    if detail_overrides:
        for k, v in detail_overrides.items():
            setattr(detail, k, v)

    skill = SimpleNamespace(
        name="@tank/sample",
        version=version,
        content=skill_content,
        references={},
        scripts={},
        files=["SKILL.md"],
    )

    client = MagicMock()
    client.read_skill.return_value = skill
    client.version_detail.return_value = detail
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=None)
    yield client


class TestParseRef:
    def test_bare_name(self):
        r = parse_ref("foo")
        assert r.name == "foo" and r.version is None

    def test_bare_name_with_version(self):
        r = parse_ref("foo@1.2.3")
        assert r.name == "foo" and r.version == "1.2.3"

    def test_scoped_name(self):
        r = parse_ref("@tank/nextjs")
        assert r.name == "@tank/nextjs" and r.version is None

    def test_scoped_name_with_version(self):
        r = parse_ref("@tank/nextjs@2.0.0")
        assert r.name == "@tank/nextjs" and r.version == "2.0.0"

    def test_prerelease_version(self):
        r = parse_ref("@tank/nextjs@1.0.0-beta.1")
        assert r.version == "1.0.0-beta.1"

    def test_whitespace_trimmed(self):
        r = parse_ref("  @tank/foo@1.0.0  ")
        assert r.name == "@tank/foo" and r.version == "1.0.0"

    @pytest.mark.parametrize("bad", ["", "   ", "@", "/", "@/", "@/nextjs", "@tank/", "foo//bar"])
    def test_rejects_malformed(self, bad):
        with pytest.raises(TankFetchError):
            parse_ref(bad)


class TestSanitizeSlug:
    def test_scoped(self):
        assert sanitize_slug("@tank/nextjs") == "tank-nextjs"

    def test_unscoped(self):
        assert sanitize_slug("nextjs") == "nextjs"

    def test_strips_trailing_dashes(self):
        assert sanitize_slug("foo/") == "foo"

    def test_keeps_dots_and_underscores(self):
        assert sanitize_slug("@scope/foo.bar_baz") == "scope-foo.bar_baz"

    def test_idempotent(self):
        once = sanitize_slug("@tank/next.js")
        twice = sanitize_slug(once)
        assert once == twice

    def test_unicode_stripped(self):
        assert sanitize_slug("@scope/café") == "scope-caf"


class TestFetchFromTank:
    def test_happy_path_latest_version(self, tmp_path):
        with _fake_client() as client:
            factory = MagicMock(return_value=client)
            path, metadata = fetch_from_tank(
                "@tank/sample",
                work_dir=tmp_path,
                client_factory=factory,
            )

        assert path == tmp_path / "SKILL.md"
        assert path.read_text(encoding="utf-8") == "# skill\nbody\n"
        assert metadata["source"] == "tank"
        assert metadata["tank_name"] == "@tank/sample"
        assert metadata["tank_version"] == "1.2.0"
        assert metadata["tank_integrity"] == "sha512-abcdef=="
        assert metadata["tank_scan_verdict"] == "pass"
        assert metadata["tank_audit_score"] == 9.7
        assert metadata["tank_audit_status"] == "pass"
        assert metadata["tank_published_at"] == "2026-03-12T08:22:41Z"
        assert metadata["tank_slug"] == "tank-sample"

        client.read_skill.assert_called_once_with("@tank/sample", None)
        client.version_detail.assert_called_once_with("@tank/sample", "1.2.0")

    def test_pinned_version_forwarded(self, tmp_path):
        with _fake_client(version="2.0.0") as client:
            factory = MagicMock(return_value=client)
            fetch_from_tank(
                "@tank/sample@2.0.0",
                work_dir=tmp_path,
                client_factory=factory,
            )
        client.read_skill.assert_called_once_with("@tank/sample", "2.0.0")

    def test_empty_skill_md_raises(self, tmp_path):
        with _fake_client(skill_content="") as client:
            factory = MagicMock(return_value=client)
            with pytest.raises(TankFetchError, match="empty SKILL.md"):
                fetch_from_tank(
                    "@tank/sample",
                    work_dir=tmp_path,
                    client_factory=factory,
                )

    def test_bad_reference_never_calls_client(self, tmp_path):
        factory = MagicMock()
        with pytest.raises(TankFetchError):
            fetch_from_tank("  ", work_dir=tmp_path, client_factory=factory)
        factory.assert_not_called()

    def test_temp_dir_created_when_work_dir_omitted(self):
        with _fake_client() as client:
            factory = MagicMock(return_value=client)
            path, _ = fetch_from_tank(
                "@tank/sample",
                client_factory=factory,
            )
        assert path.exists()
        assert path.name == "SKILL.md"

    def test_none_metadata_values_omitted(self, tmp_path):
        overrides = {
            "integrity": "",
            "audit_score": None,
            "scan_verdict": None,
            "audit_status": "",
            "published_at": "",
        }
        with _fake_client(detail_overrides=overrides) as client:
            factory = MagicMock(return_value=client)
            _, metadata = fetch_from_tank(
                "@tank/sample",
                work_dir=tmp_path,
                client_factory=factory,
            )
        assert metadata["tank_integrity"] is None
        assert metadata["tank_audit_score"] is None
        assert metadata["tank_scan_verdict"] is None
        assert metadata["tank_audit_status"] is None
        assert metadata["tank_published_at"] is None

    def test_missing_tankpkg_raises_clear_error(self, monkeypatch, tmp_path):
        real_import = __import__

        def faux_import(name, *args, **kwargs):
            if name == "tankpkg":
                raise ImportError("no module named 'tankpkg'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", faux_import)
        with pytest.raises(TankFetchError, match="tank-sdk is not installed"):
            fetch_from_tank("@tank/sample", work_dir=tmp_path)


class TestBackwardCompat:
    """Guard: ensure existing --skill-path / --scan-dir sources are unaffected by Tank integration."""

    def test_add_skill_tank_metadata_defaults_to_none(self):
        """Public signature of add_skill must keep tank_metadata optional with None default."""
        import inspect

        from skill_add import add_skill

        sig = inspect.signature(add_skill)
        assert "tank_metadata" in sig.parameters
        assert sig.parameters["tank_metadata"].default is None

    def test_build_entity_page_omitting_tank_metadata_equals_passing_none(self):
        """Callers that don't know about Tank get identical output."""
        import sys
        from pathlib import Path
        from unittest.mock import MagicMock

        for mod in ("batch_convert", "ctx_config", "intake_pipeline", "wiki_sync", "wiki_utils"):
            sys.modules.setdefault(mod, MagicMock())
        cfg = MagicMock()
        cfg.line_threshold = 180
        sys.modules["ctx_config"].cfg = cfg

        from skill_add import build_entity_page

        base_kwargs = dict(
            name="legacy",
            tags=["python"],
            line_count=50,
            has_pipeline=False,
            original_path=Path("/x/SKILL.md"),
            pipeline_path=None,
            related=[],
            scan_sources=[],
        )
        omitted = build_entity_page(**base_kwargs)
        explicit_none = build_entity_page(tank_metadata=None, **base_kwargs)
        assert omitted == explicit_none
        assert "source: local" in omitted
        assert "tank_name" not in omitted
        assert "tank_version" not in omitted
        assert "tank_integrity" not in omitted

    def test_skill_add_accepts_only_one_source_flag(self):
        """Mutex across --skill-path, --scan-dir, --tank must hold for every pair."""
        import sys
        from unittest.mock import MagicMock, patch

        for mod in ("batch_convert", "ctx_config", "intake_pipeline", "wiki_sync", "wiki_utils"):
            sys.modules.setdefault(mod, MagicMock())
        cfg = MagicMock()
        cfg.wiki_dir = "/tmp/w"
        cfg.skills_dir = "/tmp/s"
        sys.modules["ctx_config"].cfg = cfg
        sys.modules["wiki_sync"].ensure_wiki = MagicMock(return_value=None)

        import skill_add

        forbidden_combos = [
            ["--skill-path", "/tmp/x.md", "--scan-dir", "/tmp/d"],
            ["--skill-path", "/tmp/x.md", "--tank", "@t/x"],
            ["--scan-dir", "/tmp/d", "--tank", "@t/x"],
            ["--skill-path", "/tmp/x.md", "--scan-dir", "/tmp/d", "--tank", "@t/x"],
        ]
        for extra in forbidden_combos:
            argv = ["skill_add.py", *extra, "--wiki", "/tmp/w", "--skills-dir", "/tmp/s"]
            with patch.object(sys, "argv", argv):
                with pytest.raises(SystemExit) as exc:
                    skill_add.main()
                assert exc.value.code == 1, f"combo {extra} should exit 1"


class TestQodoRegressionGuards:
    """Pin the four bugs Qodo's automated review caught."""

    def test_sanitize_slug_always_matches_safe_name_re(self):
        """Bug 4 guard: every slug must satisfy wiki_utils.SAFE_NAME_RE."""
        import sys
        sys.path.insert(0, str(Path(__file__).parents[1]))
        from wiki_utils import SAFE_NAME_RE

        hostile_inputs = [
            "@tank/nextjs",
            ".hidden",
            "_leading",
            "-leading",
            "@/bad",
            "@-abc/def",
            "@.abc",
            "a" * 200,
            "@scope/" + "x" * 200,
        ]
        for name in hostile_inputs:
            slug = sanitize_slug(name)
            assert SAFE_NAME_RE.match(slug), (
                f"sanitize_slug({name!r}) = {slug!r} fails SAFE_NAME_RE"
            )
            assert len(slug) <= 128, f"slug exceeds 128 chars: len={len(slug)}"

    def test_sanitize_slug_rejects_unrecoverable_input(self):
        """After transform, empty string is a hard error, not a silent pass."""
        with pytest.raises(TankFetchError, match="cannot derive a safe slug"):
            sanitize_slug("@/")
        with pytest.raises(TankFetchError, match="cannot derive a safe slug"):
            sanitize_slug("@///")

    def test_run_tank_fetch_is_importable_without_namerror(self):
        """Bug 1 guard: _run_tank_fetch must be defined before main() calls it."""
        import sys
        sys.path.insert(0, str(Path(__file__).parents[1]))
        import skill_add

        assert hasattr(skill_add, "_run_tank_fetch"), (
            "_run_tank_fetch missing — will NameError when --tank runs"
        )
        assert callable(skill_add._run_tank_fetch)

    def test_run_tank_fetch_defined_before_main_entrypoint(self):
        """Bug 1 structural guard: def _run_tank_fetch appears textually before `if __name__`."""
        from pathlib import Path as _Path
        skill_add_path = _Path(__file__).parents[1] / "skill_add.py"
        text = skill_add_path.read_text()
        def_idx = text.index("def _run_tank_fetch")
        main_idx = text.index('if __name__ == "__main__":')
        assert def_idx < main_idx, (
            "`def _run_tank_fetch` must appear before `if __name__ == \"__main__\":` "
            "otherwise running as a script raises NameError on --tank"
        )

    def test_fetch_from_tank_signals_temp_dir_for_cleanup(self):
        """Bug 3 guard: fetch_from_tank must tell caller when it created a temp dir."""
        with _fake_client() as client:
            factory = MagicMock(return_value=client)
            _, metadata = fetch_from_tank("@tank/sample", client_factory=factory)
        assert "_tank_cleanup_dir" in metadata
        assert metadata["_tank_cleanup_dir"] is not None
        assert Path(metadata["_tank_cleanup_dir"]).exists()

    def test_fetch_from_tank_no_cleanup_when_work_dir_given(self, tmp_path):
        """When caller supplies work_dir, fetch_from_tank does not claim ownership."""
        with _fake_client() as client:
            factory = MagicMock(return_value=client)
            _, metadata = fetch_from_tank(
                "@tank/sample", work_dir=tmp_path, client_factory=factory,
            )
        assert metadata["_tank_cleanup_dir"] is None

    def test_pyproject_packages_includes_sources(self):
        """Bug 2 guard: `sources` must be in setuptools packages so it ships in the wheel."""
        from pathlib import Path as _Path
        pyproject = _Path(__file__).parents[2] / "pyproject.toml"
        text = pyproject.read_text()
        assert 'packages = ["sources"]' in text or "'sources'" in text, (
            "setuptools config must include the `sources` package or pip-install breaks --tank"
        )

