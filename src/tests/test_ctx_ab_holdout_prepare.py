from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
from typing import Any

import pytest

from scripts import ctx_ab_benchmark as benchmark
from scripts import ctx_ab_holdout as holdout
from scripts import ctx_ab_holdout_freeze as freezer
from scripts import ctx_ab_holdout_prepare as prepare


REVISION = "a" * 40
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


def _product_inputs() -> dict[str, str]:
    return {
        "benchmark_script_sha256": "8" * 64,
        "catalog_archive_sha256": "9" * 64,
        "codex_binary_sha256": "a" * 64,
        "provider_config_sha256": benchmark.codex_provider_config_sha256("openai"),
        "revision": REVISION,
        "runtime_availability_sha256": "b" * 64,
    }


def _protocol(*, rows_sha256: str = "c" * 64, expected_rows: int = 10) -> dict[str, Any]:
    protocol = prepare._build_v2_protocol(
        v1=_v1(),
        revision=REVISION,
        frozen_at="2026-07-30T10:00:00Z",
        product_inputs=_product_inputs(),
        verifier_pins=PINS,
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
) -> tuple[Path, Path, Path, dict[str, Any], dict[str, Any]]:
    required_columns = list(_v1()["universe"]["required_columns"])
    rows = [_row(index, required_columns) for index in range(10)]
    rows_bytes = b"".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n" for row in rows
    )
    protocol = _protocol(rows_sha256=_sha256(rows_bytes))
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
        lambda _root: prepare.RepositoryState(repository, REVISION),
    )
    monkeypatch.setattr(prepare, "_assert_repository_unchanged", lambda _state: None)
    monkeypatch.setattr(prepare.holdout, "evaluate_row", lambda row, _protocol: row)
    monkeypatch.setattr(prepare.holdout, "select_rows", lambda _rows, _protocol: selection)
    monkeypatch.setattr(
        prepare.holdout,
        "_validated_selection",
        lambda _selection, _protocol: (ids, repository_map),
    )
    return protocol_path, rows_path, selection_path, protocol, selection


def test_build_v2_protocol_preserves_universe_and_sets_exact_design() -> None:
    v1 = _v1()
    protocol = prepare._build_v2_protocol(
        v1=v1,
        revision=REVISION,
        frozen_at="2026-07-30T10:00:00Z",
        product_inputs=_product_inputs(),
        verifier_pins=PINS,
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
        "ctx_context": [],
        "eligible_candidates_per_repository_required": 1,
        "eligible_repositories_required": 10,
        "first_scenario_rule": (
            "first ranked candidate from each of the first ten ranked eligible repositories"
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


def test_protocol_generation_changes_seed_and_selection_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v1 = _v1()
    generation_one = prepare._build_v2_protocol(
        v1=v1,
        revision=REVISION,
        frozen_at="2026-07-30T10:00:00Z",
        product_inputs=_product_inputs(),
        verifier_pins=PINS,
    )
    monkeypatch.setattr(freezer, "PROTOCOL_GENERATION", 2)
    generation_two = prepare._build_v2_protocol(
        v1=v1,
        revision=REVISION,
        frozen_at="2026-07-30T10:00:00Z",
        product_inputs=_product_inputs(),
        verifier_pins=PINS,
    )
    repositories = [f"https://github.com/owner/repo-{index}.git" for index in range(10)]

    assert generation_one["protocol_generation"] == 1
    assert generation_two["protocol_generation"] == 2
    assert generation_one["selection_seed"] != generation_two["selection_seed"]
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
    protocol = prepare._build_v2_protocol(
        v1=_v1(),
        revision=REVISION,
        frozen_at="2026-07-30T10:00:00Z",
        product_inputs=product_inputs,
        verifier_pins=PINS,
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


def test_committed_v1_must_match_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / prepare.V1_PROTOCOL_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(prepare, "_command_bytes", lambda *_a, **_k: b'{"changed":true}\n')

    with pytest.raises(prepare.PrepareError, match="does not match"):
        prepare._committed_v1_protocol(prepare.RepositoryState(tmp_path, REVISION))


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
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
    state = prepare.RepositoryState(root, REVISION)
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
    output = tmp_path / "private" / "protocol.json"
    output.parent.mkdir(mode=0o700)

    digest = prepare.create_protocol(
        output_path=output,
        codex_path=tmp_path / "codex",
        provider="openai",
        swebench_checkout=tmp_path / "swebench",
        swebench_python=tmp_path / "swebench-python",
        docker_cli=tmp_path / "docker",
        docker_host="unix:///tmp/docker.sock",
        frozen_at="2026-07-30T13:00:00+03:00",
        root=root,
    )

    document = json.loads(output.read_bytes())
    assert output.read_bytes() == _canonical(document, newline=True)
    assert digest == _sha256(output.read_bytes())
    assert document["frozen_at"] == "2026-07-30T10:00:00Z"
    assert document["product_inputs"]["revision"] == REVISION
    assert document["product_inputs"]["codex_binary_sha256"] == codex.sha256
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


def test_clone_authenticated_mirror_uses_clone_fetch_and_fsck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "mirror.git"
    url = "https://github.com/owner/repo.git"
    commit = "d" * 40
    commands: list[list[str]] = []

    def command(argv: list[str], **_kwargs: Any) -> bytes:
        commands.append(argv)
        if "clone" in argv:
            destination.mkdir()
        if "get-url" in argv:
            return (url + "\n").encode()
        if "rev-parse" in argv:
            return (commit + "\n").encode()
        return b""

    monkeypatch.setattr(prepare, "_command_bytes", command)

    prepare._clone_authenticated_mirror(url=url, commit=commit, destination=destination)

    assert any("clone" in command for command in commands)
    assert any("fetch" in command for command in commands)
    assert any("fsck" in command for command in commands)
    assert any("cat-file" in command for command in commands)


def test_prepare_sources_clones_in_bounded_parallel_and_writes_deterministic_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol_path, rows_path, selection_path, _protocol_value, _selection = _source_fixture(
        tmp_path,
        monkeypatch,
    )
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    destinations: set[Path] = set()

    def clone(*, url: str, commit: str, destination: Path) -> None:
        nonlocal active, maximum_active
        assert url.startswith("https://github.com/")
        assert prepare.REVISION.fullmatch(commit)
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            assert destination not in destinations
            destinations.add(destination)
        destination.mkdir()
        (destination / "HEAD").write_text(commit, encoding="utf-8")
        time.sleep(0.02)
        with lock:
            active -= 1

    monkeypatch.setattr(prepare, "_clone_authenticated_mirror", clone)
    cache = tmp_path / "private-cache" / "mirrors"
    cache.parent.mkdir(mode=0o700)
    output = tmp_path / "private-map" / "source-map.json"
    output.parent.mkdir(mode=0o700)

    digest = prepare.prepare_sources(
        protocol_path=protocol_path,
        expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
        rows_path=rows_path,
        selection_path=selection_path,
        cache_root=cache,
        output_path=output,
        workers=4,
        root=tmp_path,
    )

    source_map = json.loads(output.read_bytes())
    assert output.read_bytes() == _canonical(source_map)
    assert list(source_map) == sorted(source_map)
    assert len(source_map) == 10
    assert len(destinations) == 10
    assert 2 <= maximum_active <= 4
    assert all("private-task-" not in value for value in source_map.values())
    assert digest == _sha256(output.read_bytes())
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.parametrize("workers", [0, 9, True])
def test_prepare_sources_rejects_invalid_worker_count(
    tmp_path: Path,
    workers: Any,
) -> None:
    with pytest.raises(prepare.PrepareError, match="worker count"):
        prepare.prepare_sources(
            protocol_path=tmp_path / "protocol",
            expected_acquisition_protocol_sha256="0" * 64,
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
    protocol_path, rows_path, selection_path, _protocol_value, _selection = _source_fixture(
        tmp_path,
        monkeypatch,
    )
    clone_called = False

    def clone(**_kwargs: Any) -> None:
        nonlocal clone_called
        clone_called = True

    monkeypatch.setattr(prepare, "_clone_authenticated_mirror", clone)
    cache = tmp_path / "private-cache" / "mirrors"
    cache.parent.mkdir(mode=0o700)
    output = tmp_path / "private-map" / "source-map.json"
    output.parent.mkdir(mode=0o700)

    with pytest.raises(prepare.PrepareError, match="expected SHA-256"):
        prepare.prepare_sources(
            protocol_path=protocol_path,
            expected_acquisition_protocol_sha256="0" * 64,
            rows_path=rows_path,
            selection_path=selection_path,
            cache_root=cache,
            output_path=output,
            root=tmp_path,
        )

    assert clone_called is False
    assert not cache.exists()
    assert not output.exists()


def test_prepare_sources_failure_publishes_nothing_and_removes_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol_path, rows_path, selection_path, _protocol_value, _selection = _source_fixture(
        tmp_path,
        monkeypatch,
    )
    calls = 0
    lock = threading.Lock()

    def clone(*, destination: Path, **_kwargs: Any) -> None:
        nonlocal calls
        with lock:
            calls += 1
            should_fail = calls == 3
        destination.mkdir()
        if should_fail:
            raise prepare.PrepareError("source mirror authentication failed")

    monkeypatch.setattr(prepare, "_clone_authenticated_mirror", clone)
    cache = tmp_path / "cache-parent" / "mirrors"
    cache.parent.mkdir(mode=0o700)
    output = tmp_path / "map-parent" / "source-map.json"
    output.parent.mkdir(mode=0o700)

    with pytest.raises(prepare.PrepareError, match="authentication failed"):
        prepare.prepare_sources(
            protocol_path=protocol_path,
            expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
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
    protocol_path, rows_path, selection_path, _protocol_value, _selection = _source_fixture(
        tmp_path,
        monkeypatch,
    )
    monkeypatch.setattr(
        prepare,
        "_clone_authenticated_mirror",
        lambda **kwargs: Path(kwargs["destination"]).mkdir(),
    )
    monkeypatch.setattr(prepare, "_harden_private_tree", lambda _path: None)
    output = tmp_path / "map-parent" / "source-map.json"
    cache = tmp_path / "cache"
    raced_bytes = b"created by another writer"

    def race(path: Path, _data: bytes) -> Path:
        path.write_bytes(raced_bytes)
        raise prepare.PrepareError("preparation output already exists")

    monkeypatch.setattr(prepare, "_atomic_private_write", race)

    with pytest.raises(prepare.PrepareError, match="already exists"):
        prepare.prepare_sources(
            protocol_path=protocol_path,
            expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
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
    protocol_path, rows_path, selection_path, _protocol_value, _selection = _source_fixture(
        tmp_path,
        monkeypatch,
    )
    cache = tmp_path / "private" / "cache"
    cache.parent.mkdir(mode=0o700)

    with pytest.raises(prepare.PrepareError, match="overlap"):
        prepare.prepare_sources(
            protocol_path=protocol_path,
            expected_acquisition_protocol_sha256=_sha256(protocol_path.read_bytes()),
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
    state = prepare.RepositoryState(repository, REVISION)
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
        "measured_concurrency": 1,
        "pair_count": 30,
        "retries": 0,
        "sandbox_contract": benchmark.OFFICIAL_SANDBOX_CONTRACT,
        "task_count": 10,
        "trials_per_scenario": 3,
    }
    assert document["codex"] == {"version": "codex 1.2.3"}
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
    state = prepare.RepositoryState(repository, REVISION)
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
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert observed["workers"] == 7
    assert captured.err == ""
    assert captured.out == (
        f"prepared 10 authenticated source mirrors source_map_sha256={'f' * 64}\n"
    )
    assert "private-task" not in captured.out


def test_cli_suppresses_private_failure_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(**_kwargs: Any) -> str:
        raise prepare.PrepareError("private-task-id and private task text")

    monkeypatch.setattr(prepare, "prepare_sources", fail)

    with pytest.raises(SystemExit) as raised:
        prepare.main(
            [
                "sources",
                "--protocol",
                str(tmp_path / "protocol"),
                "--expected-acquisition-protocol-sha256",
                "0" * 64,
                "--rows",
                str(tmp_path / "rows"),
                "--selection",
                str(tmp_path / "selection"),
                "--cache-root",
                str(tmp_path / "cache"),
                "--output",
                str(tmp_path / "map"),
            ]
        )

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert captured.err == "benchmark preparation failed (PrepareError)\n"
    assert "private-task" not in captured.err
