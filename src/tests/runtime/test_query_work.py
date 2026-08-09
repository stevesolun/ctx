from __future__ import annotations

import hashlib
import json

import pytest

from ctx.runtime.query_vocabulary import (
    QUERY_VOCABULARY_SCHEMA,
    AuthenticatedQueryVocabulary,
)
from ctx.runtime.query_work import (
    MAX_QUERY_LANGUAGE_CODEPOINTS,
    MAX_QUERY_TASK_BYTES,
    normalize_query_work,
)


NAMESPACE_DIGEST = hashlib.sha256(b"catalog:local-user").hexdigest()
GRAPH_DIGEST = hashlib.sha256(b"graph:v1").hexdigest()


def _vocabulary(*signals: str) -> AuthenticatedQueryVocabulary:
    ordered = tuple(sorted(signals))
    payload = {
        "catalog_namespace_digest": NAMESPACE_DIGEST,
        "graph_artifact_sha256": GRAPH_DIGEST,
        "schema": QUERY_VOCABULARY_SCHEMA,
        "signals": list(ordered),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return AuthenticatedQueryVocabulary(
        signals=ordered,
        catalog_namespace_digest=NAMESPACE_DIGEST,
        graph_artifact_sha256=GRAPH_DIGEST,
        vocabulary_digest=digest,
    )


def _normalize(
    task: str,
    *,
    language: str = "python",
    vocabulary: AuthenticatedQueryVocabulary,
):
    return normalize_query_work(
        task=task,
        language=language,
        vocabulary=vocabulary,
        expected_catalog_namespace_digest=NAMESPACE_DIGEST,
        expected_graph_artifact_sha256=GRAPH_DIGEST,
    )


def test_keeps_only_authenticated_vocabulary_and_declared_language() -> None:
    work = _normalize(
        (
            "PRIVATE_PATCH_SENTINEL implement `Response.json_or` in "
            "/Users/alice/secret-repository with Python testing"
        ),
        vocabulary=_vocabulary("json", "response", "testing"),
    )

    assert work.languages == ("python",)
    assert work.signals == ("json", "response", "testing")
    assert work.requested_limit == 5
    assert "private" not in repr(work).casefold()
    assert "secret-repository" not in repr(work)
    assert "users" not in repr(work)


def test_paths_and_secret_values_are_removed_even_if_vocabulary_contains_them() -> None:
    secret_value = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    work = _normalize(
        (
            f"OPENAI_API_KEY={secret_value} password: hunter2 "
            "repair /Users/alice/secret-repository parser"
        ),
        language="TypeScript",
        vocabulary=_vocabulary(
            "alice",
            "hunter2",
            "openai-api-key",
            "parser",
            "password",
            "repair",
            "secret-repository",
            "users",
        ),
    )

    serialized = repr(work)
    assert secret_value not in serialized
    assert work.languages == ("typescript",)
    assert work.signals == ("parser", "repair")


def test_relative_quoted_paths_and_flag_or_prose_secrets_are_removed() -> None:
    work = _normalize(
        (
            "inspect customer-mercury/src/parser.py and 'C:\\Program Files\\repo' "
            "--password hunter2; password is swordfish; then validate"
        ),
        vocabulary=_vocabulary(
            "customer-mercury",
            "files",
            "hunter2",
            "parser",
            "repo",
            "src",
            "swordfish",
            "validate",
        ),
    )

    assert work.signals == ("validate",)


def test_wrapped_paths_and_formatted_or_authorization_secrets_are_removed() -> None:
    work = _normalize(
        (
            "`src/customer-mercury/parser.py` <src/customer-mercury/parser.py> "
            "Authorization: Bearer hunter2; password is: swordfish; "
            "**credential** is privatevalue; validate"
        ),
        vocabulary=_vocabulary(
            "credential",
            "customer-mercury",
            "hunter2",
            "parser",
            "privatevalue",
            "src",
            "swordfish",
            "validate",
        ),
    )

    assert work.signals == ("validate",)


def test_mainstream_camelcase_technologies_match_compact_graph_tags() -> None:
    work = _normalize(
        "Add a FastAPI GraphQL OpenAPI PostgreSQL PyTorch endpoint",
        vocabulary=_vocabulary(
            "endpoint",
            "fastapi",
            "graphql",
            "openapi",
            "postgresql",
            "pytorch",
        ),
    )

    assert work.signals == (
        "endpoint",
        "fastapi",
        "graphql",
        "openapi",
        "postgresql",
        "pytorch",
    )


def test_language_aliases_cannot_be_inferred_from_ordinary_prose() -> None:
    with pytest.raises(ValueError, match="language aliases"):
        _vocabulary("go", "testing")

    work = _normalize(
        "please go add testing",
        language="python",
        vocabulary=_vocabulary("testing"),
    )
    assert work.languages == ("python",)
    assert work.signals == ("testing",)


def test_generic_or_unmatched_work_abstains() -> None:
    work = _normalize(
        "please implement the requested change",
        language="C++",
        vocabulary=_vocabulary("cmake", "serialization"),
    )

    assert work.languages == ("cpp",)
    assert work.signals == ()
    assert work.requested_limit == 0


def test_rejects_lexically_colliding_authenticated_signals() -> None:
    with pytest.raises(ValueError, match="lexical collision"):
        _vocabulary("mcp-server", "mcp.server", "validation")

    with pytest.raises(ValueError, match="lexical collision"):
        _vocabulary("mcp-server", "mcpserver")


def test_catalog_bindings_must_match_the_query_composition() -> None:
    vocabulary = _vocabulary("testing")

    with pytest.raises(ValueError, match="catalog binding"):
        normalize_query_work(
            task="python testing",
            language="python",
            vocabulary=vocabulary,
            expected_catalog_namespace_digest="f" * 64,
            expected_graph_artifact_sha256=GRAPH_DIGEST,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("task", 3, "task must be a string"),
        ("language", None, "language must be a string"),
        ("vocabulary", ("python",), "AuthenticatedQueryVocabulary"),
    ],
)
def test_rejects_untyped_inputs(field: str, value: object, message: str) -> None:
    values: dict[str, object] = {
        "task": "python testing",
        "language": "python",
        "vocabulary": _vocabulary("testing"),
        "expected_catalog_namespace_digest": NAMESPACE_DIGEST,
        "expected_graph_artifact_sha256": GRAPH_DIGEST,
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError), match=message):
        normalize_query_work(**values)  # type: ignore[arg-type]


def test_task_input_is_bounded_before_vocabulary_matching() -> None:
    with pytest.raises(ValueError, match="task exceeds"):
        _normalize(
            "x" * (MAX_QUERY_TASK_BYTES + 1),
            vocabulary=_vocabulary("testing"),
        )


def test_declared_language_input_is_bounded() -> None:
    with pytest.raises(ValueError, match="language exceeds"):
        _normalize(
            "testing",
            language="x" * (MAX_QUERY_LANGUAGE_CODEPOINTS + 1),
            vocabulary=_vocabulary("testing"),
        )
