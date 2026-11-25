from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import (
    MAX_ARG_CHARS,
    MAX_CHECK_ARGV,
    MAX_WRITE_BYTES,
)
from .errors import BridgeError
from .paths import ensure_private_dir
from .processes import terminate_process_tree
from .redact import redact_text
from .util import truncate_text
from .workspace import (
    atomic_workspace_write,
    is_sensitive_relative,
    read_text_file,
    resolve_workspace_path,
)

_ALLOWED_PERMISSIONS = {"read-only", "workspace-write"}


def _json_schema(
    properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "list_files": {
        "description": (
            "List entries in one explicitly selected directory inside the "
            "workspace. Sensitive (.git, .env, credential/key-like) entries are "
            "omitted. Recursive walking is off by default."
        ),
        "parameters": _json_schema(
            {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative directory; default '.'.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Whether to descend (bounded). Default false.",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "1..1000; default 200.",
                    "minimum": 1,
                    "maximum": 1000,
                },
            },
            [],
        ),
    },
    "read_file": {
        "description": (
            "Read one explicitly selected UTF-8 text file inside the "
            "workspace, subject to a size limit. Sensitive paths are denied."
        ),
        "parameters": _json_schema(
            {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file path.",
                }
            },
            ["path"],
        ),
    },
    "write_file": {
        "description": (
            "Create or replace one UTF-8 text file inside the workspace. Only "
            "available in workspace-write mode. Parent directories must exist."
        ),
        "parameters": _json_schema(
            {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file path.",
                },
                "content": {
                    "type": "string",
                    "description": "Complete new file content.",
                },
            },
            ["path", "content"],
        ),
    },
    "edit_file": {
        "description": (
            "Replace an exact string in one UTF-8 text file inside the "
            "workspace. Only available in workspace-write mode."
        ),
        "parameters": _json_schema(
            {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file path.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences. Default false.",
                },
            },
            ["path", "old_string", "new_string"],
        ),
    },
    "run_command": {
        "description": (
            "Run an argv array without a shell (no shell expansion), with "
            "timeout and output limits. Only available when the session has "
            "shell enabled. There is no OS sandbox: this command can affect "
            "files outside the workspace and read anything the user can read."
        ),
        "parameters": _json_schema(
            {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_CHECK_ARGV,
                    "description": "Executable and arguments; no shell is used.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional per-command timeout.",
                },
            },
            ["argv"],
        ),
    },
}


def _validate_argv(argv: Any, *, field: str = "argv") -> list[str]:
    if not isinstance(argv, list) or not argv:
        raise BridgeError("invalid_arguments", f"{field} must be a non-empty list.")
    if len(argv) > MAX_CHECK_ARGV:
        raise BridgeError(
            "invalid_arguments", f"{field} has too many elements."
        )
    result: list[str] = []
    for item in argv:
        if not isinstance(item, str):
            raise BridgeError(
                "invalid_arguments", f"{field} elements must be strings."
            )
        if "\x00" in item:
            raise BridgeError(
                "invalid_arguments", f"{field} elements cannot contain NUL."
            )
        if len(item) > MAX_ARG_CHARS:
            raise BridgeError(
                "invalid_arguments", f"{field} element is too long."
            )
        result.append(item)
    if not result[0]:
        raise BridgeError(
            "invalid_arguments", f"{field}[0] (executable) cannot be empty."
        )
    return result


def _safe_command_env() -> dict[str, str]:
    blocked = {
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_BATON_WORKER_TOKEN",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key not in blocked and not key.startswith("GEMINI_BATON_")
    }


def _write_output_file(path: Path) -> tuple[int, Any]:
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(fd, 0o600)
    return fd, os.fdopen(fd, "wb")


def _read_capped(path: Path, limit: int) -> tuple[str, int, bool]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            raw = handle.read(limit)
    except OSError:
        return "", 0, False
    return (
        raw.decode("utf-8", errors="replace"),
        size,
        size > limit,
    )


def _preview_limit(max_output_chars: int) -> int:
    return max(512, min(8_000, max_output_chars // 2))


class WorkspaceTools:
    """Built-in workspace file tools plus explicitly enabled argv execution."""

    def __init__(
        self,
        cwd: Path,
        *,
        permission_mode: str,
        allow_shell: bool,
        read_max_bytes: int,
        max_output_chars: int,
        command_timeout_seconds: int,
    ) -> None:
        if permission_mode not in _ALLOWED_PERMISSIONS:
            raise BridgeError(
                "invalid_permission_mode",
                "permission_mode must be read-only or workspace-write.",
            )
        if allow_shell and permission_mode != "workspace-write":
            raise BridgeError(
                "invalid_permission_mode",
                "Enabling shell requires workspace-write mode.",
            )
        self.cwd = cwd.resolve()
        self.permission_mode = permission_mode
        self.allow_shell = allow_shell
        self.read_max_bytes = read_max_bytes
        self.max_output_chars = max_output_chars
        self.command_timeout_seconds = command_timeout_seconds

    @property
    def writable(self) -> bool:
        return self.permission_mode == "workspace-write"

    def declarations(self) -> list[dict[str, Any]]:
        names = ["list_files", "read_file"]
        if self.writable:
            names.extend(["write_file", "edit_file"])
        if self.writable and self.allow_shell:
            names.append("run_command")
        return [
            {
                "name": name,
                "description": _TOOL_SPECS[name]["description"],
                "parameters_json_schema": _TOOL_SPECS[name]["parameters"],
            }
            for name in names
        ]

    def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        deadline_monotonic: float,
        artifact_dir: Path,
    ) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise BridgeError("invalid_arguments", "Tool arguments must be an object.")
        if name == "list_files":
            return self._list_files(arguments)
        if name == "read_file":
            return self._read_file(arguments)
        if name == "write_file":
            self._require_write()
            return self._write_file(arguments)
        if name == "edit_file":
            self._require_write()
            return self._edit_file(arguments)
        if name == "run_command":
            self._require_shell()
            return self._run_command_tool(
                arguments, deadline_monotonic, artifact_dir
            )
        raise BridgeError("unknown_tool", f"Tool {name!r} is not available.")

    def run_check(
        self,
        argv: list[str],
        *,
        deadline_monotonic: float,
        artifact_dir: Path,
    ) -> dict[str, Any]:
        self._require_shell()
        return self.run_argv(
            argv,
            deadline_monotonic=deadline_monotonic,
            artifact_dir=artifact_dir,
            label="check",
        )

    def _require_write(self) -> None:
        if not self.writable:
            raise BridgeError(
                "permission_denied",
                "This run is read-only; file writes are not permitted.",
            )

    def _require_shell(self) -> None:
        if not self.writable or not self.allow_shell:
            raise BridgeError(
                "permission_denied",
                "Shell execution is not enabled for this run.",
            )

    def _list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_path = args.get("path", ".")
        if not isinstance(raw_path, str):
            raise BridgeError("invalid_arguments", "path must be a string.")
        recursive = args.get("recursive", False)
        if not isinstance(recursive, bool):
            raise BridgeError("invalid_arguments", "recursive must be boolean.")
        max_entries = args.get("max_entries", 200)
        if not isinstance(max_entries, int) or isinstance(max_entries, bool):
            raise BridgeError("invalid_arguments", "max_entries must be an integer.")
        max_entries = max(1, min(1000, max_entries))
        if raw_path in {"", "."}:
            directory = self.cwd
            rel = Path(".")
        else:
            directory, rel = resolve_workspace_path(
                self.cwd, raw_path, must_exist=True, allow_directory=True
            )
            if not directory.is_dir():
                raise BridgeError("invalid_path", "Path is not a directory.")
            if is_sensitive_relative(rel):
                raise BridgeError("sensitive_path_denied", "Path is excluded.")
        entries: list[dict[str, Any]] = []
        truncated = False

        def visit(current: Path, depth: int) -> None:
            nonlocal truncated
            if truncated:
                return
            try:
                children = sorted(
                    current.iterdir(), key=lambda item: item.name.lower()
                )
            except OSError:
                return
            for child in children:
                if len(entries) >= max_entries:
                    truncated = True
                    return
                try:
                    child_rel = child.resolve().relative_to(self.cwd)
                except (OSError, ValueError):
                    continue
                if is_sensitive_relative(child_rel):
                    continue
                try:
                    is_symlink = child.is_symlink()
                    is_dir = child.is_dir()
                except OSError:
                    continue
                kind = "symlink" if is_symlink else ("dir" if is_dir else "file")
                entry: dict[str, Any] = {
                    "path": child_rel.as_posix(),
                    "kind": kind,
                }
                if kind == "file":
                    try:
                        entry["size"] = child.stat().st_size
                    except OSError:
                        pass
                entries.append(entry)
                if recursive and depth > 0 and is_dir and not is_symlink:
                    visit(child, depth - 1)

        visit(directory, 3 if recursive else 0)
        return {
            "ok": True,
            "path": rel.as_posix(),
            "entries": entries,
            "truncated": truncated,
            "note": (
                "Recursive listing is depth-bounded to 3."
                if recursive
                else "Non-recursive; pass recursive=true for a bounded walk."
            ),
        }

    def _read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_path = args.get("path")
        if not isinstance(raw_path, str):
            raise BridgeError("invalid_arguments", "path must be a string.")
        path, rel = resolve_workspace_path(self.cwd, raw_path)
        limit = min(self.read_max_bytes, self.max_output_chars)
        text, size, truncated = read_text_file(path, limit)
        return {
            "ok": True,
            "path": rel.as_posix(),
            "size_bytes": size,
            "truncated": truncated,
            "content": text,
        }

    def _write_file(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_path = args.get("path")
        content = args.get("content")
        if not isinstance(raw_path, str):
            raise BridgeError("invalid_arguments", "path must be a string.")
        if not isinstance(content, str):
            raise BridgeError("invalid_arguments", "content must be a string.")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            raise BridgeError(
                "output_limit",
                f"write_file content exceeds {MAX_WRITE_BYTES} bytes.",
            )
        path, rel = resolve_workspace_path(self.cwd, raw_path, for_write=True)
        existed = path.exists()
        atomic_workspace_write(path, content)
        return {
            "ok": True,
            "path": rel.as_posix(),
            "bytes_written": len(encoded),
            "created": not existed,
        }

    def _edit_file(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_path = args.get("path")
        old = args.get("old_string")
        new = args.get("new_string")
        replace_all = args.get("replace_all", False)
        if not isinstance(raw_path, str):
            raise BridgeError("invalid_arguments", "path must be a string.")
        if not isinstance(old, str) or not isinstance(new, str):
            raise BridgeError(
                "invalid_arguments", "old_string and new_string must be strings."
            )
        if old == "":
            raise BridgeError("invalid_arguments", "old_string cannot be empty.")
        if not isinstance(replace_all, bool):
            raise BridgeError("invalid_arguments", "replace_all must be boolean.")
        path, rel = resolve_workspace_path(self.cwd, raw_path)
        text, _size, _truncated = read_text_file(path, 16 * 1024 * 1024)
        count = text.count(old)
        if count == 0:
            raise BridgeError("edit_not_found", "old_string was not found.")
        if count > 1 and not replace_all:
            raise BridgeError(
                "edit_not_unique",
                "old_string occurs multiple times; provide more context or "
                "set replace_all=true.",
            )
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
            raise BridgeError(
                "output_limit",
                f"Edited file would exceed {MAX_WRITE_BYTES} bytes.",
            )
        atomic_workspace_write(path, updated)
        return {
            "ok": True,
            "path": rel.as_posix(),
            "replacements": count if replace_all else 1,
        }

    def _run_command_tool(
        self,
        args: dict[str, Any],
        deadline_monotonic: float,
        artifact_dir: Path,
    ) -> dict[str, Any]:
        argv = _validate_argv(args.get("argv"))
        requested = args.get("timeout_seconds")
        if requested is not None:
            if (
                not isinstance(requested, int)
                or isinstance(requested, bool)
                or requested < 1
            ):
                raise BridgeError(
                    "invalid_arguments", "timeout_seconds must be a positive integer."
                )
            timeout = min(float(requested), float(self.command_timeout_seconds))
        else:
            timeout = float(self.command_timeout_seconds)
        return self.run_argv(
            argv,
            deadline_monotonic=deadline_monotonic,
            artifact_dir=artifact_dir,
            label="command",
            command_timeout=timeout,
        )

    def run_argv(
        self,
        argv: Any,
        *,
        deadline_monotonic: float,
        artifact_dir: Path,
        label: str,
        command_timeout: float | None = None,
    ) -> dict[str, Any]:
        self._require_shell()
        validated = _validate_argv(argv)
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise BridgeError(
                "run_timeout", "The run deadline expired before execution."
            )
        timeout = min(
            command_timeout or float(self.command_timeout_seconds),
            float(self.command_timeout_seconds),
            max(0.1, remaining),
        )
        ensure_private_dir(artifact_dir)
        stdout_path = artifact_dir / f"{label}.stdout.txt"
        stderr_path = artifact_dir / f"{label}.stderr.txt"
        capture_cap = self.max_output_chars
        start = time.monotonic()
        exit_code: int | None = None
        timed_out = False
        output_limited = False
        launch_error: str | None = None
        out_fd: int | None = None
        err_fd: int | None = None
        out_handle = None
        err_handle = None
        process: subprocess.Popen[bytes] | None = None
        try:
            out_fd, out_handle = _write_output_file(stdout_path)
            err_fd, err_handle = _write_output_file(stderr_path)
            try:
                process = subprocess.Popen(
                    validated,
                    cwd=self.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=out_handle,
                    stderr=err_handle,
                    env=_safe_command_env(),
                    close_fds=True,
                )
            except (OSError, ValueError) as exc:
                launch_error = redact_text(str(exc))
            if process is not None:
                while True:
                    exit_code = process.poll()
                    if exit_code is not None:
                        break
                    elapsed = time.monotonic() - start
                    if elapsed >= timeout:
                        timed_out = True
                        terminate_process_tree(process.pid)
                        break
                    try:
                        if (
                            stdout_path.stat().st_size > capture_cap
                            or stderr_path.stat().st_size > capture_cap
                        ):
                            output_limited = True
                            terminate_process_tree(process.pid)
                            break
                    except OSError:
                        pass
                    time.sleep(0.03)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                if process.returncode is not None:
                    exit_code = process.returncode
        finally:
            for handle in (out_handle, err_handle):
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass
            if out_fd is not None:
                try:
                    os.close(out_fd)
                except OSError:
                    pass
            if err_fd is not None:
                try:
                    os.close(err_fd)
                except OSError:
                    pass
        stdout, stdout_size, stdout_cut = _read_capped(stdout_path, capture_cap)
        stderr, stderr_size, stderr_cut = _read_capped(stderr_path, capture_cap)
        stdout_preview, stdout_preview_cut = truncate_text(
            stdout, _preview_limit(self.max_output_chars)
        )
        stderr_preview, stderr_preview_cut = truncate_text(
            stderr, _preview_limit(self.max_output_chars)
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        if launch_error is None and exit_code is None:
            launch_error = "Command terminated without an exit status."
        ok = (
            launch_error is None
            and not timed_out
            and not output_limited
            and exit_code == 0
        )
        result: dict[str, Any] = {
            "ok": ok,
            "argv": validated,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "output_limited": output_limited,
            "duration_ms": duration_ms,
            "stdout": stdout_preview,
            "stderr": stderr_preview,
            "stdout_truncated": stdout_cut or stdout_preview_cut,
            "stderr_truncated": stderr_cut or stderr_preview_cut,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stdout_bytes_captured": stdout_size,
            "stderr_bytes_captured": stderr_size,
            "capture_limit_bytes": capture_cap,
            "cwd": str(self.cwd),
        }
        if launch_error is not None:
            result["error"] = f"Could not execute command: {launch_error}"
        if timed_out:
            result["error"] = f"Command timed out after {timeout:.1f}s."
        if output_limited:
            result["error"] = (
                f"Command was stopped after output exceeded {capture_cap} bytes."
            )
        return result
