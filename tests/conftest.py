"""
conftest.py -- Shared pytest fixtures for the ctx test suite.
"""

import sys
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so imports work from any working dir
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from wiki_sync import ensure_wiki  # noqa: E402  (import after path manipulation)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_root() -> Path:
    """Return the ctx project root directory as a Path."""
    return _PROJECT_ROOT


@pytest.fixture()
def tmp_wiki(tmp_path: Path) -> Path:
    """
    Create a temporary wiki directory using wiki_sync.ensure_wiki.

    The returned Path is the wiki root (tmp_path / "skill-wiki").
    All required subdirectories and seed files are created by ensure_wiki.
    """
    wiki_dir = tmp_path / "skill-wiki"
    ensure_wiki(str(wiki_dir))
    return wiki_dir


@pytest.fixture()
def tmp_skills_dir(tmp_path: Path) -> Path:
    """
    Create a temporary skills directory with three fake SKILL.md files:

    - short-skill/SKILL.md  (~50 lines)
    - medium-skill/SKILL.md (~100 lines)
    - long-skill/SKILL.md   (~250 lines)
    """
    skills_dir = tmp_path / "skills"

    # ── Short skill (~50 lines) ───────────────────────────────────────────
    short_dir = skills_dir / "short-skill"
    short_dir.mkdir(parents=True)
    short_lines = textwrap.dedent("""\
        ---
        title: short-skill
        tags: [python, testing]
        ---

        # Short Skill

        ## Overview
        A short demonstration skill used for testing.

        ## Usage
        Import and call the helper function.

        ## Examples
        ```python
        from short_skill import helper
        helper()
        ```
    """)
    # Pad to ~50 lines
    short_lines += "\n".join(f"# line {i}" for i in range(1, 35)) + "\n"
    (short_dir / "SKILL.md").write_text(short_lines, encoding="utf-8")

    # ── Medium skill (~100 lines) ─────────────────────────────────────────
    medium_dir = skills_dir / "medium-skill"
    medium_dir.mkdir(parents=True)
    medium_lines = textwrap.dedent("""\
        ---
        title: medium-skill
        tags: [python, fastapi]
        ---

        # Medium Skill

        ## Overview
        A medium-length demonstration skill used for testing.

        ## Background
        Provides patterns for FastAPI service construction.

        ## Usage
        Configure the app factory and mount routers.

        ## Configuration
        Set environment variables before startup.

        ## Examples
        ```python
        from medium_skill import create_app
        app = create_app()
        ```

        ## Notes
        Remember to add lifespan handlers.
    """)
    # Pad to ~100 lines
    medium_lines += "\n".join(f"# line {i}" for i in range(1, 73)) + "\n"
    (medium_dir / "SKILL.md").write_text(medium_lines, encoding="utf-8")

    # ── Long skill (~250 lines) ───────────────────────────────────────────
    long_dir = skills_dir / "long-skill"
    long_dir.mkdir(parents=True)
    long_lines = textwrap.dedent("""\
        ---
        title: long-skill
        tags: [python, architecture, patterns]
        ---

        # Long Skill

        ## Overview
        A long demonstration skill used for testing the line_threshold logic.

        ## Stage 1: Discovery
        Locate relevant files in the repository.

        ## Stage 2: Analysis
        Parse and classify each file by type.

        ## Stage 3: Extraction
        Pull out the key patterns and idioms.

        ## Stage 4: Synthesis
        Combine findings into a coherent summary.

        ## Stage 5: Output
        Write the final report to disk.

        ## Configuration Reference
        All options are documented below.

        ## Advanced Usage
        Chain multiple stages for batch processing.

        ## Troubleshooting
        Common issues and their resolutions.

        ## See Also
        Related skills and external references.
    """)
    # Pad to ~250 lines
    long_lines += "\n".join(f"# line {i}" for i in range(1, 210)) + "\n"
    (long_dir / "SKILL.md").write_text(long_lines, encoding="utf-8")

    return skills_dir
