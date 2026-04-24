"""
test_fs_utils.py -- Tests for the shared atomic file-write helpers.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure src/ is on sys.path
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from _fs_utils import atomic_write_bytes, atomic_write_json, atomic_write_text


# ── atomic_write_text ─────────────────────────────────────────────────────────


def test_write_text_happy_path(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world"


def test_write_text_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c" / "out.txt"
    atomic_write_text(target, "nested")
    assert target.read_text(encoding="utf-8") == "nested"


def test_write_text_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_write_text_custom_encoding(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    text = "caf\u00e9"  # contains non-ASCII
    atomic_write_text(target, text, encoding="utf-8")
    assert target.read_text(encoding="utf-8") == text


def test_write_text_no_temp_file_left_on_success(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "data")
    leftover = list(tmp_path.glob("out.txt.*"))
    assert leftover == [], f"Unexpected temp files: {leftover}"


def test_write_text_no_temp_file_left_on_error(tmp_path: Path) -> None:
    """Temp file must be cleaned up even when os.replace raises."""
    target = tmp_path / "out.txt"
    with patch("ctx.utils._fs_utils._replace_with_retry", side_effect=OSError("boom")):
        with pytest.raises(OSError, match="boom"):
            atomic_write_text(target, "data")
    leftover = list(tmp_path.glob("out.txt.*"))
    assert leftover == [], f"Temp file leaked: {leftover}"


# ── atomic_write_bytes ────────────────────────────────────────────────────────


def test_write_bytes_happy_path(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    atomic_write_bytes(target, b"\x00\x01\x02\x03")
    assert target.read_bytes() == b"\x00\x01\x02\x03"


def test_write_bytes_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "out.bin"
    atomic_write_bytes(target, b"bytes")
    assert target.read_bytes() == b"bytes"


# ── atomic_write_json ─────────────────────────────────────────────────────────


def test_write_json_happy_path(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    obj = {"key": "value", "num": 42}
    atomic_write_json(target, obj)
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == obj


def test_write_json_trailing_newline(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    atomic_write_json(target, {"x": 1})
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_write_json_custom_indent(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    atomic_write_json(target, {"a": 1}, indent=4)
    raw = target.read_text(encoding="utf-8")
    assert '    "a"' in raw  # 4-space indent present


def test_write_json_none_indent(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    atomic_write_json(target, {"a": 1}, indent=None)
    raw = target.read_text(encoding="utf-8").strip()
    assert raw == '{"a": 1}'


# ── Windows retry ─────────────────────────────────────────────────────────────


def test_replace_retries_on_permission_error(tmp_path: Path) -> None:
    """_replace_with_retry should succeed on the second attempt."""
    target = tmp_path / "out.txt"
    call_count = 0
    real_replace = os.replace

    def flaky_replace(src: str, dst: Path) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise PermissionError("locked")
        real_replace(src, dst)

    with patch("ctx.utils._fs_utils.os.replace", side_effect=flaky_replace):
        with patch("ctx.utils._fs_utils.time.sleep"):  # don't actually sleep
            atomic_write_text(target, "retry-test")

    assert call_count == 2
    assert target.read_text(encoding="utf-8") == "retry-test"


def test_replace_raises_after_max_attempts(tmp_path: Path) -> None:
    """After exhausting retries, the PermissionError must propagate."""
    target = tmp_path / "out.txt"

    with patch("ctx.utils._fs_utils.os.replace", side_effect=PermissionError("always locked")):
        with patch("ctx.utils._fs_utils.time.sleep"):
            with pytest.raises(PermissionError, match="always locked"):
                atomic_write_text(target, "data")


# ── Concurrency smoke test ────────────────────────────────────────────────────


def test_concurrent_writes_last_write_wins(tmp_path: Path) -> None:
    """Multiple threads writing to the same path must not leave corruption."""
    target = tmp_path / "shared.txt"
    errors: list[Exception] = []

    def writer(content: str) -> None:
        try:
            atomic_write_text(target, content)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"thread-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Writer threads raised: {errors}"
    content = target.read_text(encoding="utf-8")
    assert content.startswith("thread-")
