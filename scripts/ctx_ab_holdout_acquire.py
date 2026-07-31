#!/usr/bin/env python3
"""Canonicalize a revision-pinned Parquet holdout with a pinned DuckDB CLI."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, TextIO


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOT = ROOT / ".gate" / "ctx-ab-private"
_IS_WINDOWS = os.name == "nt"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sql_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def _private_text_handle(path: Path) -> TextIO:
    resolved = path.resolve(strict=False)
    private_root = PRIVATE_ROOT.resolve()
    if ROOT.resolve() in resolved.parents and private_root not in resolved.parents:
        raise ValueError("holdout evidence inside the repository must use .gate/ctx-ab-private")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _IS_WINDOWS and stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise ValueError("holdout evidence parent must be owner-only")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("holdout evidence path must be a regular file")
    if path.exists() and path.stat().st_nlink != 1:
        raise ValueError("holdout evidence path must not be a hard link")
    if path.exists():
        path.chmod(0o600)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    if not _IS_WINDOWS:
        os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="")


def _paths_are_distinct(paths: list[Path]) -> bool:
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if left.resolve(strict=False) == right.resolve(strict=False):
                return False
            if left.exists() and right.exists() and os.path.samefile(left, right):
                return False
    return True


def _canonicalize_duckdb_rows(
    raw_jsonl: str,
    *,
    required_columns: list[str],
    expected_rows: int,
) -> str:
    rows: list[dict[str, str]] = []
    for expected_index, line in enumerate(raw_jsonl.splitlines()):
        item = json.loads(line)
        if (
            not isinstance(item, dict)
            or list(item) != ["row_idx", *required_columns]
            or item["row_idx"] != expected_index
            or not all(isinstance(item[column], str) for column in required_columns)
        ):
            raise ValueError("DuckDB Parquet row is not canonical")
        rows.append({column: item[column] for column in required_columns})
    if len(rows) != expected_rows:
        raise ValueError("DuckDB Parquet row count does not match the frozen universe")
    return "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
    )


def _authenticated_protocol(
    data: bytes,
    *,
    expected_sha256: str | None,
) -> dict[str, Any]:
    if expected_sha256 is not None and SHA256.fullmatch(expected_sha256) is None:
        raise SystemExit("expected acquisition protocol SHA-256 must be 64 lowercase hex digits")
    if expected_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(data).hexdigest(),
        expected_sha256,
    ):
        raise SystemExit("acquisition protocol does not match the expected SHA-256")
    protocol: dict[str, Any] = json.loads(data)
    if (
        protocol.get("schema_version") == 2
        or protocol.get("protocol_id") == "production-graph-holdout-v2"
    ) and expected_sha256 is None:
        raise SystemExit("V2 acquisition requires --expected-acquisition-protocol-sha256")
    return protocol


def canonicalize_parquet(
    parquet_path: Path,
    duckdb_gzip_path: Path,
    *,
    required_columns: list[str],
    expected_rows: int,
    expected_gzip_sha256: str,
    expected_version: str,
    private_root: Path,
) -> tuple[str, str]:
    if _sha256(duckdb_gzip_path) != expected_gzip_sha256:
        raise ValueError("DuckDB CLI gzip does not match the frozen SHA-256")
    private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _IS_WINDOWS and stat.S_IMODE(private_root.stat().st_mode) != 0o700:
        raise ValueError("acquisition work directory must be owner-only")
    with tempfile.TemporaryDirectory(dir=private_root) as temporary:
        temp = Path(temporary)
        duckdb = temp / "duckdb"
        with gzip.open(duckdb_gzip_path, "rb") as source:
            duckdb.write_bytes(source.read())
        duckdb.chmod(0o700)
        environment = {
            "HOME": str(temp),
            "XDG_CONFIG_HOME": str(temp),
            "TMPDIR": str(temp),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "LC_ALL": "C",
        }
        version = subprocess.run(
            [str(duckdb), "-version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        ).stdout.split(maxsplit=1)[0]
        if version != expected_version:
            raise ValueError("DuckDB CLI version does not match the frozen version")
        raw_json = temp / "rows.jsonl"
        columns = ", ".join(
            f'"{column.replace(chr(34), chr(34) * 2)}"' for column in required_columns
        )
        query = (
            "SET threads=1; COPY (SELECT file_row_number AS row_idx, "
            f"{columns} FROM read_parquet('{_sql_path(parquet_path)}', "
            "file_row_number=true) ORDER BY file_row_number) "
            f"TO '{_sql_path(raw_json)}' (FORMAT JSON);"
        )
        subprocess.run(
            [str(duckdb), "-no-stdin", "-c", query],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
            env=environment,
        )
        canonical = _canonicalize_duckdb_rows(
            raw_json.read_text(encoding="utf-8"),
            required_columns=required_columns,
            expected_rows=expected_rows,
        )
        return canonical, _sha256(duckdb)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--expected-acquisition-protocol-sha256")
    parser.add_argument("--parquet", type=Path, required=True)
    parser.add_argument("--duckdb-gzip", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not _paths_are_distinct([args.protocol, args.parquet, args.duckdb_gzip, args.output]):
        raise SystemExit("canonical output must not overwrite an acquisition input")
    protocol = _authenticated_protocol(
        args.protocol.read_bytes(),
        expected_sha256=args.expected_acquisition_protocol_sha256,
    )
    universe = protocol["universe"]
    if SHA256.fullmatch(str(universe["duckdb_cli_gzip_sha256"])) is None:
        raise SystemExit("protocol DuckDB gzip SHA-256 is invalid")
    frozen_parquet = universe.get("raw_parquet_sha256")
    frozen_duckdb = universe.get("duckdb_cli_sha256")
    frozen_jsonl = universe.get("selection_jsonl_sha256")
    if protocol.get("stage") not in {
        "acquisition-preregistered",
        "acquisition-frozen",
        "execution-frozen",
    }:
        raise SystemExit("protocol acquisition stage is invalid")
    if protocol.get("stage") in {"acquisition-frozen", "execution-frozen"} and any(
        SHA256.fullmatch(str(value or "")) is None
        for value in (frozen_parquet, frozen_duckdb, frozen_jsonl)
    ):
        raise SystemExit("frozen acquisition requires Parquet, DuckDB, and JSONL SHA-256")
    if protocol.get("stage") == "acquisition-preregistered" and any(
        value is not None for value in (frozen_parquet, frozen_duckdb, frozen_jsonl)
    ):
        raise SystemExit("preregistered acquisition hashes must remain null")
    if frozen_parquet and _sha256(args.parquet) != frozen_parquet:
        raise SystemExit("Parquet does not match the frozen SHA-256")
    canonical, duckdb_sha256 = canonicalize_parquet(
        args.parquet,
        args.duckdb_gzip,
        required_columns=universe["required_columns"],
        expected_rows=universe["expected_rows"],
        expected_gzip_sha256=universe["duckdb_cli_gzip_sha256"],
        expected_version=universe["duckdb_version"],
        private_root=args.output.parent,
    )
    if frozen_duckdb and duckdb_sha256 != frozen_duckdb:
        raise SystemExit("DuckDB CLI does not match the frozen SHA-256")
    canonical_bytes = canonical.encode()
    if frozen_jsonl and hashlib.sha256(canonical_bytes).hexdigest() != frozen_jsonl:
        raise SystemExit("canonical JSONL does not match the frozen SHA-256")
    with _private_text_handle(args.output) as handle:
        handle.write(canonical)
    print(json.dumps({"duckdb_cli_sha256": duckdb_sha256}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
