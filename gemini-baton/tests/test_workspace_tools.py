from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from gemini_baton.errors import BridgeError
from gemini_baton.tools import WorkspaceTools


def make_tools(
    workspace: Path,
    *,
    permission_mode: str = "workspace-write",
    allow_shell: bool = False,
    read_max_bytes: int = 4096,
    max_output_chars: int = 4096,
) -> WorkspaceTools:
    return WorkspaceTools(
        workspace,
        permission_mode=permission_mode,
        allow_shell=allow_shell,
        read_max_bytes=read_max_bytes,
        max_output_chars=max_output_chars,
        command_timeout_seconds=10,
    )


def test_list_hides_sensitive_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('x')\n", encoding="utf-8")
    (tmp_path / "src" / "tokenizer.py").write_text("ordinary source\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret git config\n")
    (tmp_path / ".env").write_text("GEMINI_API_KEY=should-not-leak\n")
    (tmp_path / "credentials.json").write_text("not-readable\n")
    result = make_tools(tmp_path).execute(
        "list_files",
        {"path": ".", "recursive": True},
        deadline_monotonic=time.monotonic() + 5,
        artifact_dir=tmp_path / "artifacts",
    )
    paths = {item["path"] for item in result["entries"]}
    assert "src/main.py" in paths
    assert "src/tokenizer.py" in paths
    assert not any(path.startswith(".git") for path in paths)
    assert ".env" not in paths
    assert "credentials.json" not in paths


def test_read_write_edit_and_changed_paths(tmp_path: Path) -> None:
    tools = make_tools(tmp_path)
    write = tools.execute(
        "write_file",
        {"path": "src.txt", "content": "alpha beta alpha\n"},
        deadline_monotonic=time.monotonic() + 5,
        artifact_dir=tmp_path / "artifacts",
    )
    assert write["ok"] and write["created"]
    read = tools.execute(
        "read_file",
        {"path": "src.txt"},
        deadline_monotonic=time.monotonic() + 5,
        artifact_dir=tmp_path / "artifacts",
    )
    assert read["content"] == "alpha beta alpha\n"
    edit = tools.execute(
        "edit_file",
        {"path": "src.txt", "old_string": "alpha", "new_string": "ALPHA",
         "replace_all": True},
        deadline_monotonic=time.monotonic() + 5,
        artifact_dir=tmp_path / "artifacts",
    )
    assert edit["ok"] and edit["replacements"] == 2
    assert (tmp_path / "src.txt").read_text() == "ALPHA beta ALPHA\n"


def test_edit_requires_unique_match(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x x\n")
    tools = make_tools(tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        tools.execute(
            "edit_file",
            {"path": "a.txt", "old_string": "x", "new_string": "y"},
            deadline_monotonic=time.monotonic() + 5,
            artifact_dir=tmp_path / "artifacts",
        )
    assert excinfo.value.code == "edit_not_unique"


def test_traversal_absolute_and_symlink_escape_are_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret\n", encoding="utf-8")
    (workspace / "escape.txt").symlink_to(outside)
    (workspace / "escape-dir").symlink_to(tmp_path, target_is_directory=True)
    tools = make_tools(workspace)
    for path in ("../outside.txt", str(outside), "escape.txt", "escape-dir/outside.txt"):
        with pytest.raises(BridgeError) as excinfo:
            tools.execute(
                "read_file",
                {"path": path},
                deadline_monotonic=time.monotonic() + 5,
                artifact_dir=workspace / "artifacts",
            )
        assert excinfo.value.code in {
            "path_outside_workspace",
            "invalid_path",
            "sensitive_path_denied",
        }
    with pytest.raises(BridgeError):
        tools.execute(
            "write_file",
            {"path": "escape.txt", "content": "overwrite\n"},
            deadline_monotonic=time.monotonic() + 5,
            artifact_dir=workspace / "artifacts",
        )
    assert outside.read_text() == "outside secret\n"


def test_sensitive_and_binary_files_are_denied(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GEMINI_API_KEY=hidden\n")
    (tmp_path / "binary.bin").write_bytes(b"abc\x00def")
    tools = make_tools(tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        tools.execute(
            "read_file",
            {"path": ".env"},
            deadline_monotonic=time.monotonic() + 5,
            artifact_dir=tmp_path / "artifacts",
        )
    assert excinfo.value.code == "sensitive_path_denied"
    with pytest.raises(BridgeError) as excinfo:
        tools.execute(
            "read_file",
            {"path": "binary.bin"},
            deadline_monotonic=time.monotonic() + 5,
            artifact_dir=tmp_path / "artifacts",
        )
    assert excinfo.value.code == "binary_file"


def test_read_limit_truncates(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text("a" * 10_000)
    tools = make_tools(tmp_path, read_max_bytes=100, max_output_chars=100)
    result = tools.execute(
        "read_file",
        {"path": "long.txt"},
        deadline_monotonic=time.monotonic() + 5,
        artifact_dir=tmp_path / "artifacts",
    )
    assert result["truncated"] is True
    assert len(result["content"].encode("utf-8")) <= 150
    assert "read limit" in result["content"]


def test_read_only_denies_writes_and_shell(tmp_path: Path) -> None:
    tools = make_tools(tmp_path, permission_mode="read-only")
    assert [item["name"] for item in tools.declarations()] == [
        "list_files",
        "read_file",
    ]
    with pytest.raises(BridgeError) as excinfo:
        tools.execute(
            "write_file",
            {"path": "x.txt", "content": "x"},
            deadline_monotonic=time.monotonic() + 5,
            artifact_dir=tmp_path / "artifacts",
        )
    assert excinfo.value.code == "permission_denied"
    with pytest.raises(BridgeError):
        tools.execute(
            "run_command",
            {"argv": ["/bin/echo", "hi"]},
            deadline_monotonic=time.monotonic() + 5,
            artifact_dir=tmp_path / "artifacts",
        )
    with pytest.raises(BridgeError):
        make_tools(tmp_path, permission_mode="read-only", allow_shell=True)


def test_run_command_argv_only_and_credentials_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "test-command-key-must-not-leak"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    tools = make_tools(tmp_path, allow_shell=True)
    result = tools.execute(
        "run_command",
        {
            "argv": [
                sys.executable,
                "-c",
                "import os; print('semi;colon'); "
                "print(os.environ.get('GEMINI_API_KEY', 'MISSING'))",
            ]
        },
        deadline_monotonic=time.monotonic() + 10,
        artifact_dir=tmp_path / "artifacts" / "cmd",
    )
    assert result["ok"] is True
    assert "semi;colon" in result["stdout"]
    assert "MISSING" in result["stdout"]
    assert secret not in result["stdout"]
    assert Path(result["stdout_path"]).read_text() == result["stdout"]


def test_command_timeout_and_output_limit(tmp_path: Path) -> None:
    tools = make_tools(tmp_path, allow_shell=True, max_output_chars=1024)
    timed = tools.execute(
        "run_command",
        {"argv": [sys.executable, "-c", "import time; time.sleep(5)"]},
        deadline_monotonic=time.monotonic() + 0.35,
        artifact_dir=tmp_path / "artifacts" / "timeout",
    )
    assert timed["timed_out"] is True
    assert timed["ok"] is False
    limited = tools.execute(
        "run_command",
        {
            "argv": [
                sys.executable,
                "-c",
                "import sys\nwhile True: print('x'*256, flush=True)",
            ]
        },
        deadline_monotonic=time.monotonic() + 10,
        artifact_dir=tmp_path / "artifacts" / "limit",
    )
    assert limited["output_limited"] is True
    assert limited["ok"] is False


def test_checks_require_shell(tmp_path: Path) -> None:
    tools = make_tools(tmp_path, allow_shell=False)
    with pytest.raises(BridgeError) as excinfo:
        tools.run_check(
            ["/bin/echo", "hi"],
            deadline_monotonic=time.monotonic() + 5,
            artifact_dir=tmp_path / "checks",
        )
    assert excinfo.value.code == "permission_denied"
