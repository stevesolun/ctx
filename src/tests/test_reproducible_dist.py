from __future__ import annotations

import gzip
import importlib.util
import io
import os
import shutil
import subprocess
import tarfile
from hashlib import sha256
from pathlib import Path

import pytest

from scripts import build_reproducible_dist as reproducible

_EPOCH = 1_700_000_000
_ROOT = Path(__file__).resolve().parents[2]


def _add_members(tf: tarfile.TarFile, *, mtime: int, reverse: bool = False) -> None:
    root = tarfile.TarInfo("example-1.0")
    root.type = tarfile.DIRTYPE
    root.mode = 0o755
    root.uid = 501
    root.gid = 20
    root.uname = "builder"
    root.gname = "staff"
    root.mtime = mtime

    payload = b"reproducible payload\n"
    regular = tarfile.TarInfo("example-1.0/data.txt")
    regular.size = len(payload)
    regular.mode = 0o640
    regular.uid = 501
    regular.gid = 20
    regular.uname = "builder"
    regular.gname = "staff"
    regular.mtime = mtime
    regular.pax_headers = {
        "mtime": f"{mtime}.5",
        "SCHILY.xattr.user.test": "preserved",
    }

    symlink = tarfile.TarInfo("example-1.0/data-link")
    symlink.type = tarfile.SYMTYPE
    symlink.linkname = "data.txt"
    symlink.mode = 0o777
    symlink.mtime = mtime

    hardlink = tarfile.TarInfo("example-1.0/data-hardlink")
    hardlink.type = tarfile.LNKTYPE
    hardlink.linkname = "example-1.0/data.txt"
    hardlink.mode = 0o640
    hardlink.mtime = mtime

    device = tarfile.TarInfo("example-1.0/null-device")
    device.type = tarfile.CHRTYPE
    device.mode = 0o600
    device.devmajor = 1
    device.devminor = 3
    device.mtime = mtime

    members: list[tuple[tarfile.TarInfo, bytes | None]] = [
        (root, None),
        (regular, payload),
        (symlink, None),
        (hardlink, None),
        (device, None),
    ]
    if reverse:
        members.reverse()
    for info, body in members:
        tf.addfile(info, None if body is None else io.BytesIO(body))


def _write_fixture(path: Path, *, mtime: int, reverse: bool = False) -> None:
    with (
        path.open("wb") as raw,
        gzip.GzipFile(
            filename=path.name,
            mode="wb",
            fileobj=raw,
            mtime=mtime,
        ) as compressed,
        tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as tf,
    ):
        _add_members(tf, mtime=mtime, reverse=reverse)


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def test_normalize_sdist_removes_timestamp_and_order_drift(tmp_path: Path) -> None:
    first = tmp_path / "example-1.0-a.tar.gz"
    second = tmp_path / "example-1.0-b.tar.gz"
    _write_fixture(first, mtime=1_600_000_000)
    _write_fixture(second, mtime=1_650_000_000, reverse=True)

    reproducible.normalize_sdist(first, _EPOCH)
    reproducible.normalize_sdist(second, _EPOCH)

    assert first.read_bytes() == second.read_bytes()
    with first.open("rb") as handle:
        gzip_header = handle.read(10)
    assert not gzip_header[3] & 0x08
    assert int.from_bytes(gzip_header[4:8], "little") == _EPOCH

    with tarfile.open(first, "r:gz") as tf:
        members = tf.getmembers()
        by_name = {member.name: member for member in members}
        payload = tf.extractfile("example-1.0/data.txt")
        assert payload is not None
        assert payload.read() == b"reproducible payload\n"

    assert [member.name for member in members] == sorted(member.name for member in members)
    assert all(member.mtime == _EPOCH for member in members)
    assert all(member.uid == 0 and member.gid == 0 for member in members)
    assert all(not member.uname and not member.gname for member in members)
    assert by_name["example-1.0"].isdir()
    assert by_name["example-1.0"].mode == 0o755
    assert by_name["example-1.0/data.txt"].mode == 0o640
    assert by_name["example-1.0/data-link"].issym()
    assert by_name["example-1.0/data-link"].linkname == "data.txt"
    assert by_name["example-1.0/data-hardlink"].islnk()
    assert by_name["example-1.0/data-hardlink"].linkname == "example-1.0/data.txt"
    assert by_name["example-1.0/null-device"].ischr()
    assert by_name["example-1.0/null-device"].devmajor == 1
    assert by_name["example-1.0/null-device"].devminor == 3
    assert by_name["example-1.0/data.txt"].pax_headers["SCHILY.xattr.user.test"] == "preserved"
    assert all(
        not reproducible._is_normalized_pax_field(key)
        for member in members
        for key in member.pax_headers
    )


def test_equivalence_check_detects_payload_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source.tar.gz"
    tampered = tmp_path / "tampered.tar.gz"
    _write_fixture(source, mtime=1_600_000_000)
    _write_fixture(tampered, mtime=1_600_000_000)

    with tarfile.open(tampered, "w:gz", format=tarfile.PAX_FORMAT) as tf:
        root = tarfile.TarInfo("example-1.0")
        root.type = tarfile.DIRTYPE
        tf.addfile(root)
        body = b"tampered payload\n"
        regular = tarfile.TarInfo("example-1.0/data.txt")
        regular.size = len(body)
        regular.mode = 0o640
        tf.addfile(regular, io.BytesIO(body))
        symlink = tarfile.TarInfo("example-1.0/data-link")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "data.txt"
        symlink.mode = 0o777
        tf.addfile(symlink)
        hardlink = tarfile.TarInfo("example-1.0/data-hardlink")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "example-1.0/data.txt"
        hardlink.mode = 0o640
        tf.addfile(hardlink)
        device = tarfile.TarInfo("example-1.0/null-device")
        device.type = tarfile.CHRTYPE
        device.mode = 0o600
        device.devmajor = 1
        device.devminor = 3
        tf.addfile(device)

    with pytest.raises(
        reproducible.ReproducibleBuildError,
        match="payloads or structural metadata",
    ):
        reproducible._verify_equivalent_archives(source, tampered)


@pytest.mark.parametrize(
    "names",
    [
        ("example-1.0/data.txt", "example-1.0/data.txt"),
        ("example-1.0/../escape.txt",),
        ("example-1.0/Data.txt", "example-1.0/data.txt"),
    ],
)
def test_normalize_sdist_rejects_unsafe_or_ambiguous_members(
    tmp_path: Path,
    names: tuple[str, ...],
) -> None:
    source = tmp_path / "unsafe.tar.gz"
    with tarfile.open(source, "w:gz") as tf:
        for name in names:
            body = b"x"
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    before = _digest(source)

    with pytest.raises(reproducible.ReproducibleBuildError):
        reproducible.normalize_sdist(source, _EPOCH)

    assert _digest(source) == before


def test_normalize_sdist_is_atomic_when_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "example-1.0.tar.gz"
    _write_fixture(source, mtime=1_600_000_000)
    before = source.read_bytes()

    def reject(*args: object, **kwargs: object) -> None:
        raise reproducible.ReproducibleBuildError("forced verification failure")

    monkeypatch.setattr(reproducible, "_verify_equivalent_archives", reject)
    with pytest.raises(reproducible.ReproducibleBuildError, match="forced verification"):
        reproducible.normalize_sdist(source, _EPOCH)

    assert source.read_bytes() == before
    assert list(tmp_path.glob(f".{source.name}.*.tmp")) == []


def test_normalize_sdist_rejects_symlink_input(tmp_path: Path) -> None:
    source = tmp_path / "example-1.0.tar.gz"
    linked = tmp_path / "linked.tar.gz"
    _write_fixture(source, mtime=1_600_000_000)
    try:
        os.symlink(source.name, linked)
    except OSError:
        pytest.skip("this platform does not permit test symlinks")

    with pytest.raises(reproducible.ReproducibleBuildError, match="not a regular file"):
        reproducible.normalize_sdist(linked, _EPOCH)

    assert linked.is_symlink()


def test_build_output_validation_rejects_extra_or_linked_artifacts(tmp_path: Path) -> None:
    (tmp_path / "example-1.0-py3-none-any.whl").write_bytes(b"wheel")
    (tmp_path / "example-1.0.tar.gz").write_bytes(b"sdist")
    (tmp_path / "unexpected.txt").write_text("extra", encoding="utf-8")

    with pytest.raises(reproducible.ReproducibleBuildError, match="exactly one wheel"):
        reproducible._validate_build_outputs(tmp_path)

    (tmp_path / "unexpected.txt").unlink()
    (tmp_path / "example-1.0.tar.gz").unlink()
    try:
        os.symlink("example-1.0-py3-none-any.whl", tmp_path / "example-1.0.tar.gz")
    except OSError:
        pytest.skip("this platform does not permit test symlinks")
    with pytest.raises(reproducible.ReproducibleBuildError, match="symlink"):
        reproducible._validate_build_outputs(tmp_path)


def test_source_date_epoch_prefers_valid_environment() -> None:
    assert reproducible.source_date_epoch(_ROOT, {"SOURCE_DATE_EPOCH": "1700000000"}) == _EPOCH
    with pytest.raises(reproducible.ReproducibleBuildError, match="non-negative integer"):
        reproducible.source_date_epoch(_ROOT, {"SOURCE_DATE_EPOCH": "-1"})


@pytest.mark.integration
def test_two_clean_git_archive_builds_are_byte_identical() -> None:
    if importlib.util.find_spec("build") is None:
        pytest.skip("python-build is not installed")
    if shutil.which("git") is None or not (_ROOT / ".git").exists():
        pytest.skip("a Git checkout is required")
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("HEAD is not available")

    hashes = reproducible.verify_reproducible_builds(_ROOT)

    assert len(hashes) == 2
    assert sum(name.endswith(".whl") for name in hashes) == 1
    assert sum(name.endswith(".tar.gz") for name in hashes) == 1
