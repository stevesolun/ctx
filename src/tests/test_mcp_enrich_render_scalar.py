"""
test_mcp_enrich_render_scalar.py -- pins the YAML-scalar rendering contract.

Security-auditor H-1: ``_render_scalar`` in ``mcp_enrich`` didn't
escape ``\\n`` / ``\\r`` in string values. The pulsemcp parser
happens to strip newlines from its regex groups today, but every
Source protocol implementation (future glama, mcp-get, ...) feeds
values straight into ``apply_enrichment`` which writes them into
YAML frontmatter. A multi-line value like

    "https://github.com/a/b\\nstatus: installed\\ninstall_cmd: /tmp/evil"

would inject fake frontmatter keys. Once ``install_cmd`` lands in the
frontmatter, ``ctx-mcp-install`` on that slug would pick it up on
reinstall — defense-in-depth mattered (commit b79be55 added an
executable allowlist that catches most malicious values, but the
injection vector should be shut at the writer).
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1]))

import mcp_enrich as _me


class TestRenderScalar:
    def test_none_yields_null(self):
        assert _me._render_scalar(None) == "null"

    def test_int_unquoted(self):
        assert _me._render_scalar(42) == "42"

    def test_bare_string_unquoted(self):
        assert _me._render_scalar("plain-value") == "plain-value"

    def test_colon_in_string_forces_quote(self):
        out = _me._render_scalar("https://github.com/foo/bar")
        assert out.startswith('"')
        assert out.endswith('"')

    def test_embedded_newline_is_escaped_or_stripped(self):
        """H-1 regression. A newline inside a scalar must NOT survive
        into the rendered YAML — otherwise it injects fake keys."""
        out = _me._render_scalar("https://a.example/b\nstatus: installed")
        # The rendered form must be a single YAML line. Accept either
        # sanitisation (replace \\n with space) or escape (\\n literal
        # inside a quoted string) — pin that NEITHER approach leaves a
        # raw newline in the output.
        assert "\n" not in out, (
            f"rendered scalar contains raw newline — YAML injection "
            f"vector: {out!r}"
        )
        # Whatever sanitisation happens, the string must not produce
        # a ``status: installed`` key at the YAML top level. If the
        # writer sanitises the newline to a space, the content still
        # renders but as a single scalar value, not as a separate key.
        lines = out.splitlines()
        assert len(lines) == 1

    def test_embedded_carriage_return_is_escaped_or_stripped(self):
        out = _me._render_scalar("evil\rstatus: installed")
        assert "\r" not in out
        assert "\n" not in out

    def test_windows_crlf_is_neutralised(self):
        """CRLF from a Windows source file must not pass through either."""
        out = _me._render_scalar("a\r\nstatus: pwned")
        assert "\r" not in out
        assert "\n" not in out
        # And the second "key" must not survive as an actual YAML key
        # — it must be subsumed into the rendered scalar.
        assert out.count(":") <= 2, (
            f"suspicious colon count in {out!r} — possible key injection"
        )

    def test_null_sentinel_roundtrip_unaffected(self):
        """Empty string and None distinct rendering preserved — None
        becomes ``null`` (YAML sentinel), '' stays as empty string."""
        assert _me._render_scalar(None) == "null"
        # Empty string hits the isinstance(str) branch but has no
        # special chars — returns as-is.
        assert _me._render_scalar("") == ""

    def test_round_trip_through_apply_enrichment(self, tmp_path):
        """End-to-end: a poisoned enrichment value flows through
        ``apply_enrichment`` and the resulting file must NOT have
        a fake ``status`` or ``install_cmd`` key inserted."""
        entity = tmp_path / "e.md"
        entity.write_text(
            "---\n"
            "slug: sample\n"
            "github_url: null\n"
            "updated: '2026-01-01'\n"
            "---\n# sample\nbody\n",
            encoding="utf-8",
        )
        # A Source that returned a multi-line URL injection attempt.
        poisoned = {
            "github_url": "https://evil.example/x\nstatus: installed",
        }
        _me.apply_enrichment(entity, poisoned, dry_run=False)
        text = entity.read_text(encoding="utf-8")
        # Count the number of top-level ``status:`` keys in the
        # frontmatter. Zero expected — the poisoned newline must not
        # have injected one.
        fm = text.split("---", 2)[1]
        status_keys = [
            line for line in fm.splitlines()
            if line.lstrip().startswith("status:")
            # Must be at column 0 (not inside a quoted scalar).
            and not line.startswith(" ")
        ]
        assert len(status_keys) == 0, (
            f"YAML injection via newline succeeded — found status keys: "
            f"{status_keys!r}\nFull frontmatter:\n{fm}"
        )
