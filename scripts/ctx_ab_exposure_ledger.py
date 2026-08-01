#!/usr/bin/env python3
"""Build and authenticate the private CTX benchmark exposure ledger."""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOT = ROOT / ".gate" / "ctx-ab-private"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SCHEMA_KEYS = {
    "schema_version",
    "salt",
    "instance_id_hmac_sha256",
}
MAX_PRIVATE_INPUT_BYTES = 64 * 1024 * 1024
MAX_INSTANCE_ID_BYTES = 4096


def canonical_ledger_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _validate_instance_id(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("exposure input contains an invalid task identity")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("exposure input contains an invalid task identity") from exc
    if len(encoded) > MAX_INSTANCE_ID_BYTES or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("exposure input contains an invalid task identity")
    return value


def validate_ledger_document(document: object) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != SCHEMA_KEYS:
        raise ValueError("exposure ledger schema is invalid")
    schema_version = document.get("schema_version")
    salt = document.get("salt")
    hashes = document.get("instance_id_hmac_sha256")
    if schema_version != 1 or isinstance(schema_version, bool):
        raise ValueError("exposure ledger schema is invalid")
    if not isinstance(salt, str) or SHA256.fullmatch(salt) is None:
        raise ValueError("exposure ledger salt is invalid")
    if (
        not isinstance(hashes, list)
        or not all(isinstance(value, str) and SHA256.fullmatch(value) for value in hashes)
        or hashes != sorted(hashes)
        or len(hashes) != len(set(hashes))
    ):
        raise ValueError("exposure ledger hash list is invalid")
    return document


def instance_id_hmac_sha256(salt: str, instance_id: str) -> str:
    if SHA256.fullmatch(salt) is None:
        raise ValueError("exposure ledger salt is invalid")
    identity = _validate_instance_id(instance_id)
    return hmac.new(
        bytes.fromhex(salt),
        identity.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _inside_repository(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    root = ROOT.resolve()
    return resolved == root or root in resolved.parents


def _inside_private_root(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    private_root = PRIVATE_ROOT.resolve()
    return resolved == private_root or private_root in resolved.parents


def _validate_private_path(path: Path, *, must_exist: bool) -> None:
    if _inside_repository(path) and not _inside_private_root(path):
        raise ValueError("private input inside the repository must use .gate/ctx-ab-private")
    if must_exist:
        if path.is_symlink() or not path.is_file():
            raise ValueError("private input must be a regular file")
        metadata = path.stat()
        if metadata.st_nlink != 1:
            raise ValueError("private input must be a single-link regular file")
        if metadata.st_size > MAX_PRIVATE_INPUT_BYTES:
            raise ValueError("private input exceeds the size limit")
        if (
            stat.S_IMODE(metadata.st_mode) & 0o077
            or stat.S_IMODE(path.parent.stat().st_mode) & 0o077
        ):
            raise ValueError("private input must be owner-only")


def _read_private_bytes(path: Path) -> bytes:
    _validate_private_path(path, must_exist=True)
    data = path.read_bytes()
    if len(data) > MAX_PRIVATE_INPUT_BYTES:
        raise ValueError("private input exceeds the size limit")
    return data


def _parse_canonical_ledger(data: bytes) -> dict[str, Any]:
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("exposure ledger JSON is invalid") from exc
    validated = validate_ledger_document(document)
    if data != canonical_ledger_bytes(validated):
        raise ValueError("exposure ledger must use exact canonical JSON bytes")
    return validated


def load_authenticated_ledger(path: Path, expected_sha256: str) -> dict[str, Any]:
    if SHA256.fullmatch(expected_sha256) is None:
        raise ValueError("exposure ledger SHA-256 is invalid")
    data = _read_private_bytes(path)
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise ValueError("exposure ledger does not match the authenticated SHA-256")
    document = _parse_canonical_ledger(data)
    if not document["instance_id_hmac_sha256"]:
        raise ValueError("authenticated exposure ledger must not be empty")
    return document


def load_private_ledger(path: Path) -> dict[str, Any]:
    return _parse_canonical_ledger(_read_private_bytes(path))


def contains_instance_id(document: dict[str, Any], instance_id: str) -> bool:
    validated = validate_ledger_document(document)
    digest = instance_id_hmac_sha256(str(validated["salt"]), instance_id)
    return digest in set(validated["instance_id_hmac_sha256"])


def _parse_json(data: bytes, *, label: str) -> Any:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} JSON is invalid") from exc


def _selection_ids(path: Path) -> list[str]:
    data = _read_private_bytes(path)
    document = _parse_json(data, label="private selection")
    if (
        not isinstance(document, dict)
        or data
        != json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ):
        raise ValueError("private selection must use canonical JSON bytes")
    analysis = document.get("analysis_instance_ids")
    repository_map = document.get("analysis_repository_map")
    canary = document.get("canary_instance_id")
    if (
        not isinstance(analysis, list)
        or not all(isinstance(value, str) for value in analysis)
        or not isinstance(repository_map, dict)
        or set(repository_map) != set(analysis)
        or not all(isinstance(value, str) for value in repository_map.values())
        or (canary is not None and not isinstance(canary, str))
    ):
        raise ValueError("private selection schema is invalid")
    identities = [_validate_instance_id(value) for value in analysis]
    if canary is not None:
        identities.append(_validate_instance_id(canary))
    if len(identities) != len(set(identities)):
        raise ValueError("private selection contains duplicate task identities")
    return identities


def _scenario_rows(rows: object) -> list[str]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("private evidence schema is invalid")
    identities: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("scenario"), str):
            raise ValueError("private evidence schema is invalid")
        identities.append(_validate_instance_id(row["scenario"]))
    return identities


def _evidence_ids(path: Path) -> list[str]:
    data = _read_private_bytes(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("private evidence CSV is invalid") from exc
        reader = csv.DictReader(text.splitlines())
        if reader.fieldnames is None or "scenario" not in reader.fieldnames:
            raise ValueError("private evidence schema is invalid")
        identities = [
            _validate_instance_id(row.get("scenario")) for row in reader if row is not None
        ]
        if not identities:
            raise ValueError("private evidence schema is invalid")
        return list(dict.fromkeys(identities))
    if suffix == ".jsonl":
        rows: list[Any] = []
        for line in data.splitlines():
            if not line:
                raise ValueError("private evidence JSONL is invalid")
            rows.append(_parse_json(line, label="private evidence"))
        return list(dict.fromkeys(_scenario_rows(rows)))
    if suffix != ".json":
        raise ValueError("private evidence must be JSON, JSONL, or CSV")
    document = _parse_json(data, label="private evidence")
    if isinstance(document, list):
        return list(dict.fromkeys(_scenario_rows(document)))
    if not isinstance(document, dict):
        raise ValueError("private evidence schema is invalid")
    if isinstance(document.get("scenario_ids"), list):
        identities = [_validate_instance_id(value) for value in document["scenario_ids"]]
    elif isinstance(document.get("scenario_results"), dict):
        identities = [_validate_instance_id(value) for value in document["scenario_results"]]
    else:
        raise ValueError("private evidence schema is invalid")
    if not identities:
        raise ValueError("private evidence schema is invalid")
    return list(dict.fromkeys(identities))


def _explicit_ids(path: Path) -> list[str]:
    data = _read_private_bytes(path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("explicit exposure input is not UTF-8") from exc
    lines = text.splitlines()
    if not lines:
        raise ValueError("explicit exposure input is empty")
    identities = [_validate_instance_id(value) for value in lines]
    if len(identities) != len(set(identities)):
        raise ValueError("explicit exposure input contains duplicate task identities")
    return identities


def _path_key(path: Path) -> tuple[str, tuple[int, int] | None]:
    resolved = str(path.resolve(strict=False))
    inode = None
    if path.exists():
        metadata = path.stat()
        inode = (metadata.st_dev, metadata.st_ino)
    return resolved, inode


def _require_distinct_paths(paths: Iterable[Path]) -> None:
    seen_resolved: set[str] = set()
    seen_inodes: set[tuple[int, int]] = set()
    for path in paths:
        resolved, inode = _path_key(path)
        if resolved in seen_resolved or (inode is not None and inode in seen_inodes):
            raise ValueError("exposure builder received duplicate input or output paths")
        seen_resolved.add(resolved)
        if inode is not None:
            seen_inodes.add(inode)


def _write_private_ledger(path: Path, payload: bytes) -> None:
    _validate_private_path(path, must_exist=False)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise ValueError("exposure ledger parent must be owner-only")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError("exposure ledger output must be a single-link regular file")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("exposure ledger output must be owner-only")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def build_exposure_ledger(
    *,
    output: Path,
    selection_paths: Iterable[Path] = (),
    evidence_paths: Iterable[Path] = (),
    instance_id_paths: Iterable[Path] = (),
    ledger_paths: Iterable[Path] = (),
    salt: str | None = None,
) -> dict[str, Any]:
    selections = list(selection_paths)
    evidence = list(evidence_paths)
    explicit = list(instance_id_paths)
    ledgers = list(ledger_paths)
    all_inputs = [*selections, *evidence, *explicit, *ledgers]
    if not all_inputs:
        raise ValueError("exposure builder requires at least one historical source input")
    _require_distinct_paths([*all_inputs, output])

    existing = [load_private_ledger(path) for path in ledgers]
    existing_salts = {str(document["salt"]) for document in existing}
    if len(existing_salts) > 1:
        raise ValueError("merged exposure ledgers use different salts")
    if salt is not None and SHA256.fullmatch(salt) is None:
        raise ValueError("exposure ledger salt is invalid")
    if existing_salts:
        existing_salt = next(iter(existing_salts))
        if salt is not None and not hmac.compare_digest(salt, existing_salt):
            raise ValueError("merged exposure ledger salt does not match")
        salt = existing_salt
    if salt is None:
        salt = secrets.token_hex(32)

    identities: list[str] = []
    for path in selections:
        identities.extend(_selection_ids(path))
    for path in evidence:
        identities.extend(_evidence_ids(path))
    for path in explicit:
        identities.extend(_explicit_ids(path))

    hashes = {digest for document in existing for digest in document["instance_id_hmac_sha256"]}
    hashes.update(instance_id_hmac_sha256(salt, identity) for identity in identities)
    if not hashes:
        raise ValueError("exposure ledger must contain at least one historical task hash")
    document = validate_ledger_document(
        {
            "schema_version": 1,
            "salt": salt,
            "instance_id_hmac_sha256": sorted(hashes),
        }
    )
    _write_private_ledger(output, canonical_ledger_bytes(document))
    return document


def _salt_from_file(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        value = _read_private_bytes(path).decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("exposure ledger salt file is invalid") from exc
    if SHA256.fullmatch(value) is None:
        raise ValueError("exposure ledger salt file is invalid")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", action="append", default=[], type=Path)
    parser.add_argument("--evidence", action="append", default=[], type=Path)
    parser.add_argument("--instance-id-file", action="append", default=[], type=Path)
    parser.add_argument("--merge-ledger", action="append", default=[], type=Path)
    parser.add_argument("--salt-file", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    extra_paths = [args.salt_file] if args.salt_file is not None else []
    _require_distinct_paths(
        [
            *args.selection,
            *args.evidence,
            *args.instance_id_file,
            *args.merge_ledger,
            *extra_paths,
            args.output,
        ]
    )
    document = build_exposure_ledger(
        output=args.output,
        selection_paths=args.selection,
        evidence_paths=args.evidence,
        instance_id_paths=args.instance_id_file,
        ledger_paths=args.merge_ledger,
        salt=_salt_from_file(args.salt_file),
    )
    print(hashlib.sha256(canonical_ledger_bytes(document)).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
