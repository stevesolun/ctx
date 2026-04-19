"""
test_wiki_orchestrator_security.py -- Security regression tests for wiki_orchestrator.py.

Verifies that the --add flag input validation:
  1. Rejects path traversal sequences (../../etc/passwd).
  2. Rejects absolute paths that resolve outside the configured skills directory.
  3. Accepts valid skill slugs and paths inside the skills directory.
  4. Rejects names with invalid characters (uppercase, spaces, special chars).
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure src/ is importable
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wiki_orchestrator import _resolve_add_name  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_skills_dir(tmp_path: Path):
    """Context manager: patch cfg.skills_dir to a known temp directory."""
    mock_cfg = MagicMock()
    mock_cfg.skills_dir = tmp_path / "skills"
    mock_cfg.skills_dir.mkdir(parents=True, exist_ok=True)
    return patch("wiki_orchestrator.cfg", mock_cfg)


# ---------------------------------------------------------------------------
# Path traversal — should be rejected
# ---------------------------------------------------------------------------


def test_traversal_relative_dotdot(tmp_path: Path) -> None:
    """../../etc/passwd must be rejected regardless of cwd."""
    with _patch_skills_dir(tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            _resolve_add_name("../../etc/passwd")
    assert exc_info.value.code == 1


def test_traversal_with_md_extension(tmp_path: Path) -> None:
    """Traversal ending in .md should also be caught by the directory check."""
    with _patch_skills_dir(tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            _resolve_add_name("../../etc/shadow.md")
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Absolute path outside skills dir — should be rejected
# ---------------------------------------------------------------------------


def test_absolute_path_outside_skills_dir(tmp_path: Path) -> None:
    """An absolute path that does not live under skills_dir must be rejected."""
    outside = tmp_path / "other-dir" / "my-skill.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    with _patch_skills_dir(tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            _resolve_add_name(str(outside))
    assert exc_info.value.code == 1


def test_windows_style_outside_path(tmp_path: Path) -> None:
    """A path with backslashes outside the skills dir must also be rejected."""
    outside = tmp_path / "not-skills" / "evil-skill.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    # Use backslash notation to exercise Windows path detection branch.
    raw = str(outside).replace("/", "\\")
    with _patch_skills_dir(tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            _resolve_add_name(raw)
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Valid bare name — should pass through
# ---------------------------------------------------------------------------


def test_valid_bare_name_passes(tmp_path: Path) -> None:
    """A valid lowercase slug with no path separators should be accepted."""
    with _patch_skills_dir(tmp_path):
        result = _resolve_add_name("valid-skill-name")
    assert result == "valid-skill-name"


def test_valid_bare_name_alphanumeric(tmp_path: Path) -> None:
    """Alphanumeric slugs without hyphens are also valid."""
    with _patch_skills_dir(tmp_path):
        result = _resolve_add_name("mypythontool")
    assert result == "mypythontool"


# ---------------------------------------------------------------------------
# Valid path inside skills dir — should pass through
# ---------------------------------------------------------------------------


def test_valid_path_inside_skills_dir(tmp_path: Path) -> None:
    """A path inside skills_dir should resolve to the directory slug correctly."""
    mock_skills_dir = tmp_path / "skills"
    mock_skills_dir.mkdir(parents=True, exist_ok=True)
    # Use a lowercase slug directory containing a lowercase filename.
    skill_file = mock_skills_dir / "my-skill.md"
    skill_file.touch()

    mock_cfg = MagicMock()
    mock_cfg.skills_dir = mock_skills_dir

    with patch("wiki_orchestrator.cfg", mock_cfg):
        result = _resolve_add_name(str(skill_file))

    assert result == "my-skill"


def test_valid_skill_dir_path(tmp_path: Path) -> None:
    """A .md file directly inside skills_dir should yield a valid slug."""
    mock_skills_dir = tmp_path / "skills"
    mock_skills_dir.mkdir(parents=True, exist_ok=True)
    skill_file = mock_skills_dir / "fastapi-pro.md"
    skill_file.touch()

    mock_cfg = MagicMock()
    mock_cfg.skills_dir = mock_skills_dir

    with patch("wiki_orchestrator.cfg", mock_cfg):
        result = _resolve_add_name(str(skill_file))

    assert result == "fastapi-pro"


# ---------------------------------------------------------------------------
# Invalid name characters — should be rejected
# ---------------------------------------------------------------------------


def test_uppercase_bare_name_rejected(tmp_path: Path) -> None:
    """Uppercase names violate the slug contract and must be rejected."""
    with _patch_skills_dir(tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            _resolve_add_name("MySkill")
    assert exc_info.value.code == 1


def test_space_in_name_rejected(tmp_path: Path) -> None:
    """Names with spaces are not valid slugs."""
    with _patch_skills_dir(tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            _resolve_add_name("my skill")
    assert exc_info.value.code == 1


def test_leading_hyphen_rejected(tmp_path: Path) -> None:
    """Names starting with a hyphen are rejected by the slug regex."""
    with _patch_skills_dir(tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            _resolve_add_name("-bad-start")
    assert exc_info.value.code == 1


def test_null_byte_injection_rejected(tmp_path: Path) -> None:
    """Names containing null bytes must be rejected."""
    with _patch_skills_dir(tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            _resolve_add_name("skill\x00name")
    assert exc_info.value.code == 1
