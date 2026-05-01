from __future__ import annotations

import json

import ctx.cli.recommend as recommend_cli


def test_recommend_cli_text(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        recommend_cli,
        "recommend_bundle",
        lambda query, *, top_k: [
            {
                "name": "fastapi-pro",
                "type": "skill",
                "normalized_score": 0.91,
                "matching_tags": ["python", "api"],
            }
        ],
    )

    exit_code = recommend_cli.main(["build", "api", "--top-k", "5"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "fastapi-pro" in captured.out
    assert "score=0.910" in captured.out
    assert captured.err == ""


def test_recommend_cli_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        recommend_cli,
        "recommend_bundle",
        lambda query, *, top_k: [{"name": "code-reviewer", "type": "agent"}],
    )

    exit_code = recommend_cli.main(["review", "code", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["query"] == "review code"
    assert payload["results"][0]["name"] == "code-reviewer"


def test_recommend_cli_empty_prints_threshold_message(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        recommend_cli,
        "recommend_bundle",
        lambda query, *, top_k: [],
    )

    exit_code = recommend_cli.main(["unclear"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert "configured score threshold" in captured.err
