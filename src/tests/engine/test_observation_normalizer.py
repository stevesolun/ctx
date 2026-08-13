from __future__ import annotations

from ctx.engine.observation import normalize_public_current_work
from ctx.engine.planner import BoundedCapabilityPlanner, CapabilityCandidate, WorkObservation


def test_click_task_normalizes_only_bounded_concrete_public_signals() -> None:
    observation = normalize_public_current_work(
        query="implement and review a small public Python Click output helper with focused tests",
        task=(
            "Add a public `click.echo_json(value, file=None, *, sort_keys=True, "
            "ensure_ascii=False)` helper. Serialize with `json.dumps`, use `echo`, emit one "
            "trailing newline, preserve Unicode, and keep serialization errors visible."
        ),
        language="py",
        repo_slug="pallets/click",
    )

    assert observation.languages == ("python",)
    assert observation.signals == (
        "click",
        "dumps",
        "echo",
        "json",
        "output",
        "public",
        "serialization",
        "sort",
    )
    assert observation.requested_limit == 5


def test_requests_task_preserves_repository_and_exception_boundary_concepts() -> None:
    observation = normalize_public_current_work(
        query="implement and review a small Requests Response JSON fallback API with tests",
        task=(
            "Add `Response.json_or(default, **kwargs)`. Return parsed JSON including null; "
            "return the default only for `JSONDecodeError`; forward decoder keyword arguments "
            "and do not swallow unrelated exceptions."
        ),
        language="Python",
        repo_slug="psf/requests",
    )

    assert observation.languages == ("python",)
    assert observation.signals == (
        "api",
        "decoding",
        "fallback",
        "json",
        "jsondecodeerror",
        "keyword",
        "requests",
        "response",
    )


def test_language_aliases_identifier_splitting_and_repo_facts_are_canonical() -> None:
    observation = normalize_public_current_work(
        query="repair parseHTTPResponse",
        task="Update `HTTPClient.parse_response` safely.",
        language="C++",
        repo_slug="acme/http-client",
        repo_facts=("CMake", "HTTP Client", "cmake"),
    )

    assert observation.languages == ("cpp",)
    assert observation.signals == (
        "client",
        "cmake",
        "http",
        "parse",
        "response",
    )


def test_language_without_concrete_work_abstains_before_catalog_access() -> None:
    observation = normalize_public_current_work(
        query="please implement this feature",
        task="make the requested change",
        language="typescript",
        repo_slug="",
    )

    assert observation.languages == ("typescript",)
    assert observation.signals == ()
    assert observation.requested_limit == 0

    class ForbiddenSource:
        catalog_snapshot_digest = "0" * 64

        def retrieve(self, _observation: WorkObservation) -> tuple[CapabilityCandidate, ...]:
            raise AssertionError("catalog must not be queried for generic work")

    plan = BoundedCapabilityPlanner(ForbiddenSource()).plan(observation)
    assert (plan.status, plan.abstention_code) == (
        "abstained",
        "no-relevant-capability",
    )


def test_normalizer_does_not_retain_raw_private_or_workflow_prose() -> None:
    secret = "PRIVATE_REFERENCE_PATCH_SENTINEL"
    observation = normalize_public_current_work(
        query="review the implementation",
        task=f"{secret} add `safe_decode` for Unicode decoding",
        language="python",
        repo_slug="org/repo",
    )

    serialized = repr(observation)
    assert secret not in serialized
    assert "private" not in observation.signals
    assert "reference" not in observation.signals
    assert observation.signals == ("decoding", "unicode")
