"""Bundled, workspace-confined filesystem MCP for controlled CTX Fit trials.

The executable material is one self-contained file in the installed CTX
package. Callers bind its exact source digest into the child argv, and launch
that exact file with isolated Python before it accepts a request. The digest
detects ordinary package drift; it is not a signature against an attacker who
can alter code before the verifier starts.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import difflib
import fnmatch
import hashlib
import json
import os
import secrets
import shlex
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Sequence


_MODULE_NAME = "ctx.fit.workspace_mcp"
_DIGEST_FLAG = "--material-digest"
_ROOT_FLAG = "--root"
_SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2024-11-05")
_LATEST_PROTOCOL_VERSION = _SUPPORTED_PROTOCOL_VERSIONS[0]
_MAX_FILE_BYTES = 1_000_000
_MAX_MULTI_FILE_COUNT = 50
_MAX_MULTI_FILE_RESPONSE_BYTES = 1_000_000
_MAX_DIRECTORY_ENTRIES = 2_000
_MAX_SEARCH_RESULTS = 200
_MAX_TREE_DEPTH = 8
_MAX_FRAME_BYTES = 2_000_000
_MATERIAL_DIGEST_DOMAIN = b"ctx-fit-workspace-material-v1\0"


class _ToolError(ValueError):
    """A safe, user-actionable filesystem tool error."""


def _object_schema(
    properties: dict[str, dict[str, Any]], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


_PATH = {"type": "string", "description": "Workspace-relative or workspace-contained path."}
_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "read_text_file",
        "description": "Read one UTF-8 text file inside the workspace.",
        "inputSchema": _object_schema({"path": _PATH}, ("path",)),
    },
    {
        "name": "read_multiple_files",
        "description": "Read up to 50 UTF-8 text files inside the workspace.",
        "inputSchema": _object_schema(
            {"paths": {"type": "array", "items": _PATH, "maxItems": _MAX_MULTI_FILE_COUNT}},
            ("paths",),
        ),
    },
    {
        "name": "write_file",
        "description": "Atomically write one UTF-8 text file inside the workspace.",
        "inputSchema": _object_schema(
            {"path": _PATH, "content": {"type": "string", "maxLength": _MAX_FILE_BYTES}},
            ("path", "content"),
        ),
    },
    {
        "name": "edit_file",
        "description": "Apply unambiguous exact-text replacements to one workspace file.",
        "inputSchema": _object_schema(
            {
                "path": _PATH,
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": _object_schema(
                        {
                            "oldText": {"type": "string"},
                            "newText": {"type": "string"},
                        },
                        ("oldText", "newText"),
                    ),
                },
                "dryRun": {"type": "boolean", "default": False},
            },
            ("path", "edits"),
        ),
    },
    {
        "name": "create_directory",
        "description": "Create a directory and missing parents inside the workspace.",
        "inputSchema": _object_schema({"path": _PATH}, ("path",)),
    },
    {
        "name": "list_directory",
        "description": "List one directory inside the workspace.",
        "inputSchema": _object_schema({"path": _PATH}, ("path",)),
    },
    {
        "name": "directory_tree",
        "description": "Return a bounded JSON directory tree without following symlinks.",
        "inputSchema": _object_schema(
            {
                "path": _PATH,
                "maxDepth": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": _MAX_TREE_DEPTH,
                    "default": 4,
                },
            }
        ),
    },
    {
        "name": "search_files",
        "description": "Find workspace paths by glob without reading file contents.",
        "inputSchema": _object_schema(
            {
                "path": _PATH,
                "pattern": {"type": "string"},
                "excludePatterns": {"type": "array", "items": {"type": "string"}},
            },
            ("pattern",),
        ),
    },
    {
        "name": "get_file_info",
        "description": "Return bounded metadata for one workspace path.",
        "inputSchema": _object_schema({"path": _PATH}, ("path",)),
    },
    {
        "name": "list_allowed_directories",
        "description": "Return the single filesystem root available to this server.",
        "inputSchema": _object_schema({}),
    },
)


def _string(arguments: dict[str, Any], name: str, *, default: str | None = None) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _ToolError(f"{name} must be a non-empty string")
    return value


_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


@dataclass(slots=True)
class _PinnedText:
    parent_fd: int
    name: str
    relative: str
    payload: bytes
    signature: tuple[int, int, int, int, int]

    def close(self) -> None:
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1


@dataclass(slots=True)
class _Workspace:
    root: Path
    _root_fd: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not hasattr(os, "O_DIRECTORY")
            or not hasattr(os, "O_NOFOLLOW")
            or os.open not in os.supports_dir_fd
            or os.mkdir not in os.supports_dir_fd
            or os.rename not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.unlink not in os.supports_dir_fd
            or os.scandir not in os.supports_fd
        ):
            raise _ToolError("secure descriptor-rooted workspace access is unavailable")
        try:
            resolved = self.root.resolve(strict=True)
            expected = os.stat(resolved, follow_symlinks=False)
            descriptor = os.open(resolved, _DIRECTORY_OPEN_FLAGS)
        except OSError as exc:
            raise _ToolError("workspace root is unavailable or contains a symlink") from exc
        if not stat.S_ISDIR(expected.st_mode) or not os.path.samestat(
            expected, os.fstat(descriptor)
        ):
            os.close(descriptor)
            raise _ToolError("workspace root changed while opening")
        self.root = resolved
        self._root_fd = descriptor

    def close(self) -> None:
        descriptor = getattr(self, "_root_fd", -1)
        if descriptor >= 0:
            os.close(descriptor)
            self._root_fd = -1

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> _Workspace:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _parts(self, value: object) -> tuple[str, ...]:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise _ToolError("path must be a non-empty string")
        requested = Path(value)
        if requested.is_absolute():
            try:
                requested = requested.relative_to(self.root)
            except ValueError as exc:
                raise _ToolError("path is outside the workspace root") from exc
        parts = tuple(part for part in requested.parts if part not in ("", "."))
        if any(part == ".." for part in parts):
            raise _ToolError("path is outside the workspace root")
        return parts

    @staticmethod
    def _relative(parts: tuple[str, ...]) -> str:
        return "/".join(parts) or "."

    def relative_value(self, value: object) -> str:
        return self._relative(self._parts(value))

    def relative(self, path: Path) -> str:
        try:
            parts = path.relative_to(self.root).parts
        except ValueError as exc:
            raise _ToolError("path is outside the workspace root") from exc
        return self._relative(tuple(parts))

    def _fresh_root_fd(self) -> int:
        try:
            return os.open(".", _DIRECTORY_OPEN_FLAGS, dir_fd=self._root_fd)
        except OSError as exc:
            raise _ToolError("workspace root changed or became unavailable") from exc

    def _walk_directory(self, parts: tuple[str, ...], *, create: bool = False) -> int:
        descriptor = self._fresh_root_fd()
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise _ToolError("workspace path changed or contains a symlink") from exc
                try:
                    child = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
                except OSError as exc:
                    raise _ToolError("workspace path changed or contains a symlink") from exc
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _open_parent(
        self,
        value: object,
        *,
        create: bool = False,
    ) -> tuple[int, str, tuple[str, ...]]:
        parts = self._parts(value)
        if not parts:
            raise _ToolError("workspace root is not a regular file")
        return self._walk_directory(parts[:-1], create=create), parts[-1], parts

    @contextmanager
    def open_directory(self, value: object) -> Iterator[tuple[int, tuple[str, ...]]]:
        parts = self._parts(value)
        descriptor = self._walk_directory(parts)
        try:
            yield descriptor, parts
        finally:
            os.close(descriptor)

    @staticmethod
    def open_child_directory(parent_fd: int, name: str) -> int:
        try:
            return os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise _ToolError("workspace path changed or contains a symlink") from exc

    @staticmethod
    def _read_child(parent_fd: int, name: str) -> tuple[bytes, tuple[int, int, int, int, int]]:
        try:
            descriptor = os.open(name, _FILE_OPEN_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise _ToolError("workspace path changed or contains a symlink") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise _ToolError("workspace path is not a regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(_MAX_FILE_BYTES + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if _file_signature(before) != _file_signature(after):
            raise _ToolError("workspace file changed while reading")
        if len(payload) > _MAX_FILE_BYTES:
            raise _ToolError(f"file exceeds the {_MAX_FILE_BYTES}-byte read limit")
        return payload, _file_signature(after)

    @staticmethod
    def _decode_text(payload: bytes) -> str:
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _ToolError("workspace file is not UTF-8 text") from exc

    @contextmanager
    def pinned_text(self, value: object) -> Iterator[_PinnedText]:
        parent_fd, name, parts = self._open_parent(value)
        try:
            payload, signature = self._read_child(parent_fd, name)
            pinned = _PinnedText(
                parent_fd=parent_fd,
                name=name,
                relative=self._relative(parts),
                payload=payload,
                signature=signature,
            )
        except Exception:
            os.close(parent_fd)
            raise
        try:
            yield pinned
        finally:
            pinned.close()

    def read_text(self, value: object) -> str:
        with self.pinned_text(value) as pinned:
            return self._decode_text(pinned.payload)

    @staticmethod
    def _atomic_write_at(
        parent_fd: int,
        name: str,
        payload: bytes,
        *,
        expected_payload: bytes | None = None,
        expected_signature: tuple[int, int, int, int, int] | None = None,
    ) -> None:
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise _ToolError("workspace path changed or contains a symlink") from exc
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise _ToolError("workspace path is not a regular file")
        replacement_mode = 0o600 if existing is None else stat.S_IMODE(existing.st_mode) & 0o777

        temporary = f".ctx-workspace-{secrets.token_hex(12)}.tmp"
        descriptor = -1
        temporary_exists = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
            temporary_exists = True
            os.fchmod(descriptor, replacement_mode)
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1

            if expected_payload is not None and expected_signature is not None:
                current_payload, current_signature = _Workspace._read_child(parent_fd, name)
                if current_payload != expected_payload or current_signature != expected_signature:
                    raise _ToolError("workspace file changed while editing")

            os.rename(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_exists = False
        except _ToolError:
            raise
        except OSError as exc:
            raise _ToolError("workspace path changed or contains a symlink") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_exists:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except OSError:
                    pass

    def write_text(self, value: object, content: object) -> Path:
        if not isinstance(content, str):
            raise _ToolError("content must be a string")
        payload = content.encode("utf-8")
        if len(payload) > _MAX_FILE_BYTES:
            raise _ToolError(f"content exceeds the {_MAX_FILE_BYTES}-byte write limit")
        parent_fd, name, parts = self._open_parent(value, create=True)
        try:
            self._atomic_write_at(parent_fd, name, payload)
        finally:
            os.close(parent_fd)
        return self.root.joinpath(*parts)

    def replace_if_unchanged(self, pinned: _PinnedText, content: str) -> None:
        payload = content.encode("utf-8")
        if len(payload) > _MAX_FILE_BYTES:
            raise _ToolError(f"content exceeds the {_MAX_FILE_BYTES}-byte write limit")
        self._atomic_write_at(
            pinned.parent_fd,
            pinned.name,
            payload,
            expected_payload=pinned.payload,
            expected_signature=pinned.signature,
        )

    def create_directory(self, value: object) -> Path:
        parts = self._parts(value)
        descriptor = self._walk_directory(parts, create=True)
        os.close(descriptor)
        return self.root.joinpath(*parts)

    def file_info(self, value: object) -> tuple[str, os.stat_result]:
        parts = self._parts(value)
        if not parts:
            descriptor = self._fresh_root_fd()
            try:
                return ".", os.fstat(descriptor)
            finally:
                os.close(descriptor)
        parent_fd, name, parts = self._open_parent(value)
        try:
            try:
                descriptor = os.open(name, _FILE_OPEN_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise _ToolError("workspace path changed or contains a symlink") from exc
            try:
                metadata = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)
        return self._relative(parts), metadata

    @staticmethod
    def bounded_entries(
        directory_fd: int,
        seen: list[int],
        *,
        error_message: str,
    ) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        try:
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    seen[0] += 1
                    if seen[0] > _MAX_DIRECTORY_ENTRIES:
                        raise _ToolError(error_message)
                    if entry.is_symlink():
                        kind = "symlink"
                    elif entry.is_dir(follow_symlinks=False):
                        kind = "directory"
                    elif entry.is_file(follow_symlinks=False):
                        kind = "file"
                    else:
                        kind = "other"
                    entries.append((entry.name, kind))
        except _ToolError:
            raise
        except OSError as exc:
            raise _ToolError("workspace changed during directory scan") from exc
        entries.sort(key=lambda item: item[0])
        return entries


def _tree(workspace: _Workspace, base: object, *, max_depth: int) -> list[dict[str, Any]]:
    seen = [0]

    def visit(directory_fd: int, depth: int) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        entries = workspace.bounded_entries(
            directory_fd,
            seen,
            error_message="directory tree exceeds the entry limit",
        )
        for name, kind in entries:
            item: dict[str, Any] = {"name": name, "type": kind}
            if kind == "directory" and depth < max_depth:
                child_fd = workspace.open_child_directory(directory_fd, name)
                try:
                    item["children"] = visit(child_fd, depth + 1)
                finally:
                    os.close(child_fd)
            output.append(item)
        return output

    with workspace.open_directory(base) as (directory_fd, _parts):
        return visit(directory_fd, 0)


def _search_files(
    workspace: _Workspace,
    base: object,
    *,
    pattern: str,
    excluded: list[str],
) -> list[str]:
    seen = [0]
    matches: list[str] = []

    def visit(directory_fd: int, prefix: tuple[str, ...]) -> bool:
        entries = workspace.bounded_entries(
            directory_fd,
            seen,
            error_message="search exceeds the entry limit",
        )
        for name, kind in entries:
            relative = "/".join((*prefix, name))
            if any(fnmatch.fnmatch(relative, item) for item in excluded):
                continue
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern):
                matches.append(relative)
                if len(matches) >= _MAX_SEARCH_RESULTS:
                    return True
            if kind == "directory":
                child_fd = workspace.open_child_directory(directory_fd, name)
                try:
                    if visit(child_fd, (*prefix, name)):
                        return True
                finally:
                    os.close(child_fd)
        return False

    with workspace.open_directory(base) as (directory_fd, parts):
        visit(directory_fd, parts)
    return matches


def _dispatch_tool(workspace: _Workspace, name: str, arguments: dict[str, Any]) -> str:
    if name == "read_text_file":
        return workspace.read_text(arguments.get("path"))
    if name == "read_multiple_files":
        values = arguments.get("paths")
        if not isinstance(values, list) or not 1 <= len(values) <= _MAX_MULTI_FILE_COUNT:
            raise _ToolError(f"paths must contain 1 through {_MAX_MULTI_FILE_COUNT} items")
        sections: list[str] = []
        response_bytes = 0
        for value in values:
            section = f"== {workspace.relative_value(value)} ==\n{workspace.read_text(value)}"
            encoded_bytes = len(section.encode("utf-8")) + (2 if sections else 0)
            if response_bytes + encoded_bytes > _MAX_MULTI_FILE_RESPONSE_BYTES:
                raise _ToolError(
                    "combined read exceeds the "
                    f"{_MAX_MULTI_FILE_RESPONSE_BYTES}-byte response limit"
                )
            sections.append(section)
            response_bytes += encoded_bytes
        return "\n\n".join(sections)
    if name == "write_file":
        path = workspace.write_text(arguments.get("path"), arguments.get("content"))
        return f"wrote {workspace.relative(path)}"
    if name == "edit_file":
        value = arguments.get("path")
        with workspace.pinned_text(value) as pinned:
            original = workspace._decode_text(pinned.payload)
            edits = arguments.get("edits")
            if not isinstance(edits, list) or not 1 <= len(edits) <= 100:
                raise _ToolError("edits must contain 1 through 100 replacements")
            updated = original
            for edit in edits:
                if not isinstance(edit, dict):
                    raise _ToolError("each edit must be an object")
                old = edit.get("oldText")
                new = edit.get("newText")
                if not isinstance(old, str) or not old or not isinstance(new, str):
                    raise _ToolError("each edit requires non-empty oldText and string newText")
                occurrences = updated.count(old)
                if occurrences != 1:
                    raise _ToolError(f"oldText must match exactly once; found {occurrences}")
                updated = updated.replace(old, new, 1)
            if len(updated.encode("utf-8")) > _MAX_FILE_BYTES:
                raise _ToolError(f"edited file exceeds the {_MAX_FILE_BYTES}-byte write limit")
            diff = "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    updated.splitlines(keepends=True),
                    fromfile=pinned.relative,
                    tofile=pinned.relative,
                )
            )
            dry_run = arguments.get("dryRun", False)
            if not isinstance(dry_run, bool):
                raise _ToolError("dryRun must be a boolean")
            if not dry_run:
                workspace.replace_if_unchanged(pinned, updated)
        return diff or "no changes"
    if name == "create_directory":
        path = workspace.create_directory(arguments.get("path"))
        return f"created {workspace.relative(path)}"
    if name == "list_directory":
        with workspace.open_directory(arguments.get("path")) as (directory_fd, _parts):
            entries = workspace.bounded_entries(
                directory_fd,
                [0],
                error_message="directory exceeds the entry limit",
            )
        labels = {
            "symlink": "SYMLINK",
            "directory": "DIR",
            "file": "FILE",
            "other": "OTHER",
        }
        rendered = [f"[{labels[kind]}] {entry_name}" for entry_name, kind in entries]
        return "\n".join(rendered)
    if name == "directory_tree":
        depth = arguments.get("maxDepth", 4)
        if (
            isinstance(depth, bool)
            or not isinstance(depth, int)
            or not 0 <= depth <= _MAX_TREE_DEPTH
        ):
            raise _ToolError(f"maxDepth must be an integer from 0 through {_MAX_TREE_DEPTH}")
        return json.dumps(
            _tree(workspace, arguments.get("path", "."), max_depth=depth),
            sort_keys=True,
        )
    if name == "search_files":
        pattern = _string(arguments, "pattern")
        if not any(character in pattern for character in "*?["):
            pattern = f"*{pattern}*"
        excluded = arguments.get("excludePatterns", [])
        if not isinstance(excluded, list) or not all(isinstance(item, str) for item in excluded):
            raise _ToolError("excludePatterns must be an array of strings")
        return "\n".join(
            _search_files(
                workspace,
                arguments.get("path", "."),
                pattern=pattern,
                excluded=excluded,
            )
        )
    if name == "get_file_info":
        relative, metadata = workspace.file_info(arguments.get("path"))
        return json.dumps(
            {
                "path": relative,
                "type": (
                    "directory"
                    if stat.S_ISDIR(metadata.st_mode)
                    else "file"
                    if stat.S_ISREG(metadata.st_mode)
                    else "other"
                ),
                "size": metadata.st_size,
                "mode": oct(metadata.st_mode & 0o777),
                "modified_ns": metadata.st_mtime_ns,
            },
            sort_keys=True,
        )
    if name == "list_allowed_directories":
        return str(workspace.root)
    raise _ToolError(f"unknown workspace tool: {name}")


def _result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _response(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _negotiate_protocol_version(params: dict[str, Any]) -> str:
    """Echo a supported client version, otherwise use our newest contract."""

    requested = params.get("protocolVersion")
    if requested in _SUPPORTED_PROTOCOL_VERSIONS:
        return str(requested)
    return _LATEST_PROTOCOL_VERSION


def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _write(stream: BinaryIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n")
    stream.flush()


def _serve(workspace: _Workspace, input_stream: BinaryIO, output_stream: BinaryIO) -> None:
    for raw_line in input_stream:
        if len(raw_line) > _MAX_FRAME_BYTES:
            _write(output_stream, _error(None, -32600, "request exceeds the frame limit"))
            continue
        try:
            frame = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            _write(output_stream, _error(None, -32700, "parse error"))
            continue
        if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
            _write(output_stream, _error(None, -32600, "invalid request"))
            continue
        if "id" not in frame:
            continue
        request_id = frame.get("id")
        method = frame.get("method")
        params = frame.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            _write(output_stream, _error(request_id, -32600, "invalid request"))
            continue
        if method == "initialize":
            _write(
                output_stream,
                _response(
                    request_id,
                    {
                        "protocolVersion": _negotiate_protocol_version(params),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "ctx-fit-workspace", "version": "1"},
                    },
                ),
            )
            continue
        if method == "ping":
            _write(output_stream, _response(request_id, {}))
            continue
        if method == "tools/list":
            _write(output_stream, _response(request_id, {"tools": list(_TOOLS)}))
            continue
        if method != "tools/call":
            _write(output_stream, _error(request_id, -32601, f"method not found: {method}"))
            continue
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            _write(output_stream, _error(request_id, -32602, "invalid tool call"))
            continue
        try:
            text = _dispatch_tool(workspace, name, arguments)
            result = _result(text)
        except (_ToolError, OSError) as exc:
            result = _result(str(exc), is_error=True)
        except Exception as exc:  # noqa: BLE001 - keep the MCP process alive, without leaking details.
            print(f"workspace MCP internal error: {type(exc).__name__}", file=sys.stderr)
            result = _result("internal workspace filesystem error", is_error=True)
        _write(output_stream, _response(request_id, result))


def _workspace_mcp_material_files() -> tuple[tuple[str, Path], ...]:
    """Return the versioned, deterministic executable-material manifest."""

    return ((_MODULE_NAME, Path(__file__).resolve(strict=True)),)


def workspace_mcp_material_digest() -> str:
    """Bind every CTX-owned file executed by the isolated child."""

    digest = hashlib.sha256(_MATERIAL_DIGEST_DOMAIN)
    for label, path in _workspace_mcp_material_files():
        encoded_label = label.encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(encoded_label).to_bytes(4, "big"))
        digest.update(encoded_label)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def workspace_mcp_command(root: str = ".") -> tuple[str, ...]:
    """Return an isolated argv bound to this exact installed server file."""

    return (
        sys.executable,
        "-I",
        str(Path(__file__).resolve(strict=True)),
        _DIGEST_FLAG,
        workspace_mcp_material_digest(),
        _ROOT_FLAG,
        root,
    )


def workspace_mcp_spec(root: str = ".", *, server_name: str = "filesystem") -> str:
    """Render one explicit ``ctx run --mcp`` specification."""

    return f"{server_name}:{shlex.join(workspace_mcp_command(root))}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run CTX's bundled workspace filesystem MCP.")
    parser.add_argument(_DIGEST_FLAG, required=True)
    parser.add_argument(_ROOT_FLAG, default=".")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.material_digest != workspace_mcp_material_digest():
        print("workspace MCP material digest mismatch", file=sys.stderr)
        return 2
    try:
        workspace = _Workspace(Path(args.root))
    except (OSError, _ToolError) as exc:
        print(f"workspace MCP root is unavailable: {exc}", file=sys.stderr)
        return 2
    try:
        _serve(workspace, sys.stdin.buffer, sys.stdout.buffer)
    finally:
        workspace.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
