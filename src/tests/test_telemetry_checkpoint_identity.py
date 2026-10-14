from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import stat
from typing import Any, Callable, Iterator

import pytest

import ctx.telemetry as telemetry
from ctx.telemetry import record_counter, record_event


@dataclass
class TelemetryCase:
    signal: str
    root: Path
    path: Path
    salt_path: Path
    checkpoint_path: Path
    config: dict[str, Any]
    export_config: dict[str, Any]
    monkeypatch: pytest.MonkeyPatch
    calls: list[dict[str, Any]]
    use_global_config: bool = False

    def append(self, *, continuous: bool = False) -> Any:
        config: dict[str, Any] = (
            self.config if continuous else {"privacy": {"hash_salt": "fixture-key"}}
        )
        record: telemetry.TelemetryEvent | telemetry.TelemetryMetric | None
        if self.signal == "metrics":
            if not continuous:
                config["metrics"] = {"enabled": True}
            record = record_counter(
                "ctx.api.requests", path=self.path, trusted_root=self.root, config=config
            )
        else:
            record = record_event(
                "ctx.api.request",
                source="ctx-api",
                path=self.path,
                trusted_root=self.root,
                config=config,
            )
        assert record is not None
        return record

    def export(self, *, include_exported: bool = False) -> Any:
        exporter = {
            "events": telemetry.export_events,
            "metrics": telemetry.export_metrics,
            "traces": telemetry.export_traces,
        }[self.signal]
        return exporter(
            self.path,
            trusted_root=self.root,
            config=None if self.use_global_config else self.config,
            include_exported=include_exported,
        )

    def preview(self) -> Any:
        previewer = {
            "events": telemetry.preview_export,
            "metrics": telemetry.preview_metrics_export,
            "traces": telemetry.preview_traces_export,
        }[self.signal]

        def reject_creation(path: Path) -> str:
            pytest.fail(f"preview attempted salt creation: {path}")

        before = self.snapshot()
        calls_before = len(self.calls)
        try:
            with self.monkeypatch.context() as readonly:
                readonly.setattr(telemetry, "_read_or_create_hash_salt", reject_creation)
                return previewer(
                    self.path,
                    trusted_root=self.root,
                    config=None if self.use_global_config else self.config,
                )
        finally:
            assert self.snapshot() == before
            assert len(self.calls) == calls_before

    def snapshot(self) -> dict[Path, tuple[int, int, bytes | None]]:
        return {
            item.relative_to(self.root): (
                stat.S_IMODE(item.stat().st_mode),
                item.stat().st_mtime_ns,
                item.read_bytes() if item.is_file() else None,
            )
            for item in [self.root, *self.root.rglob("*")]
        }

    def checkpoint(self) -> dict[str, Any]:
        return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))

    def write_checkpoint(self, payload: dict[str, Any]) -> bytes:
        self.checkpoint_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return self.checkpoint_path.read_bytes()

    def legacy_checkpoint(self) -> bytes:
        payload = self.checkpoint()
        payload.pop("checkpoint_identity", None)
        return self.write_checkpoint(payload)

    def hashes(self) -> tuple[str, str]:
        payload = self.checkpoint()
        return payload["source_path_hash"], payload["destination_hash"]

    def block_lock(self) -> None:
        lock = self.salt_path.with_suffix(self.salt_path.suffix + ".lock")
        lock.unlink(missing_ok=True)
        lock.mkdir()

    def restore_lock(self) -> None:
        self.salt_path.with_suffix(self.salt_path.suffix + ".lock").rmdir()

    def block_storage(self) -> None:
        shutil.rmtree(self.salt_path.parent)
        self.salt_path.parent.write_text("not a directory", encoding="utf-8")

    def restore_storage(self, salt: str | None) -> None:
        self.salt_path.parent.unlink()
        self.salt_path.parent.mkdir()
        if salt is not None:
            self.salt_path.write_text(salt, encoding="utf-8")


@pytest.fixture(params=["events", "metrics", "traces"])
def checkpoint_case(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TelemetryCase:
    signal = str(request.param)
    for variable in (
        "CTX_TELEMETRY_HASH_SALT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    ):
        monkeypatch.delenv(variable, raising=False)
    path = tmp_path / "spool.jsonl"
    salt_path = tmp_path / "identity" / "hash-salt"
    salt_path.parent.mkdir()
    salt_path.write_text("salt-a", encoding="utf-8")
    checkpoint_path = tmp_path / "checkpoint.json"
    otlp_signal = "logs" if signal == "events" else signal
    export_config = {
        "enabled": True,
        "sink": "otlp_http",
        "span_maturity_seconds": 0,
        "checkpoint_path": str(checkpoint_path),
        "otlp": {"endpoint": f"http://127.0.0.1:4318/v1/{otlp_signal}"},
    }
    config: dict[str, Any] = {"privacy": {"hash_salt_path": str(salt_path)}}
    scope = {"enabled": True, "path": str(path), "export": export_config}
    if signal == "events":
        config.update(scope)
    else:
        config[signal] = scope
    calls: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any], settings: dict[str, Any]) -> None:
        calls.append(payload)

    monkeypatch.setattr(telemetry, "_post_otlp_http", fake_post)
    monkeypatch.setattr(
        telemetry,
        "_config_get",
        lambda key, default: {"privacy": config["privacy"]} if key == "telemetry" else default,
    )
    return TelemetryCase(
        signal,
        tmp_path,
        path,
        salt_path,
        checkpoint_path,
        config,
        export_config,
        monkeypatch,
        calls,
    )


def test_checkpoint_identity_survives_lock_failure_and_recovery(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    original_hashes = case.hashes()
    case.block_lock()
    case.append()
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert case.hashes() == original_hashes
    case.restore_lock()
    case.append()
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert case.hashes() == original_hashes
    assert case.preview().attempted == 0
    assert case.export().attempted == 0


def test_checkpoint_identity_survives_initial_lock_failure(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    case.block_lock()
    case.append()
    exported = case.export()
    assert exported.exported == 1
    original_hashes = case.hashes()
    assert (
        original_hashes[0]
        == "sha256:"
        + hashlib.sha256(b"ctx.telemetry.v1\x00" + str(case.path).encode("utf-8")).hexdigest()
    )
    assert case.preview().attempted == 0
    assert case.preview().destination_hash == exported.destination_hash
    case.restore_lock()
    assert case.preview().attempted == 0
    assert case.export().attempted == 0
    assert case.hashes() == original_hashes
    case.append()
    assert case.export().exported == 1
    assert case.preview().attempted == 0


def test_checkpoint_identity_survives_storage_recovery_and_detects_rotation(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    case.block_storage()
    case.append()
    assert case.export().exported == 1
    original_hashes = case.hashes()
    case.restore_storage(None)
    assert case.preview().attempted == 0
    assert not case.salt_path.exists()
    assert case.export().attempted == 0
    assert case.salt_path.is_file()
    assert case.hashes() == original_hashes
    case.salt_path.write_text("deliberately-rotated", encoding="utf-8")
    assert case.preview().attempted == 1
    assert case.export().exported == 1


def test_checkpoint_identity_distinguishes_regeneration_from_rotation(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    original_hashes = case.hashes()
    case.salt_path.unlink()
    case.append()
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert case.salt_path.read_text(encoding="utf-8").strip() != "salt-a"
    assert case.hashes() == original_hashes
    assert case.preview().attempted == 0
    case.salt_path.write_text("deliberately-rotated", encoding="utf-8")
    assert case.preview().attempted == 2
    assert case.export().exported == 2


@pytest.mark.parametrize("rotation", ["file", "inline", "env"])
def test_legacy_unsalted_checkpoint_adopts_file_identity_before_rotation(
    checkpoint_case: TelemetryCase,
    rotation: str,
) -> None:
    case = checkpoint_case
    case.block_storage()
    case.append()
    assert case.export().exported == 1
    original_hashes = case.hashes()
    legacy = case.legacy_checkpoint()
    case.restore_storage("first-adopted-key")
    assert case.preview().attempted == 0
    assert case.checkpoint_path.read_bytes() == legacy
    assert case.export().attempted == 0
    assert case.hashes() == original_hashes
    assert case.checkpoint()["checkpoint_identity"]["version"] == 2
    if rotation == "file":
        case.salt_path.write_text("rotated-key", encoding="utf-8")
    elif rotation == "inline":
        case.config["privacy"] = {"hash_salt": "rotated-key"}
    else:
        case.monkeypatch.setenv("CTX_TELEMETRY_HASH_SALT", "rotated-key")
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert case.hashes() != original_hashes


@pytest.mark.parametrize("policy", ["inline", "env"])
def test_checkpoint_identity_respects_explicit_key_rotation(
    checkpoint_case: TelemetryCase,
    policy: str,
) -> None:
    case = checkpoint_case
    if policy == "inline":
        case.config["privacy"] = {"hash_salt": "explicit-a"}
    else:
        case.monkeypatch.setenv("CTX_TELEMETRY_HASH_SALT", "explicit-a")
    case.append()
    assert case.export().exported == 1
    if policy == "inline":
        case.config["privacy"] = {"hash_salt": "explicit-b"}
    else:
        case.monkeypatch.setenv("CTX_TELEMETRY_HASH_SALT", "explicit-b")
    assert case.preview().attempted == 1
    assert case.export().exported == 1


def test_legacy_keyed_checkpoint_requires_unavailable_key_or_explicit_replay(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    original = case.legacy_checkpoint()
    case.block_storage()
    with pytest.raises(ValueError, match="checkpoint identity unavailable"):
        case.preview()
    with pytest.raises(ValueError, match="checkpoint identity unavailable"):
        case.export()
    assert case.checkpoint_path.read_bytes() == original
    assert len(case.calls) == 1
    assert case.export(include_exported=True).exported == 1


@pytest.mark.parametrize("checkpoint_case", ["events", "metrics"], indirect=True)
def test_continuous_capture_retains_records_when_legacy_identity_is_unavailable(
    checkpoint_case: TelemetryCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    original = case.legacy_checkpoint()
    case.block_storage()
    assert case.append(continuous=True) is not None
    assert len(case.path.read_text(encoding="utf-8").splitlines()) == 2
    assert case.checkpoint_path.read_bytes() == original
    assert len(case.calls) == 1
    assert "checkpoint identity unavailable" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("changed", ["source", "endpoint", "sink"])
def test_checkpoint_identity_keeps_source_and_destination_scoped(
    checkpoint_case: TelemetryCase,
    changed: str,
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    if changed == "source":
        next_path = case.root / "other-spool.jsonl"
        next_path.write_bytes(case.path.read_bytes())
        case.path = next_path
    elif changed == "endpoint":
        case.export_config["otlp"]["endpoint"] = "http://127.0.0.1:4318/v1/other"
    else:
        checkpoint = case.checkpoint()
        checkpoint["sink"] = "other"
        case.write_checkpoint(checkpoint)
    preview = case.preview()
    assert preview.attempted == 1
    assert preview.checkpoint_found is False


@pytest.mark.parametrize("checkpoint_case", ["events", "traces"], indirect=True)
def test_checkpoint_identity_separates_signals_at_the_same_endpoint(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    case.signal = "traces" if case.signal == "events" else "events"
    scope = {"enabled": True, "path": str(case.path), "export": case.export_config}
    case.config.clear()
    case.config["privacy"] = {"hash_salt_path": str(case.salt_path)}
    if case.signal == "events":
        case.config.update(scope)
    else:
        case.config["traces"] = scope
    preview = case.preview()
    assert preview.attempted == 1
    assert preview.checkpoint_found is False


@pytest.mark.parametrize(
    "damage",
    [
        "unknown_version",
        "boolean_version",
        "missing_scope",
        "missing_policy",
        "bad_file_keys",
        "bad_file_key_value",
        "missing_history",
        "bad_history",
        "bad_history_path",
        "bad_history_value",
        "bad_last_known_map",
        "bad_last_known_path",
        "bad_last_known_value",
        "null_last_known_value",
        "missing_last_known_key",
        "unknown_last_known_key",
        "inconsistent_last_known_key",
        "not_mapping",
    ],
)
def test_checkpoint_identity_never_treats_invalid_metadata_as_legacy(
    checkpoint_case: TelemetryCase,
    damage: str,
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    checkpoint = case.checkpoint()
    identity = checkpoint["checkpoint_identity"]
    if damage == "unknown_version":
        identity["version"] = 999
    elif damage == "boolean_version":
        identity["version"] = True
    elif damage == "missing_scope":
        identity.pop("scope")
    elif damage == "missing_policy":
        identity.pop("policy")
    elif damage == "bad_file_keys":
        identity["file_keys"] = ["invalid"]
    elif damage == "bad_file_key_value":
        identity["file_keys"][next(iter(identity["file_keys"]))] = "not-a-fingerprint"
    elif damage == "missing_history":
        identity.pop("file_generation_history")
    elif damage == "bad_history":
        identity["file_generation_history"] = []
    elif damage == "bad_history_path":
        identity["file_generation_history"]["raw-path"] = []
    elif damage == "bad_history_value":
        identity["file_generation_history"][next(iter(identity["file_keys"]))] = ["raw-key"]
    elif damage == "bad_last_known_map":
        identity["file_last_known_keys"] = []
    elif damage == "bad_last_known_path":
        identity["file_last_known_keys"]["raw-path"] = next(iter(identity["file_keys"].values()))
    elif damage == "bad_last_known_value":
        identity["file_last_known_keys"][next(iter(identity["file_keys"]))] = "raw-key"
    elif damage == "null_last_known_value":
        identity["file_last_known_keys"][next(iter(identity["file_keys"]))] = None
    elif damage == "missing_last_known_key":
        identity["file_last_known_keys"] = {}
    elif damage in ("unknown_last_known_key", "inconsistent_last_known_key"):
        file_hash = next(iter(identity["file_keys"]))
        different_fingerprint = "sha256:" + "0" * 64
        identity["file_last_known_keys"][file_hash] = different_fingerprint
        if damage == "inconsistent_last_known_key":
            identity["file_generation_history"][file_hash].append(different_fingerprint)
    else:
        checkpoint["checkpoint_identity"] = []
    case.write_checkpoint(checkpoint)
    assert case.preview().attempted == 1
    assert case.export().exported == 1


def test_checkpoint_identity_noop_migration_preserves_a_concurrent_newer_checkpoint(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    case.block_storage()
    case.append()
    assert case.export().exported == 1
    case.legacy_checkpoint()
    case.restore_storage("first-adopted-key")
    original_lock = telemetry.file_lock
    newer_bytes: bytes | None = None

    @contextmanager
    def concurrent_lock(path: Path, *args: Any, **kwargs: Any) -> Iterator[None]:
        nonlocal newer_bytes
        with original_lock(path, *args, **kwargs):
            if path == case.checkpoint_path and newer_bytes is None:
                newer = case.checkpoint()
                key = "last_metric_id" if case.signal == "metrics" else "last_event_id"
                newer[key] = "concurrent-newer-record"
                newer_bytes = case.write_checkpoint(newer)
            yield

    case.monkeypatch.setattr(telemetry, "file_lock", concurrent_lock)
    assert case.export().attempted == 0
    assert newer_bytes is not None
    assert case.checkpoint_path.read_bytes() == newer_bytes


def test_legacy_keyed_checkpoint_matches_readable_key_during_lock_failure(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    original_hashes = case.hashes()
    legacy = case.legacy_checkpoint()
    case.block_lock()
    assert case.preview().attempted == 0
    assert case.checkpoint_path.read_bytes() == legacy
    assert case.export().attempted == 0
    assert case.hashes() == original_hashes
    assert case.checkpoint()["checkpoint_identity"]["version"] == 2
    case.restore_lock()
    assert case.preview().attempted == 0


def test_checkpoint_identity_tracks_local_file_recovery_over_global_fallback(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    global_privacy = dict(case.config["privacy"])
    local_parent = case.root / "local-identity"
    local_parent.write_text("not a directory", encoding="utf-8")
    local_salt = local_parent / "hash-salt"
    case.config["privacy"] = {"hash_salt_path": str(local_salt)}
    case.monkeypatch.setattr(
        telemetry,
        "_config_get",
        lambda key, default: {"privacy": global_privacy} if key == "telemetry" else default,
    )
    case.append()
    assert case.export().exported == 1
    original_hashes = case.hashes()
    local_parent.unlink()
    local_parent.mkdir()
    local_salt.write_text("different-local-key", encoding="utf-8")
    assert case.preview().attempted == 0
    assert case.export().attempted == 0
    assert case.hashes() == original_hashes
    case.append()
    assert case.export().exported == 1
    local_salt.write_text("deliberately-rotated-local-key", encoding="utf-8")
    assert case.preview().attempted == 2
    assert case.export().exported == 2


@pytest.mark.parametrize("recovered_key", ["salt-a", "replacement-key"])
def test_checkpoint_identity_remembers_key_through_unavailable_storage_exports(
    checkpoint_case: TelemetryCase,
    recovered_key: str,
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    original_hashes = case.hashes()
    case.block_storage()
    case.append()
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert case.hashes() == original_hashes
    case.restore_storage(recovered_key)
    expected = 0 if recovered_key == "salt-a" else 2
    assert case.preview().attempted == expected
    assert case.export().attempted == expected


@pytest.mark.parametrize("policy", ["inline", "env"])
def test_legacy_unsalted_checkpoint_never_adopts_an_explicit_key(
    checkpoint_case: TelemetryCase,
    policy: str,
) -> None:
    case = checkpoint_case
    case.block_storage()
    case.append()
    assert case.export().exported == 1
    case.legacy_checkpoint()
    if policy == "inline":
        case.config["privacy"] = {"hash_salt": "new-explicit-key"}
    else:
        case.monkeypatch.setenv("CTX_TELEMETRY_HASH_SALT", "new-explicit-key")
    assert case.preview().attempted == 1
    assert case.export().exported == 1


@pytest.mark.parametrize("entrypoint", ["partial", "global"])
def test_checkpoint_identity_storage_transition_uses_effective_configuration(
    checkpoint_case: TelemetryCase,
    entrypoint: str,
) -> None:
    case = checkpoint_case
    global_config = dict(case.config)
    case.monkeypatch.setattr(
        telemetry,
        "_config_get",
        lambda key, default: global_config if key == "telemetry" else default,
    )
    if entrypoint == "partial":
        case.config.pop("privacy")
    else:
        case.use_global_config = True
    case.append()
    assert case.export().exported == 1
    case.block_lock()
    case.append()
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    case.restore_lock()
    assert case.preview().attempted == 0
    assert case.export().attempted == 0


@pytest.mark.parametrize("loss", ["missing", "empty", "external_regeneration"])
def test_legacy_missing_key_does_not_become_replayable_after_generation(
    checkpoint_case: TelemetryCase,
    loss: str,
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    legacy = case.legacy_checkpoint()
    if loss == "empty":
        case.salt_path.write_text("", encoding="utf-8")
    else:
        case.salt_path.unlink()
    if loss == "external_regeneration":
        telemetry.hash_identifier("unrelated-identifier")
    with pytest.raises(ValueError, match="checkpoint identity unavailable"):
        case.preview()
    for _ in range(2):
        with pytest.raises(ValueError, match="checkpoint identity unavailable"):
            case.export()
        with pytest.raises(ValueError, match="checkpoint identity unavailable"):
            case.preview()
    assert len(case.calls) == 1
    assert case.checkpoint_path.read_bytes() == legacy


@pytest.mark.parametrize("regenerator", ["hash", "capture"])
def test_checkpoint_identity_recognizes_generation_by_another_operation(
    checkpoint_case: TelemetryCase,
    regenerator: str,
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    case.salt_path.unlink()
    if regenerator == "hash":
        telemetry.hash_identifier("unrelated-identifier")
    else:
        captured = record_event(
            "unrelated.capture",
            source="ctx-api",
            path=case.root / "other-capture.jsonl",
            trusted_root=case.root,
            config={"privacy": dict(case.config["privacy"]), "export": {"enabled": False}},
        )
        assert captured is not None
    generated_key = case.salt_path.read_text(encoding="utf-8")
    marker = case.salt_path.with_suffix(case.salt_path.suffix + ".generation.json")
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert generated_key.strip() not in marker.read_text(encoding="utf-8")
    case.append()
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert case.preview().attempted == 0
    case.salt_path.write_text("manual-rotation", encoding="utf-8")
    assert case.preview().attempted == 2
    assert case.export().exported == 2
    case.salt_path.write_text(generated_key, encoding="utf-8")
    assert case.preview().attempted == 2
    assert case.export().exported == 2


@pytest.mark.parametrize("fallback", ["inline", "file"])
def test_checkpoint_identity_rotates_only_the_selected_fallback_key(
    checkpoint_case: TelemetryCase,
    fallback: str,
) -> None:
    case = checkpoint_case
    fallback_path = case.root / "fallback-salt"
    if fallback == "file":
        fallback_path.write_text("fallback-a", encoding="utf-8")
        global_privacy = {"hash_salt_path": str(fallback_path)}
    else:
        global_privacy = {"hash_salt": "fallback-a"}
    case.monkeypatch.setattr(
        telemetry,
        "_config_get",
        lambda key, default: {"privacy": global_privacy} if key == "telemetry" else default,
    )
    case.append()
    assert case.export().exported == 1
    if fallback == "file":
        fallback_path.write_text("fallback-b", encoding="utf-8")
    else:
        global_privacy["hash_salt"] = "fallback-b"
    assert case.preview().attempted == 0
    assert case.export().attempted == 0
    case.block_storage()
    case.append()
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    if fallback == "file":
        fallback_path.write_text("fallback-c", encoding="utf-8")
    else:
        global_privacy["hash_salt"] = "fallback-c"
    assert case.preview().attempted == 2
    assert case.export().exported == 2


def test_checkpoint_identity_remembers_stale_generation_on_its_first_export(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    case.salt_path.unlink()
    telemetry.hash_identifier("unrelated-identifier")
    generated_key = case.salt_path.read_text(encoding="utf-8")
    case.salt_path.write_text("manual-key-before-first-export", encoding="utf-8")
    case.append()
    assert case.export().exported == 1
    case.salt_path.write_text(generated_key, encoding="utf-8")
    assert case.preview().attempted == 1
    assert case.export().exported == 1


@pytest.mark.parametrize("fallback", ["inline", "env"])
def test_legacy_unsalted_checkpoint_resets_for_selected_explicit_global_fallback(
    checkpoint_case: TelemetryCase,
    fallback: str,
) -> None:
    case = checkpoint_case
    case.block_storage()
    case.append()
    assert case.export().exported == 1
    original_hashes = case.hashes()
    case.legacy_checkpoint()
    if fallback == "inline":
        global_privacy = {"hash_salt": "new-explicit-global-key"}
    else:
        case.monkeypatch.setenv("CTX_REVIEW_FALLBACK_SALT", "new-explicit-global-key")
        global_privacy = {"hash_salt_env": "CTX_REVIEW_FALLBACK_SALT"}
    case.monkeypatch.setattr(
        telemetry,
        "_config_get",
        lambda key, default: {"privacy": global_privacy} if key == "telemetry" else default,
    )
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert case.hashes() != original_hashes
    assert case.preview().attempted == 0


@pytest.mark.parametrize("changed", ["source", "endpoint"])
def test_legacy_checkpoint_with_available_generated_key_remains_scoped(
    checkpoint_case: TelemetryCase,
    changed: str,
) -> None:
    case = checkpoint_case
    case.salt_path.unlink()
    telemetry.hash_identifier("bootstrap-key")
    case.append()
    assert case.export().exported == 1
    case.legacy_checkpoint()
    if changed == "source":
        next_path = case.root / "other-spool.jsonl"
        next_path.write_bytes(case.path.read_bytes())
        case.path = next_path
    else:
        case.export_config["otlp"]["endpoint"] = "http://127.0.0.1:4318/v1/other"
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert case.preview().attempted == 0


def test_legacy_keyed_checkpoint_does_not_adopt_an_existing_explicit_fallback(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    case.monkeypatch.setattr(
        telemetry,
        "_config_get",
        lambda key, default: (
            {"privacy": {"hash_salt": "existing-global-fallback"}}
            if key == "telemetry"
            else default
        ),
    )
    case.append()
    assert case.export().exported == 1
    original = case.legacy_checkpoint()
    case.block_storage()
    with pytest.raises(ValueError, match="checkpoint identity unavailable"):
        case.preview()
    with pytest.raises(ValueError, match="checkpoint identity unavailable"):
        case.export()
    assert case.checkpoint_path.read_bytes() == original
    assert len(case.calls) == 1


@pytest.mark.parametrize("intervening_export", ["normal", "noop", "replay", "endpoint", "rotation"])
def test_checkpoint_identity_recognizes_restored_observed_generation(
    checkpoint_case: TelemetryCase,
    intervening_export: str,
) -> None:
    case = checkpoint_case
    case.salt_path.unlink()
    case.append()
    assert case.export().exported == 1
    saved_key = case.salt_path.read_bytes()
    marker = case.salt_path.with_suffix(case.salt_path.suffix + ".generation.json")
    saved_marker = marker.read_bytes()
    case.salt_path.unlink()
    if intervening_export != "noop":
        case.append()
    expected_records = 1 if intervening_export == "noop" else 2
    assert case.export().exported == expected_records - 1
    assert case.preview().attempted == 0
    if intervening_export == "replay":
        assert case.export(include_exported=True).exported == expected_records
    elif intervening_export == "endpoint":
        case.export_config["otlp"]["endpoint"] = "http://127.0.0.1:4318/v1/other"
        assert case.export().exported == expected_records
    elif intervening_export == "rotation":
        case.salt_path.write_text("deliberate-intermediate-key", encoding="utf-8")
        assert case.export().exported == expected_records
    checkpoint_text = case.checkpoint_path.read_text(encoding="utf-8")
    assert saved_key.decode().strip() not in checkpoint_text
    assert case.salt_path.read_text(encoding="utf-8").strip() not in checkpoint_text
    case.salt_path.write_bytes(saved_key)
    marker.write_bytes(saved_marker)
    assert case.preview().attempted == expected_records
    assert case.export().exported == expected_records
    assert case.preview().attempted == 0


@pytest.mark.parametrize("initially_available", [False, True])
@pytest.mark.parametrize("legacy_policy", [False, True])
def test_checkpoint_identity_ignores_unused_custom_env_availability(
    checkpoint_case: TelemetryCase,
    initially_available: bool,
    legacy_policy: bool,
) -> None:
    case = checkpoint_case
    variable = "CTX_REVIEW_UNUSED_CUSTOM_SALT"
    global_privacy = {"hash_salt_env": variable}
    case.monkeypatch.delenv(variable, raising=False)
    if initially_available:
        case.monkeypatch.setenv(variable, "unused-fallback-key")
    case.monkeypatch.setattr(
        telemetry,
        "_config_get",
        lambda key, default: {"privacy": global_privacy} if key == "telemetry" else default,
    )
    case.append()
    assert case.export().exported == 1
    original_hashes = case.hashes()
    if legacy_policy:
        checkpoint = case.checkpoint()
        identity = checkpoint["checkpoint_identity"]
        identity["version"] = 1
        identity.pop("file_last_known_keys", None)
        identity.pop("file_generation_history", None)
        local_selector = next(iter(identity["file_keys"]))
        old_policy = [["file", local_selector]]
        if initially_available:
            env_selector = (
                "sha256:"
                + hashlib.sha256(b"ctx.telemetry.v1\x00" + f"env:{variable}".encode()).hexdigest()
            )
            old_policy.append(["env", env_selector])
        else:
            old_policy.append(["unsalted"])
        identity["policy"] = (
            "sha256:"
            + hashlib.sha256(
                b"ctx.telemetry.v1\x00" + json.dumps(old_policy, separators=(",", ":")).encode()
            ).hexdigest()
        )
        case.write_checkpoint(checkpoint)
    for available in (not initially_available, initially_available):
        if available:
            case.monkeypatch.setenv(variable, "changed-unused-fallback-key")
        else:
            case.monkeypatch.delenv(variable, raising=False)
        assert case.preview().attempted == 0
        assert case.export().attempted == 0
        assert case.hashes() == original_hashes
        case.append()
        assert case.export().exported == 1
        assert case.preview().attempted == 0


def test_checkpoint_identity_preserves_selected_custom_env_rotation(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    variable = "CTX_REVIEW_SELECTED_CUSTOM_SALT"
    case.config["privacy"] = {"hash_salt_env": variable}
    case.monkeypatch.setenv(variable, "selected-key-a")
    case.append()
    assert case.export().exported == 1
    case.monkeypatch.setenv(variable, "selected-key-b")
    assert case.preview().attempted == 1
    assert case.export().exported == 1


def test_checkpoint_identity_ignores_malformed_unused_fallback(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    fallback_path = case.root / "unused-global-salt"
    global_privacy = {"hash_salt_path": str(fallback_path)}
    case.monkeypatch.setattr(
        telemetry,
        "_config_get",
        lambda key, default: {"privacy": global_privacy} if key == "telemetry" else default,
    )
    case.append()
    assert case.export().exported == 1
    original_hashes = case.hashes()
    fallback_path.write_bytes(b"\xff\xfeinvalid-key")
    assert case.preview().attempted == 0
    assert case.export().attempted == 0
    case.append()
    assert case.export().exported == 1
    assert case.hashes() == original_hashes
    assert case.preview().attempted == 0


@pytest.mark.parametrize("checkpoint_case", ["events", "metrics"], indirect=True)
def test_capture_retains_records_with_malformed_unused_fallback(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    fallback_path = case.root / "unused-global-salt"
    fallback_path.write_bytes(b"\xff\xfeinvalid-key")
    case.monkeypatch.setattr(
        telemetry,
        "_config_get",
        lambda key, default: (
            {"privacy": {"hash_salt_path": str(fallback_path)}} if key == "telemetry" else default
        ),
    )
    assert case.append(continuous=True) is not None
    assert len(case.path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(case.calls) == 1
    assert case.preview().attempted == 0


def test_checkpoint_identity_rejects_malformed_selected_key(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    checkpoint = case.checkpoint_path.read_bytes()
    case.salt_path.write_bytes(b"\xff\xfeinvalid-selected-key")
    with pytest.raises(UnicodeDecodeError):
        case.preview()
    with pytest.raises(UnicodeDecodeError):
        case.export()
    assert case.checkpoint_path.read_bytes() == checkpoint
    assert len(case.calls) == 1


@pytest.mark.parametrize("initially_available", [False, True])
@pytest.mark.parametrize("variable", ["CTX_REVIEW_SELECTED_CUSTOM_SALT", "CTX_TELEMETRY_HASH_SALT"])
def test_checkpoint_identity_resets_for_selected_env_availability(
    checkpoint_case: TelemetryCase,
    initially_available: bool,
    variable: str,
) -> None:
    case = checkpoint_case
    case.config["privacy"]["hash_salt_env"] = variable
    case.monkeypatch.delenv(variable, raising=False)
    if initially_available:
        case.monkeypatch.setenv(variable, "selected-env-key")
    case.append()
    assert case.export().exported == 1
    if initially_available:
        case.monkeypatch.delenv(variable)
    else:
        case.monkeypatch.setenv(variable, "selected-env-key")
    assert case.preview().attempted == 1
    assert case.export().exported == 1


@pytest.mark.parametrize("explicit_key", ["inline", "custom_env", "default_env"])
@pytest.mark.parametrize("generated_key", [False, True])
def test_legacy_keyed_checkpoint_resets_for_new_selected_explicit_key(
    checkpoint_case: TelemetryCase,
    explicit_key: str,
    generated_key: bool,
) -> None:
    case = checkpoint_case
    if generated_key:
        case.salt_path.unlink()
    case.append()
    assert case.export().exported == 1
    original_hashes = case.hashes()
    case.legacy_checkpoint()
    if explicit_key == "inline":
        case.config["privacy"]["hash_salt"] = "new-selected-key"
    else:
        variable = (
            "CTX_TELEMETRY_HASH_SALT"
            if explicit_key == "default_env"
            else "CTX_REVIEW_SELECTED_CUSTOM_SALT"
        )
        case.config["privacy"]["hash_salt_env"] = variable
        case.monkeypatch.setenv(variable, "new-selected-key")
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert case.hashes() != original_hashes


@pytest.mark.parametrize("intermediate_policy", ["inline", "other_file"])
def test_checkpoint_identity_remembers_generations_across_policy_resets(
    checkpoint_case: TelemetryCase,
    intermediate_policy: str,
) -> None:
    case = checkpoint_case
    case.salt_path.unlink()
    case.append()
    assert case.export().exported == 1
    marker = case.salt_path.with_suffix(case.salt_path.suffix + ".generation.json")
    first_key, first_marker = case.salt_path.read_bytes(), marker.read_bytes()
    case.salt_path.unlink()
    assert case.export().attempted == 0
    second_key, second_marker = case.salt_path.read_bytes(), marker.read_bytes()
    if intermediate_policy == "inline":
        case.config["privacy"] = {"hash_salt": "intermediate-inline-key"}
    else:
        other_salt = case.root / "other-file-salt"
        other_salt.write_text("intermediate-file-key", encoding="utf-8")
        case.config["privacy"] = {"hash_salt_path": str(other_salt)}
    assert case.export().exported == 1
    case.config["privacy"] = {"hash_salt_path": str(case.salt_path)}
    case.salt_path.write_bytes(first_key)
    marker.write_bytes(first_marker)
    assert case.export().exported == 1
    case.salt_path.write_bytes(second_key)
    marker.write_bytes(second_marker)
    assert case.preview().attempted == 1
    assert case.export().exported == 1


@pytest.mark.parametrize("previous_key", ["current_generation", "manual_with_stale_marker"])
def test_checkpoint_identity_migrates_known_v1_generations(
    checkpoint_case: TelemetryCase,
    previous_key: str,
) -> None:
    case = checkpoint_case
    case.salt_path.unlink()
    telemetry.hash_identifier("bootstrap-generation")
    marker = case.salt_path.with_suffix(case.salt_path.suffix + ".generation.json")
    saved_key, saved_marker = case.salt_path.read_bytes(), marker.read_bytes()
    if previous_key == "manual_with_stale_marker":
        case.salt_path.write_text("manual-before-migration", encoding="utf-8")
    case.append()
    assert case.export().exported == 1
    checkpoint = case.checkpoint()
    identity = checkpoint["checkpoint_identity"]
    identity["version"] = 1
    identity.pop("file_last_known_keys", None)
    identity.pop("file_generation_history")
    old_policy = [["file", next(iter(identity["file_keys"]))], ["unsalted"]]
    identity["policy"] = (
        "sha256:"
        + hashlib.sha256(
            b"ctx.telemetry.v1\x00" + json.dumps(old_policy, separators=(",", ":")).encode()
        ).hexdigest()
    )
    case.write_checkpoint(checkpoint)
    case.salt_path.unlink()
    assert case.preview().attempted == 0
    assert case.export().attempted == 0
    case.salt_path.write_bytes(saved_key)
    marker.write_bytes(saved_marker)
    assert case.preview().attempted == 1
    assert case.export().exported == 1


@pytest.mark.parametrize("scope_reset", ["endpoint", "replay"])
@pytest.mark.parametrize("restored_generation", ["a", "b"])
@pytest.mark.parametrize("policy_detour", ["none", "inline", "other_file"])
def test_checkpoint_identity_retains_last_file_key_through_unavailable_resets(
    checkpoint_case: TelemetryCase,
    scope_reset: str,
    restored_generation: str,
    policy_detour: str,
) -> None:
    case = checkpoint_case
    case.salt_path.unlink()
    case.append()
    assert case.export().exported == 1
    marker = case.salt_path.with_suffix(case.salt_path.suffix + ".generation.json")
    generations = {"a": (case.salt_path.read_bytes(), marker.read_bytes())}
    case.salt_path.unlink()
    case.append()
    assert case.export().exported == 1
    generations["b"] = case.salt_path.read_bytes(), marker.read_bytes()
    assert generations["a"][0] != generations["b"][0]
    case.block_storage()
    if policy_detour != "none":
        original_privacy = case.config["privacy"]
        if policy_detour == "inline":
            case.config["privacy"] = {"hash_salt": "detour-inline-key"}
        else:
            other_salt = case.root / "detour-salt"
            other_salt.write_text("detour-file-key", encoding="utf-8")
            case.config["privacy"] = {"hash_salt_path": str(other_salt)}
        assert case.export().exported == 2
        case.config["privacy"] = original_privacy
        assert case.export().exported == 2
    if scope_reset == "endpoint":
        case.export_config["otlp"]["endpoint"] = "http://127.0.0.1:4318/v1/reset"
        assert case.export().exported == 2
    else:
        assert case.export(include_exported=True).exported == 2
    assert case.preview().attempted == 0
    checkpoint_text = case.checkpoint_path.read_text(encoding="utf-8")
    for key_bytes, _ in generations.values():
        assert key_bytes.decode().strip() not in checkpoint_text
    case.restore_storage(None)
    restored_key, restored_marker = generations[restored_generation]
    case.salt_path.write_bytes(restored_key)
    marker.write_bytes(restored_marker)
    calls_before = len(case.calls)
    expected_replayed = 2 if restored_generation == "a" else 0
    assert case.preview().attempted == expected_replayed
    assert case.export().exported == expected_replayed
    assert len(case.calls) == calls_before + (1 if expected_replayed else 0)
    assert case.preview().attempted == 0
    assert case.export().attempted == 0
    assert len(case.calls) == calls_before + (1 if expected_replayed else 0)
    case.append()
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert case.preview().attempted == 0


@pytest.fixture(
    params=[
        ("env", None),
        ("env", "replacement-fallback-key-c"),
        ("inline", "replacement-fallback-key-c"),
        ("file", "replacement-fallback-key-c"),
    ],
    ids=["env-absent", "env-replaced", "inline-replaced", "file-replaced"],
)
def recovered_legacy_fallback_case(
    request: pytest.FixtureRequest,
    checkpoint_case: TelemetryCase,
) -> tuple[TelemetryCase, Callable[[str | None], None], bytes]:
    case = checkpoint_case
    fallback_kind, replacement = request.param
    variable = "CTX_REVIEW_LEGACY_GLOBAL_SALT"
    fallback_key = "legacy-fallback-key-b"
    fallback_path = case.root / "global-fallback-salt"
    global_privacy: dict[str, str] = {}
    if fallback_kind == "env":
        global_privacy["hash_salt_env"] = variable
    elif fallback_kind == "file":
        global_privacy["hash_salt_path"] = str(fallback_path)

    def set_fallback(value: str | None) -> None:
        if fallback_kind == "env":
            if value is None:
                case.monkeypatch.delenv(variable, raising=False)
            else:
                case.monkeypatch.setenv(variable, value)
        else:
            assert value is not None
            if fallback_kind == "inline":
                global_privacy["hash_salt"] = value
            else:
                fallback_path.write_text(value, encoding="utf-8")

    set_fallback(fallback_key)
    case.monkeypatch.setattr(
        telemetry,
        "_config_get",
        lambda key, default: {"privacy": global_privacy} if key == "telemetry" else default,
    )
    assert case.config["privacy"] == {"hash_salt_path": str(case.salt_path)}
    case.block_lock()
    record = case.append()
    exported = case.export()
    assert exported.exported == 1
    destination = f"otlp_http:{case.export_config['otlp']['endpoint']}"
    if case.signal == "metrics":
        destination = f"metrics:{destination}"
    source_hash = (
        "sha256:"
        + hmac.new(fallback_key.encode(), str(case.path).encode(), hashlib.sha256).hexdigest()
    )
    destination_hash = (
        "sha256:"
        + hmac.new(fallback_key.encode(), destination.encode(), hashlib.sha256).hexdigest()
    )
    assert exported.destination_hash == destination_hash
    assert case.salt_path.read_text(encoding="utf-8") == "salt-a"
    legacy: dict[str, Any] = {
        "schema_version": (
            telemetry.METRIC_SCHEMA_VERSION
            if case.signal == "metrics"
            else telemetry.SCHEMA_VERSION
        ),
        "updated_at": record.ts,
        "sink": "otlp_http",
        "source_path_hash": source_hash,
        "destination_hash": destination_hash,
    }
    if case.signal == "metrics":
        legacy.update(last_metric_id=record.metric_id, last_metric_ts=record.ts)
    else:
        legacy.update(last_event_id=record.event_id, last_event_ts=record.ts)
    original = case.write_checkpoint(legacy)
    case.restore_lock()
    set_fallback(replacement)
    return case, set_fallback, original


@pytest.mark.parametrize("recovery", ["restore_fallback", "explicit_replay"])
def test_legacy_fallback_checkpoint_rejects_ambiguous_recovery_until_resolved(
    recovered_legacy_fallback_case: tuple[TelemetryCase, Callable[[str | None], None], bytes],
    recovery: str,
) -> None:
    case, set_fallback, original = recovered_legacy_fallback_case
    for _ in range(2):
        with pytest.raises(ValueError, match="checkpoint identity unavailable"):
            case.preview()
        with pytest.raises(ValueError, match="checkpoint identity unavailable"):
            case.export()
        assert case.checkpoint_path.read_bytes() == original
        assert len(case.calls) == 1
        assert case.salt_path.read_text(encoding="utf-8") == "salt-a"
    if recovery == "restore_fallback":
        set_fallback("legacy-fallback-key-b")
        assert case.preview().attempted == 0
        assert case.checkpoint_path.read_bytes() == original
        assert case.export().attempted == 0
        assert len(case.calls) == 1
        assert "checkpoint_identity" in case.checkpoint()
        set_fallback("replacement-fallback-key-c")
    else:
        assert case.export(include_exported=True).exported == 1
        assert len(case.calls) == 2
    checkpoint_text = case.checkpoint_path.read_text(encoding="utf-8")
    assert "salt-a" not in checkpoint_text
    assert "legacy-fallback-key-b" not in checkpoint_text
    assert "replacement-fallback-key-c" not in checkpoint_text
    calls_before = len(case.calls)
    assert case.preview().attempted == 0
    assert case.export().attempted == 0
    assert len(case.calls) == calls_before
    case.append()
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert len(case.calls) == calls_before + 1
    assert case.preview().attempted == 0


@pytest.mark.parametrize("changed_scope", ["none", "source", "endpoint"])
def test_legacy_fallback_checkpoint_matches_available_original_key(
    recovered_legacy_fallback_case: tuple[TelemetryCase, Callable[[str | None], None], bytes],
    changed_scope: str,
) -> None:
    case, set_fallback, original = recovered_legacy_fallback_case
    set_fallback("legacy-fallback-key-b")
    if changed_scope == "source":
        new_path = case.root / "other-spool.jsonl"
        new_path.write_bytes(case.path.read_bytes())
        case.path = new_path
    elif changed_scope == "endpoint":
        case.export_config["otlp"]["endpoint"] = "http://127.0.0.1:4318/v1/other"
    expected = 0 if changed_scope == "none" else 1
    assert case.preview().attempted == expected
    assert case.checkpoint_path.read_bytes() == original
    assert case.export().exported == expected
    assert len(case.calls) == 1 + expected
    assert "checkpoint_identity" in case.checkpoint()
    assert case.preview().attempted == 0
    assert case.export().attempted == 0


@pytest.mark.parametrize("global_path", ["same", "alias"])
def test_legacy_single_file_rotation_does_not_gain_an_implicit_alternative(
    checkpoint_case: TelemetryCase,
    global_path: str,
) -> None:
    case = checkpoint_case
    fallback_path = case.salt_path
    if global_path == "alias":
        alias = case.root / "identity-alias"
        alias.symlink_to(case.salt_path.parent, target_is_directory=True)
        fallback_path = alias / case.salt_path.name
    case.monkeypatch.setattr(
        telemetry,
        "_config_get",
        lambda key, default: (
            {"privacy": {"hash_salt_path": str(fallback_path)}} if key == "telemetry" else default
        ),
    )
    case.append()
    assert case.export().exported == 1
    original = case.legacy_checkpoint()
    case.salt_path.write_text("deliberately-rotated-key", encoding="utf-8")
    assert case.preview().attempted == 1
    assert case.checkpoint_path.read_bytes() == original
    assert case.export().exported == 1
    assert case.preview().attempted == 0
    assert case.export().attempted == 0


@pytest.mark.parametrize("checkpoint_case", ["events", "metrics"], indirect=True)
def test_continuous_capture_retains_records_during_legacy_fallback_ambiguity(
    recovered_legacy_fallback_case: tuple[TelemetryCase, Callable[[str | None], None], bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    case, set_fallback, original = recovered_legacy_fallback_case
    assert case.append(continuous=True) is not None
    assert len(case.path.read_text(encoding="utf-8").splitlines()) == 2
    assert case.checkpoint_path.read_bytes() == original
    assert len(case.calls) == 1
    assert "checkpoint identity unavailable" in capsys.readouterr().err.lower()
    set_fallback("legacy-fallback-key-b")
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert case.preview().attempted == 0


def test_checkpoint_identity_upgrades_v2_without_last_known_keys(
    checkpoint_case: TelemetryCase,
) -> None:
    case = checkpoint_case
    case.append()
    assert case.export().exported == 1
    checkpoint = case.checkpoint()
    identity = checkpoint["checkpoint_identity"]
    assert identity["version"] == 2
    identity.pop("file_last_known_keys")
    old_checkpoint = case.write_checkpoint(checkpoint)
    assert case.preview().attempted == 0
    assert case.checkpoint_path.read_bytes() == old_checkpoint
    assert case.export().attempted == 0
    assert len(case.calls) == 1
    migrated = case.checkpoint()["checkpoint_identity"]
    assert migrated["file_last_known_keys"] == identity["file_keys"]
    case.append()
    assert case.preview().attempted == 1
    assert case.export().exported == 1
    assert case.preview().attempted == 0


@pytest.mark.parametrize("restored_key", ["known_a", "known_b", "generated_c", "manual_d"])
def test_checkpoint_identity_migrates_v2_with_lost_last_known_key(
    checkpoint_case: TelemetryCase,
    restored_key: str,
) -> None:
    case = checkpoint_case
    case.salt_path.unlink()
    case.append()
    assert case.export().exported == 1
    marker = case.salt_path.with_suffix(case.salt_path.suffix + ".generation.json")
    generations = {"known_a": (case.salt_path.read_bytes(), marker.read_bytes())}
    case.salt_path.unlink()
    case.append()
    assert case.export().exported == 1
    generations["known_b"] = case.salt_path.read_bytes(), marker.read_bytes()
    case.block_storage()
    case.export_config["otlp"]["endpoint"] = "http://127.0.0.1:4318/v1/legacy-reset"
    assert case.export().exported == 2
    checkpoint = case.checkpoint()
    identity = checkpoint["checkpoint_identity"]
    file_hash = next(iter(identity["file_keys"]))
    identity.pop("file_last_known_keys")
    identity["file_keys"][file_hash] = None
    identity["file_generations"][file_hash] = None
    identity["selector"] = None
    identity["key_fingerprint"] = None
    assert len(identity["file_generation_history"][file_hash]) == 2
    original = case.write_checkpoint(checkpoint)
    case.restore_storage(None)
    calls_before = len(case.calls)
    if restored_key in generations:
        key_bytes, marker_bytes = generations[restored_key]
        case.salt_path.write_bytes(key_bytes)
        marker.write_bytes(marker_bytes)
        with pytest.raises(ValueError, match="restore checkpoint metadata.*--all"):
            case.preview()
        with pytest.raises(ValueError, match="restore checkpoint metadata.*--all"):
            case.export()
        assert case.checkpoint_path.read_bytes() == original
        assert len(case.calls) == calls_before
        assert case.export(include_exported=True).exported == 2
        assert len(case.calls) == calls_before + 1
    elif restored_key == "generated_c":
        assert case.preview().attempted == 0
        assert case.checkpoint_path.read_bytes() == original
        assert not case.salt_path.exists()
        assert case.export().attempted == 0
        assert case.salt_path.read_bytes() not in {
            key_bytes for key_bytes, _ in generations.values()
        }
        assert len(case.calls) == calls_before
    else:
        case.salt_path.write_text("new-manual-key-d", encoding="utf-8")
        assert case.preview().attempted == 2
        assert case.checkpoint_path.read_bytes() == original
        assert case.export().exported == 2
        assert len(case.calls) == calls_before + 1
    assert case.preview().attempted == 0
    assert case.export().attempted == 0
    case.append()
    assert case.export().exported == 1
    assert case.preview().attempted == 0
