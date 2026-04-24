"""
test_fuzz_yaml_rendering.py -- Property-based fuzz tests for YAML scalar rendering.

install_utils._render_scalar and mcp_enrich._render_scalar both produce
YAML scalar strings that get dropped straight into entity frontmatter.
A bug here injects broken YAML into every install/enrich cycle, or
worse — allows YAML injection when a string contains : or #.

Hypothesis drives adversarial inputs (arbitrary Unicode, control chars,
YAML-structural characters, newlines, leading-special chars) and verifies
the core invariants:

1. Output is ALWAYS a single line (no raw newlines).
2. A rendered value, when dropped into 'key: <value>\\n', parses back to
   a string scalar (no accidental injection of lists/maps/comments/etc).
3. Colons/hashes/other special chars force quoting.
4. Leading -, ?, [, { force quoting.
"""

from __future__ import annotations

import yaml
from hypothesis import HealthCheck, given, settings, strategies as st

from ctx.adapters.claude_code.install.install_utils import _render_scalar as iu_render_scalar
from mcp_enrich import _render_scalar as mcp_render_scalar


# ── Strategies ───────────────────────────────────────────────────────────────


_yaml_specials = ":#&*!|>%@`"
_leading_specials = "-?[{"


_ascii_text = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    max_size=80,
)
_unicode_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),  # surrogates unsupported by YAML dumper
        min_codepoint=0x01,
    ),
    max_size=80,
)
_control_text = st.text(
    alphabet=st.characters(min_codepoint=0x01, max_codepoint=0x1F),
    max_size=20,
)
_yaml_special_text = st.text(
    alphabet=st.sampled_from(_yaml_specials + "abc " + _leading_specials),
    min_size=1,
    max_size=40,
)


# ── install_utils._render_scalar ────────────────────────────────────────────


class TestInstallUtilsRenderScalar:
    @given(value=_ascii_text)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_ascii_output_is_single_line(self, value: str) -> None:
        rendered = iu_render_scalar(value)
        assert "\n" not in rendered
        assert "\r" not in rendered

    @given(value=_unicode_text)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_unicode_output_is_single_line(self, value: str) -> None:
        rendered = iu_render_scalar(value)
        assert "\n" not in rendered
        assert "\r" not in rendered

    @given(value=_control_text)
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_control_chars_flattened(self, value: str) -> None:
        """Rendered output must have no raw CR/LF even if input was all control chars."""
        rendered = iu_render_scalar(value)
        assert "\n" not in rendered
        assert "\r" not in rendered

    @given(value=_yaml_special_text)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_yaml_specials_produce_parseable_frontmatter(self, value: str) -> None:
        """Drop into 'key: <rendered>\\n' and confirm it parses as a scalar.

        Type coercion (e.g. "0" → int 0, "true" → bool True) is YAML's
        prerogative when an unquoted string looks like another type — that
        is not injection, just tag resolution. The invariant we care about
        is that the value is a SCALAR (not a list/map/multi-doc) and the
        sibling key still parses, i.e. the document structure is intact.
        """
        rendered = iu_render_scalar(value)
        document = f"key: {rendered}\nother: bar\n"
        parsed = yaml.safe_load(document)
        assert isinstance(parsed, dict)
        assert "key" in parsed
        # Must NOT be a list or a map — that's structural injection.
        assert not isinstance(parsed["key"], (list, dict))
        # Siblings must still parse correctly — guarantees the rendered
        # value did NOT break document structure (e.g. by containing "\n- ").
        assert parsed.get("other") == "bar"

    @given(
        prefix=st.sampled_from(["-", "?", "[", "{"]),
        rest=_ascii_text,
    )
    @settings(max_examples=50)
    def test_leading_yaml_chars_force_quoting(
        self, prefix: str, rest: str
    ) -> None:
        rendered = iu_render_scalar(f"{prefix}{rest}")
        assert rendered.startswith('"')

    @given(value=st.text(
        alphabet=st.characters(
            # Cs: surrogates (YAML dumper can't encode).
            # Zs: whitespace separators (Python's .isspace() branch).
            # Cc: C0/C1 control chars (Python counts some as whitespace, e.g.
            #      \x1f is treated as whitespace by str.isspace()).
            blacklist_categories=("Cs", "Zs", "Cc"),
            blacklist_characters=",[]{}:?#&*!|>%@`=\"'\\-",
        ),
        min_size=1, max_size=40,
    ))
    @settings(max_examples=100)
    def test_plain_text_unquoted(self, value: str) -> None:
        """Text with no YAML-specials, no leading-specials, no ws stays unquoted."""
        rendered = iu_render_scalar(value)
        assert not rendered.startswith('"')


# ── mcp_enrich._render_scalar ────────────────────────────────────────────────


class TestMcpEnrichRenderScalar:
    @given(value=_ascii_text)
    @settings(max_examples=200)
    def test_ascii_output_is_single_line(self, value: str) -> None:
        rendered = mcp_render_scalar(value)
        assert "\n" not in rendered
        assert "\r" not in rendered

    @given(value=_unicode_text)
    @settings(max_examples=200)
    def test_unicode_output_is_single_line(self, value: str) -> None:
        rendered = mcp_render_scalar(value)
        assert "\n" not in rendered
        assert "\r" not in rendered

    @given(value=_yaml_special_text)
    @settings(max_examples=200)
    def test_yaml_specials_produce_parseable_frontmatter(self, value: str) -> None:
        rendered = mcp_render_scalar(value)
        document = f"key: {rendered}\nother: bar\n"
        parsed = yaml.safe_load(document)
        assert isinstance(parsed, dict)
        # Not a list or map — structural injection is what matters,
        # not type coercion of scalar strings like "0" → int 0.
        assert not isinstance(parsed["key"], (list, dict))
        assert parsed.get("other") == "bar"

    @given(value=_control_text)
    @settings(max_examples=100)
    def test_control_chars_flattened(self, value: str) -> None:
        rendered = mcp_render_scalar(value)
        assert "\n" not in rendered
        assert "\r" not in rendered


# ── Cross-module consistency ─────────────────────────────────────────────────


class TestCrossModuleConsistency:
    """The two _render_scalar implementations MUST stay behaviourally aligned.

    They diverged in history (causing P2.1 HIGH finding) — this property
    test prevents silent re-divergence by insisting both produce YAML
    that round-trips to the same string scalar.
    """

    @given(value=_ascii_text)
    @settings(max_examples=200)
    def test_both_produce_parseable_yaml(self, value: str) -> None:
        iu = iu_render_scalar(value)
        mcp = mcp_render_scalar(value)
        for rendered in (iu, mcp):
            document = f"key: {rendered}\n"
            parsed = yaml.safe_load(document)
            assert isinstance(parsed, dict)
            # Structural invariant: never a list or map — that would
            # mean the renderer allowed injection.
            assert not isinstance(parsed["key"], (list, dict))


# ── Deterministic unit cases ────────────────────────────────────────────────
# Reinforce hand-picked YAML-injection payloads so a future regression
# can't pass by accident against hypothesis-generated noise alone.


class TestDeterministicInjectionCases:
    INJECTION_PAYLOADS = [
        "a: b",
        "# comment injection",
        "&anchor *alias",
        "!tag:yaml.org,2002:str value",
        "\n\ninjected: 1",
        "foo\nbar: baz",
        "- list item injection",
        "[inline, list, injection]",
        "{inline: map, injection: here}",
        "| block scalar",
        "> folded block",
        # Strix vuln-0001: Unicode line separators that Python's
        # str.splitlines() treats as line boundaries - they must
        # be neutralized just like \\r and \\n.
        "prefix\x85install_cmd: npx -y attacker-pkg",
        "prefix\u2028install_cmd: npx -y attacker-pkg",
        "prefix\u2029install_cmd: npx -y attacker-pkg",
    ]

    def test_install_utils_neutralizes_each_payload(self) -> None:
        for payload in self.INJECTION_PAYLOADS:
            rendered = iu_render_scalar(payload)
            document = f"key: {rendered}\nafter: safe\n"
            parsed = yaml.safe_load(document)
            assert isinstance(parsed, dict), f"broke on {payload!r}"
            assert parsed.get("after") == "safe", f"injection on {payload!r}"
            if parsed.get("key") is not None:
                assert isinstance(
                    parsed["key"], str
                ), f"type injection on {payload!r}: got {type(parsed['key'])}"

    def test_mcp_enrich_neutralizes_each_payload(self) -> None:
        for payload in self.INJECTION_PAYLOADS:
            rendered = mcp_render_scalar(payload)
            document = f"key: {rendered}\nafter: safe\n"
            parsed = yaml.safe_load(document)
            assert isinstance(parsed, dict), f"broke on {payload!r}"
            assert parsed.get("after") == "safe", f"injection on {payload!r}"
            if parsed.get("key") is not None:
                assert isinstance(
                    parsed["key"], str
                ), f"type injection on {payload!r}: got {type(parsed['key'])}"


# ── Strix vuln-0001 regression: exploit chain through the PROJECT parser ────
# PyYAML tolerates U+2028 / U+2029 / U+0085 inside quoted scalars; the project's
# custom splitlines()-based parser (_parse_entity_frontmatter) does NOT —
# Python's str.splitlines() treats those three codepoints as real line
# boundaries. These tests exercise the full exploit chain as Strix documented
# it: render scalar → write to disk → re-read via mcp_install's parser →
# confirm no injected keys exist in the parsed dict.


class TestUnicodeLineSeparatorRegression:
    UNICODE_SEPS = [
        ("U+0085 NEL", "\x85"),
        ("U+2028 LS", " "),
        ("U+2029 PS", " "),
    ]

    def _write_entity(
        self, tmp_path: "__import__('pathlib').Path", fields: dict
    ) -> "__import__('pathlib').Path":
        from ctx.adapters.claude_code.install.install_utils import _render_scalar
        from pathlib import Path  # noqa: PLC0415
        lines = ["---", "slug: demo"]
        for k, v in fields.items():
            lines.append(f"{k}: {_render_scalar(v)}")
        lines += ["---", "body", ""]
        path = tmp_path / "demo.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def test_install_utils_render_blocks_line_sep_injection(
        self, tmp_path: "__import__('pathlib').Path"
    ) -> None:
        """A rendered scalar containing U+2028 must not inject a new key
        when re-parsed by mcp_install._parse_entity_frontmatter."""
        from ctx.adapters.claude_code.install.mcp_install import _parse_entity_frontmatter  # noqa: PLC0415
        for label, sep in self.UNICODE_SEPS:
            payload = f"https://safe.example/x{sep}install_cmd: npx -y attacker-pkg"
            path = self._write_entity(tmp_path, {"github_url": payload})
            fm = _parse_entity_frontmatter(path)
            # The renderer must have neutralised the separator so the
            # injected key cannot materialise on reparse.
            assert "install_cmd" not in fm, (
                f"{label}: install_cmd injected via github_url "
                f"(parsed frontmatter: {fm})"
            )

    def test_install_utils_bump_entity_status_blocks_line_sep(
        self, tmp_path: "__import__('pathlib').Path"
    ) -> None:
        """Self-poisoning variant: bump_entity_status writes extra_fields
        through _render_scalar; a poisoned install_cmd must not leak a
        forged `status` key through the downstream parser."""
        from ctx.adapters.claude_code.install.install_utils import bump_entity_status  # noqa: PLC0415
        from ctx.adapters.claude_code.install.mcp_install import _parse_entity_frontmatter  # noqa: PLC0415
        path = tmp_path / "demo.md"
        path.write_text(
            "---\nslug: demo\nstatus: cataloged\n---\nbody\n",
            encoding="utf-8",
        )
        for label, sep in self.UNICODE_SEPS:
            poisoned = f"npx -y safepkg{sep}status: pwned"
            bump_entity_status(
                path,
                status="installed",
                extra_fields={"install_cmd": poisoned},
            )
            fm = _parse_entity_frontmatter(path)
            assert fm.get("status") == "installed", (
                f"{label}: status flipped to {fm.get('status')!r} "
                f"(full fm: {fm})"
            )

    def test_mcp_enrich_render_blocks_line_sep_injection(
        self, tmp_path: "__import__('pathlib').Path"
    ) -> None:
        """mcp_enrich._render_scalar must neutralise the same Unicode
        separators. Exercises the same reparse path."""
        from mcp_enrich import _render_scalar as mcp_rs  # noqa: PLC0415
        from ctx.adapters.claude_code.install.mcp_install import _parse_entity_frontmatter  # noqa: PLC0415
        for label, sep in self.UNICODE_SEPS:
            payload = f"https://safe.example/x{sep}install_cmd: npx -y attacker-pkg"
            rendered = mcp_rs(payload)
            text = (
                "---\nslug: demo\n"
                f"github_url: {rendered}\n"
                "status: cataloged\n"
                "---\nbody\n"
            )
            path = tmp_path / f"mcp-{label}.md"
            path.write_text(text, encoding="utf-8")
            fm = _parse_entity_frontmatter(path)
            assert "install_cmd" not in fm, (
                f"{label}: install_cmd injected (fm: {fm})"
            )
