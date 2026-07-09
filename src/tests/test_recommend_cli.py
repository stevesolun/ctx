from __future__ import annotations

import json

import ctx.cli.recommend as recommend_cli


def test_recommend_cli_text(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        recommend_cli,
        "recommend_bundle",
        lambda query, **kwargs: [
            {
                "id": "skill:fastapi-pro",
                "name": "fastapi-pro",
                "type": "skill",
                "normalized_score": 0.91,
                "matching_tags": ["python", "api"],
                "selection_state": "suggested",
                "tldr": "Build FastAPI services safely.",
                "reason": "matches python, api",
            }
        ],
    )

    exit_code = recommend_cli.main(["build", "api", "--top-k", "5"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "fastapi-pro" in captured.out
    assert "score=0.910" in captured.out
    assert "id=skill:fastapi-pro" in captured.out
    assert "Build FastAPI services safely." in captured.out
    assert "reason=matches python, api" in captured.out
    assert captured.err == ""


def test_recommend_cli_text_shows_workflow_action(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        recommend_cli,
        "recommend_bundle",
        lambda query, **kwargs: [
            {
                "name": "no-mistakes",
                "type": "skill",
                "normalized_score": 0.95,
                "matching_tags": ["git", "validation"],
                "category": "workflow",
                "invoke_command": 'no-mistakes axi run --intent "<intent>"',
            }
        ],
    )

    exit_code = recommend_cli.main(["git", "validation", "--top-k", "5"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "no-mistakes" in captured.out
    assert "category=workflow" in captured.out
    assert 'run=no-mistakes axi run --intent "<intent>"' in captured.out


def test_recommend_cli_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        recommend_cli,
        "recommend_bundle",
        lambda query, **kwargs: [{"name": "code-reviewer", "type": "agent"}],
    )

    exit_code = recommend_cli.main(["review", "code", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["query"] == "review code"
    assert payload["results"][0]["name"] == "code-reviewer"


def test_recommend_cli_json_includes_selection_payload(monkeypatch, capsys) -> None:
    calls: list[tuple[list[str], list[str], int]] = []

    monkeypatch.setattr(
        recommend_cli,
        "recommend_bundle",
        lambda query, **kwargs: [{"id": "skill:fastapi-pro", "name": "fastapi-pro"}],
    )

    def fake_recommend_related(
        selected: list[str],
        *,
        rejected: list[str],
        top_n: int,
    ) -> list[dict[str, str]]:
        calls.append((selected, rejected, top_n))
        return [{"id": "agent:api-reviewer", "name": "api-reviewer"}]

    monkeypatch.setattr(recommend_cli, "recommend_related", fake_recommend_related)

    exit_code = recommend_cli.main(
        [
            "build",
            "api",
            "--selected",
            "skill:fastapi-pro, skill:fastapi-pro",
            "--rejected",
            "mcp:legacy-api",
            "--related-top-n",
            "2",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert calls == [(["skill:fastapi-pro"], ["mcp:legacy-api"], 2)]
    assert payload["selection"] == {
        "selected": ["skill:fastapi-pro"],
        "rejected": ["mcp:legacy-api"],
        "active_context": [],
        "baseline_context": [],
        "related_results": [{"id": "agent:api-reviewer", "name": "api-reviewer"}],
    }


def test_recommend_cli_related_excludes_session_context(monkeypatch, capsys) -> None:
    calls: list[tuple[list[str], list[str], int]] = []

    monkeypatch.setattr(
        recommend_cli,
        "recommend_bundle",
        lambda query, **kwargs: [{"id": "skill:new-skill", "name": "new-skill"}],
    )

    def fake_recommend_related(
        selected: list[str],
        *,
        rejected: list[str],
        top_n: int,
    ) -> list[dict[str, str]]:
        calls.append((selected, rejected, top_n))
        return []

    monkeypatch.setattr(recommend_cli, "recommend_related", fake_recommend_related)

    exit_code = recommend_cli.main(
        [
            "build",
            "api",
            "--selected",
            "skill:fastapi-pro",
            "--rejected",
            "skill:skip",
            "--active",
            "mcp-server:codex-cli",
            "--baseline-context",
            "skill:baseline-helper",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert calls == [
        (
            ["skill:fastapi-pro"],
            ["skill:skip", "mcp-server:codex-cli", "skill:baseline-helper"],
            5,
        )
    ]
    assert payload["selection"]["related_results"] == []


def test_recommend_cli_suppresses_primary_bundle_with_session_state(monkeypatch, capsys) -> None:
    bundle_calls: list[tuple[str, dict[str, object]]] = []

    def fake_recommend_bundle(query: str, **kwargs: object) -> list[dict[str, str]]:
        bundle_calls.append((query, kwargs))
        return [{"id": "skill:new-skill", "name": "new-skill"}]

    monkeypatch.setattr(recommend_cli, "recommend_bundle", fake_recommend_bundle)
    monkeypatch.setattr(recommend_cli, "recommend_related", lambda *args, **kwargs: [])

    exit_code = recommend_cli.main(
        [
            "build",
            "api",
            "--selected",
            "skill:fastapi-pro",
            "--rejected",
            "skill:skip",
            "--active",
            "mcp-server:codex-cli",
            "--baseline-context",
            "mcp-server:codex-cli",
            "--local-code-task",
            "--no-api-keys",
            "--language",
            "python",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["results"] == [{"id": "skill:new-skill", "name": "new-skill"}]
    assert bundle_calls == [
        (
            "build api",
            {
                "top_k": 5,
                "selected": ["skill:fastapi-pro"],
                "rejected": ["skill:skip"],
                "active_context": ["mcp-server:codex-cli"],
                "baseline_context": ["mcp-server:codex-cli"],
                "local_code_task": True,
                "no_api_keys": True,
                "language": "python",
            },
        )
    ]


def test_recommend_cli_text_renders_related_recommendations(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        recommend_cli,
        "recommend_bundle",
        lambda query, **kwargs: [
            {"id": "skill:fastapi-pro", "name": "fastapi-pro", "type": "skill"}
        ],
    )
    monkeypatch.setattr(
        recommend_cli,
        "recommend_related",
        lambda selected, *, rejected, top_n: [
            {
                "id": "agent:api-reviewer",
                "name": "api-reviewer",
                "type": "agent",
                "normalized_score": 0.82,
                "selection_state": "suggested_related",
                "related_to": ["fastapi-pro"],
                "reason": "related to selected skill",
            }
        ],
    )

    exit_code = recommend_cli.main(
        ["build", "api", "--selected", "skill:fastapi-pro", "--rejected", "skill:skip"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Related recommendations:" in captured.out
    assert "api-reviewer" in captured.out
    assert "state=suggested_related" in captured.out
    assert "related_to=['fastapi-pro']" in captured.out
    assert "reason=related to selected skill" in captured.out


def test_recommend_cli_empty_prints_threshold_message(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        recommend_cli,
        "recommend_bundle",
        lambda query, **kwargs: [],
    )

    exit_code = recommend_cli.main(["unclear"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert "configured score threshold" in captured.err
