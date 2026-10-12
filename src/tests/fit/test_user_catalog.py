from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ctx.cli.run import main as ctx_main
from ctx.engine.planner import BoundedCapabilityPlanner, WorkObservation
from ctx.fit.candidates import (
    MAX_CANDIDATE_USER_CONTEXT_BYTES,
    generate_candidates,
    render_candidate_user_context,
)
from ctx.fit.experiment import ModelPrice, authorize_experiment, resolve_experiment, run_experiment
from ctx.fit.profile import build_fit_profile
from ctx.fit.release_catalog import (
    MAX_USER_CATALOG_BYTES,
    USER_CATALOG_SCHEMA,
    open_release_candidate_source,
    open_user_candidate_source,
)


def _entry(**overrides: object) -> dict[str, object]:
    return {
        "id": "skill:local-testing",
        "type": "skill",
        "signals": ["python", "test"],
        "content": "---\nname: local-testing\n---\n\nCheck boundary behavior.\n",
        **overrides,
    }


def _catalog(tmp_path: Path, entries: list[dict[str, object]] | None = None) -> Path:
    path = tmp_path / "user-catalog.json"
    path.write_text(
        json.dumps(
            {"schema": USER_CATALOG_SCHEMA, "entries": entries if entries is not None else [_entry()]}
        ),
        encoding="utf-8",
    )
    return path


def test_default_stays_empty_while_explicit_material_can_be_evaluated(
    tmp_path: Path, repo_with_history
) -> None:
    profile = build_fit_profile(repo_with_history(commits=3))
    assert open_release_candidate_source() is None
    assert resolve_experiment(profile, budget_usd=500).plan.decision == "blocked-no-candidates"

    path = _catalog(tmp_path)
    experiment = resolve_experiment(profile, catalog_path=path, budget_usd=500)

    assert experiment.plan.decision == "ready"
    assert experiment.can_authorize
    assert not experiment.can_execute
    treatment = experiment.candidates.candidates[1]
    assert treatment.capability_ids == ("skill:local-testing",)
    assert treatment.capability_materials[0].content == _entry()["content"]
    assert treatment.capability_materials[0].source_identity == (
        f"user-catalog:{path}#skill:local-testing"
    )
    assert "untrusted reference material" in render_candidate_user_context(treatment)
    with pytest.raises(PermissionError, match="authorized"):
        run_experiment(experiment, live=True)

    authorized = authorize_experiment(experiment, expected_digest=experiment.plan.executable_digest)
    path.write_text("replaced after approval", encoding="utf-8")
    assert authorized.can_execute
    assert authorized.candidates.candidates[1].capability_materials == treatment.capability_materials


def test_explicit_candidate_source_and_materials_do_not_need_package_content(
    tmp_path: Path, repo_with_history
) -> None:
    profile = build_fit_profile(repo_with_history())
    source = open_user_candidate_source(_catalog(tmp_path))
    planner = BoundedCapabilityPlanner(source=source)

    missing = generate_candidates(profile, planner, model="gpt-4o-mini")
    supplied = generate_candidates(
        profile, planner, model="gpt-4o-mini", capability_materials=source.capability_materials
    )
    resolved = resolve_experiment(profile, candidate_source=source, budget_usd=500)

    assert missing.abstained
    assert not supplied.abstained
    assert supplied.candidates == resolved.candidates.candidates
    assert resolved.plan.decision == "ready"
    assert source.capability_materials is not None
    with pytest.raises(ValueError, match="duplicate identities"):
        generate_candidates(
            profile,
            planner,
            capability_materials=source.capability_materials * 2,
        )
    with pytest.raises(ValueError, match="either"):
        resolve_experiment(profile, candidate_source=source, catalog_path=tmp_path / "unused")


def test_catalog_and_plan_identity_change_with_supplied_content(
    tmp_path: Path, repo_with_history
) -> None:
    profile = build_fit_profile(repo_with_history())
    path = _catalog(tmp_path)
    first_source = open_user_candidate_source(path)
    first = resolve_experiment(profile, candidate_source=first_source, budget_usd=500)
    updated = _entry(content="---\nname: local-testing\n---\n\nCheck concurrency.\n")
    second_source = open_user_candidate_source(_catalog(tmp_path, [updated]))
    second = resolve_experiment(profile, candidate_source=second_source, budget_usd=500)

    assert first_source.catalog_snapshot_digest != second_source.catalog_snapshot_digest
    assert first.plan.executable_digest != second.plan.executable_digest
    assert (
        first.candidates.candidates[1].configuration_hash
        != second.candidates.candidates[1].configuration_hash
    )
    selected = first_source.retrieve(WorkObservation(signals=("python",)))[0]
    assert first_source.capability_materials is not None
    assert selected.source_digest == first_source.capability_materials[0].catalog_entry_digest
    material_changed = replace(first_source, capability_materials=second_source.capability_materials)
    assert first_source.catalog_snapshot_digest != material_changed.catalog_snapshot_digest


def test_empty_or_unmatched_user_catalog_abstains(tmp_path: Path, repo_with_history) -> None:
    profile = build_fit_profile(repo_with_history())
    for entries in ([], [_entry(signals=["rust"])]):
        experiment = resolve_experiment(
            profile, catalog_path=_catalog(tmp_path, entries), budget_usd=500
        )
        assert experiment.plan.decision == "blocked-no-candidates"
        assert not experiment.can_authorize


def test_supplied_material_keeps_unknown_cost_unknown(
    tmp_path: Path, repo_with_history, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ModelPrice, "from_litellm", staticmethod(lambda _model: None))
    experiment = resolve_experiment(
        build_fit_profile(repo_with_history()), catalog_path=_catalog(tmp_path), budget_usd=500
    )
    assert experiment.plan.decision == "blocked-unknown-cost"
    assert experiment.plan.cost.completeness == "unknown"
    assert not experiment.can_authorize


@pytest.mark.parametrize(
    "entry",
    [
        _entry(id="skill:../escape"),
        _entry(id="agent:local-testing", type="agent"),
        _entry(type="agent"),
        _entry(signals="python"),
        _entry(signals=["python", "python"]),
        _entry(signals=["unsafe\n"]),
        _entry(signals=[{}]),
        _entry(signals=[f"signal-{index}" for index in range(33)]),
        _entry(content=""),
        _entry(content="---\nname: wrong-name\n---\n\nA body.\n"),
        _entry(content="---\nname: local-testing\n---\n"),
        _entry(content="\ud800"),
        _entry(content="x" * (MAX_CANDIDATE_USER_CONTEXT_BYTES + 1)),
        _entry(path="../../private-data"),
        _entry(url="https://example.invalid/skill"),
    ],
)
def test_rejects_unsafe_or_unreproducible_entries(tmp_path: Path, entry: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        open_user_candidate_source(_catalog(tmp_path, [entry]))


@pytest.mark.parametrize(
    "payload",
    [
        b"not JSON",
        b"\xff",
        b'{"schema":"ctx.fit.catalog-v1","schema":"ctx.fit.catalog-v1","entries":[]}',
        b'{"schema":"ctx.fit.catalog-v1","entries":[],"path":"other.json"}',
        b'{"schema":"unknown","entries":[]}',
        b'{"schema":"ctx.fit.catalog-v1","entries":{}}',
        b"[]",
        b" " * (MAX_USER_CATALOG_BYTES + 1),
    ],
)
def test_rejects_invalid_catalog_files(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        open_user_candidate_source(path)


def test_rejects_duplicate_entries_symlinks_and_missing_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate catalog identity"):
        open_user_candidate_source(_catalog(tmp_path, [_entry(), _entry()]))
    with pytest.raises(ValueError, match="at most"):
        open_user_candidate_source(_catalog(tmp_path, [_entry()] * 513))
    link = tmp_path / "linked.json"
    link.symlink_to(_catalog(tmp_path))
    for path in (link, tmp_path / "absent.json", tmp_path):
        with pytest.raises(ValueError):
            open_user_candidate_source(path)


@pytest.mark.parametrize(
    ("options", "code", "decision"),
    [
        ([], 0, "blocked-no-budget"),
        (["--test", "--yes"], 1, "blocked-no-budget"),
        (["--test", "--yes", "--budget", "0"], 1, "blocked-over-budget"),
        (["--dry-run", "--budget", "500"], 0, "ready"),
        (["--test", "--yes", "--budget", "500"], 1, "ready"),
    ],
)
def test_cli_catalog_plans_without_bypassing_spend_gates(
    tmp_path: Path,
    repo_with_history,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    code: int,
    decision: str,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("read-only catalog planning reached execution")

    monkeypatch.setattr("ctx.fit.experiment.run_experiment", forbidden)
    repo = repo_with_history()
    assert ctx_main(["fit", str(repo), "--catalog", str(_catalog(tmp_path)), "--json", *options]) == code
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan"]["decision"] == decision
    assert payload["plan"]["candidate_count"] == 2
    assert "execution" not in payload
    if decision == "ready" and "--test" in options:
        assert "plan-only" in payload["execution_refusal"]


@pytest.mark.parametrize("catalog_argument", ["missing.json", ""])
def test_cli_reports_invalid_catalog_without_execution(
    tmp_path: Path,
    repo_with_history,
    capsys: pytest.CaptureFixture[str],
    catalog_argument: str,
) -> None:
    repo = repo_with_history()
    path = str(tmp_path / catalog_argument) if catalog_argument else ""
    assert ctx_main(["fit", str(repo), "--catalog", path]) == 2
    assert "catalog" in capsys.readouterr().err


def test_cli_can_run_explicit_catalog_in_honest_simulation(
    tmp_path: Path,
    repo_with_history,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ctx.cli.fit._provider_available", lambda _model: False)
    repo = repo_with_history()
    result = ctx_main(
        ["fit", str(repo), "--catalog", str(_catalog(tmp_path)), "--test", "--budget", "500", "--yes"]
    )
    output = capsys.readouterr().out
    assert result == 0
    assert "skill:local-testing" in output
    assert "SIMULATION" in output
    assert "proves nothing about this repository" in output
