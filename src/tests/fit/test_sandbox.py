"""Adversarial checks for CTX Fit's untrusted repository boundary."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from ctx.fit.sandbox import SandboxUnavailable, _macos_policy, sandboxed_command


_CHILD_STARTED_SENTINEL = "__CTX_FIT_SANDBOX_CHILD_STARTED__"


def _python_roots() -> tuple[Path, ...]:
    executable = Path(sys.executable)
    return (executable.absolute().parent.parent, executable.resolve().parent.parent)


def _run_python(
    workspace: Path,
    source: str,
    *,
    network: bool = False,
    read_roots: tuple[Path, ...] = (),
    read_paths: tuple[Path, ...] = (),
) -> subprocess.CompletedProcess[str]:
    environment = {
        "HOME": str(workspace / "home"),
        "PATH": os.environ.get("PATH", os.defpath),
        "TEMP": str(workspace / "tmp"),
        "TMP": str(workspace / "tmp"),
        "TMPDIR": str(workspace / "tmp"),
    }
    (workspace / "home").mkdir(exist_ok=True)
    (workspace / "tmp").mkdir(exist_ok=True)
    try:
        command = sandboxed_command(
            (
                sys.executable,
                "-c",
                f"print({_CHILD_STARTED_SENTINEL!r}, flush=True)\n{source}",
            ),
            cwd=workspace,
            writable_root=workspace,
            network=network,
            environment=environment,
            read_roots=(*_python_roots(), *read_roots),
            read_paths=read_paths,
        )
    except SandboxUnavailable as exc:
        pytest.skip(str(exc))
    completed = subprocess.run(
        command,
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    stdout_lines = completed.stdout.splitlines(keepends=True)
    for index, line in enumerate(stdout_lines):
        if line.rstrip("\r\n") == _CHILD_STARTED_SENTINEL:
            completed.stdout = "".join((*stdout_lines[:index], *stdout_lines[index + 1 :]))
            return completed
    raise AssertionError(
        "sandbox child never started; a launcher failure is not isolation evidence\n"
        f"exit={completed.returncode}\n"
        f"stdout={completed.stdout[-1000:]}\n"
        f"stderr={completed.stderr[-1000:]}"
    )


def test_sandbox_startup_failure_cannot_masquerade_as_an_isolation_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(
        f"{__name__}.sandboxed_command",
        lambda *args, **kwargs: (sys.executable, "-c", "raise SystemExit(17)"),
    )

    with pytest.raises(AssertionError, match="sandbox child never started"):
        _run_python(workspace, "print('repository command started')")


def test_repository_process_can_write_inside_but_not_beside_its_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "escaped.txt"

    inside = _run_python(
        workspace,
        "from pathlib import Path; Path('inside.txt').write_text('inside')",
    )
    escaped = _run_python(
        workspace,
        f"from pathlib import Path; Path({str(outside)!r}).write_text('escaped')",
    )

    assert inside.returncode == 0, inside.stderr
    assert (workspace / "inside.txt").read_text(encoding="utf-8") == "inside"
    assert escaped.returncode != 0
    assert not outside.exists()


def test_repository_process_cannot_read_an_ambient_temporary_secret(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "ambient-secret.txt"
    secret.write_text("must-not-cross", encoding="utf-8")

    attempted = _run_python(
        workspace,
        f"from pathlib import Path; print(Path({str(secret)!r}).read_text())",
    )

    assert attempted.returncode != 0
    assert "must-not-cross" not in attempted.stdout


def test_workspace_symlink_cannot_expand_the_read_boundary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "ambient-secret.txt"
    secret.write_text("must-not-cross", encoding="utf-8")
    link = workspace / "outside"
    link.symlink_to(secret)

    attempted = _run_python(
        workspace,
        "from pathlib import Path; print(Path('outside').read_text())",
    )

    assert attempted.returncode != 0
    assert "must-not-cross" not in attempted.stdout


def test_repository_process_can_read_an_explicit_runtime_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    material = runtime / "runtime.txt"
    material.write_text("declared-runtime", encoding="utf-8")

    attempted = _run_python(
        workspace,
        f"from pathlib import Path; print(Path({str(material)!r}).read_text())",
        read_roots=(runtime,),
    )

    assert attempted.returncode == 0, attempted.stderr
    assert attempted.stdout.strip() == "declared-runtime"


def test_exact_read_path_does_not_expose_its_sibling(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    material = ambient / "material.txt"
    material.write_text("declared-material", encoding="utf-8")
    sibling = ambient / "ambient-secret.txt"
    sibling.write_text("must-not-cross", encoding="utf-8")

    allowed = _run_python(
        workspace,
        f"from pathlib import Path; print(Path({str(material)!r}).read_text())",
        read_paths=(material,),
    )
    attempted = _run_python(
        workspace,
        f"from pathlib import Path; print(Path({str(sibling)!r}).read_text())",
        read_paths=(material,),
    )

    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == "declared-material"
    assert attempted.returncode != 0
    assert "must-not-cross" not in attempted.stdout


def test_repository_process_cannot_read_ambient_host_configuration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    attempted = _run_python(
        workspace,
        "from pathlib import Path; print(Path('/etc/hosts').read_text())",
    )

    assert attempted.returncode != 0
    assert "localhost" not in attempted.stdout.lower()


def test_network_enabled_process_cannot_connect_to_an_ambient_unix_socket(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with tempfile.TemporaryDirectory(prefix="ctx-fit-socket-", dir="/tmp") as directory:
        socket_path = Path(directory) / "host.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(socket_path))
            listener.listen()
            attempted = _run_python(
                workspace,
                (
                    "import socket; "
                    "client = socket.socket(socket.AF_UNIX); "
                    f"client.connect({str(socket_path)!r}); "
                    "print('connected')"
                ),
                network=True,
            )

    assert attempted.returncode != 0
    assert "connected" not in attempted.stdout


def test_network_disabled_process_cannot_connect_to_tcp(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        host, port = listener.getsockname()
        attempted = _run_python(
            workspace,
            (
                "import socket; "
                f"socket.create_connection(({host!r}, {port}), timeout=1); "
                "print('connected')"
            ),
        )

    assert attempted.returncode != 0
    assert "connected" not in attempted.stdout


def test_repository_process_cannot_signal_an_ambient_host_process(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    attempted = _run_python(
        workspace,
        f"import os; os.kill({os.getpid()}, 0); print('reachable')",
    )

    assert attempted.returncode != 0
    assert "reachable" not in attempted.stdout


def test_repository_process_cannot_create_host_posix_shared_memory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    attempted = _run_python(
        workspace,
        (
            "from multiprocessing import shared_memory; "
            "region = shared_memory.SharedMemory(create=True, size=1); "
            "print('created'); region.close(); region.unlink()"
        ),
    )

    assert attempted.returncode != 0
    assert "created" not in attempted.stdout


def test_repository_process_cannot_lookup_a_host_mach_service(tmp_path: Path) -> None:
    if sys.platform != "darwin":
        pytest.skip("Mach bootstrap services are macOS-specific")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    attempted = _run_python(
        workspace,
        """\
import ctypes

libsystem = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
bootstrap_port = ctypes.c_uint.in_dll(libsystem, 'bootstrap_port').value
service_port = ctypes.c_uint()
result = libsystem.bootstrap_look_up(
    bootstrap_port,
    b'com.apple.SystemConfiguration.configd',
    ctypes.byref(service_port),
)
if result == 0:
    print('reached')
raise SystemExit(0 if result == 0 else 1)
""",
    )

    assert attempted.returncode != 0
    assert "reached" not in attempted.stdout


def test_macos_policy_denies_ambient_process_and_ipc_authority() -> None:
    policy = _macos_policy(
        writable_root_count=1,
        read_root_count=0,
        read_path_count=0,
        read_ancestor_count=1,
        network=False,
    )

    assert "(deny signal" in policy
    assert "(target self)" in policy
    assert "(deny process-info*" in policy
    assert "(deny mach-lookup)" in policy
    assert "(deny mach-register)" in policy
    assert "(deny mach-per-user-lookup)" in policy
    assert "(deny ipc-posix-shm)" in policy


def test_macos_provider_network_policy_stays_separate_from_repository_ipc_denials() -> None:
    policy = _macos_policy(
        writable_root_count=1,
        read_root_count=0,
        read_path_count=0,
        read_ancestor_count=1,
        network=True,
    )

    assert "(deny signal" not in policy
    assert "(deny mach-register)" not in policy
    assert "(deny ipc-posix-shm)" not in policy
    assert "(deny mach-lookup)" not in policy


def test_a_read_exception_cannot_expand_to_the_users_whole_home(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(SandboxUnavailable, match="too broad"):
        sandboxed_command(
            (sys.executable, "-c", "pass"),
            cwd=workspace,
            writable_root=workspace,
            network=False,
            environment={"PATH": os.environ.get("PATH", os.defpath)},
            read_roots=(Path.home(),),
        )


def test_linux_uses_an_empty_root_and_only_explicit_read_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    exact = tmp_path / "exact" / "tool"
    exact.parent.mkdir()
    exact.write_text("tool", encoding="utf-8")
    monkeypatch.setattr("ctx.fit.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("ctx.fit.sandbox.shutil.which", lambda name, path=None: "/usr/bin/bwrap")

    command = sandboxed_command(
        ("python", "-c", "pass"),
        cwd=workspace,
        writable_root=workspace,
        network=False,
        environment={"PATH": "/usr/bin"},
        read_roots=(runtime,),
        read_paths=(exact,),
    )

    assert command[0] == "/usr/bin/bwrap"
    assert ("--tmpfs", "/") == tuple(
        command[command.index("--tmpfs") : command.index("--tmpfs") + 2]
    )
    assert ("--ro-bind", "/", "/") not in tuple(zip(command, command[1:], command[2:]))
    assert "--unshare-net" in command
    bind_at = command.index("--bind")
    assert command[bind_at + 1 : bind_at + 3] == (str(workspace.resolve()),) * 2
    assert ("--ro-bind", str(runtime.resolve()), str(runtime.resolve())) in tuple(
        zip(command, command[1:], command[2:])
    )
    assert ("--ro-bind", str(exact.resolve()), str(exact.resolve())) in tuple(
        zip(command, command[1:], command[2:])
    )
    assert (
        "--ro-bind",
        str(exact.parent.resolve()),
        str(exact.parent.resolve()),
    ) not in tuple(zip(command, command[1:], command[2:]))
    assert str(Path.home().resolve()) not in command


def test_linux_network_window_does_not_bind_host_socket_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr("ctx.fit.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("ctx.fit.sandbox.shutil.which", lambda name, path=None: "/usr/bin/bwrap")

    command = sandboxed_command(
        ("python", "-c", "pass"),
        cwd=workspace,
        writable_root=workspace,
        network=True,
        environment={"PATH": "/usr/bin"},
        read_roots=(runtime,),
    )

    triples = tuple(zip(command, command[1:], command[2:]))
    assert "--unshare-net" not in command
    assert ("--ro-bind", "/", "/") not in triples
    assert "/etc/hosts" not in command
    assert not any(
        option == "--ro-bind" and source in {"/run", "/var/run"}
        for option, source, _destination in triples
    )


def test_linux_fails_closed_without_bubblewrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("ctx.fit.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("ctx.fit.sandbox.shutil.which", lambda name, path=None: None)

    with pytest.raises(SandboxUnavailable, match="Bubblewrap"):
        sandboxed_command(
            ("python", "-c", "pass"),
            cwd=workspace,
            writable_root=workspace,
            network=False,
            environment={"PATH": "/usr/bin"},
        )
