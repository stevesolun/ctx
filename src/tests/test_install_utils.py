"""
test_install_utils.py -- Coverage sprint for install_utils.py.

install_utils is shared by skill_install, agent_install, and mcp_install.
A single bug here (e.g. dedup keying on slug alone instead of the
(slug, entity_type) tuple) silently corrupts all three install paths,
so tests here compound in value. Edge cases are exercised deliberately.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import install_utils


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def isolated_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point install_utils.MANIFEST_PATH at a tmp file so real ~/.claude is untouched."""
    manifest = tmp_path / "skill-manifest.json"
    monkeypatch.setattr(install_utils, "MANIFEST_PATH", manifest)
    return manifest


# ── load_manifest ────────────────────────────────────────────────────────────


class TestLoadManifest:
    def test_missing_file_returns_empty_shell(self, isolated_manifest: Path) -> None:
        assert not isolated_manifest.exists()
        m = install_utils.load_manifest()
        assert m == {"load": [], "unload": [], "warnings": []}

    def test_corrupt_json_returns_empty_shell(self, isolated_manifest: Path) -> None:
        isolated_manifest.write_text("{not json", encoding="utf-8")
        m = install_utils.load_manifest()
        assert m == {"load": [], "unload": [], "warnings": []}

    def test_valid_manifest_preserved(self, isolated_manifest: Path) -> None:
        payload = {
            "load": [{"skill": "x", "entity_type": "skill", "source": "test"}],
            "unload": [],
            "warnings": ["stale cache"],
        }
        isolated_manifest.write_text(json.dumps(payload), encoding="utf-8")
        m = install_utils.load_manifest()
        assert m["load"] == payload["load"]
        assert m["warnings"] == ["stale cache"]

    def test_missing_keys_backfilled(self, isolated_manifest: Path) -> None:
        """Old manifests may lack some top-level keys — setdefault them in."""
        isolated_manifest.write_text('{"load": [{"skill": "a"}]}', encoding="utf-8")
        m = install_utils.load_manifest()
        assert m["load"] == [{"skill": "a"}]
        assert m["unload"] == []
        assert m["warnings"] == []

    def test_empty_object_json_backfilled(self, isolated_manifest: Path) -> None:
        isolated_manifest.write_text("{}", encoding="utf-8")
        m = install_utils.load_manifest()
        assert m == {"load": [], "unload": [], "warnings": []}


# ── save_manifest ────────────────────────────────────────────────────────────


class TestSaveManifest:
    def test_round_trips(self, isolated_manifest: Path) -> None:
        payload = {
            "load": [{"skill": "s", "entity_type": "skill", "source": "t"}],
            "unload": [],
            "warnings": [],
        }
        install_utils.save_manifest(payload)
        assert json.loads(isolated_manifest.read_text(encoding="utf-8")) == payload

    def test_indentation_is_human_readable(self, isolated_manifest: Path) -> None:
        install_utils.save_manifest({"load": [], "unload": [], "warnings": []})
        text = isolated_manifest.read_text(encoding="utf-8")
        assert "\n" in text  # indent=2 produces multi-line output


# ── record_install ───────────────────────────────────────────────────────────


class TestRecordInstall:
    def test_fresh_install_appends_entry(self, isolated_manifest: Path) -> None:
        install_utils.record_install("foo", entity_type="skill", source="ctx-skill-install")
        m = install_utils.load_manifest()
        assert m["load"] == [
            {"skill": "foo", "entity_type": "skill", "source": "ctx-skill-install"}
        ]

    def test_duplicate_tuple_is_idempotent(self, isolated_manifest: Path) -> None:
        install_utils.record_install("foo", entity_type="skill", source="ctx-skill-install")
        install_utils.record_install("foo", entity_type="skill", source="ctx-skill-install")
        m = install_utils.load_manifest()
        assert len(m["load"]) == 1

    def test_skill_and_agent_same_slug_coexist(self, isolated_manifest: Path) -> None:
        """Dedup keys on (slug, entity_type); a skill and an agent may share a slug."""
        install_utils.record_install("debugger", entity_type="skill", source="s")
        install_utils.record_install("debugger", entity_type="agent", source="a")
        m = install_utils.load_manifest()
        assert len(m["load"]) == 2
        types = {e["entity_type"] for e in m["load"]}
        assert types == {"skill", "agent"}

    def test_extra_fields_merged(self, isolated_manifest: Path) -> None:
        install_utils.record_install(
            "github",
            entity_type="mcp-server",
            source="ctx-mcp-install",
            extra={"command": "npx -y @modelcontextprotocol/server-github"},
        )
        m = install_utils.load_manifest()
        assert m["load"][0]["command"] == "npx -y @modelcontextprotocol/server-github"
        assert m["load"][0]["entity_type"] == "mcp-server"

    def test_reinstall_scrubs_matching_unload(self, isolated_manifest: Path) -> None:
        install_utils.record_install("foo", entity_type="skill", source="s")
        install_utils.record_uninstall("foo", entity_type="skill", source="s")
        assert len(install_utils.load_manifest()["unload"]) == 1

        install_utils.record_install("foo", entity_type="skill", source="s")
        m = install_utils.load_manifest()
        assert m["unload"] == []  # scrubbed
        assert len(m["load"]) == 1  # reinstated

    def test_reinstall_preserves_unrelated_unload(self, isolated_manifest: Path) -> None:
        """Re-installing skill 'foo' must not scrub agent 'foo' from unload."""
        install_utils.record_uninstall("foo", entity_type="agent", source="a")
        install_utils.record_install("foo", entity_type="skill", source="s")
        m = install_utils.load_manifest()
        assert any(
            e["skill"] == "foo" and e["entity_type"] == "agent" for e in m["unload"]
        )

    def test_legacy_entry_without_entity_type_treated_as_skill(
        self, isolated_manifest: Path
    ) -> None:
        """Old manifests default entity_type to 'skill' for dedup purposes."""
        isolated_manifest.write_text(
            json.dumps({"load": [{"skill": "old"}], "unload": [], "warnings": []}),
            encoding="utf-8",
        )
        install_utils.record_install("old", entity_type="skill", source="s")
        m = install_utils.load_manifest()
        # The legacy entry is treated as (old, skill); re-install is a no-op.
        assert len(m["load"]) == 1


# ── record_uninstall ─────────────────────────────────────────────────────────


class TestRecordUninstall:
    def test_removes_matching_load_entry(self, isolated_manifest: Path) -> None:
        install_utils.record_install("foo", entity_type="skill", source="s")
        install_utils.record_uninstall("foo", entity_type="skill", source="s")
        m = install_utils.load_manifest()
        assert m["load"] == []

    def test_appends_unload_entry(self, isolated_manifest: Path) -> None:
        install_utils.record_uninstall("foo", entity_type="skill", source="s")
        m = install_utils.load_manifest()
        assert m["unload"] == [
            {"skill": "foo", "entity_type": "skill", "source": "s"}
        ]

    def test_duplicate_uninstall_dedups(self, isolated_manifest: Path) -> None:
        install_utils.record_uninstall("foo", entity_type="skill", source="s")
        install_utils.record_uninstall("foo", entity_type="skill", source="s")
        assert len(install_utils.load_manifest()["unload"]) == 1

    def test_uninstall_skill_preserves_agent_load(self, isolated_manifest: Path) -> None:
        install_utils.record_install("x", entity_type="skill", source="s")
        install_utils.record_install("x", entity_type="agent", source="a")
        install_utils.record_uninstall("x", entity_type="skill", source="s")
        m = install_utils.load_manifest()
        assert len(m["load"]) == 1
        assert m["load"][0]["entity_type"] == "agent"

    def test_uninstall_nonexistent_still_records_unload(
        self, isolated_manifest: Path
    ) -> None:
        """Uninstalling something never installed still records the unload intent."""
        install_utils.record_uninstall("ghost", entity_type="skill", source="s")
        m = install_utils.load_manifest()
        assert m["unload"][0]["skill"] == "ghost"


# ── _render_scalar ───────────────────────────────────────────────────────────


class TestRenderScalar:
    def test_none_is_yaml_null(self) -> None:
        assert install_utils._render_scalar(None) == "null"

    def test_bool_lowercase(self) -> None:
        assert install_utils._render_scalar(True) == "true"
        assert install_utils._render_scalar(False) == "false"

    def test_int(self) -> None:
        assert install_utils._render_scalar(42) == "42"
        assert install_utils._render_scalar(0) == "0"
        assert install_utils._render_scalar(-1) == "-1"

    def test_plain_string_unquoted(self) -> None:
        assert install_utils._render_scalar("hello") == "hello"
        assert install_utils._render_scalar("path/to/thing") == "path/to/thing"

    def test_colon_forces_quote(self) -> None:
        assert install_utils._render_scalar("a: b") == '"a: b"'

    def test_hash_forces_quote(self) -> None:
        assert install_utils._render_scalar("v#1") == '"v#1"'

    @pytest.mark.parametrize("ch", list(":#&*!|>%@`"))
    def test_each_special_char_forces_quote(self, ch: str) -> None:
        assert install_utils._render_scalar(f"x{ch}y").startswith('"')

    @pytest.mark.parametrize("prefix", ["-", "?", "[", "{"])
    def test_yaml_leading_char_forces_quote(self, prefix: str) -> None:
        assert install_utils._render_scalar(f"{prefix}rest").startswith('"')

    def test_newlines_flattened(self) -> None:
        out = install_utils._render_scalar("line1\nline2")
        assert "\n" not in out
        assert "line1 line2" in out

    def test_crlf_flattened(self) -> None:
        out = install_utils._render_scalar("a\r\nb")
        assert "\r" not in out and "\n" not in out

    def test_embedded_quote_escaped(self) -> None:
        # "!" triggers the quote branch, then `"` inside is escaped.
        out = install_utils._render_scalar('say "hi"!')
        assert out.startswith('"') and out.endswith('"')
        assert '\\"' in out

    def test_fallback_for_unknown_type(self) -> None:
        out = install_utils._render_scalar(3.14)
        assert out == '"3.14"'

    def test_empty_string(self) -> None:
        assert install_utils._render_scalar("") == ""


# ── _replace_or_insert_field ─────────────────────────────────────────────────


class TestReplaceOrInsertField:
    def test_replaces_existing_field(self) -> None:
        text = "---\nstatus: stub\nname: x\n---\nbody"
        out = install_utils._replace_or_insert_field(text, "status", "ready")
        assert "status: ready" in out
        assert "status: stub" not in out
        assert "name: x" in out

    def test_inserts_before_closing_delimiter(self) -> None:
        text = "---\nname: x\n---\nbody"
        out = install_utils._replace_or_insert_field(text, "status", "ready")
        assert "status: ready" in out
        # Must land inside the frontmatter block, not after.
        fm_end = out.index("\n---\nbody")
        assert out.index("status: ready") < fm_end

    def test_no_frontmatter_returns_text_unchanged(self) -> None:
        text = "no frontmatter here\n"
        out = install_utils._replace_or_insert_field(text, "status", "ready")
        assert out == text

    def test_only_first_match_replaced(self) -> None:
        """Guards against repeated fields in malformed frontmatter."""
        text = "---\nstatus: a\nstatus: b\n---\n"
        out = install_utils._replace_or_insert_field(text, "status", "c")
        # First occurrence swapped, second left alone.
        assert out.count("status: c") == 1
        assert "status: b" in out


# ── bump_entity_status ───────────────────────────────────────────────────────


class TestBumpEntityStatus:
    def _write(self, path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")

    def test_nonexistent_file_returns_false(self, tmp_path: Path) -> None:
        assert install_utils.bump_entity_status(
            tmp_path / "nope.md", status="ready"
        ) is False

    def test_updates_existing_status_field(self, tmp_path: Path) -> None:
        f = tmp_path / "e.md"
        self._write(f, "---\nstatus: stub\n---\nbody\n")
        changed = install_utils.bump_entity_status(f, status="ready")
        assert changed is True
        assert "status: ready" in f.read_text(encoding="utf-8")

    def test_inserts_missing_status_field(self, tmp_path: Path) -> None:
        f = tmp_path / "e.md"
        self._write(f, "---\nname: e\n---\nbody\n")
        install_utils.bump_entity_status(f, status="ready")
        assert "status: ready" in f.read_text(encoding="utf-8")

    def test_no_frontmatter_returns_false(self, tmp_path: Path) -> None:
        f = tmp_path / "e.md"
        self._write(f, "just markdown, no frontmatter\n")
        assert install_utils.bump_entity_status(f, status="ready") is False

    def test_extra_fields_rendered(self, tmp_path: Path) -> None:
        f = tmp_path / "e.md"
        self._write(f, "---\nstatus: stub\n---\nbody\n")
        install_utils.bump_entity_status(
            f,
            status="ready",
            extra_fields={"install_cmd": "npx -y foo", "loaded_at": 1700000000},
        )
        text = f.read_text(encoding="utf-8")
        assert "install_cmd: npx -y foo" in text
        assert "loaded_at: 1700000000" in text

    def test_extra_none_renders_yaml_null(self, tmp_path: Path) -> None:
        f = tmp_path / "e.md"
        self._write(f, "---\nstatus: stub\ninstall_cmd: x\n---\nbody\n")
        install_utils.bump_entity_status(
            f, status="ready", extra_fields={"install_cmd": None}
        )
        text = f.read_text(encoding="utf-8")
        assert "install_cmd: null" in text

    def test_idempotent_when_content_identical(self, tmp_path: Path) -> None:
        f = tmp_path / "e.md"
        self._write(f, "---\nstatus: ready\n---\nbody\n")
        assert install_utils.bump_entity_status(f, status="ready") is False

    def test_value_with_special_chars_quoted(self, tmp_path: Path) -> None:
        f = tmp_path / "e.md"
        self._write(f, "---\nstatus: stub\n---\nbody\n")
        install_utils.bump_entity_status(
            f, status="ready", extra_fields={"note": "key: value"}
        )
        text = f.read_text(encoding="utf-8")
        assert 'note: "key: value"' in text


# ── emit_load_event ──────────────────────────────────────────────────────────


class TestEmitLoadEvent:
    def test_success_path_invokes_telemetry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str, str]] = []

        import skill_telemetry

        def fake_log_event(event: str, slug: str, session_id: str) -> None:
            calls.append((event, slug, session_id))

        monkeypatch.setattr(skill_telemetry, "log_event", fake_log_event)
        install_utils.emit_load_event("foo", "session-abc")
        assert calls == [("load", "foo", "session-abc")]

    def test_telemetry_exception_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import skill_telemetry

        def boom(*_a: object, **_kw: object) -> None:
            raise RuntimeError("telemetry sink down")

        monkeypatch.setattr(skill_telemetry, "log_event", boom)
        # Must not raise.
        install_utils.emit_load_event("foo", "session-abc")

    def test_import_failure_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even if skill_telemetry is unimportable, the install path must not crash."""
        import sys as _sys

        # Force re-import to hit the exception branch.
        monkeypatch.setitem(_sys.modules, "skill_telemetry", None)
        install_utils.emit_load_event("foo", "session-abc")
