"""
test_mcp_install.py -- Coverage for mcp_install (subprocess surface).

The install path shells out to ``claude mcp add/add-json/remove``. A bug
here can invoke arbitrary commands via frontmatter-sourced install_cmd,
so the allowlist, shlex split, and subcommand gate are covered with
hostile inputs alongside the happy paths.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ctx.adapters.claude_code.install import install_utils
from ctx.adapters.claude_code.install import mcp_install
# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def wiki_dir(tmp_path: Path) -> Path:
    """Minimal wiki root with the mcp-servers shard scaffolding."""
    (tmp_path / "entities" / "mcp-servers").mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def isolated_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    manifest = tmp_path / "skill-manifest.json"
    monkeypatch.setattr(install_utils, "MANIFEST_PATH", manifest)
    return manifest


def _write_entity(wiki_dir: Path, slug: str, frontmatter: dict[str, str]) -> Path:
    shard = slug[0] if slug and slug[0].isalpha() else "0-9"
    d = wiki_dir / "entities" / "mcp-servers" / shard
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{slug}.md"
    body = "---\n"
    for k, v in frontmatter.items():
        body += f"{k}: {v}\n"
    body += "---\nbody text\n"
    path.write_text(body, encoding="utf-8")
    return path


@dataclass
class _FakeProc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@pytest.fixture()
def fake_claude(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture calls to the ``claude mcp <args>`` subprocess."""
    state: dict[str, Any] = {
        "calls": [],
        "rc": 0,
        "stdout": "registered",
        "stderr": "",
        "raise_exc": None,
    }

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeProc:
        state["calls"].append(cmd)
        if state["raise_exc"] is not None:
            raise state["raise_exc"]
        return _FakeProc(
            returncode=state["rc"], stdout=state["stdout"], stderr=state["stderr"]
        )

    monkeypatch.setattr(mcp_install.subprocess, "run", fake_run)
    return state


# ── _mcp_shard ───────────────────────────────────────────────────────────────


class TestShard:
    def test_alphabetic_first_char(self) -> None:
        assert mcp_install._mcp_shard("github") == "g"

    def test_uppercase_first_char(self) -> None:
        # isalpha matches uppercase too; catalog writer lowercases slugs but
        # the shard fn should remain consistent if called with mixed case.
        assert mcp_install._mcp_shard("Filesystem") == "F"

    def test_digit_first_char_goes_to_0_9(self) -> None:
        assert mcp_install._mcp_shard("1password") == "0-9"

    def test_empty_slug_no_crash(self) -> None:
        assert mcp_install._mcp_shard("") == "0-9"

    def test_symbol_first_char_goes_to_0_9(self) -> None:
        assert mcp_install._mcp_shard("-dash") == "0-9"


# ── _parse_entity_frontmatter ────────────────────────────────────────────────


class TestParseFrontmatter:
    def test_missing_file(self, tmp_path: Path) -> None:
        assert mcp_install._parse_entity_frontmatter(tmp_path / "nope.md") == {}

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "e.md"
        f.write_text("just markdown\n", encoding="utf-8")
        assert mcp_install._parse_entity_frontmatter(f) == {}

    def test_flat_scalars(self, wiki_dir: Path) -> None:
        path = _write_entity(
            wiki_dir, "github", {"name": "GitHub MCP", "stars": "42"}
        )
        fm = mcp_install._parse_entity_frontmatter(path)
        assert fm["name"] == "GitHub MCP"
        assert fm["stars"] == "42"

    def test_quoted_values_unwrapped(self, wiki_dir: Path) -> None:
        path = _write_entity(
            wiki_dir, "quoted", {"description": '"a: b"'}
        )
        fm = mcp_install._parse_entity_frontmatter(path)
        assert fm["description"] == "a: b"

    def test_null_sentinel_becomes_empty(self, wiki_dir: Path) -> None:
        path = _write_entity(wiki_dir, "nul", {"install_cmd": "null"})
        fm = mcp_install._parse_entity_frontmatter(path)
        assert fm["install_cmd"] == ""

    def test_tilde_sentinel_becomes_empty(self, wiki_dir: Path) -> None:
        path = _write_entity(wiki_dir, "til", {"install_cmd": "~"})
        fm = mcp_install._parse_entity_frontmatter(path)
        assert fm["install_cmd"] == ""

    def test_list_continuation_lines_skipped(self, wiki_dir: Path) -> None:
        """Indented/list-item continuations in frontmatter must not pollute the flat dict."""
        d = wiki_dir / "entities" / "mcp-servers" / "l"
        d.mkdir(parents=True)
        path = d / "listish.md"
        path.write_text(
            "---\nname: x\ntags:\n  - a\n  - b\n---\nbody\n",
            encoding="utf-8",
        )
        fm = mcp_install._parse_entity_frontmatter(path)
        assert fm == {"name": "x", "tags": ""}

    def test_line_without_colon_skipped(self, wiki_dir: Path) -> None:
        d = wiki_dir / "entities" / "mcp-servers" / "b"
        d.mkdir(parents=True)
        path = d / "broken.md"
        path.write_text(
            "---\nname: x\nmalformed_line_no_colon\nauthor: y\n---\nbody\n",
            encoding="utf-8",
        )
        fm = mcp_install._parse_entity_frontmatter(path)
        assert fm == {"name": "x", "author": "y"}


# ── _run_claude_mcp ──────────────────────────────────────────────────────────


class TestRunClaudeMcp:
    def test_unknown_subcommand_refused(self) -> None:
        rc, out, err = mcp_install._run_claude_mcp(["exec", "foo"])
        assert rc == 127
        assert "refused" in err

    def test_empty_args_refused(self) -> None:
        rc, _, err = mcp_install._run_claude_mcp([])
        assert rc == 127
        assert "empty" in err or "refused" in err

    def test_happy_path(self, fake_claude: dict[str, Any]) -> None:
        fake_claude["rc"] = 0
        fake_claude["stdout"] = "ok"
        rc, out, err = mcp_install._run_claude_mcp(["list"])
        assert rc == 0 and out == "ok"
        assert fake_claude["calls"][0] == ["claude", "mcp", "list"]

    def test_claude_not_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: Any, **_kw: Any) -> Any:
            raise FileNotFoundError("claude not found")

        monkeypatch.setattr(mcp_install.subprocess, "run", boom)
        rc, _, err = mcp_install._run_claude_mcp(["list"])
        assert rc == 127 and "claude CLI not found" in err

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any, **_kw: Any) -> Any:
            raise mcp_install.subprocess.TimeoutExpired(cmd="claude", timeout=60)

        monkeypatch.setattr(mcp_install.subprocess, "run", boom)
        rc, _, err = mcp_install._run_claude_mcp(["list"])
        assert rc == 124 and "timed out" in err


# ── render_card ──────────────────────────────────────────────────────────────


class TestRenderCard:
    def test_includes_slug_header(self) -> None:
        out = mcp_install.render_card({}, "github", command=None)
        assert "github" in out

    def test_long_description_truncated(self) -> None:
        fm = {"description": "x" * 500}
        out = mcp_install.render_card(fm, "s", command=None)
        # Truncated to 297 + "…"
        assert "…" in out
        for line in out.splitlines():
            if line.startswith("  description:"):
                assert len(line) < 320

    def test_command_line_rendered_when_given(self) -> None:
        out = mcp_install.render_card({}, "s", command="npx -y pkg")
        assert "claude mcp add s -- npx -y pkg" in out

    def test_omits_command_line_when_none(self) -> None:
        out = mcp_install.render_card({"name": "X"}, "s", command=None)
        assert "claude mcp add" not in out

    def test_quality_grade_needs_score(self) -> None:
        """Grade without score: the grade line is skipped entirely."""
        out = mcp_install.render_card(
            {"quality_grade": "A"}, "s", command=None
        )
        assert "quality:" not in out

    def test_all_fields_rendered(self) -> None:
        fm = {
            "name": "GitHub",
            "description": "test",
            "github_url": "https://example/x",
            "stars": "42",
            "quality_grade": "A",
            "quality_score": "95",
            "author": "alice",
        }
        out = mcp_install.render_card(fm, "gh", command="npx -y p")
        for needle in ["GitHub", "test", "https://example/x", "42", "grade A",
                       "alice", "npx -y p"]:
            assert needle in out


# ── install_mcp ──────────────────────────────────────────────────────────────


class TestInstallMcp:
    def test_invalid_slug_rejected(self, wiki_dir: Path) -> None:
        r = mcp_install.install_mcp(
            "../evil", wiki_dir=wiki_dir, command="npx -y p", auto=True,
        )
        assert r.status == "not-in-wiki"
        assert "invalid slug" in r.message

    def test_not_in_wiki(self, wiki_dir: Path) -> None:
        r = mcp_install.install_mcp(
            "ghost", wiki_dir=wiki_dir, command="npx -y p", auto=True,
        )
        assert r.status == "not-in-wiki"

    def test_already_installed_skipped(self, wiki_dir: Path) -> None:
        _write_entity(wiki_dir, "gh", {"status": "installed"})
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, command="npx -y p", auto=True,
        )
        assert r.status == "skipped-existing"

    def test_force_overrides_skip(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "installed"})
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, command="npx -y p",
            auto=True, force=True,
        )
        assert r.status == "installed"

    def test_no_command_no_json(self, wiki_dir: Path) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        r = mcp_install.install_mcp("gh", wiki_dir=wiki_dir, auto=True)
        assert r.status == "no-command"
        assert "--cmd" in r.message

    def test_invalid_json_config(self, wiki_dir: Path) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, json_config="{not json",
            auto=True,
        )
        assert r.status == "invalid-cmd"

    def test_dry_run_never_invokes_cli(
        self, wiki_dir: Path, fake_claude: dict[str, Any]
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, command="npx -y p", dry_run=True,
        )
        assert r.status == "aborted"
        assert fake_claude["calls"] == []

    def test_user_declines(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, command="npx -y p",
        )
        assert r.status == "aborted"
        assert fake_claude["calls"] == []

    def test_user_eof_treated_as_decline(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})

        def raise_eof(_prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)
        r = mcp_install.install_mcp("gh", wiki_dir=wiki_dir, command="npx -y p")
        assert r.status == "aborted"

    def test_executable_allowlist_blocks_unknown(
        self, wiki_dir: Path, fake_claude: dict[str, Any], isolated_manifest: Path
    ) -> None:
        """rm -rf /  as install_cmd must be refused before shell-out."""
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, command="rm -rf /",
            auto=True,
        )
        assert r.status == "invalid-cmd"
        assert "allowlist" in r.message
        assert fake_claude["calls"] == []  # never reached the CLI

    @pytest.mark.parametrize(
        "exe", ["npx", "uvx", "node", "python", "python3", "deno", "bunx"]
    )
    def test_executable_allowlist_permits_known(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
        exe: str,
    ) -> None:
        _write_entity(wiki_dir, f"srv-{exe}", {"status": "cataloged"})
        r = mcp_install.install_mcp(
            f"srv-{exe}", wiki_dir=wiki_dir, command=f"{exe} -y pkg",
            auto=True,
        )
        assert r.status == "installed"

    def test_unparseable_shlex_rejected(
        self, wiki_dir: Path, fake_claude: dict[str, Any], isolated_manifest: Path
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, command='npx -y "unclosed',
            auto=True,
        )
        assert r.status == "invalid-cmd"

    # Strix vuln-0002 regression: even when the first token is allowlisted,
    # code-execution argument forms must be rejected. A tampered frontmatter
    # install_cmd could otherwise invoke arbitrary interpreter-controlled
    # code through python, node, or deno.
    @pytest.mark.parametrize("cmd", [
        'python -c "import os; os.system(\'whoami\')"',
        'python3 -c "pwn"',
        "python -m http.server",
        'python3 -m http.server',
        "node -e require('fs').writeFileSync('/tmp/p','owned')",
        'node --eval "process.exit(1)"',
        'deno eval "await Deno.writeTextFile(\'/tmp/p\',\'x\')"',
        'deno repl',
    ])
    def test_rejects_code_execution_args_through_allowlisted_interpreter(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
        cmd: str,
    ) -> None:
        """Each payload uses an allowlisted exec but a code-execution arg."""
        _write_entity(wiki_dir, "srv", {"status": "cataloged"})
        r = mcp_install.install_mcp(
            "srv", wiki_dir=wiki_dir, command=cmd, auto=True,
        )
        assert r.status == "invalid-cmd", (
            f"{cmd!r} slipped past arg-shape filter (status={r.status})"
        )
        assert fake_claude["calls"] == [], "claude CLI was invoked"

    # Non-regression: the supported package-launcher patterns MUST still work.
    @pytest.mark.parametrize("cmd", [
        "npx -y @modelcontextprotocol/server-github",
        "uvx atlassian-mcp",
        "bunx some-pkg",
        "node /opt/mcp-server/index.js",         # script path, not -e
        "python /opt/mcp-server/server.py",      # script path, not -c
        "python3 ./server.py",
        "deno run --allow-net /opt/mcp/main.ts", # deno run is the designed path
    ])
    def test_accepts_supported_launcher_patterns(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
        cmd: str,
    ) -> None:
        _write_entity(wiki_dir, "srv-ok", {"status": "cataloged"})
        r = mcp_install.install_mcp(
            "srv-ok", wiki_dir=wiki_dir, command=cmd, auto=True,
        )
        assert r.status == "installed", (
            f"legitimate launcher {cmd!r} falsely rejected (msg={r.message})"
        )

    def test_empty_command_tokens_rejected(
        self, wiki_dir: Path, fake_claude: dict[str, Any], isolated_manifest: Path
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, command="   ", auto=True,
        )
        assert r.status == "invalid-cmd"
        assert "empty" in r.message.lower()

    def test_claude_cli_nonzero_failure(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        fake_claude["rc"] = 1
        fake_claude["stderr"] = "boom"
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, command="npx -y p", auto=True,
        )
        assert r.status == "claude-cli-failed"
        assert "boom" in r.message

    def test_json_config_routes_to_add_json(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        cfg = json.dumps({"command": "npx", "args": ["-y", "pkg"]})
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, json_config=cfg, auto=True,
        )
        assert r.status == "installed"
        call = fake_claude["calls"][0]
        assert "add-json" in call and "gh" in call and cfg in call
        manifest = install_utils.load_manifest()
        assert manifest["load"][0]["json_config"] == cfg
        assert "command" not in manifest["load"][0]

    def test_happy_path_writes_manifest_and_status(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
    ) -> None:
        entity = _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, command="npx -y pkg", auto=True,
        )
        assert r.status == "installed"
        # Status flipped on disk.
        assert "status: installed" in entity.read_text(encoding="utf-8")
        # Manifest has the entry tagged as mcp-server.
        m = install_utils.load_manifest()
        assert any(
            e["skill"] == "gh" and e["entity_type"] == "mcp-server"
            for e in m["load"]
        )

    def test_install_cmd_fallback_from_frontmatter(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
    ) -> None:
        """Reinstall with --force reads install_cmd from frontmatter."""
        _write_entity(
            wiki_dir, "gh", {"status": "installed", "install_cmd": "npx -y pkg"}
        )
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, force=True, auto=True,
        )
        assert r.status == "installed"
        assert r.command == "npx -y pkg"

    def test_install_cmd_from_frontmatter_still_allowlisted(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
    ) -> None:
        """Frontmatter-supplied install_cmd must still pass the allowlist."""
        _write_entity(
            wiki_dir, "gh", {"status": "installed", "install_cmd": "rm -rf /"}
        )
        r = mcp_install.install_mcp(
            "gh", wiki_dir=wiki_dir, force=True, auto=True,
        )
        assert r.status == "invalid-cmd"
        assert fake_claude["calls"] == []


# ── uninstall_mcp ────────────────────────────────────────────────────────────


class TestUninstallMcp:
    def test_invalid_slug(self, wiki_dir: Path) -> None:
        r = mcp_install.uninstall_mcp("../evil", wiki_dir=wiki_dir)
        assert r.status == "not-installed"

    def test_not_installed_short_circuits(
        self, wiki_dir: Path, fake_claude: dict[str, Any]
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        r = mcp_install.uninstall_mcp("gh", wiki_dir=wiki_dir)
        assert r.status == "not-installed"
        assert fake_claude["calls"] == []

    def test_dry_run(
        self, wiki_dir: Path, fake_claude: dict[str, Any]
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "installed"})
        r = mcp_install.uninstall_mcp("gh", wiki_dir=wiki_dir, dry_run=True)
        assert r.status == "uninstalled"
        assert fake_claude["calls"] == []

    def test_happy_path(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
    ) -> None:
        entity = _write_entity(
            wiki_dir,
            "gh",
            {"status": "installed", "install_cmd": "npx -y pkg"},
        )
        install_utils.record_install(
            "gh",
            entity_type="mcp-server",
            source="ctx-mcp-install",
            extra={"command": "npx -y pkg"},
        )
        r = mcp_install.uninstall_mcp("gh", wiki_dir=wiki_dir)
        assert r.status == "uninstalled"
        text = entity.read_text(encoding="utf-8")
        assert "status: cataloged" in text
        assert "install_cmd: npx -y pkg" in text
        m = install_utils.load_manifest()
        assert not any(e["skill"] == "gh" for e in m["load"])
        assert any(
            e["skill"] == "gh" and e["entity_type"] == "mcp-server"
            and e["command"] == "npx -y pkg"
            for e in m["unload"]
        )

    def test_uninstall_updates_existing_unload_with_preserved_command(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
    ) -> None:
        _write_entity(
            wiki_dir,
            "gh",
            {"status": "installed", "install_cmd": "npx -y pkg"},
        )
        install_utils.save_manifest({
            "load": [{
                "skill": "gh",
                "entity_type": "mcp-server",
                "source": "ctx-mcp-install",
                "command": "npx -y pkg",
            }],
            "unload": [{
                "skill": "gh",
                "entity_type": "mcp-server",
                "source": "old-dashboard",
            }],
            "warnings": [],
        })

        result = mcp_install.uninstall_mcp("gh", wiki_dir=wiki_dir)

        assert result.status == "uninstalled"
        manifest = install_utils.load_manifest()
        assert manifest["unload"] == [{
            "skill": "gh",
            "entity_type": "mcp-server",
            "source": "old-dashboard",
            "command": "npx -y pkg",
        }]

    def test_cli_failure_without_force(
        self, wiki_dir: Path, fake_claude: dict[str, Any]
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "installed"})
        fake_claude["rc"] = 1
        fake_claude["stderr"] = "drift"
        r = mcp_install.uninstall_mcp("gh", wiki_dir=wiki_dir)
        assert r.status == "claude-cli-failed"

    def test_cli_failure_with_force_still_flips_local(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
    ) -> None:
        entity = _write_entity(wiki_dir, "gh", {"status": "installed"})
        fake_claude["rc"] = 1
        fake_claude["stderr"] = "drift"
        r = mcp_install.uninstall_mcp("gh", wiki_dir=wiki_dir, force=True)
        assert r.status == "uninstalled"
        assert "status: cataloged" in entity.read_text(encoding="utf-8")

    def test_force_without_entity(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
    ) -> None:
        """Entity missing but user forces — still calls CLI and records unload."""
        r = mcp_install.uninstall_mcp("ghost", wiki_dir=wiki_dir, force=True)
        assert r.status == "uninstalled"


# ── _prompt_confirm ──────────────────────────────────────────────────────────


class TestPromptConfirm:
    @pytest.mark.parametrize("ans,expected", [
        ("y", True), ("Y", True), ("yes", True), ("YES", True),
        ("n", False), ("no", False), ("", False), ("maybe", False),
    ])
    def test_answers(
        self, monkeypatch: pytest.MonkeyPatch, ans: str, expected: bool
    ) -> None:
        monkeypatch.setattr("builtins.input", lambda _p: ans)
        assert mcp_install._prompt_confirm("go?") is expected

    def test_eof_is_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_p: str) -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", boom)
        assert mcp_install._prompt_confirm("go?") is False

    def test_ctrl_c_is_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_p: str) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", boom)
        assert mcp_install._prompt_confirm("go?") is False


# ── CLI main wrappers ────────────────────────────────────────────────────────


class TestInstallMain:
    def test_install_main_happy_path(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-mcp-install", "gh", "--cmd", "npx -y p",
             "--auto", "--wiki-dir", str(wiki_dir)],
        )
        with pytest.raises(SystemExit) as ei:
            mcp_install.install_main()
        assert ei.value.code == 0
        assert "[OK]" in capsys.readouterr().out

    def test_install_main_json_output(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-mcp-install", "gh", "--cmd", "npx -y p", "--auto",
             "--wiki-dir", str(wiki_dir), "--json"],
        )
        with pytest.raises(SystemExit):
            mcp_install.install_main()
        out = capsys.readouterr().out
        # Tolerate the card's non-JSON preamble; JSON object is at the end.
        start = out.rindex("{")
        payload = json.loads(out[start:])
        assert payload["slug"] == "gh"
        assert payload["status"] == "installed"

    def test_install_main_exit_code_on_failure(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        fake_claude["rc"] = 1
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-mcp-install", "gh", "--cmd", "npx -y p", "--auto",
             "--wiki-dir", str(wiki_dir)],
        )
        with pytest.raises(SystemExit) as ei:
            mcp_install.install_main()
        assert ei.value.code == 1

    def test_uninstall_main_happy_path(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        isolated_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "installed"})
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-mcp-uninstall", "gh", "--wiki-dir", str(wiki_dir)],
        )
        with pytest.raises(SystemExit) as ei:
            mcp_install.uninstall_main()
        assert ei.value.code == 0

    def test_uninstall_main_not_installed_exit_nonzero(
        self,
        wiki_dir: Path,
        fake_claude: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_entity(wiki_dir, "gh", {"status": "cataloged"})
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-mcp-uninstall", "gh", "--wiki-dir", str(wiki_dir)],
        )
        with pytest.raises(SystemExit) as ei:
            mcp_install.uninstall_main()
        assert ei.value.code == 1


# ── _force_utf8_stdio ────────────────────────────────────────────────────────


class TestForceUtf8Stdio:
    def test_survives_stream_without_reconfigure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Dumb:
            pass

        monkeypatch.setattr(mcp_install.sys, "stdout", _Dumb())
        monkeypatch.setattr(mcp_install.sys, "stderr", _Dumb())
        mcp_install._force_utf8_stdio()  # must not raise

    def test_tolerates_reconfigure_oserror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _ThrowsOnReconfig:
            def reconfigure(self, **_kw: Any) -> None:
                raise OSError("not a tty")

        monkeypatch.setattr(mcp_install.sys, "stdout", _ThrowsOnReconfig())
        monkeypatch.setattr(mcp_install.sys, "stderr", _ThrowsOnReconfig())
        mcp_install._force_utf8_stdio()  # must not raise
