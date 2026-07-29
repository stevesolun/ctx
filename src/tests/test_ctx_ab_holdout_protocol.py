from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = ROOT / "benchmarks" / "ctx_ab" / "holdout-protocol-v1.json"
SCRIPT = ROOT / "scripts" / "ctx_ab_holdout.py"
SPEC = importlib.util.spec_from_file_location("ctx_ab_holdout", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
selector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = selector
SPEC.loader.exec_module(selector)
ACQUIRE_SCRIPT = ROOT / "scripts" / "ctx_ab_holdout_acquire.py"
ACQUIRE_SPEC = importlib.util.spec_from_file_location("ctx_ab_holdout_acquire", ACQUIRE_SCRIPT)
assert ACQUIRE_SPEC is not None and ACQUIRE_SPEC.loader is not None
acquire = importlib.util.module_from_spec(ACQUIRE_SPEC)
sys.modules[ACQUIRE_SPEC.name] = acquire
ACQUIRE_SPEC.loader.exec_module(acquire)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _row(repo: str, suffix: str, *, path: str | None = None) -> dict[str, str]:
    production_path = path or f"src/{suffix}.py"
    return {
        "repo": repo,
        "instance_id": f"{repo.replace('/', '__')}-{suffix}",
        "base_commit": suffix[0] * 40,
        "patch": (
            f"diff --git a/{production_path} b/{production_path}\n"
            f"--- a/{production_path}\n+++ b/{production_path}\n"
            "@@ -1 +1,4 @@\n-old\n+new\n+one\n+two\n+three\n"
        ),
        "test_patch": (
            f"diff --git a/tests/test_{suffix}.py b/tests/test_{suffix}.py\n"
            f"--- a/tests/test_{suffix}.py\n+++ b/tests/test_{suffix}.py\n"
            "@@ -1 +1,2 @@\n-old\n+new\n+assert True\n"
        ),
        "problem_statement": " ".join(["public behavior must remain compatible"] * 6),
    }


def test_holdout_protocol_authenticates_pinned_product_inputs() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    product = protocol["product_inputs"]

    assert protocol["schema_version"] == 1
    assert protocol["stage"] == "acquisition-frozen"
    assert datetime.fromisoformat(protocol["frozen_at"]).tzinfo == UTC
    assert datetime.fromisoformat(protocol["acquisition_frozen_at"]).tzinfo == UTC
    assert re.fullmatch(r"[0-9a-f]{40}", product["revision"])
    assert SHA256.fullmatch(protocol["selection_seed"])
    assert protocol["universe"]["revision"] == "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
    assert protocol["universe"]["expected_rows"] == 500
    assert (
        protocol["universe"]["raw_parquet_sha256"]
        == "a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd"
    )
    assert (
        protocol["universe"]["selection_jsonl_sha256"]
        == "392529c5e79ca273bf0b073be35169beb68c604a26d9aef5514912fc584fa6cb"
    )
    assert (
        protocol["universe"]["duckdb_cli_sha256"]
        == "5f5fafb02b609cdb20d199c06835d095023616e7366033775ba99a6a0b6969f3"
    )
    assert set(protocol["execution_inputs"].values()) == {None}
    assert protocol["timeouts"]["control_verification_seconds"] == 900
    assert protocol["universe"]["duckdb_version"] == "v1.5.2"
    assert (
        protocol["universe"]["duckdb_cli_gzip_sha256"]
        == "c04495beb458c9f0451ee6a384d363cf14cf08276d6a6e8f1edcd2a3f7627075"
    )
    assert protocol["universe"]["parquet_url"].endswith(
        "/c104f840cc67f8b6eec6f759ebc8b2693d585d4a/data/test-00000-of-00001.parquet"
    )
    expected_seed = hashlib.sha256(
        "\0".join(
            [
                "ctx-holdout-selection-v1",
                protocol["universe"]["revision"],
            ]
        ).encode()
    ).hexdigest()
    assert protocol["selection_seed"] == expected_seed
    pinned_inputs = {
        "graph/wiki-graph-runtime.tar.gz": ("catalog_archive_sha256", True),
        "src/ctx/assets/runtime-availability.json": ("runtime_availability_sha256", False),
        "scripts/ctx_ab_benchmark.py": ("benchmark_script_sha256", False),
    }
    assert set(product["git_blob_sha1"]) == set(pinned_inputs)
    for path, (digest_key, is_lfs_pointer) in pinned_inputs.items():
        object_spec = f"{product['revision']}:{path}"
        blob = subprocess.run(
            ["git", "rev-parse", object_spec],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        assert blob.stdout.strip() == product["git_blob_sha1"][path]
        content = subprocess.run(
            ["git", "cat-file", "blob", object_spec],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        expected_digest = product[digest_key]
        assert SHA256.fullmatch(expected_digest)
        if is_lfs_pointer:
            assert f"oid sha256:{expected_digest}".encode() in content.splitlines()
        else:
            assert hashlib.sha256(content).hexdigest() == expected_digest


def test_static_filter_uses_declared_rejection_codes() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    valid = _row("fresh/repo", "a")
    excluded = _row("pallets/click", "b")
    networked = _row("fresh/other", "c")
    networked["test_patch"] = (
        networked["test_patch"]
        .replace(
            "@@ -1 +1,2 @@",
            "@@ -1 +1,3 @@",
        )
        .replace(
            "+assert True\n",
            "+assert True\n+requests.get('https://example.com')\n",
        )
    )

    assert selector.evaluate_row(valid, protocol)["status"] == "eligible"
    assert selector.evaluate_row(excluded, protocol)["rejection_code"] == "excluded-repository"
    assert selector.evaluate_row(networked, protocol)["rejection_code"] == "test-dependency"


@pytest.mark.parametrize(
    ("code", "mutate"),
    [
        ("base-commit", lambda row: row.update(base_commit="z" * 40)),
        ("excluded-repository", lambda row: row.update(repo="pallets/click")),
        (
            "patch-lines",
            lambda row: row.update(
                patch=(
                    "diff --git a/src/a.py b/src/a.py\n"
                    "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n"
                )
            ),
        ),
        (
            "patch-paths",
            lambda row: row.update(
                patch=(
                    "diff --git a/docs/a.py b/docs/a.py\n"
                    "--- a/docs/a.py\n+++ b/docs/a.py\n"
                    "@@ -1 +1,4 @@\n-old\n+new\n+one\n+two\n+three\n"
                )
            ),
        ),
        ("problem-statement", lambda row: row.update(problem_statement="too short")),
        ("row-schema", lambda row: row.update(repo="../invalid")),
        (
            "test-dependency",
            lambda row: row.update(
                test_patch=row["test_patch"]
                .replace("@@ -1 +1,2 @@", "@@ -1 +1,3 @@")
                .replace("+assert True\n", "+assert True\n+time.sleep(1)\n")
            ),
        ),
        (
            "test-paths",
            lambda row: row.update(
                test_patch=(
                    "diff --git a/src/check.py b/src/check.py\n"
                    "--- a/src/check.py\n+++ b/src/check.py\n@@ -1 +1 @@\n-old\n+new\n"
                )
            ),
        ),
    ],
)
def test_static_filter_rejection_codes(code: str, mutate: object) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    row = _row("fresh/repo", "a")
    mutate(row)  # type: ignore[operator]

    assert selector.evaluate_row(row, protocol)["rejection_code"] == code


@pytest.mark.parametrize(
    "path",
    [
        ".github/tool.py",
        "benchmarks/tool.py",
        "docs/tool.py",
        "examples/tool.py",
        "generated/tool.py",
        "src/generated.py",
        "src/client_generated.py",
        "src/autogenerated.py",
        "src/foo.generated.py",
        "migrations/tool.py",
        "scripts/tool.py",
        "tests/tool.py",
        "package/conftest.py",
        "setup.py",
    ],
)
def test_static_filter_enforces_every_declared_path_exclusion(path: str) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))

    assert (
        selector.evaluate_row(_row("fresh/repo", "a", path=path), protocol)["rejection_code"]
        == "patch-paths"
    )


@pytest.mark.parametrize(
    "patch",
    [
        (
            "diff --git a/src/good.py b/src/good.py\n"
            "--- a/docs/leak.py\n+++ b/docs/leak.py\n"
            "@@ -1 +1,4 @@\n-old\n+new\n+one\n+two\n+three\n"
        ),
        (
            "diff --git a/src/good.py b/src/good.py\n"
            "--- a/src/good.py\n+++ b/src/good.py\n"
            "-old\n+new\n+one\n+two\n+three\n"
        ),
        (
            "diff --git a/src/good.py b/src/good.py\n"
            "--- a/src/good.py\n+++ b/src/good.py\n"
            "@@ -1,2 +1,4 @@\n-old\n+new\n+one\n+two\n+three\n"
        ),
        _row("fresh/repo", "a")["patch"] + _row("fresh/repo", "a")["patch"],
    ],
)
def test_static_filter_rejects_malformed_unified_diffs(patch: str) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    row = _row("fresh/repo", "a")
    row["patch"] = patch

    assert selector.evaluate_row(row, protocol)["rejection_code"] == "patch-paths"


@pytest.mark.parametrize(
    "added_line",
    [
        "from requests import get",
        "import urllib.request as client",
        "import importlib; importlib.import_module('socket')",
        "from os import getenv",
        "from time import sleep",
        "import http.client",
        "importlib.import_module('os').getenv('TOKEN')",
        "os.getenv('TOKEN')",
        "asyncio.sleep(1)",
    ],
)
def test_static_filter_rejects_forbidden_dependency_variants(added_line: str) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    row = _row("fresh/repo", "a")
    row["test_patch"] = (
        row["test_patch"]
        .replace("@@ -1 +1,2 @@", "@@ -1 +1,3 @@")
        .replace("+assert True\n", f"+assert True\n+{added_line}\n")
    )

    assert selector.evaluate_row(row, protocol)["rejection_code"] == "test-dependency"


def test_static_filter_allows_rename_phrase_inside_added_code() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    row = _row("fresh/repo", "a")
    row["patch"] = row["patch"].replace(
        "+three\n",
        '+message = "rename from source"\n',
    )

    assert selector.evaluate_row(row, protocol)["status"] == "eligible"


@pytest.mark.parametrize(
    "source",
    [
        "import requests as req\n\ndef test_it():\n    req.get('https://example.com')\n",
        "from requests import get as fetch\n\ndef test_it():\n    fetch('https://example.com')\n",
        "import os as operating_system\n\ndef test_it():\n    operating_system.getenv('TOKEN')\n",
        "import time as clock\n\ndef test_it():\n    clock.sleep(1)\n",
        "from importlib import import_module as load\nload('socket')\n",
        "from os import *\ngetenv('TOKEN')\n",
        "from importlib import *\nimport_module('socket')\n",
        "import builtins\nbuiltins.__import__('socket')\n",
        "import builtins as runtime\nruntime.__import__('socket')\n",
        "from builtins import __import__ as load\nload('socket')\n",
        "import importlib\nmodule_name = 'socket'\nimportlib.import_module(module_name)\n",
        "import builtins\nmodule_name = 'requests'\nbuiltins.__import__(module_name)\n",
        "import os.path\nos.getenv('TOKEN')\n",
        "import importlib.util\nimportlib.import_module('socket')\n",
    ],
)
def test_reconstructed_test_module_rejects_existing_dependency_aliases(source: str) -> None:
    with pytest.raises(ValueError, match="forbidden"):
        selector.validate_reconstructed_test_module(source)


def test_reconstructed_test_module_allows_local_deterministic_code() -> None:
    selector.validate_reconstructed_test_module(
        "from pathlib import Path\n\ndef test_it(tmp_path: Path):\n    assert tmp_path.is_dir()\n"
    )


def test_selector_is_golden_deterministic_and_repository_clustered() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    source = [
        _row(f"owner/repo-{repo}", suffix, path=f"src/{repo}/{suffix}.py")
        for repo, suffixes in zip("abcdefg", ("ab", "cd", "ef", "12", "34", "56", "78"))
        for suffix in suffixes
    ]
    ledger = [selector.evaluate_row(row, protocol) for row in source]

    first = selector.select_rows(ledger, protocol)
    second = selector.select_rows(list(reversed(ledger)), protocol)

    assert first == second
    assert first == {
        "protocol_id": "production-graph-holdout-v1",
        "analysis_instance_ids": [
            "owner__repo-g-8",
            "owner__repo-c-e",
            "owner__repo-b-c",
            "owner__repo-d-2",
            "owner__repo-e-4",
            "owner__repo-g-7",
        ],
        "analysis_repository_map": {
            "owner__repo-g-8": "https://github.com/owner/repo-g.git",
            "owner__repo-c-e": "https://github.com/owner/repo-c.git",
            "owner__repo-b-c": "https://github.com/owner/repo-b.git",
            "owner__repo-d-2": "https://github.com/owner/repo-d.git",
            "owner__repo-e-4": "https://github.com/owner/repo-e.git",
            "owner__repo-g-7": "https://github.com/owner/repo-g.git",
        },
        "canary_instance_id": "owner__repo-a-a",
        "canary_repository": "https://github.com/owner/repo-a.git",
    }


def test_selector_fails_instead_of_replacing_missing_repository() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    ledger = [
        selector.evaluate_row(_row(f"owner/repo-{repo}", suffix), protocol)
        for repo, suffixes in zip("abcde", ("ab", "cd", "ef", "12", "34"))
        for suffix in suffixes
    ]

    with pytest.raises(ValueError, match="six repositories"):
        selector.select_rows(ledger, protocol)


def test_selector_rejects_duplicate_instance_ids() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    row = selector.evaluate_row(_row("fresh/repo", "a"), protocol)

    with pytest.raises(ValueError, match="duplicate instance"):
        selector.select_rows([row, row], protocol)


def test_selector_rejects_missing_disjoint_second_candidate() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    source = [
        _row(f"owner/repo-{repo}", suffix, path=f"src/{repo}/shared.py")
        for repo, suffixes in zip("abcdefg", ("ab", "cd", "ef", "12", "34", "56", "78"))
        for suffix in suffixes
    ]
    ledger = [selector.evaluate_row(row, protocol) for row in source]

    with pytest.raises(ValueError, match="disjoint second"):
        selector.select_rows(ledger, protocol)


def test_selector_cli_writes_owner_only_evidence(tmp_path: Path) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    source = [
        _row(f"owner/repo-{repo}", suffix, path=f"src/{repo}/{suffix}.py")
        for repo, suffixes in zip("abcdefg", ("ab", "cd", "ef", "12", "34", "56", "78"))
        for suffix in suffixes
    ]
    protocol["universe"]["expected_rows"] = len(source)
    protocol_path = tmp_path / "protocol.json"
    source_path = tmp_path / "source.jsonl"
    ledger_path = tmp_path / "private" / "ledger.csv"
    selection_path = tmp_path / "private" / "selection.json"
    source_path.write_text(
        "".join(json.dumps(row) + "\n" for row in source),
        encoding="utf-8",
    )
    protocol["stage"] = "acquisition-frozen"
    protocol["universe"]["raw_parquet_sha256"] = "1" * 64
    protocol["universe"]["duckdb_cli_sha256"] = "2" * 64
    protocol["universe"]["selection_jsonl_sha256"] = hashlib.sha256(
        source_path.read_bytes()
    ).hexdigest()
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")

    assert (
        selector.main(
            [
                "--protocol",
                str(protocol_path),
                "--selection-jsonl",
                str(source_path),
                "--ledger",
                str(ledger_path),
                "--selection",
                str(selection_path),
            ]
        )
        == 0
    )
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(selection_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ledger_path.parent.stat().st_mode) == 0o700
    assert len(ledger_path.read_text(encoding="utf-8").splitlines()) == len(source) + 1
    selection_path.chmod(0o644)

    assert (
        selector.main(
            [
                "--protocol",
                str(protocol_path),
                "--selection-jsonl",
                str(source_path),
                "--ledger",
                str(ledger_path),
                "--selection",
                str(selection_path),
            ]
        )
        == 0
    )
    assert stat.S_IMODE(selection_path.stat().st_mode) == 0o600


def test_selector_cli_rejects_path_collisions_and_tracked_output(tmp_path: Path) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    protocol_path = tmp_path / "protocol.json"
    source_path = tmp_path / "source.jsonl"
    private_path = tmp_path / "private" / "evidence"
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
    source_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="paths must be distinct"):
        selector.main(
            [
                "--protocol",
                str(protocol_path),
                "--selection-jsonl",
                str(source_path),
                "--ledger",
                str(private_path),
                "--selection",
                str(private_path),
            ]
        )
    tracked = ROOT / "holdout-must-not-write"
    with pytest.raises(ValueError, match="must use .gate"):
        selector._private_text_handle(tracked)
    assert not tracked.exists()


def test_selector_cli_rejects_symlink_output(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "target"
    target.write_text("preserve", encoding="utf-8")
    target.chmod(0o600)
    link = private / "link"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="regular file"):
        selector._private_text_handle(link)
    assert target.read_text(encoding="utf-8") == "preserve"


def test_selector_cli_rejects_hard_link_output(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    source = tmp_path / "source.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    link = private / "selection.json"
    os.link(source, link)

    assert not selector._paths_are_distinct([source, link])
    with pytest.raises(ValueError, match="hard link"):
        selector._private_text_handle(link)
    assert source.read_text(encoding="utf-8") == "{}\n"


def test_selector_cli_rejects_unfrozen_or_mutated_source(tmp_path: Path) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    protocol["stage"] = "acquisition-preregistered"
    protocol["universe"]["raw_parquet_sha256"] = None
    protocol["universe"]["duckdb_cli_sha256"] = None
    protocol["universe"]["selection_jsonl_sha256"] = None
    protocol_path = tmp_path / "protocol.json"
    source_path = tmp_path / "source.jsonl"
    private = tmp_path / "private"
    source_path.write_text("{}\n", encoding="utf-8")
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
    arguments = [
        "--protocol",
        str(protocol_path),
        "--selection-jsonl",
        str(source_path),
        "--ledger",
        str(private / "ledger.csv"),
        "--selection",
        str(private / "selection.json"),
    ]

    with pytest.raises(SystemExit, match="frozen authenticated"):
        selector.main(arguments)

    protocol["stage"] = "acquisition-frozen"
    protocol["universe"]["raw_parquet_sha256"] = "1" * 64
    protocol["universe"]["duckdb_cli_sha256"] = "2" * 64
    protocol["universe"]["selection_jsonl_sha256"] = "9" * 64
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
    private.mkdir(mode=0o700)
    selection_path = private / "selection.json"
    selection_path.write_text("stale", encoding="utf-8")
    selection_path.chmod(0o600)
    with pytest.raises(SystemExit, match="does not match"):
        selector.main(arguments)
    assert not selection_path.exists()


def test_selector_cli_preserves_ledger_on_no_go(tmp_path: Path) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    source = [
        _row(f"owner/repo-{repo}", suffix)
        for repo, suffixes in zip("abcde", ("ab", "cd", "ef", "12", "34"))
        for suffix in suffixes
    ]
    protocol["universe"]["expected_rows"] = len(source)
    protocol_path = tmp_path / "protocol.json"
    source_path = tmp_path / "source.jsonl"
    ledger_path = tmp_path / "private" / "ledger.csv"
    selection_path = tmp_path / "private" / "selection.json"
    source_path.write_text(
        "".join(json.dumps(row) + "\n" for row in source),
        encoding="utf-8",
    )
    protocol["stage"] = "acquisition-frozen"
    protocol["universe"]["raw_parquet_sha256"] = "1" * 64
    protocol["universe"]["duckdb_cli_sha256"] = "2" * 64
    protocol["universe"]["selection_jsonl_sha256"] = hashlib.sha256(
        source_path.read_bytes()
    ).hexdigest()
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
    selection_path.parent.mkdir(mode=0o700)
    selection_path.write_text("stale", encoding="utf-8")
    selection_path.chmod(0o600)

    with pytest.raises(ValueError, match="six repositories"):
        selector.main(
            [
                "--protocol",
                str(protocol_path),
                "--selection-jsonl",
                str(source_path),
                "--ledger",
                str(ledger_path),
                "--selection",
                str(selection_path),
            ]
        )
    assert ledger_path.is_file()
    assert not selection_path.exists()
    assert len(ledger_path.read_text(encoding="utf-8").splitlines()) == len(source) + 1


def _duckdb_raw_rows(columns: list[str], count: int) -> str:
    return "".join(
        json.dumps(
            {
                "row_idx": index,
                **{column: f"{column}-{index}" for column in columns},
            }
        )
        + "\n"
        for index in range(count)
    )


def _fake_duckdb_gzip(path: Path, columns: list[str], count: int) -> None:
    script = f"""#!/usr/bin/env python3
import json
import re
import sys

if "-version" in sys.argv:
    print("v1.5.2 fake")
    raise SystemExit(0)
query = sys.argv[-1]
output = re.search(r"TO '([^']+)'", query).group(1)
columns = {columns!r}
with open(output, "w", encoding="utf-8") as handle:
    for index in range({count}):
        row = {{"row_idx": index, **{{column: f"{{column}}-{{index}}" for column in columns}}}}
        handle.write(json.dumps(row) + "\\n")
"""
    with path.open("wb") as handle:
        with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as compressed:
            compressed.write(script.encode())


def test_acquisition_canonicalizer_is_byte_stable() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    columns = protocol["universe"]["required_columns"]
    expected = "".join(
        json.dumps(
            {column: f"{column}-{index}" for column in columns},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
        for index in range(5)
    )

    assert (
        acquire._canonicalize_duckdb_rows(
            _duckdb_raw_rows(columns, 5),
            required_columns=columns,
            expected_rows=5,
        )
        == expected
    )


def test_acquisition_runs_pinned_cli_and_verifies_frozen_rerun(tmp_path: Path) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    protocol["stage"] = "acquisition-preregistered"
    protocol["universe"]["raw_parquet_sha256"] = None
    protocol["universe"]["duckdb_cli_sha256"] = None
    protocol["universe"]["selection_jsonl_sha256"] = None
    columns = protocol["universe"]["required_columns"]
    protocol["universe"]["expected_rows"] = 5
    parquet = tmp_path / "source.parquet"
    duckdb_gzip = tmp_path / "duckdb.gz"
    protocol_path = tmp_path / "protocol.json"
    output = tmp_path / "private" / "selection.jsonl"
    parquet.write_bytes(b"frozen parquet")
    _fake_duckdb_gzip(duckdb_gzip, columns, 5)
    protocol["universe"]["duckdb_cli_gzip_sha256"] = hashlib.sha256(
        duckdb_gzip.read_bytes()
    ).hexdigest()
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
    arguments = [
        "--protocol",
        str(protocol_path),
        "--parquet",
        str(parquet),
        "--duckdb-gzip",
        str(duckdb_gzip),
        "--output",
        str(output),
    ]

    assert acquire.main(arguments) == 0
    first = output.read_bytes()
    with gzip.open(duckdb_gzip, "rb") as handle:
        duckdb_sha256 = hashlib.sha256(handle.read()).hexdigest()
    protocol["stage"] = "acquisition-frozen"
    protocol["universe"]["raw_parquet_sha256"] = hashlib.sha256(parquet.read_bytes()).hexdigest()
    protocol["universe"]["duckdb_cli_sha256"] = duckdb_sha256
    protocol["universe"]["selection_jsonl_sha256"] = hashlib.sha256(first).hexdigest()
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")

    assert acquire.main(arguments) == 0
    assert output.read_bytes() == first
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("raw_parquet_sha256", "Parquet does not match"),
        ("duckdb_cli_sha256", "DuckDB CLI does not match"),
        ("selection_jsonl_sha256", "canonical JSONL does not match"),
    ],
)
def test_acquisition_rejects_frozen_hash_mismatch(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    protocol["universe"]["expected_rows"] = 5
    parquet = tmp_path / "source.parquet"
    duckdb_gzip = tmp_path / "duckdb.gz"
    protocol_path = tmp_path / "protocol.json"
    output = tmp_path / "private" / "selection.jsonl"
    parquet.write_bytes(b"frozen parquet")
    _fake_duckdb_gzip(duckdb_gzip, protocol["universe"]["required_columns"], 5)
    with gzip.open(duckdb_gzip, "rb") as handle:
        duckdb_sha256 = hashlib.sha256(handle.read()).hexdigest()
    protocol["universe"]["duckdb_cli_gzip_sha256"] = hashlib.sha256(
        duckdb_gzip.read_bytes()
    ).hexdigest()
    protocol["universe"]["raw_parquet_sha256"] = hashlib.sha256(parquet.read_bytes()).hexdigest()
    protocol["universe"]["duckdb_cli_sha256"] = duckdb_sha256
    canonical = acquire._canonicalize_duckdb_rows(
        _duckdb_raw_rows(protocol["universe"]["required_columns"], 5),
        required_columns=protocol["universe"]["required_columns"],
        expected_rows=5,
    )
    protocol["universe"]["selection_jsonl_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    protocol["stage"] = "acquisition-frozen"
    protocol["universe"][field] = "0" * 64
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")

    with pytest.raises(SystemExit, match=message):
        acquire.main(
            [
                "--protocol",
                str(protocol_path),
                "--parquet",
                str(parquet),
                "--duckdb-gzip",
                str(duckdb_gzip),
                "--output",
                str(output),
            ]
        )


def test_acquisition_rejects_missing_frozen_hashes(tmp_path: Path) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    protocol["stage"] = "acquisition-frozen"
    protocol["universe"]["raw_parquet_sha256"] = None
    protocol["universe"]["duckdb_cli_sha256"] = None
    protocol["universe"]["selection_jsonl_sha256"] = None
    protocol["universe"]["expected_rows"] = 1
    parquet = tmp_path / "source.parquet"
    duckdb_gzip = tmp_path / "duckdb.gz"
    protocol_path = tmp_path / "protocol.json"
    parquet.write_bytes(b"parquet")
    _fake_duckdb_gzip(duckdb_gzip, protocol["universe"]["required_columns"], 1)
    protocol["universe"]["duckdb_cli_gzip_sha256"] = hashlib.sha256(
        duckdb_gzip.read_bytes()
    ).hexdigest()
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")

    with pytest.raises(SystemExit, match="requires Parquet, DuckDB, and JSONL"):
        acquire.main(
            [
                "--protocol",
                str(protocol_path),
                "--parquet",
                str(parquet),
                "--duckdb-gzip",
                str(duckdb_gzip),
                "--output",
                str(tmp_path / "private" / "selection.jsonl"),
            ]
        )


def test_acquisition_rejects_unknown_protocol_stage(tmp_path: Path) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    protocol["stage"] = "unknown"
    parquet = tmp_path / "source.parquet"
    duckdb_gzip = tmp_path / "duckdb.gz"
    protocol_path = tmp_path / "protocol.json"
    parquet.write_bytes(b"parquet")
    _fake_duckdb_gzip(duckdb_gzip, protocol["universe"]["required_columns"], 1)
    protocol["universe"]["duckdb_cli_gzip_sha256"] = hashlib.sha256(
        duckdb_gzip.read_bytes()
    ).hexdigest()
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")

    with pytest.raises(SystemExit, match="stage is invalid"):
        acquire.main(
            [
                "--protocol",
                str(protocol_path),
                "--parquet",
                str(parquet),
                "--duckdb-gzip",
                str(duckdb_gzip),
                "--output",
                str(tmp_path / "private" / "selection.jsonl"),
            ]
        )


def test_acquisition_canonicalizer_rejects_schema_drift() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    columns = protocol["universe"]["required_columns"]
    raw = json.loads(_duckdb_raw_rows(columns, 1))
    raw.pop(columns[0])

    with pytest.raises(ValueError, match="not canonical"):
        acquire._canonicalize_duckdb_rows(
            json.dumps(raw) + "\n",
            required_columns=columns,
            expected_rows=1,
        )


def test_acquisition_canonicalizer_rejects_output_collision(tmp_path: Path) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    parquet = tmp_path / "source.parquet"
    duckdb_gzip = tmp_path / "duckdb.gz"
    protocol_path = tmp_path / "protocol.json"
    parquet.write_bytes(b"parquet")
    _fake_duckdb_gzip(duckdb_gzip, protocol["universe"]["required_columns"], 1)
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")

    with pytest.raises(SystemExit, match="must not overwrite"):
        acquire.main(
            [
                "--protocol",
                str(protocol_path),
                "--parquet",
                str(parquet),
                "--duckdb-gzip",
                str(duckdb_gzip),
                "--output",
                str(parquet),
            ]
        )


def test_acquisition_canonicalizer_rejects_hard_link_collision(tmp_path: Path) -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    parquet = tmp_path / "source.parquet"
    duckdb_gzip = tmp_path / "duckdb.gz"
    protocol_path = tmp_path / "protocol.json"
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "selection.jsonl"
    parquet.write_bytes(b"parquet")
    os.link(parquet, output)
    _fake_duckdb_gzip(duckdb_gzip, protocol["universe"]["required_columns"], 1)
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")

    with pytest.raises(SystemExit, match="must not overwrite"):
        acquire.main(
            [
                "--protocol",
                str(protocol_path),
                "--parquet",
                str(parquet),
                "--duckdb-gzip",
                str(duckdb_gzip),
                "--output",
                str(output),
            ]
        )
    assert parquet.read_bytes() == b"parquet"


def test_claim_protocol_predeclares_clustered_primary_endpoint() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    analysis = protocol["analysis"]
    claims = protocol["claim_gates"]

    assert analysis["scenario_effect"] == "median pair effect with no outlier removal"
    assert analysis["repository_effect"].startswith("equal-weight median")
    assert analysis["overall_token_effect"].startswith("equal-weight median")
    assert analysis["overall_time_effect"].startswith("equal-weight median")
    assert analysis["failure_policy"].startswith("any missing pair")
    assert analysis["delivery"] == "at least one trusted verified CTX delivery in every repository"
    assert analysis["claim_scope"].endswith(
        "broader software-development productivity claims are unsupported"
    )
    assert claims["primary_endpoint"] == "uncached_provider_tokens"
    assert claims["primary_endpoint_aggregation"] == "overall_token_effect"
    assert claims["primary_endpoint_maximum_ratio"] == 0.85
    assert claims["total_seconds_aggregation"] == "overall_time_effect"
    assert claims["total_seconds_maximum_ratio"] == 1.1
    assert claims["minimum_repositories_with_verified_delivery"] == 5
    assert claims["required_benefiting_repositories"] == 5


def _claim_fixture(
    token_ratios: list[float],
    *,
    time_ratios: list[float] | None = None,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
]:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    repository_map = {
        **{f"scenario-{index}": f"https://github.com/owner/repo-{index}.git" for index in range(5)},
        "scenario-5": "https://github.com/owner/repo-0.git",
    }
    selection: dict[str, object] = {
        "protocol_id": protocol["protocol_id"],
        "analysis_instance_ids": list(repository_map),
        "analysis_repository_map": repository_map,
        "canary_instance_id": "canary",
        "canary_repository": "https://github.com/owner/canary.git",
    }
    selected_ids = [*repository_map, "canary"]
    reconstructed_tests = {
        scenario_id: f"def test_{index}():\n    assert True\n"
        for index, scenario_id in enumerate(selected_ids)
    }
    scenario_pack_bytes = json.dumps(
        {
            "scenarios": [
                {
                    "id": scenario_id,
                    "reconstructed_test_sha256": hashlib.sha256(
                        reconstructed_tests[scenario_id].encode()
                    ).hexdigest(),
                }
                for scenario_id in selected_ids
            ]
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    collision_attestation_bytes = json.dumps(
        {
            "guard": "runtime-pack-distinctive-evidence-v1",
            "runtime_availability_sha256": protocol["product_inputs"][
                "runtime_availability_sha256"
            ],
            "catalog_archive_sha256": protocol["product_inputs"]["catalog_archive_sha256"],
            "scenarios_sha256": hashlib.sha256(scenario_pack_bytes).hexdigest(),
            "collision_free": True,
            "collision_count": 0,
            "scenario_ids": sorted(selected_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    protocol["stage"] = "execution-frozen"
    reconstructed_attestation = selector.build_reconstructed_test_attestation(
        selection,
        protocol,
        reconstructed_tests,
    )
    selection_sha256 = hashlib.sha256(
        json.dumps(selection, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    scenario_pack_sha256 = hashlib.sha256(scenario_pack_bytes).hexdigest()
    control_results_bytes = json.dumps(
        {
            "guard": "holdout-control-results-v1",
            "selection_sha256": selection_sha256,
            "scenario_pack_sha256": scenario_pack_sha256,
            "all_seven_passed": True,
            "scenario_results": {
                scenario_id: {
                    "parent_with_test_patch_red": True,
                    "reference_patch_green": True,
                    "changed_test_module_green": True,
                    "timeout_compliant": True,
                    "reconstructed_test_sha256": hashlib.sha256(
                        reconstructed_tests[scenario_id].encode()
                    ).hexdigest(),
                    "red_evidence_sha256": "a" * 64,
                    "green_evidence_sha256": "b" * 64,
                    "module_evidence_sha256": "c" * 64,
                    "elapsed_seconds": 1.0,
                    "timeout_seconds": protocol["timeouts"]["control_verification_seconds"],
                }
                for scenario_id in selected_ids
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    protocol["execution_inputs"] = {
        "selection_output_sha256": selection_sha256,
        "scenario_pack_sha256": scenario_pack_sha256,
        "collision_attestation_sha256": hashlib.sha256(collision_attestation_bytes).hexdigest(),
        "reconstructed_test_attestation_sha256": hashlib.sha256(
            json.dumps(
                reconstructed_attestation,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "control_results_sha256": hashlib.sha256(control_results_bytes).hexdigest(),
    }
    times = time_ratios or [1.0] * len(token_ratios)
    repositories = sorted(set(repository_map.values()))
    rows = [
        {
            "repository": repository,
            "scenario_ids": sorted(
                scenario_id
                for scenario_id, selected_repository in repository_map.items()
                if selected_repository == repository
            ),
            "paired_trials_by_scenario": {
                scenario_id: protocol["claim_gates"]["paired_trials_per_scenario"]
                for scenario_id, selected_repository in repository_map.items()
                if selected_repository == repository
            },
            "missing_pairs": 0,
            "token_usage_exact": True,
            "trusted_policy_outcomes": True,
            "uncached_provider_tokens_ratio": token_ratio,
            "total_seconds_ratio": time_ratio,
            "quality_preserved": True,
            "verified_delivery": True,
            "unresolved_incidents": 0,
        }
        for repository, token_ratio, time_ratio in zip(
            repositories, token_ratios, times, strict=True
        )
    ]
    return (
        protocol,
        selection,
        rows,
        {
            "scenario_pack_bytes": scenario_pack_bytes,
            "collision_attestation_bytes": collision_attestation_bytes,
            "control_results_bytes": control_results_bytes,
            "reconstructed_tests": reconstructed_tests,
        },
    )


def test_claim_gate_passes_only_declared_overall_aggregation() -> None:
    protocol, selection, rows, artifacts = _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88])

    result = selector.evaluate_repository_claim(
        rows,
        protocol,
        selection,
        **artifacts,
    )

    assert result["overall_token_ratio"] == 0.84
    assert result["overall_time_ratio"] == 1.0
    assert result["exact_one_sided_sign_p"] == 0.03125
    assert result["passed"] is True


def test_claim_gate_rejects_failed_global_or_evidence_gate() -> None:
    fixtures = [
        _claim_fixture([0.86, 0.87, 0.88, 0.89, 0.90]),
        _claim_fixture(
            [0.80, 0.82, 0.84, 0.86, 0.88],
            time_ratios=[1.0, 1.1, 1.2, 1.3, 1.4],
        ),
        _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88]),
    ]
    fixtures[-1][2][0]["missing_pairs"] = 1

    for protocol, selection, rows, artifacts in fixtures:
        assert (
            selector.evaluate_repository_claim(
                rows,
                protocol,
                selection,
                **artifacts,
            )["passed"]
            is False
        )


def test_claim_gate_rejects_wrong_or_unfrozen_selection() -> None:
    protocol, selection, rows, artifacts = _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88])
    rows[0]["repository"] = "https://github.com/owner/wrong.git"
    with pytest.raises(ValueError, match="frozen selection"):
        selector.evaluate_repository_claim(rows, protocol, selection, **artifacts)

    protocol, selection, rows, artifacts = _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88])
    protocol["execution_inputs"]["selection_output_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="execution freeze"):
        selector.evaluate_repository_claim(rows, protocol, selection, **artifacts)


def test_claim_gate_requires_six_trials_for_each_scenario() -> None:
    protocol, selection, rows, artifacts = _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88])
    rows[0]["paired_trials_by_scenario"] = {
        "scenario-0": 12,
        "scenario-5": 0,
    }

    assert (
        selector.evaluate_repository_claim(
            rows,
            protocol,
            selection,
            **artifacts,
        )["passed"]
        is False
    )


def test_claim_gate_authenticates_execution_artifact_bytes() -> None:
    protocol, selection, rows, artifacts = _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88])
    artifacts["scenario_pack_bytes"] += b" "
    with pytest.raises(ValueError, match="scenario pack"):
        selector.evaluate_repository_claim(rows, protocol, selection, **artifacts)

    protocol, selection, rows, artifacts = _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88])
    legacy_rows = json.loads(artifacts["scenario_pack_bytes"])["scenarios"]
    artifacts["scenario_pack_bytes"] = json.dumps(
        legacy_rows,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    protocol["execution_inputs"]["scenario_pack_sha256"] = hashlib.sha256(
        artifacts["scenario_pack_bytes"]
    ).hexdigest()
    with pytest.raises(ValueError, match="scenario pack"):
        selector.evaluate_repository_claim(rows, protocol, selection, **artifacts)

    protocol, selection, rows, artifacts = _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88])
    artifacts["collision_attestation_bytes"] = b"{}"
    with pytest.raises(ValueError, match="collision attestation"):
        selector.evaluate_repository_claim(rows, protocol, selection, **artifacts)

    protocol, selection, rows, artifacts = _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88])
    collision = json.loads(artifacts["collision_attestation_bytes"])
    collision["collision_free"] = False
    artifacts["collision_attestation_bytes"] = json.dumps(
        collision,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    protocol["execution_inputs"]["collision_attestation_sha256"] = hashlib.sha256(
        artifacts["collision_attestation_bytes"]
    ).hexdigest()
    with pytest.raises(ValueError, match="collision attestation is invalid"):
        selector.evaluate_repository_claim(rows, protocol, selection, **artifacts)

    protocol, selection, rows, artifacts = _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88])
    controls = json.loads(artifacts["control_results_bytes"])
    controls["scenario_results"]["scenario-0"]["reference_patch_green"] = False
    artifacts["control_results_bytes"] = json.dumps(
        controls,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    protocol["execution_inputs"]["control_results_sha256"] = hashlib.sha256(
        artifacts["control_results_bytes"]
    ).hexdigest()
    with pytest.raises(ValueError, match="control results are invalid"):
        selector.evaluate_repository_claim(rows, protocol, selection, **artifacts)

    protocol, selection, rows, artifacts = _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88])
    controls = json.loads(artifacts["control_results_bytes"])
    controls["scenario_results"]["scenario-0"]["timeout_seconds"] = 901
    artifacts["control_results_bytes"] = json.dumps(
        controls,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    protocol["execution_inputs"]["control_results_sha256"] = hashlib.sha256(
        artifacts["control_results_bytes"]
    ).hexdigest()
    with pytest.raises(ValueError, match="control results are invalid"):
        selector.evaluate_repository_claim(rows, protocol, selection, **artifacts)


def test_claim_gate_runs_reconstructed_module_validator() -> None:
    protocol, selection, rows, artifacts = _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88])
    artifacts["reconstructed_tests"]["scenario-0"] = "import requests\n"

    with pytest.raises(ValueError, match="forbidden import"):
        selector.evaluate_repository_claim(rows, protocol, selection, **artifacts)

    protocol, selection, rows, artifacts = _claim_fixture([0.80, 0.82, 0.84, 0.86, 0.88])
    artifacts["reconstructed_tests"]["scenario-0"] = "def test_changed():\n    assert True\n"
    with pytest.raises(ValueError, match="reconstructed tests"):
        selector.evaluate_repository_claim(rows, protocol, selection, **artifacts)


def test_protocol_requires_execution_freeze_before_selected_task_exposure() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    blinding = protocol["pre_execution_blinding"]

    assert "either model arm" in blinding["forbidden_before_freeze"]
    assert blinding["collision_outcome"] == "protocol no-go with no candidate replacement"
    assert "execution-freeze commit" in blinding["invalidation"]
    assert (
        "runtime-pack-distinctive-evidence-v1 collision attestation"
        in protocol["freeze_manifest_requirements"]
    )
    assert any(
        "reconstructed selected test module" in requirement
        for requirement in protocol["control_requirements"]
    )
