from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import stat
import subprocess
import threading
import time
from typing import Any

import pytest

from scripts import ctx_ab_benchmark as benchmark
from scripts import ctx_ab_holdout as holdout
from scripts import ctx_ab_holdout_freeze as freezer
from scripts import ctx_ab_holdout_prepare as prepare


REVISION = "a" * 40
ORIGIN_URL = "https://github.com/stevesolun/ctx.git"
PINS = {
    "bridge_sha256": "1" * 64,
    "docker_cli_sha256": "2" * 64,
    "docker_daemon_id": "daemon-fixture",
    "docker_package_sha256": "3" * 64,
    "docker_server_version": "29.5.2",
    "namespace": "swebench",
    "python_environment_sha256": "4" * 64,
    "python_sha256": "5" * 64,
    "revision": "6" * 40,
    "run_evaluation_sha256": "7" * 64,
    "schema_version": 1,
}
MODEL_REASONING_EFFORT = "high"
MODEL_AUTO_COMPACT_TOKEN_LIMIT = 200_000
CODEX_RUNTIME_CONTRACT = {
    "arms": ["baseline", "ctx-light"],
    "model_auto_compact_token_limit": MODEL_AUTO_COMPACT_TOKEN_LIMIT,
    "model_reasoning_effort": MODEL_REASONING_EFFORT,
}


def test_documented_official_environment_command_covers_runtime_contract() -> None:
    readme = (prepare.ROOT / "benchmarks" / "ctx_ab" / "README.md").read_text(encoding="utf-8")
    command = readme.split(
        '"$PY" -m scripts.ctx_ab_holdout_prepare environment',
        maxsplit=1,
    )[1].split(
        '"$PY" -m scripts.ctx_ab_holdout_freeze',
        maxsplit=1,
    )[0]

    assert '--model-reasoning-effort "$MODEL_REASONING_EFFORT"' in command
    assert '--model-auto-compact-token-limit "$MODEL_AUTO_COMPACT_TOKEN_LIMIT"' in command


def _canonical(value: object, *, newline: bool = False) -> bytes:
    data = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return data + (b"\n" if newline else b"")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _private_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _v1() -> dict[str, Any]:
    return json.loads(
        (prepare.ROOT / "benchmarks" / "ctx_ab" / "holdout-protocol-v1.json").read_text(
            encoding="utf-8"
        )
    )


def _product_inputs(*, source_trust: bool = True) -> dict[str, str]:
    inputs = {
        "benchmark_script_sha256": "8" * 64,
        "catalog_archive_sha256": "9" * 64,
        "codex_binary_sha256": "a" * 64,
        "provider_config_sha256": benchmark.codex_provider_config_sha256("openai"),
        "revision": REVISION,
        "runtime_availability_sha256": "b" * 64,
    }
    if source_trust:
        inputs.update(
            {
                "origin_main_revision": REVISION,
                "origin_url": ORIGIN_URL,
            }
        )
    return inputs


def _exposure_ledger() -> dict[str, Any]:
    return {
        "instance_id_hmac_sha256": ["1" * 64, "2" * 64],
        "salt": "3" * 64,
        "schema_version": 1,
    }


def _protocol(
    *,
    rows_sha256: str = "c" * 64,
    expected_rows: int = 10,
    exposure_ledger_sha256: str = "d" * 64,
) -> dict[str, Any]:
    protocol = prepare._build_v2_protocol(
        v1=_v1(),
        revision=REVISION,
        frozen_at="2026-07-30T10:00:00Z",
        product_inputs=_product_inputs(),
        verifier_pins=PINS,
        exposure_ledger_sha256=exposure_ledger_sha256,
    )
    protocol["universe"]["selection_jsonl_sha256"] = rows_sha256
    protocol["universe"]["expected_rows"] = expected_rows
    return protocol


def _row(index: int, required_columns: list[str]) -> dict[str, str]:
    values = {
        "repo": f"owner/repo-{index}",
        "instance_id": f"private-task-{index}",
        "base_commit": f"{index:x}" * 40,
        "patch": "diff --git a/src/x.py b/src/x.py\n",
        "test_patch": "diff --git a/tests/test_x.py b/tests/test_x.py\n",
        "problem_statement": "private task text",
        "hints_text": "",
        "created_at": "2026-01-01",
        "version": "1",
        "FAIL_TO_PASS": '["tests/test_x.py::test_x"]',
        "PASS_TO_PASS": '["tests/test_x.py"]',
        "environment_setup_commit": f"{index:x}" * 40,
        "difficulty": "easy",
    }
    return {column: values[column] for column in required_columns}


def _source_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, Path, dict[str, Any], dict[str, Any]]:
    required_columns = list(_v1()["universe"]["required_columns"])
    rows = [_row(index, required_columns) for index in range(10)]
    rows_bytes = b"".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n" for row in rows
    )
    exposure_path = _private_file(
        tmp_path / "inputs" / "exposure.json",
        _canonical(_exposure_ledger()),
    )
    protocol = _protocol(
        rows_sha256=_sha256(rows_bytes),
        exposure_ledger_sha256=_sha256(exposure_path.read_bytes()),
    )
    ids = [str(row["instance_id"]) for row in rows]
    repository_map = {
        item: holdout.canonical_repo_url(str(rows[index]["repo"])) for index, item in enumerate(ids)
    }
    selection = {
        "analysis_instance_ids": ids,
        "analysis_repository_map": repository_map,
        "canary_instance_id": None,
        "canary_repository": None,
        "protocol_id": prepare.PROTOCOL_ID,
    }
    protocol_path = _private_file(
        tmp_path / "inputs" / "protocol.json", _canonical(protocol, newline=True)
    )
    rows_path = _private_file(tmp_path / "inputs" / "rows.jsonl", rows_bytes)
    selection_path = _private_file(
        tmp_path / "inputs" / "selection.json",
        _canonical(selection),
    )
    monkeypatch.setattr(prepare.freezer, "validate_acquisition_protocol", lambda *_a, **_k: PINS)
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(
        prepare,
        "_repository_state",
        lambda _root: prepare.RepositoryState(
            repository,
            REVISION,
            ORIGIN_URL,
            REVISION,
        ),
    )
    monkeypatch.setattr(prepare, "_assert_repository_unchanged", lambda _state: None)
    monkeypatch.setattr(prepare.holdout, "evaluate_row", lambda row, _protocol: row)
    monkeypatch.setattr(prepare.holdout, "select_rows", lambda _rows, _protocol: selection)
    monkeypatch.setattr(
        prepare.holdout,
        "_validated_selection",
        lambda _selection, _protocol: (ids, repository_map),
    )
    return exposure_path, protocol_path, rows_path, selection_path, protocol, selection


def test_build_v2_protocol_preserves_universe_and_sets_exact_design() -> None:
    v1 = _v1()
    protocol = prepare._build_v2_protocol(
        v1=v1,
        revision=REVISION,
        frozen_at="2026-07-30T10:00:00Z",
        product_inputs=_product_inputs(),
        verifier_pins=PINS,
        exposure_ledger_sha256="d" * 64,
    )

    assert protocol["schema_version"] == 2
    assert protocol["protocol_id"] == prepare.PROTOCOL_ID
    assert protocol["protocol_generation"] == freezer.PROTOCOL_GENERATION == 1
    assert protocol["stage"] == "acquisition-frozen"
    assert protocol["universe"] == v1["universe"]
    assert protocol["static_candidate_rules"] == v1["static_candidate_rules"]
    assert protocol["ranking"] == v1["ranking"]
    assert protocol["selection"] == {
        "analysis_repositories": 10,
        "analysis_scenarios": 10,
        "candidate_slot": 0,
        "ctx_context": [],
        "eligible_candidates_per_repository_required": 1,
        "eligible_repositories_required": 10,
        "first_scenario_rule": (
            "candidate at the zero-based candidate_slot from the stable candidate-partition "
            "ranking for each of the first ten generation-ranked eligible repositories"
        ),
        "private_canary": False,
        "query": "first 240 characters of whitespace-normalized problem_statement",
        "replacement_after_control_failure": "forbidden",
        "strategy": "one-per-repository",
        "task": "exact problem_statement bytes from the frozen dataset row",
    }
    assert protocol["claim_gates"]["paired_trials_per_scenario"] == 3
    assert protocol["claim_gates"]["minimum_repositories_with_verified_delivery"] == 10
    assert protocol["claim_gates"]["required_benefiting_repositories"] == 9
    assert protocol["analysis"]["support_test"] == (
        "exact one-sided sign test across ten repository effects"
    )
    assert not any(
        "seven" in requirement for requirement in protocol["freeze_manifest_requirements"]
    )
    assert any(
        "all ten selected test modules" in requirement
        for requirement in protocol["freeze_manifest_requirements"]
    )
    assert "canary_policy" not in protocol
    assert protocol["official_swebench_verifier"] == PINS
    assert protocol["exposure_ledger_sha256"] == "d" * 64
    assert protocol["product_inputs"]["origin_url"] == ORIGIN_URL
    assert protocol["product_inputs"]["origin_main_revision"] == REVISION
    assert protocol["execution_inputs"] == {
        key: None for key in sorted(freezer.ACQUISITION_EXECUTION_INPUT_KEYS)
    }
    assert protocol["selection_seed"] == _sha256(
        prepare.SEED_PREFIX
        + str(freezer.PROTOCOL_GENERATION).encode()
        + b"\0"
        + str(v1["universe"]["revision"]).encode()
    )
    assert protocol["selection_seed_input"] == (
        "fixed literal ctx-holdout-selection-v2 NUL decimal protocol generation "
        "NUL external dataset revision"
    )
    assert protocol["candidate_partition_seed"] == _sha256(
        freezer.CANDIDATE_PARTITION_PREFIX + str(v1["universe"]["revision"]).encode()
    )
    assert protocol["candidate_partition_seed_input"] == (
        "fixed literal ctx-holdout-candidate-partition-v2 NUL external dataset revision"
    )


def test_protocol_generation_changes_seed_and_selects_disjoint_candidate_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v1 = _v1()
    generation_one = prepare._build_v2_protocol(
        v1=v1,
        revision=REVISION,
        frozen_at="2026-07-30T10:00:00Z",
        product_inputs=_product_inputs(),
        verifier_pins=PINS,
        exposure_ledger_sha256="d" * 64,
    )
    monkeypatch.setattr(freezer, "PROTOCOL_GENERATION", 2)
    generation_two = prepare._build_v2_protocol(
        v1=v1,
        revision=REVISION,
        frozen_at="2026-07-30T10:00:00Z",
        product_inputs=_product_inputs(),
        verifier_pins=PINS,
        exposure_ledger_sha256="d" * 64,
    )
    repositories = [f"https://github.com/owner/repo-{index}.git" for index in range(10)]

    assert generation_one["protocol_generation"] == 1
    assert generation_two["protocol_generation"] == 2
    assert generation_one["selection_seed"] != generation_two["selection_seed"]
    assert generation_one["candidate_partition_seed"] == generation_two["candidate_partition_seed"]
    assert generation_one["selection"]["candidate_slot"] == 0
    assert generation_two["selection"]["candidate_slot"] == 1
    assert generation_one["selection"]["eligible_candidates_per_repository_required"] == 1
    assert generation_two["selection"]["eligible_candidates_per_repository_required"] == 2
    assert sorted(
        repositories,
        key=lambda repository: (
            holdout._digest(generation_one["selection_seed"], repository),
            repository,
        ),
    ) != sorted(
        repositories,
        key=lambda repository: (
            holdout._digest(generation_two["selection_seed"], repository),
            repository,
        ),
    )
    ledger = [
        {
            "instance_id": f"repo-{repository}-candidate-{candidate}",
            "repo": f"owner/repo-{repository}",
            "production_paths": f"src/repo_{repository}/feature_{candidate}.py",
            "test_path": f"tests/repo_{repository}/test_{candidate}.py",
            "status": "eligible",
        }
        for repository in range(10)
        for candidate in range(2)
    ]
    first_selection = holdout.select_rows(ledger, generation_one)
    second_selection = holdout.select_rows(ledger, generation_two)
    assert set(first_selection["analysis_instance_ids"]).isdisjoint(
        second_selection["analysis_instance_ids"]
    )


def test_built_protocol_satisfies_real_freezer_acquisition_contract() -> None:
    product_inputs = {
        "benchmark_script_sha256": _sha256(Path(benchmark.__file__).read_bytes()),
        "catalog_archive_sha256": _sha256(benchmark.PRODUCTION_CATALOG_ARCHIVE.read_bytes()),
        "codex_binary_sha256": "a" * 64,
        "provider_config_sha256": benchmark.codex_provider_config_sha256("openai"),
        "revision": REVISION,
        "runtime_availability_sha256": _sha256(
            benchmark.PRODUCTION_RUNTIME_AVAILABILITY.read_bytes()
        ),
    }
    if "origin_url" in freezer.PRODUCT_INPUT_KEYS:
        product_inputs.update(
            {
                "origin_main_revision": REVISION,
                "origin_url": ORIGIN_URL,
            }
        )
    supports_exposure = (
        "exposure_ledger_sha256" in inspect.signature(freezer.build_acquisition_protocol).parameters
    )
    protocol = prepare._build_v2_protocol(
        v1=_v1(),
        revision=REVISION,
        frozen_at="2026-07-30T10:00:00Z",
        product_inputs=product_inputs,
        verifier_pins=PINS,
        exposure_ledger_sha256="d" * 64 if supports_exposure else None,
    )

    assert freezer.validate_acquisition_protocol(protocol) == PINS


def test_repository_state_rejects_dirty_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def command(argv: list[str], **_kwargs: Any) -> bytes:
        if argv[1] == "rev-parse":
            return (REVISION + "\n").encode()
        return b"?? private-task-id\n"

    monkeypatch.setattr(prepare, "_command_bytes", command)

    with pytest.raises(prepare.PrepareError, match="clean"):
        prepare._repository_state(tmp_path)


def _mock_repository_commands(
    monkeypatch: pytest.MonkeyPatch,
    *,
    head: str = REVISION,
    tracking: str = REVISION,
    remote_main: str = REVISION,
    fetch_url: str = ORIGIN_URL,
    push_url: str = ORIGIN_URL,
) -> None:
    def command(argv: list[str], **_kwargs: Any) -> bytes:
        if argv[1:4] == ["rev-parse", "--verify", "HEAD^{commit}"]:
            return f"{head}\n".encode()
        if argv[1] == "status":
            return b""
        if argv[1:4] == ["remote", "get-url", "origin"]:
            return f"{fetch_url}\n".encode()
        if argv[1:5] == ["remote", "get-url", "--push", "origin"]:
            return f"{push_url}\n".encode()
        if argv[1] == "rev-parse":
            return f"{tracking}\n".encode()
        if argv[1] == "ls-remote":
            return f"{remote_main}\trefs/heads/main\n".encode()
        raise AssertionError(argv)

    monkeypatch.setattr(prepare, "_command_bytes", command)


def test_repository_state_authenticates_exact_origin_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_repository_commands(monkeypatch)

    state = prepare._repository_state(tmp_path)

    assert state == prepare.RepositoryState(tmp_path.resolve(), REVISION, ORIGIN_URL, REVISION)


@pytest.mark.parametrize(
    ("head", "tracking", "remote_main"),
    [
        ("b" * 40, REVISION, REVISION),
        (REVISION, REVISION, "b" * 40),
        (REVISION, "b" * 40, "b" * 40),
    ],
)
def test_repository_state_rejects_unmerged_or_stale_origin_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    head: str,
    tracking: str,
    remote_main: str,
) -> None:
    _mock_repository_commands(
        monkeypatch,
        head=head,
        tracking=tracking,
        remote_main=remote_main,
    )

    with pytest.raises(prepare.PrepareError, match="exact current origin/main"):
        prepare._repository_state(tmp_path)


@pytest.mark.parametrize(
    ("fetch_url", "push_url"),
    [
        ("https://token@github.com/stevesolun/ctx.git", ORIGIN_URL),
        (ORIGIN_URL, "git@github.com:stevesolun/ctx.git"),
    ],
)
def test_repository_state_rejects_credentialed_or_mismatched_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_url: str,
    push_url: str,
) -> None:
    _mock_repository_commands(
        monkeypatch,
        fetch_url=fetch_url,
        push_url=push_url,
    )

    with pytest.raises(prepare.PrepareError, match="credential-free"):
        prepare._repository_state(tmp_path)


def test_committed_v1_must_match_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / prepare.V1_PROTOCOL_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(prepare, "_command_bytes", lambda *_a, **_k: b'{"changed":true}\n')

    with pytest.raises(prepare.PrepareError, match="does not match"):
        prepare._committed_v1_protocol(
            prepare.RepositoryState(tmp_path, REVISION, ORIGIN_URL, REVISION)
        )


def test_atomic_private_write_is_canonical_owner_only_and_no_overwrite(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    output = parent / "artifact.json"
    data = _canonical({"b": 2, "a": 1})

    prepare._atomic_private_write(output, data)

    assert output.read_bytes() == b'{"a":1,"b":2}'
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.stat().st_nlink == 1
    with pytest.raises(prepare.PrepareError, match="exists"):
        prepare._atomic_private_write(output, data)


def test_atomic_private_write_rejects_unsafe_parent(tmp_path: Path) -> None:
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o755)

    with pytest.raises(prepare.PrepareError, match="owner-only"):
        prepare._atomic_private_write(parent / "artifact.json", b"{}")


def test_repository_local_output_requires_private_gate_root(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()

    with pytest.raises(prepare.PrepareError, match="ctx-ab-private"):
        prepare._require_private_repository_location(
            repository / "benchmark-evidence.json",
            root=repository,
        )

    prepare._require_private_repository_location(
        repository / ".gate" / "ctx-ab-private" / "v2" / "protocol.json",
        root=repository,
    )


def test_load_private_json_rejects_noncanonical_bytes(tmp_path: Path) -> None:
    path = _private_file(tmp_path / "input.json", b'{ "a": 1 }\n')

    with pytest.raises(prepare.PrepareError, match="not canonical"):
        prepare._load_private_canonical_json(path, label="fixture", newline=False)


@pytest.mark.parametrize(
    "ledger",
    [
        {"schema_version": 1, "salt": "3" * 64, "instance_id_hmac_sha256": ["2" * 64, "1" * 64]},
        {"schema_version": 1, "salt": "3" * 64, "instance_id_hmac_sha256": ["1" * 64] * 2},
        {"schema_version": 1, "salt": "3" * 64, "instance_id_hmac_sha256": []},
        {"schema_version": 1, "salt": "not-a-digest", "instance_id_hmac_sha256": []},
        {"schema_version": True, "salt": "3" * 64, "instance_id_hmac_sha256": []},
        {
            "schema_version": 1,
            "salt": "3" * 64,
            "instance_id_hmac_sha256": [],
            "unexpected": True,
        },
    ],
)
def test_exposure_ledger_requires_exact_canonical_shape(
    tmp_path: Path,
    ledger: dict[str, Any],
) -> None:
    path = _private_file(tmp_path / "ledger.json", _canonical(ledger))

    with pytest.raises(prepare.PrepareError, match="unsupported shape"):
        prepare._validated_exposure_ledger(path)


def test_load_rows_rejects_noncanonical_field_order(tmp_path: Path) -> None:
    path = _private_file(tmp_path / "rows.jsonl", b'{"b":"2","a":"1"}\n')

    with pytest.raises(prepare.PrepareError, match="invalid"):
        prepare._load_canonical_rows(path, required_columns=["a", "b"])


def test_create_protocol_writes_authenticated_canonical_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "graph").mkdir()
    (root / "src" / "ctx" / "assets").mkdir(parents=True)
    product_paths = [
        root / "scripts" / "ctx_ab_benchmark.py",
        root / "graph" / "wiki-graph-runtime.tar.gz",
        root / "src" / "ctx" / "assets" / "runtime-availability.json",
    ]
    for index, path in enumerate(product_paths):
        path.write_bytes(f"product-{index}".encode())
    state = prepare.RepositoryState(root, REVISION, ORIGIN_URL, REVISION)
    codex = prepare.CodexIdentity(
        path=tmp_path / "codex",
        sha256="d" * 64,
        version="codex 1.2.3",
        provider_config_sha256=benchmark.codex_provider_config_sha256("openai"),
    )
    monkeypatch.setattr(prepare, "_repository_state", lambda _root: state)
    monkeypatch.setattr(prepare, "_assert_repository_unchanged", lambda _state: None)
    monkeypatch.setattr(prepare, "_committed_v1_protocol", lambda _state: _v1())
    monkeypatch.setattr(prepare, "_probe_codex", lambda *_a, **_k: codex)
    monkeypatch.setattr(prepare, "_probe_verifier", lambda **_kwargs: deepcopy(PINS))
    monkeypatch.setattr(prepare.freezer, "validate_acquisition_protocol", lambda *_a, **_k: PINS)
    exposure_path = _private_file(
        tmp_path / "private" / "exposure.json",
        _canonical(_exposure_ledger()),
    )
    output = tmp_path / "private" / "protocol.json"
    output.parent.mkdir(mode=0o700, exist_ok=True)

    digest = prepare.create_protocol(
        output_path=output,
        codex_path=tmp_path / "codex",
        provider="openai",
        swebench_checkout=tmp_path / "swebench",
        swebench_python=tmp_path / "swebench-python",
        docker_cli=tmp_path / "docker",
        docker_host="unix:///tmp/docker.sock",
        exposure_ledger_path=exposure_path,
        frozen_at="2026-07-30T13:00:00+03:00",
        root=root,
    )

    document = json.loads(output.read_bytes())
    assert output.read_bytes() == _canonical(document, newline=True)
    assert digest == _sha256(output.read_bytes())
    assert document["frozen_at"] == "2026-07-30T10:00:00Z"
    assert document["product_inputs"]["revision"] == REVISION
    assert document["product_inputs"]["origin_url"] == ORIGIN_URL
    assert document["product_inputs"]["origin_main_revision"] == REVISION
    assert document["product_inputs"]["codex_binary_sha256"] == codex.sha256
    assert document["exposure_ledger_sha256"] == _sha256(exposure_path.read_bytes())
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_probe_verifier_rejects_runtime_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots = [deepcopy(PINS), {**PINS, "docker_daemon_id": "changed"}]
    monkeypatch.setattr(prepare, "_verifier_snapshot", lambda **_kwargs: snapshots.pop(0))

    with pytest.raises(prepare.PrepareError, match="changed"):
        prepare._probe_verifier(
            swebench_checkout=Path("/swebench"),
            swebench_python=Path("/python"),
            docker_cli=Path("/docker"),
            docker_host="unix:///tmp/docker.sock",
        )


def test_verifier_snapshot_executes_authenticated_python_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_launcher = tmp_path / "venv-python"
    python_launcher.write_bytes(b"python-runtime")
    python_launcher.chmod(0o700)
    docker = tmp_path / "docker"
    docker.write_bytes(b"docker-runtime")
    docker.chmod(0o700)
    observed: list[Path] = []

    monkeypatch.setattr(prepare, "_checkout_snapshot", lambda _path: (REVISION, "1" * 64))
    monkeypatch.setattr(prepare, "_stable_digest", lambda *_args, **_kwargs: "2" * 64)

    def python_environment(path: Path) -> str:
        observed.append(path)
        return "3" * 64

    def docker_package(path: Path) -> str:
        observed.append(path)
        return "4" * 64

    monkeypatch.setattr(prepare, "_python_environment_sha256", python_environment)
    monkeypatch.setattr(prepare, "_docker_package_sha256", docker_package)
    monkeypatch.setattr(prepare, "_docker_identity", lambda *_args: ("daemon", "29.5.2"))

    snapshot = prepare._verifier_snapshot(
        swebench_checkout=tmp_path,
        swebench_python=python_launcher,
        docker_cli=docker,
        docker_host="unix:///tmp/docker.sock",
    )

    assert observed == [python_launcher, python_launcher]
    assert snapshot["python_sha256"] == _sha256(python_launcher.read_bytes())


def test_verifier_snapshot_rejects_python_target_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_launcher = tmp_path / "venv-python"
    python_launcher.write_bytes(b"python-runtime")
    python_launcher.chmod(0o700)
    docker = tmp_path / "docker"
    docker.write_bytes(b"docker-runtime")
    docker.chmod(0o700)

    monkeypatch.setattr(prepare, "_checkout_snapshot", lambda _path: (REVISION, "1" * 64))
    monkeypatch.setattr(prepare, "_stable_digest", lambda *_args, **_kwargs: "2" * 64)
    monkeypatch.setattr(prepare, "_python_environment_sha256", lambda _path: "3" * 64)

    def change_python(_path: Path) -> str:
        python_launcher.write_bytes(b"changed-runtime")
        return "4" * 64

    monkeypatch.setattr(prepare, "_docker_package_sha256", change_python)
    monkeypatch.setattr(prepare, "_docker_identity", lambda *_args: ("daemon", "29.5.2"))

    with pytest.raises(prepare.PrepareError, match="Python changed"):
        prepare._verifier_snapshot(
            swebench_checkout=tmp_path,
            swebench_python=python_launcher,
            docker_cli=docker,
            docker_host="unix:///tmp/docker.sock",
        )


def test_execution_python_probe_uses_authenticated_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_launcher = tmp_path / "venv-python"
    python_launcher.write_bytes(b"python-runtime")
    python_launcher.chmod(0o700)
    observed: dict[str, Path] = {}

    def command(argv: list[str], **_kwargs: Any) -> bytes:
        observed["command"] = Path(argv[0])
        return b"3.12.13\n"

    def dependencies(path: Path) -> str:
        observed["dependencies"] = path
        return "5" * 64

    monkeypatch.setattr(prepare, "_command_bytes", command)
    monkeypatch.setattr(prepare.benchmark, "python_dependencies_sha256", dependencies)

    identity = prepare._probe_execution_python(python_launcher)

    assert observed == {
        "command": python_launcher,
        "dependencies": python_launcher,
    }
    assert identity.path == python_launcher
    assert identity.sha256 == _sha256(python_launcher.read_bytes())


def test_python_probes_reject_symlink_launchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "python-target"
    target.write_bytes(b"python-runtime")
    target.chmod(0o700)
    launcher = tmp_path / "venv-python"
    launcher.symlink_to(target)

    monkeypatch.setattr(prepare, "_checkout_snapshot", lambda _path: (REVISION, "1" * 64))

    with pytest.raises(prepare.PrepareError, match="must be a regular file"):
        prepare._verifier_snapshot(
            swebench_checkout=tmp_path,
            swebench_python=launcher,
            docker_cli=target,
            docker_host="unix:///tmp/docker.sock",
        )
    with pytest.raises(prepare.PrepareError, match="must be a regular file"):
        prepare._probe_execution_python(launcher)


def test_execution_python_probe_accepts_real_copied_venv(tmp_path: Path) -> None:
    discovered = shutil.which("python3.12")
    candidates = [
        Path("/opt/homebrew/bin/python3.12"),
        Path("/usr/local/bin/python3.12"),
        *((Path(discovered),) if discovered is not None else ()),
    ]
    unique_candidates = list(dict.fromkeys(path for path in candidates if path.is_file()))
    if not unique_candidates:
        pytest.skip("official benchmark Python 3.12 is unavailable")

    launcher: Path | None = None
    for index, python in enumerate(unique_candidates):
        venv = tmp_path / f"copied-venv-{index}"
        result = subprocess.run(
            [str(python), "-m", "venv", "--copies", str(venv)],
            check=False,
            capture_output=True,
            text=True,
        )
        candidate = venv / "bin/python"
        if result.returncode == 0 and candidate.is_file() and not candidate.is_symlink():
            launcher = candidate
            break
    if launcher is None:
        pytest.skip("no available Python 3.12 runtime can create a copied virtual environment")

    identity = prepare._probe_execution_python(launcher)

    assert not launcher.is_symlink()
    assert identity.path == launcher
    assert identity.version.startswith("3.12.")
    assert len(identity.dependencies_sha256) == 64


def test_command_runner_requires_descendant_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> prepare.swebench.CommandResult:
        observed["argv"] = argv
        observed.update(kwargs)
        return prepare.swebench.CommandResult(0, "authenticated\n", "", 0.1)

    monkeypatch.setattr(prepare.swebench, "_run_process", run)

    assert prepare._command_bytes(["git", "status"], cwd=tmp_path, timeout=5) == b"authenticated\n"
    assert observed["contain_descendants"] is True
    assert observed["timeout"] == 5


def test_command_runner_preserves_bounded_private_failure_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = prepare.swebench.CommandResult(
        17,
        "private stdout",
        "private stderr",
        0.25,
        reaped_descendants=2,
        residual_descendants=(101, 202),
    )
    monkeypatch.setattr(prepare.swebench, "_run_process", lambda *_args, **_kwargs: result)

    with pytest.raises(prepare.PrepareError) as raised:
        prepare._command_bytes(["git", "fsck"], cwd=tmp_path, timeout=5)

    prefix = "authenticated preparation command failed; private_result="
    assert str(raised.value).startswith(prefix)
    evidence = json.loads(str(raised.value).removeprefix(prefix))
    assert evidence == {
        "elapsed_seconds": 0.25,
        "reaped_descendants": 2,
        "residual_descendants": [101, 202],
        "returncode": 17,
        "stderr": {
            "bytes": 14,
            "sha256": _sha256(b"private stderr"),
            "text": "private stderr",
            "truncated": False,
        },
        "stdout": {
            "bytes": 14,
            "sha256": _sha256(b"private stdout"),
            "text": "private stdout",
            "truncated": False,
        },
        "timed_out": False,
    }


def _git(cwd: Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def _literal_tree_source(
    tmp_path: Path,
    *,
    directory: str,
    mode: bytes,
    name: bytes,
) -> tuple[Path, str, str]:
    source = tmp_path / directory
    source.mkdir()
    _git(source, "init", "--quiet", "--initial-branch=main")
    _git(source, "config", "user.name", "Fixture")
    _git(source, "config", "user.email", "fixture@example.com")
    blob = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=source,
            input=b"literal tree fixture\n",
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    tree = (
        subprocess.run(
            ["git", "hash-object", "-t", "tree", "--literally", "-w", "--stdin"],
            cwd=source,
            input=mode + b" " + name + b"\0" + bytes.fromhex(blob),
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    commit = (
        subprocess.run(
            ["git", "commit-tree", tree],
            cwd=source,
            input=b"literal tree fixture\n",
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    _git(source, "update-ref", "refs/heads/main", commit)
    return source, commit, tree


def _replace_github_fetch_with_local_source(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: Path,
    commit: str,
) -> None:
    original_command = prepare._command_bytes

    def command(argv: list[str], **kwargs: Any) -> bytes:
        if "fetch" in argv and "origin" in argv:
            repository = argv[argv.index("-C") + 1]
            argv = [
                "git",
                "-C",
                repository,
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "--quiet",
                "--force",
                "--no-tags",
                source.as_uri(),
                f"{commit}:refs/heads/base",
            ]
        return original_command(argv, **kwargs)

    monkeypatch.setattr(prepare, "_command_bytes", command)


def test_authenticated_bundle_excludes_future_gold_refs_objects_and_remotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--quiet", "--initial-branch=main")
    _git(source, "config", "user.name", "Fixture")
    _git(source, "config", "user.email", "fixture@example.com")
    feature = source / "feature.py"
    feature.write_text("def answer():\n    return 0\n", encoding="utf-8")
    _git(source, "add", "feature.py")
    _git(source, "commit", "--quiet", "-m", "base")
    base = _git(source, "rev-parse", "HEAD").stdout.strip()
    tree = _git(source, "rev-parse", "HEAD^{tree}").stdout.strip()
    feature.write_text("def answer():\n    return 42\n", encoding="utf-8")
    _git(source, "commit", "--quiet", "-am", "future gold fix")
    future = _git(source, "rev-parse", "HEAD").stdout.strip()

    destination = tmp_path / "bundles" / "base.bundle"
    destination.parent.mkdir()
    url = "https://github.com/owner/repo.git"
    original_command = prepare._command_bytes

    def command(argv: list[str], **kwargs: Any) -> bytes:
        if "fetch" in argv and "origin" in argv:
            repository = argv[argv.index("-C") + 1]
            argv = [
                "git",
                "-C",
                repository,
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "--quiet",
                "--force",
                "--no-tags",
                source.as_uri(),
                f"{base}:refs/heads/base",
            ]
        return original_command(argv, **kwargs)

    monkeypatch.setattr(prepare, "_command_bytes", command)

    observed_tree, bundle_sha256 = prepare._create_authenticated_bundle(
        url=url,
        commit=base,
        destination=destination,
    )

    assert observed_tree == tree
    assert bundle_sha256 == _sha256(destination.read_bytes())
    assert _git(tmp_path, "bundle", "list-heads", str(destination)).stdout.strip() == (
        f"{base} refs/heads/base"
    )
    materialized = tmp_path / "materialized.git"
    _git(tmp_path, "init", "--bare", "--quiet", str(materialized))
    _git(
        tmp_path,
        "-C",
        str(materialized),
        "-c",
        "protocol.file.allow=always",
        "fetch",
        "--quiet",
        str(destination),
        "refs/heads/base:refs/heads/base",
    )
    assert _git(tmp_path, "-C", str(materialized), "remote").stdout == ""
    assert future not in _git(tmp_path, "-C", str(materialized), "rev-list", "--all").stdout
    assert (
        _git(
            tmp_path,
            "-C",
            str(materialized),
            "cat-file",
            "-e",
            f"{future}^{{commit}}",
            check=False,
        ).returncode
        != 0
    )
    assert (
        _git(
            tmp_path,
            "-C",
            str(materialized),
            "fsck",
            "--full",
            "--unreachable",
            "--no-reflogs",
        ).stdout
        == ""
    )


def test_authenticated_bundle_accepts_legacy_zero_padded_tree_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, commit, tree = _literal_tree_source(
        tmp_path,
        directory="legacy-source",
        mode=b"0100644",
        name=b"legacy.py",
    )
    strict = _git(
        source,
        "fsck",
        "--full",
        "--strict",
        "--unreachable",
        "--no-reflogs",
        check=False,
    )
    assert strict.returncode != 0
    assert "zeroPaddedFilemode" in strict.stderr

    destination = tmp_path / "bundles" / "base.bundle"
    destination.parent.mkdir()
    _replace_github_fetch_with_local_source(monkeypatch, source=source, commit=commit)

    observed_tree, bundle_sha256 = prepare._create_authenticated_bundle(
        url="https://github.com/owner/legacy-repo.git",
        commit=commit,
        destination=destination,
    )

    assert observed_tree == tree
    assert bundle_sha256 == _sha256(destination.read_bytes())


def test_authenticated_bundle_rejects_unrelated_strict_fsck_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, commit, _tree = _literal_tree_source(
        tmp_path,
        directory="invalid-source",
        mode=b"120000",
        name=b".gitmodules",
    )
    scoped = _git(
        source,
        "-c",
        "fsck.zeroPaddedFilemode=ignore",
        "fsck",
        "--full",
        "--strict",
        "--unreachable",
        "--no-reflogs",
        check=False,
    )
    assert scoped.returncode != 0
    assert "gitmodulesSymlink" in scoped.stderr

    destination = tmp_path / "invalid-bundles" / "base.bundle"
    destination.parent.mkdir()
    _replace_github_fetch_with_local_source(monkeypatch, source=source, commit=commit)

    with pytest.raises(prepare.PrepareError, match="authenticated preparation command failed"):
        prepare._create_authenticated_bundle(
            url="https://github.com/owner/invalid-repo.git",
            commit=commit,
            destination=destination,
        )

    assert not destination.exists()


def test_prepare_sources_bundles_in_bounded_parallel_and_writes_deterministic_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        exposure_path,
        protocol_path,
        rows_path,
        selection_path,
        _protocol_value,
        _selection,
    ) = _source_fixture(tmp_path, monkeypatch)
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    destinations: set[Path] = set()

    def bundle(*, url: str, commit: str, destination: Path) -> tuple[str, str]:
        nonlocal active, maximum_active
        assert url.startswith("https://github.com/")
        assert prepare.REVISION.fullmatch(commit)
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            assert destination not in destinations
            destinations.add(destination)
        destination.write_bytes(commit.encode())
        time.sleep(0.02)
        with lock:
            active -= 1
        return "f" * 40, _sha256(destination.read_bytes())

    monkeypatch.setattr(prepare, "_create_authenticated_bundle", bundle)
    private = tmp_path / "private-map"
    private.mkdir(mode=0o700)
    cache = private / "bundles"
    output = private / "source-map.json"

    digest = prepare.prepare_sources(
        protocol_path=protocol_path,
        expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
        exposure_ledger_path=exposure_path,
        rows_path=rows_path,
        selection_path=selection_path,
        cache_root=cache,
        output_path=output,
        workers=4,
        root=tmp_path,
    )

    source_map = json.loads(output.read_bytes())
    assert output.read_bytes() == _canonical(source_map)
    assert source_map["schema_version"] == 1
    repositories = source_map["repositories"]
    assert list(repositories) == sorted(repositories)
    assert len(repositories) == 10
    assert len(destinations) == 10
    assert 2 <= maximum_active <= 4
    assert all(
        set(value) == {"base_commit", "bundle_path", "bundle_sha256", "tree_sha1"}
        and value["bundle_path"].startswith("bundles/")
        and not Path(value["bundle_path"]).is_absolute()
        and "private-task-" not in value["bundle_path"]
        for value in repositories.values()
    )
    assert digest == _sha256(output.read_bytes())
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_prepare_sources_reconstructs_exposure_filtered_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_columns = list(_v1()["universe"]["required_columns"])
    rows: list[dict[str, str]] = []
    for repository_index in range(10):
        for candidate_index in range(2):
            instance_id = f"repo-{repository_index}-candidate-{candidate_index}"
            row = _row(repository_index + 1, required_columns)
            row.update(
                {
                    "base_commit": hashlib.sha1(instance_id.encode()).hexdigest(),
                    "instance_id": instance_id,
                    "repo": f"owner/repo-{repository_index}",
                }
            )
            rows.append(row)
    rows_bytes = b"".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n" for row in rows
    )
    provisional_protocol = _protocol(
        rows_sha256=_sha256(rows_bytes),
        expected_rows=len(rows),
    )

    def evaluate(row: dict[str, str], _protocol_value: dict[str, Any]) -> dict[str, Any]:
        return {
            "base_commit": row["base_commit"],
            "instance_id": row["instance_id"],
            "production_paths": "src/feature.py",
            "rejection_code": "",
            "repo": row["repo"],
            "status": "eligible",
            "test_path": "tests/test_feature.py",
        }

    evaluated = [evaluate(row, provisional_protocol) for row in rows]
    baseline = holdout.select_rows(evaluated, provisional_protocol)
    exposed_id = str(baseline["analysis_instance_ids"][0])
    salt = "4" * 64
    exposure_document = {
        "instance_id_hmac_sha256": [
            holdout.exposure_ledger.instance_id_hmac_sha256(salt, exposed_id)
        ],
        "salt": salt,
        "schema_version": 1,
    }
    private = tmp_path / "private"
    exposure_path = _private_file(
        private / "exposure.json",
        _canonical(exposure_document),
    )
    protocol = _protocol(
        rows_sha256=_sha256(rows_bytes),
        expected_rows=len(rows),
        exposure_ledger_sha256=_sha256(exposure_path.read_bytes()),
    )
    filtered = holdout.reject_historical_exposures(evaluated, exposure_document)
    replacement = holdout.select_rows(filtered, protocol)
    exposed_repository = baseline["analysis_repository_map"][exposed_id]
    replacement_ids = [
        instance_id
        for instance_id, repository in replacement["analysis_repository_map"].items()
        if repository == exposed_repository
    ]
    assert exposed_id not in replacement["analysis_instance_ids"]
    assert len(replacement_ids) == 1

    protocol_path = _private_file(
        private / "protocol.json",
        _canonical(protocol, newline=True),
    )
    rows_path = _private_file(private / "rows.jsonl", rows_bytes)
    selection_path = _private_file(
        private / "selection.json",
        _canonical(replacement),
    )
    repository = tmp_path / "repo"
    repository.mkdir()
    state = prepare.RepositoryState(repository, REVISION, ORIGIN_URL, REVISION)
    monkeypatch.setattr(prepare, "_repository_state", lambda _root: state)
    monkeypatch.setattr(prepare, "_assert_repository_unchanged", lambda _state: None)
    monkeypatch.setattr(prepare.freezer, "validate_acquisition_protocol", lambda *_a, **_k: PINS)
    monkeypatch.setattr(prepare.holdout, "evaluate_row", evaluate)

    def bundle(*, destination: Path, commit: str, **_kwargs: Any) -> tuple[str, str]:
        destination.write_bytes(commit.encode())
        return "f" * 40, _sha256(destination.read_bytes())

    monkeypatch.setattr(prepare, "_create_authenticated_bundle", bundle)
    output = private / "source-map.json"

    prepare.prepare_sources(
        protocol_path=protocol_path,
        expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
        exposure_ledger_path=exposure_path,
        rows_path=rows_path,
        selection_path=selection_path,
        cache_root=private / "bundles",
        output_path=output,
        workers=4,
        root=tmp_path,
    )

    source_map = json.loads(output.read_bytes())
    assert exposed_repository in source_map["repositories"]
    assert source_map["repositories"][exposed_repository]["base_commit"] == next(
        row["base_commit"] for row in rows if row["instance_id"] == replacement_ids[0]
    )


@pytest.mark.parametrize("workers", [0, 9, True])
def test_prepare_sources_rejects_invalid_worker_count(
    tmp_path: Path,
    workers: Any,
) -> None:
    with pytest.raises(prepare.PrepareError, match="worker count"):
        prepare.prepare_sources(
            protocol_path=tmp_path / "protocol",
            expected_acquisition_protocol_sha256="0" * 64,
            exposure_ledger_path=tmp_path / "exposure",
            rows_path=tmp_path / "rows",
            selection_path=tmp_path / "selection",
            cache_root=tmp_path / "cache",
            output_path=tmp_path / "map",
            workers=workers,
        )


def test_prepare_sources_rejects_protocol_digest_drift_before_cloning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        exposure_path,
        protocol_path,
        rows_path,
        selection_path,
        _protocol_value,
        _selection,
    ) = _source_fixture(tmp_path, monkeypatch)
    clone_called = False

    def bundle(**_kwargs: Any) -> tuple[str, str]:
        nonlocal clone_called
        clone_called = True
        return "f" * 40, "e" * 64

    monkeypatch.setattr(prepare, "_create_authenticated_bundle", bundle)
    private = tmp_path / "private-map"
    private.mkdir(mode=0o700)
    cache = private / "bundles"
    output = private / "source-map.json"

    with pytest.raises(prepare.PrepareError, match="expected SHA-256"):
        prepare.prepare_sources(
            protocol_path=protocol_path,
            expected_acquisition_protocol_sha256="0" * 64,
            exposure_ledger_path=exposure_path,
            rows_path=rows_path,
            selection_path=selection_path,
            cache_root=cache,
            output_path=output,
            root=tmp_path,
        )

    assert clone_called is False
    assert not cache.exists()
    assert not output.exists()


def test_prepare_sources_rejects_exposure_ledger_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _exposure_path,
        protocol_path,
        rows_path,
        selection_path,
        _protocol_value,
        _selection,
    ) = _source_fixture(tmp_path, monkeypatch)
    private = tmp_path / "private-map"
    private.mkdir(mode=0o700)

    with pytest.raises(prepare.PrepareError, match="distinct"):
        prepare.prepare_sources(
            protocol_path=protocol_path,
            expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
            exposure_ledger_path=selection_path,
            rows_path=rows_path,
            selection_path=selection_path,
            cache_root=private / "bundles",
            output_path=private / "source-map.json",
            root=tmp_path,
        )


def test_prepare_sources_authenticates_exposure_ledger_against_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        exposure_path,
        protocol_path,
        rows_path,
        selection_path,
        _protocol_value,
        _selection,
    ) = _source_fixture(tmp_path, monkeypatch)
    exposure_path.write_bytes(
        _canonical(
            {
                "instance_id_hmac_sha256": ["8" * 64],
                "salt": "9" * 64,
                "schema_version": 1,
            }
        )
    )
    private = tmp_path / "private-map"
    private.mkdir(mode=0o700)

    with pytest.raises(prepare.PrepareError, match="does not match"):
        prepare.prepare_sources(
            protocol_path=protocol_path,
            expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
            exposure_ledger_path=exposure_path,
            rows_path=rows_path,
            selection_path=selection_path,
            cache_root=private / "bundles",
            output_path=private / "source-map.json",
            root=tmp_path,
        )


def test_prepare_sources_rejects_exposure_ledger_changed_during_bundling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        exposure_path,
        protocol_path,
        rows_path,
        selection_path,
        _protocol_value,
        _selection,
    ) = _source_fixture(tmp_path, monkeypatch)
    mutation_lock = threading.Lock()
    mutated = False

    def bundle(*, destination: Path, **_kwargs: Any) -> tuple[str, str]:
        nonlocal mutated
        destination.write_bytes(b"bundle")
        with mutation_lock:
            if not mutated:
                exposure_path.write_bytes(b"changed")
                mutated = True
        return "f" * 40, _sha256(destination.read_bytes())

    monkeypatch.setattr(prepare, "_create_authenticated_bundle", bundle)
    private = tmp_path / "private-map"
    private.mkdir(mode=0o700)

    with pytest.raises(prepare.PrepareError, match="inputs changed"):
        prepare.prepare_sources(
            protocol_path=protocol_path,
            expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
            exposure_ledger_path=exposure_path,
            rows_path=rows_path,
            selection_path=selection_path,
            cache_root=private / "bundles",
            output_path=private / "source-map.json",
            workers=4,
            root=tmp_path,
        )

    assert not (private / "source-map.json").exists()
    assert not (private / "bundles").exists()


def test_prepare_sources_failure_publishes_nothing_and_removes_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        exposure_path,
        protocol_path,
        rows_path,
        selection_path,
        _protocol_value,
        _selection,
    ) = _source_fixture(tmp_path, monkeypatch)
    calls = 0
    lock = threading.Lock()

    def bundle(*, destination: Path, **_kwargs: Any) -> tuple[str, str]:
        nonlocal calls
        with lock:
            calls += 1
            should_fail = calls == 3
        destination.write_bytes(b"bundle")
        if should_fail:
            raise prepare.PrepareError("source bundle authentication failed")
        return "f" * 40, _sha256(destination.read_bytes())

    monkeypatch.setattr(prepare, "_create_authenticated_bundle", bundle)
    private = tmp_path / "map-parent"
    private.mkdir(mode=0o700)
    cache = private / "bundles"
    output = private / "source-map.json"

    with pytest.raises(prepare.PrepareError, match="authentication failed"):
        prepare.prepare_sources(
            protocol_path=protocol_path,
            expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
            exposure_ledger_path=exposure_path,
            rows_path=rows_path,
            selection_path=selection_path,
            cache_root=cache,
            output_path=output,
            workers=4,
            root=tmp_path,
        )

    assert calls >= 3
    assert not output.exists()
    assert not cache.exists()


def test_prepare_sources_preserves_output_created_by_a_racing_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        exposure_path,
        protocol_path,
        rows_path,
        selection_path,
        _protocol_value,
        _selection,
    ) = _source_fixture(tmp_path, monkeypatch)

    def bundle(*, destination: Path, **_kwargs: Any) -> tuple[str, str]:
        destination.write_bytes(b"bundle")
        return "f" * 40, _sha256(destination.read_bytes())

    monkeypatch.setattr(prepare, "_create_authenticated_bundle", bundle)
    monkeypatch.setattr(prepare, "_harden_private_tree", lambda _path: None)
    private = tmp_path / "map-parent"
    private.mkdir(mode=0o700)
    output = private / "source-map.json"
    cache = private / "bundles"
    raced_bytes = b"created by another writer"

    def race(path: Path, _data: bytes) -> Path:
        path.write_bytes(raced_bytes)
        raise prepare.PrepareError("preparation output already exists")

    monkeypatch.setattr(prepare, "_atomic_private_write", race)

    with pytest.raises(prepare.PrepareError, match="already exists"):
        prepare.prepare_sources(
            protocol_path=protocol_path,
            expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
            exposure_ledger_path=exposure_path,
            rows_path=rows_path,
            selection_path=selection_path,
            cache_root=cache,
            output_path=output,
            workers=4,
            root=tmp_path,
        )

    assert output.read_bytes() == raced_bytes
    assert not cache.exists()


def test_prepare_sources_rejects_nested_cache_and_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        exposure_path,
        protocol_path,
        rows_path,
        selection_path,
        _protocol_value,
        _selection,
    ) = _source_fixture(tmp_path, monkeypatch)
    cache = tmp_path / "private" / "cache"
    cache.parent.mkdir(mode=0o700)

    with pytest.raises(prepare.PrepareError, match="overlap"):
        prepare.prepare_sources(
            protocol_path=protocol_path,
            expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
            exposure_ledger_path=exposure_path,
            rows_path=rows_path,
            selection_path=selection_path,
            cache_root=cache,
            output_path=cache / "source-map.json",
            root=tmp_path,
        )


def test_write_environment_matches_freezer_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _protocol()
    protocol_path = _private_file(
        tmp_path / "private" / "protocol.json",
        _canonical(protocol, newline=True),
    )
    repository = tmp_path / "repo"
    repository.mkdir()
    state = prepare.RepositoryState(repository, REVISION, ORIGIN_URL, REVISION)
    codex = prepare.CodexIdentity(
        path=tmp_path / "codex",
        sha256=protocol["product_inputs"]["codex_binary_sha256"],
        version="codex 1.2.3",
        provider_config_sha256=protocol["product_inputs"]["provider_config_sha256"],
    )
    python = prepare.PythonIdentity(
        path=tmp_path / "python",
        sha256="e" * 64,
        version="3.12.11",
        dependencies_sha256="9" * 64,
    )
    snapshot = (codex, deepcopy(PINS), python)
    monkeypatch.setattr(prepare, "_repository_state", lambda _root: state)
    monkeypatch.setattr(prepare, "_assert_repository_unchanged", lambda _state: None)
    monkeypatch.setattr(prepare.freezer, "validate_acquisition_protocol", lambda *_a, **_k: PINS)
    monkeypatch.setattr(prepare, "_runtime_snapshot", lambda **_kwargs: snapshot)
    output = tmp_path / "environment" / "execution-environment.json"
    output.parent.mkdir(mode=0o700)

    digest = prepare.write_environment(
        protocol_path=protocol_path,
        expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
        output_path=output,
        model="gpt-5.5",
        model_reasoning_effort=MODEL_REASONING_EFFORT,
        model_auto_compact_token_limit=MODEL_AUTO_COMPACT_TOKEN_LIMIT,
        provider="openai",
        agent_timeout_seconds=900,
        codex_path=tmp_path / "codex",
        execution_python=tmp_path / "python",
        swebench_checkout=tmp_path / "swebench",
        swebench_python=tmp_path / "swebench-python",
        docker_cli=tmp_path / "docker",
        docker_host="unix:///tmp/docker.sock",
        root=tmp_path,
    )

    document = json.loads(output.read_bytes())
    assert output.read_bytes() == _canonical(document)
    assert digest == _sha256(output.read_bytes())
    assert document["limits"] == {
        "agent_timeout_seconds": 900,
        "arms": ["baseline", "ctx-light"],
        "catalog_cache_hit": False,
        "measured_concurrency": 1,
        "pair_count": 30,
        "retries": 0,
        "sandbox_contract": benchmark.OFFICIAL_SANDBOX_CONTRACT,
        "task_count": 10,
        "trials_per_scenario": 3,
    }
    assert document["codex"] == {
        "runtime_contract": CODEX_RUNTIME_CONTRACT,
        "version": "codex 1.2.3",
    }
    assert document["python"] == {
        "dependencies_sha256": python.dependencies_sha256,
        "executable_sha256": python.sha256,
        "version": python.version,
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_write_environment_rejects_runtime_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _protocol()
    protocol_path = _private_file(
        tmp_path / "private" / "protocol.json",
        _canonical(protocol, newline=True),
    )
    repository = tmp_path / "repo"
    repository.mkdir()
    state = prepare.RepositoryState(repository, REVISION, ORIGIN_URL, REVISION)
    codex = prepare.CodexIdentity(
        path=tmp_path / "codex",
        sha256=protocol["product_inputs"]["codex_binary_sha256"],
        version="codex 1",
        provider_config_sha256=protocol["product_inputs"]["provider_config_sha256"],
    )
    python = prepare.PythonIdentity(
        tmp_path / "python",
        "e" * 64,
        "3.12.11",
        "9" * 64,
    )
    snapshots = [
        (codex, deepcopy(PINS), python),
        (codex, {**PINS, "docker_daemon_id": "drifted"}, python),
    ]
    monkeypatch.setattr(prepare, "_repository_state", lambda _root: state)
    monkeypatch.setattr(prepare, "_assert_repository_unchanged", lambda _state: None)
    monkeypatch.setattr(prepare.freezer, "validate_acquisition_protocol", lambda *_a, **_k: PINS)
    monkeypatch.setattr(prepare, "_runtime_snapshot", lambda **_kwargs: snapshots.pop(0))
    output = tmp_path / "output" / "environment.json"
    output.parent.mkdir(mode=0o700)

    with pytest.raises(prepare.PrepareError, match="changed"):
        prepare.write_environment(
            protocol_path=protocol_path,
            expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
            output_path=output,
            model="gpt-5.5",
            model_reasoning_effort=MODEL_REASONING_EFFORT,
            model_auto_compact_token_limit=MODEL_AUTO_COMPACT_TOKEN_LIMIT,
            provider="openai",
            agent_timeout_seconds=900,
            codex_path=tmp_path / "codex",
            execution_python=tmp_path / "python",
            swebench_checkout=tmp_path / "swebench",
            swebench_python=tmp_path / "swebench-python",
            docker_cli=tmp_path / "docker",
            docker_host="unix:///tmp/docker.sock",
            root=tmp_path,
        )
    assert not output.exists()


def test_write_environment_rejects_noncanonical_model() -> None:
    with pytest.raises(prepare.PrepareError, match="arguments"):
        prepare.write_environment(
            protocol_path=Path("/protocol"),
            expected_acquisition_protocol_sha256="0" * 64,
            output_path=Path("/environment"),
            model=" gpt-5.5 ",
            model_reasoning_effort=MODEL_REASONING_EFFORT,
            model_auto_compact_token_limit=MODEL_AUTO_COMPACT_TOKEN_LIMIT,
            provider="openai",
            agent_timeout_seconds=900,
            codex_path=Path("/codex"),
            execution_python=Path("/python"),
            swebench_checkout=Path("/swebench"),
            swebench_python=Path("/swebench-python"),
            docker_cli=Path("/docker"),
            docker_host="unix:///tmp/docker.sock",
        )


@pytest.mark.parametrize(
    ("reasoning_effort", "auto_compact_token_limit"),
    [("HIGH", MODEL_AUTO_COMPACT_TOKEN_LIMIT), (MODEL_REASONING_EFFORT, 0)],
)
def test_write_environment_rejects_noncanonical_codex_runtime_contract(
    reasoning_effort: str,
    auto_compact_token_limit: int,
) -> None:
    with pytest.raises(prepare.PrepareError, match="runtime contract"):
        prepare.write_environment(
            protocol_path=Path("/protocol"),
            expected_acquisition_protocol_sha256="0" * 64,
            output_path=Path("/environment"),
            model="gpt-5.5",
            model_reasoning_effort=reasoning_effort,
            model_auto_compact_token_limit=auto_compact_token_limit,
            provider="openai",
            agent_timeout_seconds=900,
            codex_path=Path("/codex"),
            execution_python=Path("/python"),
            swebench_checkout=Path("/swebench"),
            swebench_python=Path("/swebench-python"),
            docker_cli=Path("/docker"),
            docker_host="unix:///tmp/docker.sock",
        )


def test_cli_forwards_source_workers_and_prints_no_private_identifiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, Any] = {}

    def sources(**kwargs: Any) -> str:
        observed.update(kwargs)
        return "f" * 64

    monkeypatch.setattr(prepare, "prepare_sources", sources)

    assert (
        prepare.main(
            [
                "sources",
                "--protocol",
                str(tmp_path / "protocol"),
                "--expected-acquisition-protocol-sha256",
                "0" * 64,
                "--exposure-ledger",
                str(tmp_path / "exposure"),
                "--rows",
                str(tmp_path / "rows"),
                "--selection",
                str(tmp_path / "selection"),
                "--cache-root",
                str(tmp_path / "cache"),
                "--output",
                str(tmp_path / "map"),
                "--workers",
                "7",
                "--failure-evidence-output",
                str(tmp_path / "unused-failure-evidence"),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert observed["workers"] == 7
    assert observed["exposure_ledger_path"] == tmp_path / "exposure"
    assert captured.err == ""
    assert captured.out == (
        f"prepared 10 authenticated source bundles source_map_sha256={'f' * 64}\n"
    )
    assert "private-task" not in captured.out


def test_cli_suppresses_private_failure_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(**_kwargs: Any) -> str:
        raise KeyboardInterrupt("private-task-id and private task text")

    monkeypatch.setattr(prepare, "prepare_sources", fail)
    failure_root = tmp_path / "prepare-failure-evidence"

    with pytest.raises(SystemExit) as raised:
        prepare.main(
            [
                "sources",
                "--protocol",
                str(tmp_path / "protocol"),
                "--expected-acquisition-protocol-sha256",
                "0" * 64,
                "--exposure-ledger",
                str(tmp_path / "exposure"),
                "--rows",
                str(tmp_path / "rows"),
                "--selection",
                str(tmp_path / "selection"),
                "--cache-root",
                str(tmp_path / "cache"),
                "--output",
                str(tmp_path / "map"),
                "--failure-evidence-output",
                str(failure_root),
            ]
        )

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert captured.err == "benchmark preparation failed (KeyboardInterrupt); evidence=preserved\n"
    assert "private-task" not in captured.err
    failure = json.loads((failure_root / "failure.json").read_text(encoding="utf-8"))
    assert failure["operation"] == "holdout-prepare-sources"
    assert failure["exception_chain"] == [
        {"message": "private-task-id and private task text", "type": "KeyboardInterrupt"}
    ]
    assert (failure_root / "artifact-manifest.json").is_file()


def test_cli_privately_bounds_oversized_command_failure_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_stdout = "private-task-id-" + "x" * prepare.PRIVATE_COMMAND_TEXT_LIMIT
    private_stderr = "private-task-error-" + "y" * prepare.PRIVATE_COMMAND_TEXT_LIMIT
    result = prepare.swebench.CommandResult(19, private_stdout, private_stderr, 0.5)
    monkeypatch.setattr(prepare.swebench, "_run_process", lambda *_args, **_kwargs: result)

    def fail(**_kwargs: Any) -> str:
        prepare._command_bytes(["git", "fsck"], cwd=tmp_path, timeout=5)
        raise AssertionError("unreachable")

    monkeypatch.setattr(prepare, "prepare_sources", fail)
    failure_root = tmp_path / "oversized-command-failure"

    with pytest.raises(SystemExit) as raised:
        prepare.main(
            [
                "sources",
                "--protocol",
                str(tmp_path / "protocol"),
                "--expected-acquisition-protocol-sha256",
                "0" * 64,
                "--exposure-ledger",
                str(tmp_path / "exposure"),
                "--rows",
                str(tmp_path / "rows"),
                "--selection",
                str(tmp_path / "selection"),
                "--cache-root",
                str(tmp_path / "cache"),
                "--output",
                str(tmp_path / "map"),
                "--failure-evidence-output",
                str(failure_root),
            ]
        )

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert captured.err == "benchmark preparation failed (PrepareError); evidence=preserved\n"
    assert "private-task" not in captured.err
    failure = json.loads((failure_root / "failure.json").read_text(encoding="utf-8"))
    message = failure["exception_chain"][0]["message"]
    prefix = "authenticated preparation command failed; private_result="
    evidence = json.loads(message.removeprefix(prefix))
    for stream, raw in (("stdout", private_stdout), ("stderr", private_stderr)):
        stream_evidence = evidence[stream]
        encoded = raw.encode()
        assert stream_evidence["bytes"] == len(encoded)
        assert stream_evidence["sha256"] == _sha256(encoded)
        assert len(stream_evidence["text"].encode()) == prepare.PRIVATE_COMMAND_TEXT_LIMIT
        assert stream_evidence["truncated"] is True
    assert (failure_root / "artifact-manifest.json").is_file()


def test_cli_rejects_used_failure_destination_before_private_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def sources(**_kwargs: Any) -> str:
        calls.append("called")
        return "f" * 64

    monkeypatch.setattr(prepare, "prepare_sources", sources)
    failure_root = tmp_path / "used-failure-evidence"
    failure_root.mkdir()
    argv = [
        "sources",
        "--protocol",
        str(tmp_path / "protocol"),
        "--expected-acquisition-protocol-sha256",
        "0" * 64,
        "--exposure-ledger",
        str(tmp_path / "exposure"),
        "--rows",
        str(tmp_path / "rows"),
        "--selection",
        str(tmp_path / "selection"),
        "--cache-root",
        str(tmp_path / "cache"),
        "--output",
        str(tmp_path / "map"),
        "--failure-evidence-output",
        str(failure_root),
    ]

    with pytest.raises(SystemExit) as raised:
        prepare.main(argv)

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert calls == []
    assert captured.out == ""
    assert captured.err == ("benchmark preparation precondition failed; evidence=unavailable\n")
