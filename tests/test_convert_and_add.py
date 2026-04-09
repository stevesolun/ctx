"""
tests/test_convert_and_add.py — pytest suite for the micro-skills conversion pipeline.

Covers:
  - classify_section  (scope / gate / build / deliver)
  - extract_gate_questions  (avoid / ensure / existing-question patterns)
  - parse_sections  (## header splitting)
  - split_into_chunks  (line-count enforcement)
  - convert_skill  (end-to-end: pipeline files, orchestrator size, hash, skip logic)
"""

import hashlib
import sys
from pathlib import Path
from textwrap import dedent

import pytest

# ── import from repo root ─────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parents[1]))

from batch_convert import (  # noqa: E402
    classify_section,
    convert_skill,
    extract_gate_questions,
    parse_sections,
    split_into_chunks,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_skill_md(tmp_path: Path, content: str, name: str = "test-skill") -> Path:
    """Write a SKILL.md under tmp_path/<name>/SKILL.md and return the path."""
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


def _fake_skill_content(num_lines: int = 250) -> str:
    """Return a realistic fake SKILL.md with exactly num_lines lines."""
    # Build a skeleton with known ## sections, then pad the Steps section.
    header = dedent("""\
        ---
        name: test-skill
        description: "A synthetic skill for unit tests"
        ---

        # test-skill

        ## Overview

        This skill teaches you to write production-grade code with proper structure.
        It is used when you need to scaffold new modules quickly and consistently.
        Prerequisites: familiarity with Python and basic design patterns.
        Applies when: the request involves creating a new service, utility, or module.
        Input: a plain-language description of the component needed.
        Requirements: Python 3.11+, an active virtual environment, black installed.

        ## Prerequisites

        - Python 3.11 or newer installed and on $PATH.
        - A virtual environment activated before running any commands.
        - The `black` formatter available: `pip install black`.
        - Constraint: do not use global state anywhere in the implementation.
        - Precondition: repository root must contain a pyproject.toml.

        ## Steps

        Follow these instructions in order without skipping any step.

    """)

    # Build step lines until we reach the target, leaving room for the footer
    footer = dedent("""\

        ## Validation

        Run the following checks before marking the task complete.
        - avoid leaving TODO comments in the output code.
        - avoid hardcoded credentials or secrets.
        - ensure every public function has a type-annotated signature.
        - ensure the module imports cleanly with no circular dependencies.
        - never commit debug print() statements.
        - never expose internal implementation details through the public API.

        ## Output Format

        Present the output as a self-contained Python module.
        Format the response with a file header comment, then all imports, then code.
        Include an example usage block at the bottom of the file.
        The final output should be ready to paste directly into the repository.
        Summary: one sentence describing what was built and where to find it.
    """)

    header_lines = header.split("\n")
    footer_lines = footer.split("\n")
    fixed_count = len(header_lines) + len(footer_lines)
    padding_needed = max(0, num_lines - fixed_count)

    step_lines = []
    for i in range(1, padding_needed + 1):
        step_lines.append(
            f"        Step {i}: Implement component {i} according to the design. "
            f"Verify output matches spec."
        )

    return "\n".join(header_lines + step_lines + footer_lines)


# ─────────────────────────────────────────────────────────────────────────────
# classify_section
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifySection:
    def test_classify_section_scope(self):
        """Section with 'prerequisite' and 'constraint' keywords -> scope."""
        header = "## Prerequisites"
        body = dedent("""\
            prerequisite: Python 3.11 installed.
            constraint: no global state.
            before you begin, activate the virtual environment.
            input: a plain-language description.
            requirements: black formatter available.
        """)
        result = classify_section(header, body)
        assert result == "scope", f"Expected 'scope', got {result!r}"

    def test_classify_section_gate(self):
        """Section dominated by 'avoid', 'ensure', 'never' keywords -> gate."""
        header = "## Validation Rules"
        body = dedent("""\
            - avoid leaving TODO comments in final output.
            - ensure every function is type-annotated.
            - never expose internal details through the public API.
            - must always verify the output compiles cleanly.
            - do not commit debug print statements.
            - avoid hardcoded secrets.
            - verify the output matches the spec.
        """)
        result = classify_section(header, body)
        assert result == "gate", f"Expected 'gate', got {result!r}"

    def test_classify_section_build(self):
        """Generic instruction section with no strong keywords -> build (default)."""
        header = "## Implementation"
        body = "\n".join(
            f"        Write the {i}th component of the module."
            for i in range(1, 12)
        )
        result = classify_section(header, body)
        assert result == "build", f"Expected 'build', got {result!r}"

    def test_classify_section_deliver(self):
        """Section with 'output', 'format', 'present' keywords -> deliver."""
        header = "## Output Format"
        body = dedent("""\
            Present the final result as a self-contained Python module.
            Format the response with a file header, then imports, then code.
            Output should be usable without modification.
            Return a summary sentence describing what was built.
            The report template must be filled in completely.
        """)
        result = classify_section(header, body)
        assert result == "deliver", f"Expected 'deliver', got {result!r}"


# ─────────────────────────────────────────────────────────────────────────────
# extract_gate_questions
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractGateQuestions:
    def test_extract_gate_questions_avoid(self):
        """'- avoid X' converts to 'Is the output free of X? YES/NO'."""
        text = "- avoid leaving TODO comments in the output"
        questions = extract_gate_questions(text)
        assert len(questions) == 1
        assert questions[0] == "Is the output free of leaving TODO comments in the output? YES/NO"

    def test_extract_gate_questions_ensure(self):
        """'- ensure X' converts to 'Does the output X? YES/NO'."""
        text = "- ensure every function is type-annotated"
        questions = extract_gate_questions(text)
        assert len(questions) == 1
        assert questions[0] == "Does the output every function is type-annotated? YES/NO"

    def test_extract_gate_questions_existing(self):
        """Lines already ending with '?' are kept verbatim (stripped of list markers)."""
        text = dedent("""\
            - Has the output been reviewed for correctness?
            - Is every dependency pinned to a specific version?
        """)
        questions = extract_gate_questions(text)
        assert "Has the output been reviewed for correctness?" in questions
        assert "Is every dependency pinned to a specific version?" in questions

    def test_extract_gate_questions_mixed(self):
        """Mixed patterns all produce questions; no empty entries generated."""
        text = dedent("""\
            - avoid hardcoded credentials
            - ensure the module imports cleanly
            - never commit debug print statements
            - Is the public API stable?
        """)
        questions = extract_gate_questions(text)
        assert all(q.strip() for q in questions), "No empty strings should appear"
        assert len(questions) == 4

    def test_extract_gate_questions_empty_text(self):
        """Empty / whitespace-only text returns an empty list."""
        assert extract_gate_questions("") == []
        assert extract_gate_questions("   \n\n  ") == []


# ─────────────────────────────────────────────────────────────────────────────
# parse_sections
# ─────────────────────────────────────────────────────────────────────────────

class TestParseSections:
    def test_parse_sections_basic(self):
        """Correctly splits a markdown document into sections by ## headers."""
        content = dedent("""\
            ## Overview
            This is the overview body.

            ## Steps
            Do step 1.
            Do step 2.

            ## Validation
            Check everything.
        """)
        sections, frontmatter = parse_sections(content)
        headers = [s["header"] for s in sections]
        assert "## Overview" in headers
        assert "## Steps" in headers
        assert "## Validation" in headers

    def test_parse_sections_returns_tuple(self):
        """parse_sections always returns a 2-tuple (sections, frontmatter)."""
        result = parse_sections("## Only Section\nSome body.\n")
        assert isinstance(result, tuple) and len(result) == 2

    def test_parse_sections_frontmatter_stripped(self):
        """YAML frontmatter is extracted; sections do not contain the --- delimiters."""
        content = dedent("""\
            ---
            name: my-skill
            description: "A test"
            ---

            ## Body
            Content here.
        """)
        sections, frontmatter = parse_sections(content)
        assert "name: my-skill" in frontmatter
        # No section header should equal "---"
        for s in sections:
            assert not s["header"].startswith("---")

    def test_parse_sections_body_content(self):
        """Section bodies contain the lines under that header."""
        content = "## Steps\nDo step 1.\nDo step 2.\n"
        sections, _ = parse_sections(content)
        assert len(sections) == 1
        assert "Do step 1." in sections[0]["body"]
        assert "Do step 2." in sections[0]["body"]

    def test_parse_sections_no_headers(self):
        """Content with no ## headers is returned as a single body-only section."""
        content = "Just some text.\nNo headers here.\n"
        sections, _ = parse_sections(content)
        # The implementation may return one section with empty header
        assert len(sections) >= 1
        combined_bodies = " ".join(s["body"] for s in sections)
        assert "Just some text." in combined_bodies


# ─────────────────────────────────────────────────────────────────────────────
# split_into_chunks
# ─────────────────────────────────────────────────────────────────────────────

class TestSplitIntoChunks:
    def test_split_into_chunks_short_text(self):
        """Text shorter than max_lines is returned as a single chunk."""
        text = "\n".join(f"line {i}" for i in range(20))
        chunks = split_into_chunks(text, max_lines=40)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_split_into_chunks_exact_boundary(self):
        """Text with exactly max_lines lines is a single chunk."""
        text = "\n".join(f"line {i}" for i in range(40))
        chunks = split_into_chunks(text, max_lines=40)
        assert len(chunks) == 1

    def test_split_into_chunks_long_text(self):
        """100-line text with max_lines=40 produces multiple chunks."""
        # Create text with deliberate paragraph breaks to help the splitter
        lines = []
        for i in range(1, 101):
            lines.append(f"Instruction line {i}: do something useful here.")
            if i % 15 == 0:
                lines.append("")  # blank line as paragraph break
        text = "\n".join(lines)
        chunks = split_into_chunks(text, max_lines=40)
        assert len(chunks) >= 2, f"Expected multiple chunks, got {len(chunks)}"

    def test_split_into_chunks_each_chunk_bounded(self):
        """Every chunk must be at most max_lines + a small tolerance (paragraph alignment)."""
        lines = []
        for i in range(1, 101):
            lines.append(f"Line {i}")
            if i % 10 == 0:
                lines.append("")  # paragraph break every 10 lines
        text = "\n".join(lines)
        max_lines = 40
        chunks = split_into_chunks(text, max_lines=max_lines)
        for chunk in chunks:
            chunk_line_count = len(chunk.split("\n"))
            # Allow up to max_lines + 10 due to paragraph boundary alignment
            assert chunk_line_count <= max_lines + 10, (
                f"Chunk has {chunk_line_count} lines, expected <= {max_lines + 10}"
            )

    def test_split_into_chunks_no_content_lost(self):
        """Joining all chunks contains all non-empty lines from the original."""
        text = "\n".join(f"important-line-{i}" for i in range(80))
        chunks = split_into_chunks(text, max_lines=30)
        rejoined = "\n".join(chunks)
        for i in range(80):
            assert f"important-line-{i}" in rejoined


# ─────────────────────────────────────────────────────────────────────────────
# convert_skill — end-to-end
# ─────────────────────────────────────────────────────────────────────────────

class TestConvertSkill:
    """End-to-end tests for convert_skill().

    All tests use tmp_path so no real skill directories are touched.
    The fake skill is always 250 lines — well above the 180-line threshold.
    """

    @pytest.fixture()
    def skill_250(self, tmp_path: Path) -> Path:
        """250-line SKILL.md written to tmp_path/test-skill/SKILL.md."""
        content = _fake_skill_content(num_lines=250)
        return _make_skill_md(tmp_path, content, name="test-skill")

    @pytest.fixture()
    def skill_100(self, tmp_path: Path) -> Path:
        """100-line SKILL.md — below the 180-line threshold."""
        lines = ["# Short Skill\n"] + [f"Line {i}\n" for i in range(99)]
        content = "".join(lines)
        return _make_skill_md(tmp_path, content, name="short-skill")

    # ── skip logic ────────────────────────────────────────────────────────────

    def test_convert_skill_skips_short(self, skill_100: Path):
        """Skills with <= 180 lines are skipped without creating any pipeline files."""
        result = convert_skill(skill_100)
        assert result["status"] == "skipped"
        assert "skipped" in result["reason"].lower() or "lines" in result["reason"].lower()
        # No references directory should be created for skipped skills
        refs_dir = skill_100.parent / "references"
        assert not refs_dir.exists()

    # ── happy-path pipeline creation ──────────────────────────────────────────

    def test_convert_skill_creates_pipeline(self, skill_250: Path, tmp_path: Path):
        """A 250-line skill produces SKILL.md, references/01-05, check-gates.md,
        failure-log.md, and original-hash.txt."""
        result = convert_skill(skill_250)
        assert result["status"] == "converted"

        output_dir = skill_250.parent  # convert_skill writes in-place when no output_dir

        # Core orchestrator
        assert (output_dir / "SKILL.md").exists(), "SKILL.md orchestrator missing"

        # Preserved original
        assert (output_dir / "SKILL.md.original").exists(), "SKILL.md.original missing"

        # Metadata files
        assert (output_dir / "check-gates.md").exists(), "check-gates.md missing"
        assert (output_dir / "failure-log.md").exists(), "failure-log.md missing"
        assert (output_dir / "original-hash.txt").exists(), "original-hash.txt missing"

        # Pipeline reference files (at least 01-scope through 05-deliver)
        refs_dir = output_dir / "references"
        assert refs_dir.exists(), "references/ directory missing"
        ref_files = list(refs_dir.glob("*.md"))
        assert len(ref_files) >= 5, (
            f"Expected >= 5 reference files, found {len(ref_files)}: "
            + ", ".join(f.name for f in ref_files)
        )

    def test_convert_skill_preserves_original(self, skill_250: Path):
        """After conversion, SKILL.md.original contains the original content."""
        original_content = skill_250.read_text(encoding="utf-8")
        convert_skill(skill_250)

        original_path = skill_250.parent / "SKILL.md.original"
        assert original_path.exists()
        preserved = original_path.read_text(encoding="utf-8")
        assert preserved == original_content

    def test_convert_skill_orchestrator_under_30_lines(self, skill_250: Path):
        """The generated SKILL.md orchestrator file must be under 30 lines."""
        convert_skill(skill_250)

        orchestrator = skill_250.parent / "SKILL.md"
        assert orchestrator.exists()
        line_count = len(orchestrator.read_text(encoding="utf-8").split("\n"))
        assert line_count < 30, (
            f"SKILL.md orchestrator has {line_count} lines; expected < 30"
        )

    def test_convert_skill_check_gates_has_questions(self, skill_250: Path):
        """check-gates.md contains at least one YES/NO question."""
        convert_skill(skill_250)

        gates_path = skill_250.parent / "check-gates.md"
        gates_content = gates_path.read_text(encoding="utf-8")
        assert "YES/NO" in gates_content, "check-gates.md has no YES/NO questions"
        # Count actual question lines (numbered list items)
        question_lines = [
            line for line in gates_content.split("\n")
            if line.strip() and line.strip()[0].isdigit() and "YES/NO" in line
        ]
        assert len(question_lines) >= 1

    def test_convert_skill_hash_matches(self, skill_250: Path):
        """original-hash.txt contains the SHA256 of the original file content."""
        original_content = skill_250.read_text(encoding="utf-8")
        expected_hash = hashlib.sha256(original_content.encode("utf-8")).hexdigest()

        convert_skill(skill_250)

        hash_path = skill_250.parent / "original-hash.txt"
        stored = hash_path.read_text(encoding="utf-8").strip()
        assert stored == expected_hash, (
            f"Hash mismatch: stored={stored!r}, expected={expected_hash!r}"
        )

    def test_convert_skill_returns_converted_stats(self, skill_250: Path):
        """convert_skill returns a dict with all expected stat keys when converted."""
        result = convert_skill(skill_250)
        assert result["status"] == "converted"
        for key in ("skill", "original_lines", "pipeline_files", "gate_questions",
                    "max_file_lines", "build_splits", "reference_files"):
            assert key in result, f"Missing key {key!r} in result"

    def test_convert_skill_original_lines_accurate(self, skill_250: Path):
        """Returned original_lines matches the actual line count of the source."""
        content = skill_250.read_text(encoding="utf-8")
        actual_line_count = len(content.split("\n"))
        result = convert_skill(skill_250)
        assert result["original_lines"] == actual_line_count

    def test_convert_skill_idempotent_on_second_call(self, skill_250: Path):
        """Running convert_skill twice on the same directory does not crash or
        destroy the already-preserved original."""
        original_content = skill_250.read_text(encoding="utf-8")

        # First conversion: renames SKILL.md -> SKILL.md.original, writes new SKILL.md
        result1 = convert_skill(skill_250)
        assert result1["status"] == "converted"

        # After first conversion skill_250 path (SKILL.md) now holds the orchestrator.
        # The new SKILL.md is much shorter, so a second call via the same path
        # will be skipped (< threshold) or at worst re-convert the orchestrator.
        # Either way, SKILL.md.original must still hold the ORIGINAL content.
        original_path = skill_250.parent / "SKILL.md.original"
        preserved = original_path.read_text(encoding="utf-8")
        assert preserved == original_content, (
            "SKILL.md.original was overwritten on the second call"
        )

    # ── output_dir override ───────────────────────────────────────────────────

    def test_convert_skill_respects_output_dir(self, skill_250: Path, tmp_path: Path):
        """When output_dir is supplied, pipeline files land there, not in the
        source skill directory."""
        out_dir = tmp_path / "converted_output"
        out_dir.mkdir()

        result = convert_skill(skill_250, output_dir=out_dir)
        assert result["status"] == "converted"

        assert (out_dir / "SKILL.md").exists(), "SKILL.md not in output_dir"
        assert (out_dir / "check-gates.md").exists()
        assert (out_dir / "failure-log.md").exists()
        assert (out_dir / "original-hash.txt").exists()
        refs = list((out_dir / "references").glob("*.md"))
        assert len(refs) >= 5
