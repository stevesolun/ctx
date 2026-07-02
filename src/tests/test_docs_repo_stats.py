from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_docs_refresh_github_repo_stats_at_runtime() -> None:
    mkdocs_config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")

    assert "repo_url: https://github.com/stevesolun/ctx" in mkdocs_config
    assert "repo_name: stevesolun/ctx" in mkdocs_config
    assert "assets/javascripts/repo-stats-refresh.js" in mkdocs_config

    script = (ROOT / "docs" / "assets" / "javascripts" / "repo-stats-refresh.js").read_text(
        encoding="utf-8"
    )

    assert "https://api.github.com/repos/stevesolun/ctx" in script
    assert "__source" in script
    assert "stargazers_count" in script
    assert "forks_count" in script
    assert not re.search(r"\bstargazers_count\s*\|\|\s*[1-9]\d*\b", script)
    assert not re.search(r"\bforks_count\s*\|\|\s*[1-9]\d*\b", script)
    assert not re.search(r"\b(?:stars|forks)\s*:\s*\d+\b", script)
