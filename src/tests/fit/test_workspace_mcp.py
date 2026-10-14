from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ctx.fit import workspace_mcp
from ctx.fit.workspace_mcp import workspace_mcp_command


def _run_server(
    root: Path,
    frames: list[dict[str, object]],
    *,
    material_digest: str | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = list(workspace_mcp_command(str(root)))
    if material_digest is not None:
        index = command.index("--material-digest") + 1
        command[index] = material_digest
    return subprocess.run(
        command,
        input="".join(json.dumps(frame) + "\n" for frame in frames),
        capture_output=True,
        cwd=cwd,
        env=env,
        text=True,
        timeout=10,
        check=False,
    )


def _request(request_id: int, method: str, params: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _responses(completed: subprocess.CompletedProcess[str]) -> dict[int, dict[str, object]]:
    frames = [json.loads(line) for line in completed.stdout.splitlines()]
    return {int(frame["id"]): frame for frame in frames}


def _tool_text(response: dict[str, object]) -> str:
    result = response["result"]
    assert isinstance(result, dict)
    content = result["content"]
    assert isinstance(content, list)
    block = content[0]
    assert isinstance(block, dict)
    return str(block["text"])


def test_command_binds_deterministic_bundled_material_before_start() -> None:
    first = workspace_mcp_command(".")
    second = workspace_mcp_command(".")

    assert first == second
    assert first[0] == sys.executable
    assert first[1] == "-I"
    assert Path(first[2]).samefile(Path(workspace_mcp.__file__))
    assert "-m" not in first
    assert "npx" not in first
    digest = first[first.index("--material-digest") + 1]
    assert digest == workspace_mcp.workspace_mcp_material_digest()
    assert len(digest) == 64


@pytest.mark.parametrize("attack_surface", ["cwd", "pythonpath"])
def test_command_ignores_untrusted_python_import_roots(
    tmp_path: Path,
    attack_surface: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shadow_root = tmp_path / "shadow"
    shadow_module = shadow_root / "ctx" / "fit" / "workspace_mcp.py"
    shadow_module.parent.mkdir(parents=True)
    (shadow_root / "ctx" / "__init__.py").write_text("", encoding="utf-8")
    (shadow_root / "ctx" / "fit" / "__init__.py").write_text("", encoding="utf-8")
    sentinel = tmp_path / f"{attack_surface}-shadow-executed"
    shadow_module.write_text(
        "from pathlib import Path\n"
        "import json\n"
        "import sys\n"
        f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
        f"print(json.dumps({{'shadow': {attack_surface!r}, 'argv': sys.argv}}))\n",
        encoding="utf-8",
    )
    safe_cwd = tmp_path / "safe-cwd"
    safe_cwd.mkdir()
    env = dict(os.environ)
    if attack_surface == "cwd":
        cwd = shadow_root
        env.pop("PYTHONPATH", None)
    else:
        cwd = safe_cwd
        env["PYTHONPATH"] = str(shadow_root)

    completed = _run_server(
        workspace,
        [
            _request(
                1,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            )
        ],
        cwd=cwd,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert not sentinel.exists()
    response = _responses(completed)[1]
    result = response["result"]
    assert isinstance(result, dict)
    assert result["serverInfo"] == {"name": "ctx-fit-workspace", "version": "1"}


def test_child_handshake_exposes_only_workspace_coding_tools(tmp_path: Path) -> None:
    completed = _run_server(
        tmp_path,
        [
            _request(
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            ),
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            _request(2, "tools/list", {}),
            _request(3, "ping", {}),
            {"jsonrpc": "2.0", "method": "ping", "params": {}},
        ],
    )

    assert completed.returncode == 0, completed.stderr
    responses = _responses(completed)
    assert responses[1]["result"]["protocolVersion"] == "2024-11-05"  # type: ignore[index]
    tools = responses[2]["result"]["tools"]  # type: ignore[index]
    assert {tool["name"] for tool in tools} == {
        "create_directory",
        "directory_tree",
        "edit_file",
        "get_file_info",
        "list_allowed_directories",
        "list_directory",
        "read_multiple_files",
        "read_text_file",
        "search_files",
        "write_file",
    }
    assert responses[3]["result"] == {}


@pytest.mark.parametrize(
    ("requested", "expected"),
    (
        ("2025-11-25", "2025-11-25"),
        ("2024-11-05", "2024-11-05"),
        ("2026-01-01", "2025-11-25"),
        (None, "2025-11-25"),
    ),
)
def test_initialize_negotiates_only_supported_protocol_versions(
    tmp_path: Path, requested: str | None, expected: str
) -> None:
    params: dict[str, object] = {"capabilities": {}}
    if requested is not None:
        params["protocolVersion"] = requested

    completed = _run_server(tmp_path, [_request(1, "initialize", params)])

    assert completed.returncode == 0, completed.stderr
    response = _responses(completed)[1]
    result = response["result"]
    assert isinstance(result, dict)
    assert result["protocolVersion"] == expected
    assert result["protocolVersion"] != "2026-01-01"


def test_child_can_edit_inside_root_and_rejects_parent_traversal(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    completed = _run_server(
        tmp_path,
        [
            _request(
                1,
                "tools/call",
                {
                    "name": "write_file",
                    "arguments": {"path": "src/value.txt", "content": "one\n"},
                },
            ),
            _request(
                2,
                "tools/call",
                {
                    "name": "edit_file",
                    "arguments": {
                        "path": "src/value.txt",
                        "edits": [{"oldText": "one", "newText": "two"}],
                    },
                },
            ),
            _request(
                3,
                "tools/call",
                {"name": "read_text_file", "arguments": {"path": "src/value.txt"}},
            ),
            _request(
                4,
                "tools/call",
                {
                    "name": "write_file",
                    "arguments": {"path": f"../{outside.name}", "content": "escaped"},
                },
            ),
        ],
    )

    assert completed.returncode == 0, completed.stderr
    responses = _responses(completed)
    assert _tool_text(responses[3]) == "two\n"
    rejected = responses[4]["result"]
    assert isinstance(rejected, dict)
    assert rejected["isError"] is True
    assert "outside the workspace root" in _tool_text(responses[4])
    assert not outside.exists()


@pytest.mark.skipif(os.name == "nt", reason="native Windows is not supported")
def test_child_refuses_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "escape").symlink_to(outside)

    completed = _run_server(
        tmp_path,
        [
            _request(
                1,
                "tools/call",
                {"name": "read_text_file", "arguments": {"path": "escape"}},
            )
        ],
    )

    assert completed.returncode == 0, completed.stderr
    response = _responses(completed)[1]
    result = response["result"]
    assert isinstance(result, dict)
    assert result["isError"] is True
    assert "symlink" in _tool_text(response)


@pytest.mark.skipif(os.name == "nt", reason="native Windows is not supported")
def test_read_rejects_a_symlink_swap_at_the_final_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    victim = root / "victim.txt"
    victim.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")
    workspace = workspace_mcp._Workspace(root)
    original_is_file = Path.is_file
    original_open = os.open
    swapped = False

    def swap_victim() -> None:
        nonlocal swapped
        if swapped:
            return
        victim.unlink()
        victim.symlink_to(outside)
        swapped = True

    def racing_is_file(path: Path) -> bool:
        if path == victim:
            swap_victim()
        return original_is_file(path)

    def racing_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "victim.txt" and dir_fd is not None:
            swap_victim()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(Path, "is_file", racing_is_file)
    monkeypatch.setattr(os, "open", racing_open)

    with pytest.raises(workspace_mcp._ToolError, match="symlink|changed"):
        workspace.read_text("victim.txt")

    assert swapped is True
    assert outside.read_text(encoding="utf-8") == "OUTSIDE-SECRET"


@pytest.mark.skipif(os.name == "nt", reason="native Windows is not supported")
def test_create_directory_rejects_a_parent_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    parent = root / "parent"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = workspace_mcp._Workspace(root)
    original_mkdir = Path.mkdir
    original_open = os.open
    swapped = False

    def swap_parent() -> None:
        nonlocal swapped
        if swapped:
            return
        parent.rmdir()
        parent.symlink_to(outside, target_is_directory=True)
        swapped = True

    def racing_mkdir(
        path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if path == parent / "created":
            swap_parent()
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    def racing_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "parent" and dir_fd is not None:
            swap_parent()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    monkeypatch.setattr(os, "open", racing_open)

    with pytest.raises(workspace_mcp._ToolError, match="symlink|changed"):
        workspace_mcp._dispatch_tool(
            workspace,
            "create_directory",
            {"path": "parent/created"},
        )

    assert swapped is True
    assert not (outside / "created").exists()


@pytest.mark.skipif(os.name == "nt", reason="native Windows is not supported")
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("list_directory", {"path": "target"}),
        ("directory_tree", {"path": "target"}),
        ("search_files", {"path": "target", "pattern": "secret"}),
    ],
)
def test_directory_reads_reject_a_base_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    root = tmp_path / "workspace"
    target = root / "target"
    target.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("OUTSIDE-SECRET", encoding="utf-8")
    workspace = workspace_mcp._Workspace(root)
    original_is_dir = Path.is_dir
    original_open = os.open
    swapped = False

    def swap_target() -> None:
        nonlocal swapped
        if swapped:
            return
        target.rmdir()
        target.symlink_to(outside, target_is_directory=True)
        swapped = True

    def racing_is_dir(path: Path) -> bool:
        if path == target:
            swap_target()
        return original_is_dir(path)

    def racing_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "target" and dir_fd is not None:
            swap_target()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(Path, "is_dir", racing_is_dir)
    monkeypatch.setattr(os, "open", racing_open)

    with pytest.raises(workspace_mcp._ToolError, match="symlink|changed"):
        workspace_mcp._dispatch_tool(workspace, tool_name, arguments)

    assert swapped is True


def test_read_multiple_files_enforces_one_aggregate_response_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "a.txt").write_text("12345678", encoding="utf-8")
    (tmp_path / "b.txt").write_text("abcdefgh", encoding="utf-8")
    workspace = workspace_mcp._Workspace(tmp_path)
    monkeypatch.setattr(workspace_mcp, "_MAX_MULTI_FILE_RESPONSE_BYTES", 20, raising=False)

    with pytest.raises(workspace_mcp._ToolError, match="combined read exceeds"):
        workspace_mcp._dispatch_tool(
            workspace,
            "read_multiple_files",
            {"paths": ["a.txt", "b.txt"]},
        )


def test_search_files_enforces_a_global_visited_entry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    workspace = workspace_mcp._Workspace(tmp_path)
    monkeypatch.setattr(workspace_mcp, "_MAX_DIRECTORY_ENTRIES", 2)

    with pytest.raises(workspace_mcp._ToolError, match="search exceeds the entry limit"):
        workspace_mcp._dispatch_tool(
            workspace,
            "search_files",
            {"path": ".", "pattern": "never-matches"},
        )


def test_edit_file_refuses_to_overwrite_a_concurrent_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("one", encoding="utf-8")
    workspace = workspace_mcp._Workspace(tmp_path)
    original_write_text = workspace_mcp._Workspace.write_text
    original_open = os.open
    target_open_count = 0

    def racing_write_text(
        self: workspace_mcp._Workspace,
        value: object,
        content: object,
    ) -> Path:
        target.write_text("concurrent", encoding="utf-8")
        return original_write_text(self, value, content)

    def racing_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal target_open_count
        if path == "target.txt" and dir_fd is not None:
            target_open_count += 1
            if target_open_count == 2:
                target.write_text("concurrent", encoding="utf-8")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(workspace_mcp._Workspace, "write_text", racing_write_text)
    monkeypatch.setattr(os, "open", racing_open)

    with pytest.raises(workspace_mcp._ToolError, match="changed while editing"):
        workspace_mcp._dispatch_tool(
            workspace,
            "edit_file",
            {
                "path": "target.txt",
                "edits": [{"oldText": "one", "newText": "two"}],
            },
        )

    assert target.read_text(encoding="utf-8") == "concurrent"


@pytest.mark.parametrize(
    ("tool_name", "arguments", "initial_mode", "expected_text"),
    [
        (
            "edit_file",
            {
                "path": "run.sh",
                "edits": [{"oldText": "echo one", "newText": "echo two"}],
            },
            0o755,
            "echo two\n",
        ),
        (
            "write_file",
            {"path": "run.sh", "content": "replacement\n"},
            0o644,
            "replacement\n",
        ),
    ],
)
def test_overwrites_preserve_regular_file_permissions(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, object],
    initial_mode: int,
    expected_text: str,
) -> None:
    target = tmp_path / "run.sh"
    target.write_text("echo one\n", encoding="utf-8")
    target.chmod(initial_mode)
    workspace = workspace_mcp._Workspace(tmp_path)

    workspace_mcp._dispatch_tool(workspace, tool_name, arguments)

    assert stat.S_IMODE(target.stat().st_mode) == initial_mode
    assert target.read_text(encoding="utf-8") == expected_text


def test_helper_material_drift_is_rejected_before_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_material = tmp_path / "workspace_mcp.py"
    helper_material = tmp_path / "_fs_utils.py"
    server_material.write_text("server-v1", encoding="utf-8")
    helper_material.write_text("helper-v1", encoding="utf-8")
    monkeypatch.setattr(
        workspace_mcp,
        "_workspace_mcp_material_files",
        lambda: (("server", server_material), ("helper", helper_material)),
        raising=False,
    )
    expected_digest = workspace_mcp.workspace_mcp_material_digest()
    helper_material.write_text("helper-v2", encoding="utf-8")
    served = False

    def record_serve(*_args: object) -> None:
        nonlocal served
        served = True

    monkeypatch.setattr(workspace_mcp, "_serve", record_serve)

    result = workspace_mcp.main(
        [
            "--material-digest",
            expected_digest,
            "--root",
            str(tmp_path),
        ]
    )

    assert result == 2
    assert served is False


def test_child_fails_before_protocol_start_when_material_changed(tmp_path: Path) -> None:
    completed = _run_server(tmp_path, [], material_digest="0" * 64)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "material digest mismatch" in completed.stderr
