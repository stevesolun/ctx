#!/usr/bin/env python3
"""Build byte-reproducible wheel and sdist artifacts."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

_MAX_GZIP_MTIME = (1 << 32) - 1
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_TIME_PAX_FIELDS = frozenset({"atime", "birthtime", "creationtime", "ctime", "mtime"})
_OWNER_PAX_FIELDS = frozenset({"gid", "gname", "uid", "uname"})
_TRANSPORT_PAX_FIELDS = frozenset({"linkpath", "path", "size"})


class ReproducibleBuildError(RuntimeError):
    """Raised when a distribution cannot be built or verified safely."""


@dataclass(frozen=True)
class BuildArtifacts:
    wheel: Path
    sdist: Path


@dataclass(frozen=True)
class _MemberRecord:
    name: str
    type: bytes
    mode: int
    linkname: str
    size: int
    devmajor: int
    devminor: int
    pax_headers: tuple[tuple[str, str], ...]
    payload_sha256: str | None


def source_date_epoch(
    repo_root: Path,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Resolve the canonical build epoch from the environment or Git."""
    env = os.environ if environ is None else environ
    configured = env.get("SOURCE_DATE_EPOCH")
    if configured is not None:
        return _parse_epoch(configured, "SOURCE_DATE_EPOCH")

    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReproducibleBuildError(f"could not read the Git commit timestamp: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "git log failed"
        raise ReproducibleBuildError(f"could not read the Git commit timestamp: {detail}")
    return _parse_epoch(result.stdout.strip(), "Git commit timestamp")


def build_distributions(
    repo_root: Path,
    *,
    output_dir: Path | None = None,
    epoch: int | None = None,
) -> BuildArtifacts:
    """Build one wheel and one normalized sdist into ``output_dir``."""
    repo_root = repo_root.resolve()
    if not (repo_root / "pyproject.toml").is_file():
        raise ReproducibleBuildError(f"missing pyproject.toml under {repo_root}")
    resolved_epoch = source_date_epoch(repo_root) if epoch is None else _validate_epoch(epoch)
    requested_target = (repo_root / "dist") if output_dir is None else output_dir
    target_dir = Path(os.path.abspath(requested_target))
    if target_dir.is_symlink() or (target_dir.exists() and not target_dir.is_dir()):
        raise ReproducibleBuildError(f"output path is not a real directory: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".ctx-dist-build-", dir=target_dir.parent) as tmp:
        staging_dir = Path(tmp) / "dist"
        staging_dir.mkdir()
        env = dict(os.environ)
        env["SOURCE_DATE_EPOCH"] = str(resolved_epoch)
        command = [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(staging_dir),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=repo_root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise ReproducibleBuildError(f"could not run {' '.join(command)}: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ReproducibleBuildError(
                f"distribution build failed with exit code {result.returncode}: {detail}"
            )

        staged = _validate_build_outputs(staging_dir)
        normalize_sdist(staged.sdist, resolved_epoch)
        wheel = _install_artifact(staged.wheel, target_dir)
        sdist = _install_artifact(staged.sdist, target_dir)
    return BuildArtifacts(wheel=wheel, sdist=sdist)


def normalize_sdist(path: Path, epoch: int) -> None:
    """Normalize an sdist in place, replacing it only after verification."""
    path = Path(os.path.abspath(path))
    resolved_epoch = _validate_epoch(epoch)
    _require_regular_file(path, "sdist")
    if not path.name.endswith(".tar.gz"):
        raise ReproducibleBuildError(f"sdist must end in .tar.gz: {path.name}")

    expected = _archive_manifest(path, require_single_root=True)
    original_mode = stat.S_IMODE(path.stat().st_mode)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp_path = Path(raw_tmp)
    try:
        _write_normalized_archive(path, tmp_path, resolved_epoch)
        _verify_equivalent_archives(path, tmp_path, expected=expected)
        _assert_normalized_archive(tmp_path, resolved_epoch)
        os.chmod(tmp_path, original_mode)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def verify_reproducible_builds(
    repo_root: Path,
    *,
    output_dir: Path | None = None,
    git_ref: str | None = None,
) -> dict[str, str]:
    """Build the same source twice and optionally publish the verified artifacts.

    The default source is the current working tree so local preflight covers
    uncommitted changes. ``git_ref`` is available for callers that explicitly
    require a clean Git archive.
    """
    repo_root = repo_root.resolve()
    epoch = source_date_epoch(repo_root)
    target_dir = _prepare_verified_output(output_dir) if output_dir is not None else None
    results: list[dict[str, str]] = []
    first_artifacts: BuildArtifacts | None = None
    temp_parent = target_dir.parent if target_dir is not None else None
    with tempfile.TemporaryDirectory(
        prefix=".ctx-reproducible-build-",
        dir=temp_parent,
    ) as tmp:
        temp_root = Path(tmp)
        snapshot_root = _export_git_tree(
            repo_root,
            "HEAD" if git_ref is None else git_ref,
            temp_root / "snapshot",
        )
        if git_ref is None:
            _overlay_worktree(repo_root, snapshot_root)
        for index in range(2):
            source_root = Path(
                shutil.copytree(
                    snapshot_root,
                    temp_root / f"root-{index}",
                    symlinks=True,
                )
            )
            artifacts = build_distributions(
                source_root,
                output_dir=temp_root / f"dist-{index}",
                epoch=epoch,
            )
            if first_artifacts is None:
                first_artifacts = artifacts
            results.append(
                {
                    artifacts.wheel.name: _sha256_path(artifacts.wheel),
                    artifacts.sdist.name: _sha256_path(artifacts.sdist),
                }
            )
        if results[0] != results[1]:
            raise ReproducibleBuildError(
                f"two builds were not byte-identical: {results[0]} != {results[1]}"
            )
        if target_dir is not None:
            if first_artifacts is None:
                raise ReproducibleBuildError("reproducibility verification produced no artifacts")
            installed = BuildArtifacts(
                wheel=_install_artifact(first_artifacts.wheel, target_dir),
                sdist=_install_artifact(first_artifacts.sdist, target_dir),
            )
            installed_hashes = {
                installed.wheel.name: _sha256_path(installed.wheel),
                installed.sdist.name: _sha256_path(installed.sdist),
            }
            if installed_hashes != results[0]:
                raise ReproducibleBuildError("installed artifacts differ from the verified build")
    return results[0]


def _parse_epoch(raw: str, source: str) -> int:
    if not re.fullmatch(r"[0-9]+", raw):
        raise ReproducibleBuildError(f"{source} must be a non-negative integer")
    return _validate_epoch(int(raw))


def _validate_epoch(epoch: int) -> int:
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        raise ReproducibleBuildError("source date epoch must be an integer")
    if epoch < 0 or epoch > _MAX_GZIP_MTIME:
        raise ReproducibleBuildError(f"source date epoch must be between 0 and {_MAX_GZIP_MTIME}")
    return epoch


def _validate_build_outputs(directory: Path) -> BuildArtifacts:
    entries = list(directory.iterdir())
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise ReproducibleBuildError("build output contains a symlink or non-file entry")
    wheels = [entry for entry in entries if entry.name.endswith(".whl")]
    sdists = [entry for entry in entries if entry.name.endswith(".tar.gz")]
    if len(entries) != 2 or len(wheels) != 1 or len(sdists) != 1:
        names = sorted(entry.name for entry in entries)
        raise ReproducibleBuildError(
            f"build must produce exactly one wheel and one .tar.gz sdist; found {names}"
        )
    for artifact in (*wheels, *sdists):
        if artifact.stat().st_size == 0:
            raise ReproducibleBuildError(f"build produced an empty artifact: {artifact.name}")
    return BuildArtifacts(wheel=wheels[0], sdist=sdists[0])


def _install_artifact(source: Path, target_dir: Path) -> Path:
    target = target_dir / source.name
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ReproducibleBuildError(f"refusing to replace non-file artifact: {target}")
    os.replace(source, target)
    return target


def _prepare_verified_output(output_dir: Path) -> Path:
    target_dir = Path(os.path.abspath(output_dir))
    if target_dir.is_symlink() or (target_dir.exists() and not target_dir.is_dir()):
        raise ReproducibleBuildError(f"output path is not a real directory: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    if any(target_dir.iterdir()):
        raise ReproducibleBuildError(f"verified output directory must be empty: {target_dir}")
    return target_dir


def _write_normalized_archive(source: Path, target: Path, epoch: int) -> None:
    try:
        with (
            tarfile.open(source, "r:gz", errorlevel=2) as src,
            target.open("wb") as raw_target,
            gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_target,
                compresslevel=9,
                mtime=epoch,
            ) as compressed,
            tarfile.open(
                fileobj=compressed,
                mode="w|",
                format=tarfile.PAX_FORMAT,
            ) as dst,
        ):
            members = _validate_members(src.getmembers(), require_single_root=True)
            for member in sorted(members, key=lambda item: item.name):
                normalized = copy.copy(member)
                normalized.mtime = epoch
                normalized.uid = 0
                normalized.gid = 0
                normalized.uname = ""
                normalized.gname = ""
                normalized.pax_headers = _normalized_pax_headers(member.pax_headers)
                if member.isreg():
                    payload = src.extractfile(member)
                    if payload is None:
                        raise ReproducibleBuildError(
                            f"archive member payload is unreadable: {member.name}"
                        )
                    with payload:
                        dst.addfile(normalized, payload)
                else:
                    dst.addfile(normalized)
    except (OSError, tarfile.TarError) as exc:
        raise ReproducibleBuildError(f"could not normalize {source}: {exc}") from exc


def _verify_equivalent_archives(
    source: Path,
    candidate: Path,
    *,
    expected: tuple[_MemberRecord, ...] | None = None,
) -> None:
    source_manifest = (
        _archive_manifest(source, require_single_root=True) if expected is None else expected
    )
    candidate_manifest = _archive_manifest(candidate, require_single_root=True)
    if source_manifest != candidate_manifest:
        raise ReproducibleBuildError(
            "normalized sdist changed member payloads or structural metadata"
        )


def _archive_manifest(path: Path, *, require_single_root: bool) -> tuple[_MemberRecord, ...]:
    try:
        with tarfile.open(path, "r:gz", errorlevel=2) as tf:
            members = _validate_members(tf.getmembers(), require_single_root=require_single_root)
            records: list[_MemberRecord] = []
            for member in members:
                digest: str | None = None
                if member.isreg():
                    payload = tf.extractfile(member)
                    if payload is None:
                        raise ReproducibleBuildError(
                            f"archive member payload is unreadable: {member.name}"
                        )
                    with payload:
                        digest_hash = hashlib.sha256()
                        copied = 0
                        for chunk in iter(lambda: payload.read(1024 * 1024), b""):
                            digest_hash.update(chunk)
                            copied += len(chunk)
                    if copied != member.size:
                        raise ReproducibleBuildError(f"archive member size mismatch: {member.name}")
                    digest = digest_hash.hexdigest()
                records.append(
                    _MemberRecord(
                        name=member.name,
                        type=member.type,
                        mode=member.mode,
                        linkname=member.linkname,
                        size=member.size,
                        devmajor=member.devmajor,
                        devminor=member.devminor,
                        pax_headers=_semantic_pax_headers(member.pax_headers),
                        payload_sha256=digest,
                    )
                )
    except ReproducibleBuildError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise ReproducibleBuildError(f"could not inspect archive {path}: {exc}") from exc
    return tuple(sorted(records, key=lambda record: record.name))


def _validate_members(
    members: Sequence[tarfile.TarInfo],
    *,
    require_single_root: bool,
) -> list[tarfile.TarInfo]:
    if not members:
        raise ReproducibleBuildError("archive is empty")
    exact_names: set[str] = set()
    portable_names: set[str] = set()
    roots: set[str] = set()
    allowed = (
        tarfile.REGTYPE,
        tarfile.AREGTYPE,
        tarfile.DIRTYPE,
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
    )
    for member in members:
        _validate_member_name(member.name)
        if member.type not in allowed or getattr(member, "sparse", None):
            raise ReproducibleBuildError(f"archive contains unsupported member type: {member.name}")
        if not member.isreg() and member.size != 0:
            raise ReproducibleBuildError(
                f"non-file archive member has a non-zero size: {member.name}"
            )
        portable = unicodedata.normalize("NFC", member.name).casefold()
        if member.name in exact_names or portable in portable_names:
            raise ReproducibleBuildError(f"archive contains an ambiguous name: {member.name}")
        exact_names.add(member.name)
        portable_names.add(portable)
        roots.add(PurePosixPath(member.name).parts[0])
        if member.issym() or member.islnk():
            _safe_link_target(member)
    if require_single_root and len(roots) != 1:
        raise ReproducibleBuildError(
            f"sdist members must share one top-level directory; found {sorted(roots)}"
        )
    for member in members:
        if member.islnk() and _safe_link_target(member).as_posix() not in exact_names:
            raise ReproducibleBuildError(f"hard link target is missing from archive: {member.name}")
    return list(members)


def _validate_member_name(name: str) -> None:
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or _WINDOWS_DRIVE_RE.match(name)
        or any(ord(char) < 32 for char in name)
    ):
        raise ReproducibleBuildError(f"unsafe archive member name: {name!r}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReproducibleBuildError(f"non-canonical archive member name: {name!r}")
    if PurePosixPath(name).as_posix() != name:
        raise ReproducibleBuildError(f"non-canonical archive member name: {name!r}")


def _safe_link_target(member: tarfile.TarInfo) -> PurePosixPath:
    linkname = member.linkname
    if (
        not linkname
        or linkname.startswith("/")
        or "\\" in linkname
        or _WINDOWS_DRIVE_RE.match(linkname)
        or any(ord(char) < 32 for char in linkname)
    ):
        raise ReproducibleBuildError(f"unsafe archive link target: {member.name}")
    base = list(PurePosixPath(member.name).parent.parts) if member.issym() else []
    for part in linkname.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not base:
                raise ReproducibleBuildError(f"archive link escapes its root: {member.name}")
            base.pop()
        else:
            base.append(part)
    if not base or base[0] != PurePosixPath(member.name).parts[0]:
        raise ReproducibleBuildError(f"archive link escapes its root: {member.name}")
    return PurePosixPath(*base)


def _normalized_pax_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return dict(
        sorted((key, value) for key, value in headers.items() if not _is_normalized_pax_field(key))
    )


def _semantic_pax_headers(headers: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (key, value)
            for key, value in headers.items()
            if not _is_normalized_pax_field(key) and key.lower() not in _TRANSPORT_PAX_FIELDS
        )
    )


def _is_normalized_pax_field(key: str) -> bool:
    normalized = key.lower()
    return normalized in _TIME_PAX_FIELDS or normalized in _OWNER_PAX_FIELDS


def _assert_normalized_archive(path: Path, epoch: int) -> None:
    with path.open("rb") as handle:
        header = handle.read(10)
    if len(header) != 10 or header[:3] != b"\x1f\x8b\x08":
        raise ReproducibleBuildError("normalized sdist does not have a valid gzip header")
    if header[3] & 0x08:
        raise ReproducibleBuildError("normalized gzip header contains a filename")
    if int.from_bytes(header[4:8], "little") != epoch:
        raise ReproducibleBuildError("normalized gzip header has the wrong timestamp")

    try:
        with tarfile.open(path, "r:gz", errorlevel=2) as tf:
            members = _validate_members(tf.getmembers(), require_single_root=True)
    except (OSError, tarfile.TarError) as exc:
        raise ReproducibleBuildError(f"could not verify normalized archive: {exc}") from exc
    if [member.name for member in members] != sorted(member.name for member in members):
        raise ReproducibleBuildError("normalized archive members are not sorted")
    for member in members:
        if (
            member.mtime != epoch
            or member.uid != 0
            or member.gid != 0
            or member.uname
            or member.gname
        ):
            raise ReproducibleBuildError(
                f"normalized ownership or timestamp mismatch: {member.name}"
            )
        if any(_is_normalized_pax_field(key) for key in member.pax_headers):
            raise ReproducibleBuildError(
                f"normalized archive retains time or ownership PAX fields: {member.name}"
            )


def _export_git_tree(repo_root: Path, git_ref: str, target: Path) -> Path:
    target.mkdir(parents=True)
    archive_path = target / "source.tar"
    try:
        with archive_path.open("wb") as archive:
            result = subprocess.run(
                ["git", "archive", "--format=tar", "--prefix=source/", git_ref],
                cwd=repo_root,
                stdout=archive,
                stderr=subprocess.PIPE,
                check=False,
            )
    except OSError as exc:
        raise ReproducibleBuildError(f"could not run git archive: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReproducibleBuildError(f"git archive failed: {detail}")
    source_root = target / "source"
    _extract_git_archive(archive_path, target)
    archive_path.unlink()
    if not source_root.is_dir():
        raise ReproducibleBuildError("git archive did not contain the expected source root")
    return source_root


def _overlay_worktree(repo_root: Path, snapshot_root: Path) -> None:
    changed = _git_paths(
        repo_root,
        ["diff", "--name-only", "--no-renames", "-z", "HEAD", "--"],
    )
    untracked = _git_paths(
        repo_root,
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    for name in sorted(set(changed + untracked)):
        _sync_snapshot_path(repo_root, snapshot_root, name)


def _git_paths(repo_root: Path, args: Sequence[str]) -> list[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReproducibleBuildError(f"could not inspect the current worktree: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReproducibleBuildError(f"could not inspect the current worktree: {detail}")
    return [os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw]


def _sync_snapshot_path(repo_root: Path, snapshot_root: Path, name: str) -> None:
    _validate_member_name(name)
    source = _archive_target(repo_root, name)
    target = _archive_target(snapshot_root, name)
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    if not source.exists() and not source.is_symlink():
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        linkname = os.readlink(source)
        member = tarfile.TarInfo(f"source/{name}")
        member.type = tarfile.SYMTYPE
        member.linkname = linkname
        _safe_link_target(member)
        os.symlink(linkname, target)
    elif source.is_file():
        shutil.copy2(source, target)
    else:
        raise ReproducibleBuildError(f"unsupported worktree entry: {name}")


def _extract_git_archive(archive_path: Path, target: Path) -> None:
    try:
        with tarfile.open(archive_path, "r:", errorlevel=2) as tf:
            members = _validate_members(tf.getmembers(), require_single_root=True)
            directories = sorted(
                (member for member in members if member.isdir()),
                key=lambda member: len(PurePosixPath(member.name).parts),
            )
            for member in directories:
                _archive_target(target, member.name).mkdir(parents=True, exist_ok=False)
            for member in (item for item in members if item.isreg()):
                destination = _archive_target(target, member.name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                payload = tf.extractfile(member)
                if payload is None:
                    raise ReproducibleBuildError(
                        f"git archive member payload is unreadable: {member.name}"
                    )
                with payload, destination.open("xb") as output:
                    shutil.copyfileobj(payload, output)
                os.chmod(destination, stat.S_IMODE(member.mode))
            for member in (item for item in members if item.islnk()):
                destination = _archive_target(target, member.name)
                link_target = _archive_target(target, _safe_link_target(member).as_posix())
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.link(link_target, destination)
            for member in (item for item in members if item.issym()):
                destination = _archive_target(target, member.name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(member.linkname, destination)
            unsupported = [
                member.name
                for member in members
                if not (member.isdir() or member.isreg() or member.islnk() or member.issym())
            ]
            if unsupported:
                raise ReproducibleBuildError(
                    f"git archive contains unsupported filesystem entries: {unsupported}"
                )
            for member in sorted(
                directories,
                key=lambda item: len(PurePosixPath(item.name).parts),
                reverse=True,
            ):
                os.chmod(_archive_target(target, member.name), stat.S_IMODE(member.mode))
    except ReproducibleBuildError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise ReproducibleBuildError(f"could not extract clean Git archive: {exc}") from exc


def _archive_target(root: Path, name: str) -> Path:
    return root.joinpath(*PurePosixPath(name).parts)


def _require_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ReproducibleBuildError(f"{label} is not a regular file: {path}")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument(
        "--verify",
        action="store_true",
        help="build the current worktree twice and compare artifact hashes",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="write built artifacts here; with --verify, only verified bytes are written",
    )
    args = parser.parse_args(argv)
    try:
        if args.verify:
            hashes = verify_reproducible_builds(args.repo, output_dir=args.output_dir)
            for name, digest in sorted(hashes.items()):
                print(f"{digest}  {name}")
        else:
            artifacts = build_distributions(args.repo, output_dir=args.output_dir)
            print(artifacts.wheel)
            print(artifacts.sdist)
    except ReproducibleBuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
