"""Fail-closed process isolation for untrusted CTX Fit trial code.

Repository verification commands and package build backends are arbitrary code
from a repository CTX has not yet established is trustworthy.  A temporary
directory and a scrubbed environment are necessary, but neither is a filesystem
boundary: code can name an absolute path.  This module turns one explicit root
into the only writable tree and can independently disable network access.

The macOS path uses the host's Seatbelt implementation directly because the
Codex ``:workspace`` compatibility profile deliberately leaves the system temp
tree writable and the host readable. Linux builds an empty Bubblewrap root and
binds back only named runtime roots plus a short platform-runtime allowlist.
These are operating-system policy boundaries, not a cryptographic or VM
boundary. Native Windows is outside the supported platform contract.
"""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path


_MACOS_RUNTIME_READ_TREES = (
    "/bin",
    "/System/Library",
    "/usr/bin",
    "/usr/lib",
    # Current macOS releases may load system libraries from the cryptex rather
    # than the legacy paths above. These trees contain signed OS runtime code,
    # not user or repository data.
    "/System/Volumes/Preboot/Cryptexes/OS/System/Library",
    "/System/Volumes/Preboot/Cryptexes/OS/usr/lib",
    "/private/var/db/timezone/zoneinfo",
)
_MACOS_RUNTIME_READ_FILES = (
    "/dev/null",
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/tty",
    "/dev/ptmx",
    "/private/etc/localtime",
)
_MACOS_DNS_SOCKET = "/private/var/run/mDNSResponder"

# Bubblewrap starts from an empty root. These read-only bindings are the small
# set a dynamically linked POSIX process can need in addition to its declared
# executable/runtime roots. Missing paths are simply omitted.
_LINUX_RUNTIME_TREES = (
    Path("/bin"),
    Path("/lib"),
    Path("/lib64"),
)
_LINUX_RUNTIME_FILES = (
    Path("/etc/ld.so.cache"),
    Path("/etc/localtime"),
)
_LINUX_NETWORK_READ_TREES = (Path("/etc/ssl/certs"),)
_LINUX_NETWORK_READ_FILES = (
    Path("/etc/host.conf"),
    Path("/etc/nsswitch.conf"),
    Path("/etc/resolv.conf"),
    Path("/etc/services"),
    Path("/etc/gai.conf"),
    Path("/etc/ssl/cert.pem"),
)


class SandboxUnavailable(RuntimeError):
    """The host cannot enforce the boundary a live trial requires."""


def require_sandbox_available(environment: Mapping[str, str]) -> None:
    """Fail before planning/spend when this host lacks an enforceable boundary."""

    system = platform.system()
    if system == "Darwin":
        if not Path("/usr/bin/sandbox-exec").is_file():
            raise SandboxUnavailable("macOS sandbox-exec is unavailable")
        return
    if system == "Linux":
        if shutil.which("bwrap", path=environment.get("PATH")) is None:
            raise SandboxUnavailable("the Bubblewrap Linux sandbox is unavailable")
        return
    raise SandboxUnavailable(f"live CTX Fit trials are not supported on {system or 'this host'}")


def _real(path: Path) -> Path:
    """Normalize aliases such as macOS ``/tmp`` -> ``/private/tmp``."""

    return Path(os.path.realpath(path))


def _aliases(path: Path) -> tuple[Path, ...]:
    """Keep both the spelling a command may use and its resolved target."""

    return tuple(dict.fromkeys((path.absolute(), _real(path))))


def _parents(paths: Sequence[Path]) -> tuple[Path, ...]:
    """Return deterministic ancestors needed to resolve allowed paths."""

    parents: set[Path] = set()
    for path in paths:
        current = path.parent
        while True:
            parents.add(current)
            if current == current.parent:
                break
            current = current.parent
    return tuple(sorted(parents, key=lambda item: (len(item.parts), str(item))))


def _macos_policy(
    *,
    writable_root_count: int,
    read_root_count: int,
    read_path_count: int,
    read_ancestor_count: int,
    network: bool,
) -> str:
    """A Seatbelt policy with one writable and bounded readable subtrees.

    Seatbelt deny rules take precedence over the compatibility ``allow
    default``. The global read deny therefore reduces filesystem visibility to
    the writable workspace, declared runtime roots, path-resolution ancestors,
    and the enumerated macOS runtime files above. Ancestors are exact literals,
    so allowing ``/Users/alice`` to resolve a workspace does not allow any of
    its children to be read.
    """

    network_clause = (
        # Internet-enabled setup still must not connect to arbitrary host Unix
        # sockets. macOS DNS uses this one OS service; TCP and UDP remain under
        # the caller's explicit ``network=True`` authority.
        "\n(deny network-outbound\n"
        "  (require-all\n"
        '    (regex #"^/")\n'
        f'    (require-not (literal "{_MACOS_DNS_SOCKET}"))))'
        if network
        else "\n(deny network*)"
    )
    process_ipc_clause = ""
    if not network:
        # Repository code may fork/exec its own test tools, but it may neither
        # signal nor inspect ambient same-user processes. Seatbelt exposes only
        # a self target filter, so child-directed signaling is intentionally
        # unavailable as part of the fail-closed boundary. The trusted provider
        # process is the sole production network=True caller and remains a
        # separate authority because it must manage its MCP child and macOS
        # networking services; repository code never enters that process.
        process_ipc_clause = (
            "(deny signal (require-not (target self)))\n"
            "(deny process-info* (require-not (target self)))\n"
            "(deny mach-register)\n"
            "(deny mach-per-user-lookup)\n"
            "(deny ipc-posix-shm)\n"
            "(deny mach-lookup)\n"
            "(deny mach-bootstrap)\n"
            "(deny mach-issue-extension)\n"
        )
    writable_subtree_exceptions = "".join(
        f'\n    (require-not (subpath (param "CTX_FIT_WRITABLE_ROOT_{index}")))'
        for index in range(writable_root_count)
    )
    dynamic_subtree_exceptions = "".join(
        f'\n    (require-not (subpath (param "CTX_FIT_READ_ROOT_{index}")))'
        for index in range(read_root_count)
    )
    dynamic_path_exceptions = "".join(
        f'\n    (require-not (literal (param "CTX_FIT_READ_PATH_{index}")))'
        for index in range(read_path_count)
    )
    ancestor_exceptions = "".join(
        f'\n    (require-not (literal (param "CTX_FIT_READ_ANCESTOR_{index}")))'
        for index in range(read_ancestor_count)
    )
    platform_tree_exceptions = "".join(
        f'\n    (require-not (subpath "{path}"))' for path in _MACOS_RUNTIME_READ_TREES
    )
    platform_file_exceptions = "".join(
        f'\n    (require-not (literal "{path}"))' for path in _MACOS_RUNTIME_READ_FILES
    )
    read_boundary = (
        "  (require-all\n"
        f"{writable_subtree_exceptions}"
        f"{dynamic_subtree_exceptions}"
        f"{dynamic_path_exceptions}"
        f"{ancestor_exceptions}"
        f"{platform_tree_exceptions}"
        f"{platform_file_exceptions}\n"
        '    (require-not (regex #"^/dev/fd/[0-9]+$"))\n'
        '    (require-not (regex #"^/dev/ttys[0-9]+$")))'
    )
    read_deny = f"(deny file-read*\n{read_boundary})\n(deny file-map-executable\n{read_boundary})\n"
    return (
        "(version 1)\n"
        "(allow default)\n"
        f"{process_ipc_clause}"
        f"{read_deny}"
        "(deny file-write*\n"
        "  (require-all\n"
        f"{writable_subtree_exceptions}\n"
        '    (require-not (literal "/dev/null"))\n'
        '    (require-not (literal "/dev/zero"))\n'
        '    (require-not (literal "/dev/tty"))\n'
        '    (require-not (literal "/dev/ptmx"))\n'
        '    (require-not (regex #"^/dev/fd/[0-9]+$"))\n'
        '    (require-not (regex #"^/dev/ttys[0-9]+$"))))'
        f"{network_clause}"
    )


def sandboxed_command(
    command: Sequence[str],
    *,
    cwd: Path,
    writable_root: Path,
    network: bool,
    environment: Mapping[str, str],
    read_roots: Sequence[Path] = (),
    read_paths: Sequence[Path] = (),
) -> tuple[str, ...]:
    """Return an argv that enforces the requested trial boundary.

    The returned program inherits ``environment`` from the caller.  ``cwd`` may
    be below ``writable_root`` (the campaign environment and trial repository
    intentionally share one private root), but it may never escape it.
    """

    root_aliases = _aliases(writable_root)
    root = _real(writable_root)
    working = _real(cwd)
    try:
        working.relative_to(root)
    except ValueError as exc:
        raise SandboxUnavailable("the trial working directory escapes its writable root") from exc

    readable = tuple(dict.fromkeys(alias for path in read_roots for alias in _aliases(path)))
    readable_paths = tuple(dict.fromkeys(alias for path in read_paths for alias in _aliases(path)))
    private_roots = (_real(Path.home()), _real(Path(tempfile.gettempdir())))
    for readable_root in readable:
        for private_root in private_roots:
            try:
                private_root.relative_to(readable_root)
            except ValueError:
                continue
            raise SandboxUnavailable(
                f"a runtime read root is too broad for isolation: {readable_root}"
            )

    system = platform.system()
    if system == "Darwin":
        executable = Path("/usr/bin/sandbox-exec")
        if not executable.is_file():
            raise SandboxUnavailable("macOS sandbox-exec is unavailable")
        ancestors = _parents(
            (
                *root_aliases,
                *readable,
                *readable_paths,
                *map(Path, _MACOS_RUNTIME_READ_TREES),
            )
        )
        parameters = (
            *(f"-DCTX_FIT_WRITABLE_ROOT_{index}={path}" for index, path in enumerate(root_aliases)),
            *(f"-DCTX_FIT_READ_ROOT_{index}={path}" for index, path in enumerate(readable)),
            *(f"-DCTX_FIT_READ_PATH_{index}={path}" for index, path in enumerate(readable_paths)),
            *(f"-DCTX_FIT_READ_ANCESTOR_{index}={path}" for index, path in enumerate(ancestors)),
        )
        return (
            str(executable),
            "-p",
            _macos_policy(
                writable_root_count=len(root_aliases),
                read_root_count=len(readable),
                read_path_count=len(readable_paths),
                read_ancestor_count=len(ancestors),
                network=network,
            ),
            *parameters,
            "--",
            *command,
        )

    if system == "Linux":
        linux_executable = shutil.which("bwrap", path=environment.get("PATH"))
        if linux_executable is None:
            raise SandboxUnavailable("the Bubblewrap Linux sandbox is unavailable")
        argv: list[str] = [
            linux_executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--tmpfs",
            "/",
        ]
        if not network:
            argv.append("--unshare-net")

        platform_trees = tuple(path for path in _LINUX_RUNTIME_TREES if path.exists())
        platform_files = tuple(path for path in _LINUX_RUNTIME_FILES if path.exists())
        if network:
            platform_trees += tuple(path for path in _LINUX_NETWORK_READ_TREES if path.exists())
            platform_files += tuple(path for path in _LINUX_NETWORK_READ_FILES if path.exists())

        readonly_bindings: list[Path] = [*platform_trees, *platform_files, *readable_paths]
        for path in readable:
            try:
                path.relative_to(root)
            except ValueError:
                readonly_bindings.append(path)

        # The empty root contains no ambient host files or socket paths. Build
        # only the destination ancestors needed for explicit bindings.
        for directory in _parents((*readonly_bindings, root)):
            if directory != Path("/"):
                argv.extend(("--dir", str(directory)))
        for path in dict.fromkeys(readonly_bindings):
            argv.extend(("--ro-bind", str(path), str(path)))
        argv.extend(
            (
                "--bind",
                str(root),
                str(root),
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--chdir",
                str(working),
                "--",
                *command,
            )
        )
        return tuple(argv)

    raise SandboxUnavailable(f"live CTX Fit trials are not supported on {system or 'this host'}")


__all__ = ["SandboxUnavailable", "require_sandbox_available", "sandboxed_command"]
