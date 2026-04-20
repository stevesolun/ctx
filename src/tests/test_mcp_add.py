"""
tests/test_mcp_add.py -- Integration-style tests for the add_mcp orchestrator.

Mirrors the structure of test_skill_add.py. Heavy external deps
(check_intake, record_embedding) are monkeypatched on the mcp_add module
so the tests run without sentence-transformers or a real vector store.

Coverage:
  - New record creates entity file, returns is_new_page=True
  - Created file has valid YAML frontmatter containing type: mcp-server
  - Calling add_mcp twice with same record returns is_new_page=False, sources deduped
  - Two records with same slug from different sources merges sources list
  - dry_run=True does not write any file, still returns computed dict
  - Intake gate rejection raises IntakeRejected
  - Numeric slug sharding: slug starting with digit lands under 0-9/
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcp_entity import McpRecord  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    name: str = "github-mcp",
    description: str = "A GitHub MCP server",
    sources: list[str] | None = None,
    github_url: str | None = "https://github.com/Org/github-mcp",
    **kwargs: Any,
) -> McpRecord:
    data: dict[str, Any] = {
        "name": name,
        "description": description,
        "sources": sources if sources is not None else ["awesome-mcp"],
        "github_url": github_url,
        "tags": ["github"],
        "transports": ["stdio"],
    }
    data.update(kwargs)
    return McpRecord.from_dict(data)


def _fake_allow(*args: Any, **kwargs: Any) -> Any:
    from intake_gate import IntakeDecision

    return IntakeDecision(allow=True)


def _fake_reject(*args: Any, **kwargs: Any) -> Any:
    from intake_gate import IntakeDecision, IntakeFinding

    finding = IntakeFinding(code="DUPLICATE", severity="fail", message="test rejection")
    return IntakeDecision(allow=False, findings=(finding,))


def _fake_record_embedding(**kwargs: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def wiki_dir(tmp_path: Path) -> Path:
    """Minimal wiki root with entities/mcp-servers directory pre-created."""
    wiki = tmp_path / "skill-wiki"
    wiki.mkdir()
    (wiki / "entities" / "mcp-servers").mkdir(parents=True)
    return wiki


@pytest.fixture()
def patched_mcp_add(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import mcp_add with intake, embedding, and wiki-sync deps patched out.

    ``update_index`` and ``append_log`` mutate top-level wiki files
    (``index.md``, ``log.md``) that this fixture's tmp_path wiki does
    not pre-populate. The behaviour they implement is exercised in
    ``test_wiki_sync.py``; here we just need ``add_mcp`` to call them
    once with the right slug.
    """
    import mcp_add  # noqa: PLC0415

    monkeypatch.setattr("mcp_add.check_intake", _fake_allow)
    monkeypatch.setattr("mcp_add.record_embedding", _fake_record_embedding)
    monkeypatch.setattr("mcp_add.update_index", lambda *a, **k: None)
    monkeypatch.setattr("mcp_add.append_log", lambda *a, **k: None)
    return mcp_add


# ---------------------------------------------------------------------------
# Core orchestrator behaviour
# ---------------------------------------------------------------------------


class TestAddMcpNewRecord:
    def test_new_record_creates_entity_file(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record = _make_record(name="github-mcp")
        patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)

        expected = (
            wiki_dir / "entities" / "mcp-servers" / "g" / "github-mcp.md"
        )
        assert expected.exists(), f"Entity file not found at {expected}"

    def test_new_record_returns_is_new_page_true(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record = _make_record(name="github-mcp")
        result = patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert result["is_new_page"] is True

    def test_new_record_result_contains_slug(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record = _make_record(name="github-mcp")
        result = patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert result["slug"] == record.slug

    def test_new_record_result_contains_path(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record = _make_record(name="github-mcp")
        result = patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert "path" in result


class TestEntityFileContent:
    def test_created_file_has_valid_yaml_frontmatter(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record = _make_record(name="github-mcp")
        patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)

        entity_file = wiki_dir / "entities" / "mcp-servers" / "g" / "github-mcp.md"
        content = entity_file.read_text(encoding="utf-8")

        # Must start with YAML frontmatter
        assert content.startswith("---"), "File does not start with YAML frontmatter"
        _, fm_block, _ = content.split("---", 2)
        parsed = yaml.safe_load(fm_block)
        assert isinstance(parsed, dict), "Frontmatter did not parse to a dict"

    def test_created_file_contains_type_mcp_server(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record = _make_record(name="github-mcp")
        patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)

        entity_file = wiki_dir / "entities" / "mcp-servers" / "g" / "github-mcp.md"
        content = entity_file.read_text(encoding="utf-8")
        _, fm_block, _ = content.split("---", 2)
        parsed = yaml.safe_load(fm_block)
        assert parsed.get("type") == "mcp-server"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestAddMcpIdempotency:
    def test_second_call_same_record_returns_is_new_page_false(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record = _make_record(name="github-mcp", sources=["awesome-mcp"])
        patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        result2 = patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert result2["is_new_page"] is False

    def test_second_call_same_source_keeps_sources_length_one(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record = _make_record(name="github-mcp", sources=["awesome-mcp"])
        patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        result2 = patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert len(result2["merged_sources"]) == 1


class TestAddMcpSourceMerging:
    def test_two_different_sources_merged_and_sorted(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record_a = _make_record(name="github-mcp", sources=["awesome-mcp"])
        record_b = _make_record(name="github-mcp", sources=["pulsemcp"])

        patched_mcp_add.add_mcp(record=record_a, wiki_path=wiki_dir)
        result2 = patched_mcp_add.add_mcp(record=record_b, wiki_path=wiki_dir)

        assert sorted(result2["merged_sources"]) == ["awesome-mcp", "pulsemcp"]
        assert result2["is_new_page"] is False


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------


class TestAddMcpDryRun:
    def test_dry_run_does_not_create_file(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record = _make_record(name="dry-run-mcp")
        patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir, dry_run=True)

        expected = wiki_dir / "entities" / "mcp-servers" / "d" / "dry-run-mcp.md"
        assert not expected.exists(), "dry_run=True must not create the entity file"

    def test_dry_run_returns_result_dict(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record = _make_record(name="dry-run-mcp")
        result = patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir, dry_run=True)

        assert "slug" in result
        assert "is_new_page" in result
        assert "merged_sources" in result
        assert "path" in result


# ---------------------------------------------------------------------------
# Intake gate rejection
# ---------------------------------------------------------------------------


class TestAddMcpIntakeRejection:
    def test_rejected_intake_raises_intake_rejected(
        self, monkeypatch: pytest.MonkeyPatch, wiki_dir: Path
    ) -> None:
        import mcp_add  # noqa: PLC0415
        from intake_pipeline import IntakeRejected  # noqa: PLC0415

        monkeypatch.setattr("mcp_add.check_intake", _fake_reject)
        monkeypatch.setattr("mcp_add.record_embedding", _fake_record_embedding)

        record = _make_record(name="rejected-mcp")
        with pytest.raises(IntakeRejected):
            mcp_add.add_mcp(record=record, wiki_path=wiki_dir)

    def test_rejected_intake_does_not_create_file(
        self, monkeypatch: pytest.MonkeyPatch, wiki_dir: Path
    ) -> None:
        import mcp_add  # noqa: PLC0415
        from intake_pipeline import IntakeRejected  # noqa: PLC0415

        monkeypatch.setattr("mcp_add.check_intake", _fake_reject)
        monkeypatch.setattr("mcp_add.record_embedding", _fake_record_embedding)

        record = _make_record(name="rejected-mcp")
        with pytest.raises(IntakeRejected):
            mcp_add.add_mcp(record=record, wiki_path=wiki_dir)

        entity_file = wiki_dir / "entities" / "mcp-servers" / "r" / "rejected-mcp.md"
        assert not entity_file.exists()


# ---------------------------------------------------------------------------
# Phase 3.5 regression: existence check bypasses intake on the merge path
# ---------------------------------------------------------------------------


class TestExistenceBypassesIntakeOnMerge:
    """Regression: when target entity already exists, intake gate must
    NOT run. The intake gate would flag the re-fetched record as
    DUPLICATE against its own cached embedding (cosine 1.0 > 0.93
    threshold) and block the source-merge path entirely.

    This was discovered live in Phase 3b: re-running
    ``ctx-mcp-fetch --source awesome-mcp --limit 5`` after a prior
    N=1 ingest produced ``[1/5] [rejected] 1mcp-agent: DUPLICATE``
    instead of the expected merge.
    """

    def test_intake_not_called_when_target_exists(
        self, monkeypatch: pytest.MonkeyPatch, wiki_dir: Path
    ) -> None:
        import mcp_add  # noqa: PLC0415

        # First write: intake should be called (allowed) and the file
        # created. We use a counter so the second call's lack of intake
        # invocation is observable.
        intake_calls: list[str] = []

        def _counting_intake(*args: Any, **_kwargs: Any) -> Any:
            from intake_gate import IntakeDecision  # noqa: PLC0415
            intake_calls.append("called")
            return IntakeDecision(allow=True)

        monkeypatch.setattr("mcp_add.check_intake", _counting_intake)
        monkeypatch.setattr("mcp_add.record_embedding", _fake_record_embedding)
        monkeypatch.setattr("mcp_add.update_index", lambda *a, **k: None)
        monkeypatch.setattr("mcp_add.append_log", lambda *a, **k: None)

        record = _make_record(name="merge-test-mcp")

        # First ingest: new entity → intake runs.
        mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert len(intake_calls) == 1, "first add must trigger intake"

        # Second ingest of the same record: existing entity → intake
        # MUST be skipped, otherwise the gate's DUPLICATE check would
        # reject and the source-merge path becomes unreachable.
        mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert len(intake_calls) == 1, (
            "re-add must NOT trigger intake (was: "
            f"{len(intake_calls)} calls; expected to stay at 1)"
        )

    def test_re_add_with_new_source_merges_without_intake(
        self, monkeypatch: pytest.MonkeyPatch, wiki_dir: Path
    ) -> None:
        # Even if intake would reject as DUPLICATE, the merge path
        # should run because the target file already exists.
        import mcp_add  # noqa: PLC0415

        # Allow first add, then reject everything afterwards. If the
        # merge path correctly bypasses intake, the second add still
        # succeeds.
        call_count: list[int] = [0]

        def _allow_then_reject(*args: Any, **_kwargs: Any) -> Any:
            from intake_gate import IntakeDecision, IntakeFinding  # noqa: PLC0415
            call_count[0] += 1
            if call_count[0] == 1:
                return IntakeDecision(allow=True)
            return IntakeDecision(
                allow=False,
                findings=(IntakeFinding(code="DUPLICATE", severity="fail",
                                        message="cosine 1.0"),),
            )

        monkeypatch.setattr("mcp_add.check_intake", _allow_then_reject)
        monkeypatch.setattr("mcp_add.record_embedding", _fake_record_embedding)
        monkeypatch.setattr("mcp_add.update_index", lambda *a, **k: None)
        monkeypatch.setattr("mcp_add.append_log", lambda *a, **k: None)

        record_v1 = _make_record(name="cross-source-mcp", sources=["awesome-mcp"])
        record_v2 = _make_record(name="cross-source-mcp", sources=["pulsemcp"])

        # First add succeeds (new entity, intake allows).
        result1 = mcp_add.add_mcp(record=record_v1, wiki_path=wiki_dir)
        assert result1["is_new_page"] is True

        # Second add MUST NOT raise IntakeRejected even though our
        # mock intake would now reject. The merge path bypasses intake.
        result2 = mcp_add.add_mcp(record=record_v2, wiki_path=wiki_dir)
        assert result2["is_new_page"] is False
        assert result2["merged_sources"] == ["awesome-mcp", "pulsemcp"]
        # Intake was called exactly once (the first add); the second
        # never reached the gate.
        assert call_count[0] == 1

    def test_record_embedding_not_called_on_merge(
        self, monkeypatch: pytest.MonkeyPatch, wiki_dir: Path
    ) -> None:
        # Re-merging an existing entity must NOT re-embed (the existing
        # vector is correct; re-embedding would be wasted I/O).
        import mcp_add  # noqa: PLC0415

        monkeypatch.setattr("mcp_add.check_intake", _fake_allow)
        monkeypatch.setattr("mcp_add.update_index", lambda *a, **k: None)
        monkeypatch.setattr("mcp_add.append_log", lambda *a, **k: None)

        embed_calls: list[str] = []

        def _counting_embed(**kwargs: Any) -> None:
            embed_calls.append(kwargs.get("subject_id", "?"))

        monkeypatch.setattr("mcp_add.record_embedding", _counting_embed)

        record = _make_record(name="no-reembed-mcp")
        mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert embed_calls == ["no-reembed-mcp"]

        mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert embed_calls == ["no-reembed-mcp"], (
            "embedding must not be re-recorded on merge"
        )


# ---------------------------------------------------------------------------
# Phase 3.6 regression: cross-source canonical-key dedup
# ---------------------------------------------------------------------------


class TestCrossSourceCanonicalKeyDedup:
    """Regression: when two sources catalog the same upstream repo under
    different slugs, the second source must merge into the first
    entity rather than creating a duplicate at its own slug path.

    The current pulsemcp scraper (Phase 2b.5) does not extract
    github_url from listing pages — that's Phase 6 detail-page work.
    These tests use hand-crafted records that DO carry github_url to
    prove the dedup mechanism. Phase 6's enrichment will then make the
    pulsemcp side benefit automatically.
    """

    def test_normalize_github_url_strips_trailing_slash_and_lowercases(self) -> None:
        from mcp_add import _normalize_github_url  # noqa: PLC0415

        assert _normalize_github_url(
            "https://GitHub.com/Org/Repo/"
        ) == "https://github.com/org/repo"

    def test_normalize_returns_none_for_non_github(self) -> None:
        from mcp_add import _normalize_github_url  # noqa: PLC0415

        assert _normalize_github_url("https://gitlab.com/org/repo") is None
        assert _normalize_github_url("https://example.com/foo") is None
        assert _normalize_github_url(None) is None
        assert _normalize_github_url("") is None

    def test_find_existing_returns_none_for_empty_dir(self, tmp_path: Path) -> None:
        from mcp_add import _find_existing_by_github_url  # noqa: PLC0415

        # Directory doesn't exist → None (no scan needed)
        assert _find_existing_by_github_url(
            tmp_path / "missing", "https://github.com/foo/bar"
        ) is None

    def test_find_existing_matches_by_canonical_url(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        from mcp_add import _find_existing_by_github_url  # noqa: PLC0415

        # Add an entity with github_url = .../org/repo
        record = _make_record(
            name="awesome-cataloged-repo",
            github_url="https://github.com/Org/Repo",
        )
        patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)

        # Search for the same URL with different casing + trailing slash
        mcp_dir = wiki_dir / "entities" / "mcp-servers"
        match = _find_existing_by_github_url(
            mcp_dir, "https://GITHUB.com/org/repo/"
        )
        assert match is not None
        assert match.name == "awesome-cataloged-repo.md"

    def test_second_source_merges_into_first_entity_path(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        # awesome-mcp adds the repo first under its name-derived slug.
        record_awesome = _make_record(
            name="modelcontextprotocol/servers",
            github_url="https://github.com/modelcontextprotocol/servers",
            sources=["awesome-mcp"],
        )
        result1 = patched_mcp_add.add_mcp(record=record_awesome, wiki_path=wiki_dir)
        assert result1["is_new_page"] is True
        first_path = Path(result1["path"])

        # pulsemcp finds the same repo under a different slug.
        record_pulsemcp = _make_record(
            name="modelcontextprotocol-servers-mcp",
            github_url="https://github.com/modelcontextprotocol/servers",
            sources=["pulsemcp"],
        )
        result2 = patched_mcp_add.add_mcp(record=record_pulsemcp, wiki_path=wiki_dir)

        # Critical: the second add merged into the FIRST entity's path,
        # not its own slug-based path.
        assert result2["is_new_page"] is False
        assert result2["path"] == str(first_path)
        assert result2["merged_sources"] == ["awesome-mcp", "pulsemcp"]

        # And only ONE entity file exists in the wiki.
        all_entities = list(
            (wiki_dir / "entities" / "mcp-servers").rglob("*.md")
        )
        assert len(all_entities) == 1, f"expected 1 entity, found {all_entities}"

    def test_records_without_github_url_still_use_slug_dedup(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        # When the new record has no github_url (e.g. pulsemcp listing
        # records before Phase 6 detail enrichment), canonical-key
        # dedup is skipped and slug-based dedup applies.
        record = _make_record(
            name="no-github-mcp",
            github_url=None,
            sources=["pulsemcp"],
        )
        result1 = patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert result1["is_new_page"] is True

        # Same slug → existence check fires (Phase 3.5), merges by slug.
        result2 = patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert result2["is_new_page"] is False

    def test_non_github_homepage_url_does_not_collide(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        # A pulsemcp record with homepage_url = pulsemcp.com/servers/...
        # must NOT match an awesome-mcp record with the same string in
        # a different field — only github_url is the canonical key.
        record_aw = _make_record(
            name="aw-record",
            github_url="https://github.com/foo/bar",
            sources=["awesome-mcp"],
        )
        patched_mcp_add.add_mcp(record=record_aw, wiki_path=wiki_dir)

        # Different repo, no github_url at all → must create separate entity.
        record_ps = _make_record(
            name="ps-record",
            github_url=None,
            sources=["pulsemcp"],
        )
        result = patched_mcp_add.add_mcp(record=record_ps, wiki_path=wiki_dir)
        assert result["is_new_page"] is True
        assert "ps-record" in result["path"]


# ---------------------------------------------------------------------------
# Numeric slug sharding (0-9 bucket)
# ---------------------------------------------------------------------------


class TestCorpusTextStructure:
    """Regression: _build_corpus_text must produce SKILL.md-shaped markdown.

    The intake gate's structural check (intake_gate._check_structure)
    rejects raw_md without YAML frontmatter, name+description fields,
    and H1+H2 in the body. Real-wiki ingest uncovered that the original
    plain-text corpus blob was rejected with FRONTMATTER_MISSING. These
    tests pin the SKILL.md shape so the regression cannot return.
    """

    def test_corpus_text_passes_structural_check(self) -> None:
        from intake_gate import IntakeConfig, _check_structure, _parse_candidate
        from mcp_add import _build_corpus_text

        # Realistic-length description so the body clears the 120-char
        # min_body_chars gate. Real MCP records from awesome-mcp /
        # pulsemcp routinely have descriptions of this length or longer.
        record = _make_record(
            name="github-mcp",
            description=(
                "Repository management, file operations, and GitHub API "
                "integration via the Model Context Protocol."
            ),
        )
        text = _build_corpus_text(record)
        parsed = _parse_candidate(text)

        # All four structural checks pass: frontmatter present, name +
        # description fields populated, H1 and H2 in body.
        findings = _check_structure(parsed, IntakeConfig())
        assert findings == [], f"unexpected structural failures: {findings}"

    def test_corpus_text_has_frontmatter_with_required_fields(self) -> None:
        from wiki_utils import parse_frontmatter_and_body
        from mcp_add import _build_corpus_text

        record = _make_record(name="github-mcp")
        fm, body = parse_frontmatter_and_body(_build_corpus_text(record))
        assert fm.get("name") == "github-mcp"
        assert fm.get("description")
        assert "# " in body  # H1
        assert "## " in body  # H2

    def test_yaml_injection_in_description_does_not_corrupt_frontmatter(self) -> None:
        # Regression: malicious description with embedded ``\n---\n`` or
        # YAML key syntax must NOT escape the frontmatter block when
        # parsed by a real YAML parser (which is what generate_mcp_page
        # and the wiki entity writer use). The fix uses yaml.safe_dump
        # so the dangerous content is stored as a properly-escaped
        # scalar rather than naked interpolation.
        import re
        from mcp_add import _build_corpus_text

        record = _make_record(
            name="evil-mcp",
            description=(
                "Innocent description.\n---\nname: hijacked\n---\n"
                "More text. Long enough to clear the body length gate "
                "for the structural intake check."
            ),
        )
        text = _build_corpus_text(record)

        # Extract the frontmatter block by the real YAML fences and
        # parse it with PyYAML — the same parser that generate_mcp_page
        # downstream relies on for the on-disk entity page.
        m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        assert m is not None, "frontmatter fences missing"
        fm = yaml.safe_load(m.group(1))
        assert isinstance(fm, dict)
        # The injected key must NOT appear as a real frontmatter field.
        assert fm.get("name") == "evil-mcp"
        # And the injected description must be the literal string,
        # quoted/escaped, not parsed as YAML structure.
        assert "hijacked" in fm.get("description", "")
        assert isinstance(fm.get("description"), str)

    def test_short_description_correctly_rejected_by_intake(self) -> None:
        # Documents the inverse: very short descriptions trip
        # BODY_TOO_SHORT, which is the intake gate working as intended.
        # MCPs from sources with bare-bones metadata will be flagged.
        from intake_gate import IntakeConfig, _check_structure, _parse_candidate
        from mcp_add import _build_corpus_text

        record = _make_record(name="bare", description="too short")
        text = _build_corpus_text(record)
        parsed = _parse_candidate(text)
        findings = _check_structure(parsed, IntakeConfig())
        codes = {f.code for f in findings}
        assert "BODY_TOO_SHORT" in codes


class TestAddMcpNumericSharding:
    def test_numeric_slug_lands_in_0_9_directory(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record = _make_record(
            name="007-mcp",
            github_url="https://github.com/Org/007-mcp",
        )
        patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)

        expected = wiki_dir / "entities" / "mcp-servers" / "0-9" / "007-mcp.md"
        assert expected.exists(), (
            f"Numeric-slug entity file not found at {expected}"
        )

    def test_numeric_slug_result_is_new_page_true(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        record = _make_record(
            name="007-mcp",
            github_url="https://github.com/Org/007-mcp",
        )
        result = patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert result["is_new_page"] is True


# ---------------------------------------------------------------------------
# Fixture-based smoke test
# ---------------------------------------------------------------------------


class TestAddMcpFromFixtures:
    def test_github_fixture_can_be_added(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "mcp_github.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        record = McpRecord.from_dict(data)
        result = patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert result["is_new_page"] is True

    def test_pulsemcp_fixture_can_be_added(
        self, patched_mcp_add: Any, wiki_dir: Path
    ) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "mcp_pulsemcp.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        record = McpRecord.from_dict(data)
        result = patched_mcp_add.add_mcp(record=record, wiki_path=wiki_dir)
        assert result["is_new_page"] is True
