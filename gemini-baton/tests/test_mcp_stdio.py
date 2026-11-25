from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "gemini_write",
    "gemini_write_result",
    "gemini_settings",
    "gemini_status",
    "gemini_list_models",
    "gemini_send",
    "gemini_wait",
    "gemini_result",
    "gemini_cancel",
    "gemini_list_sessions",
}


def _server_env(state_dir: Path) -> dict[str, str]:
    blocked = {
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_BATON_ENV_FILE",
        "GEMINI_BATON_STATE_DIR",
        "GEMINI_BATON_MODEL",
        "GEMINI_BATON_FAKE_SCENARIO",
    }
    env = {key: value for key, value in os.environ.items() if key not in blocked}
    env["GEMINI_BATON_STATE_DIR"] = str(state_dir)
    return env


@pytest.mark.mcp_smoke
def test_official_mcp_client_lists_tools_and_calls_status(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gemini_baton.cli", "mcp"],
        env=_server_env(state_dir),
        cwd=str(Path.cwd()),
    )

    async def run() -> None:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert names == EXPECTED_TOOLS
                send_tool = next(
                    tool for tool in listed.tools if tool.name == "gemini_send"
                )
                assert "cwd" in send_tool.inputSchema["properties"]
                assert "checks" in send_tool.inputSchema["properties"]
                write_tool = next(tool for tool in listed.tools if tool.name == "gemini_write")
                assert {"prompt", "cwd", "file_paths", "request_id", "conversation_id", "route"} <= write_tool.inputSchema["properties"].keys()
                route_schema = write_tool.inputSchema["properties"]["route"]
                assert "cli:4 Argon" in json.dumps(route_schema)
                assert "cli:gemini-4-argon-high" in json.dumps(route_schema)
                assert "aistudio:4 Argon" in json.dumps(route_schema)
                assert "gemini-4-hydrogen" in json.dumps(route_schema)

                result = await session.call_tool("gemini_status", {})
                assert result.isError is False
                structured = result.structuredContent
                assert structured is not None
                assert structured["ok"] is True
                assert structured["writing"]["routes"] == [
                    "cli:4 Argon", "cli:4 Carbon", "cli:4 Neon", "cli:3.8 Flash",
                    "api:4 Argon", "api:4 Carbon", "api:4 Neon", "api:3.8 Flash",
                    "aistudio:4 Argon", "aistudio:4 Carbon", "aistudio:4 Neon", "aistudio:3.8 Flash",
                    "web:4 Argon", "web:4 Carbon", "web:4 Neon", "web:3.8 Flash",
                ]
                assert structured["writing"]["opt_in_routes"] == [
                    "cli:4 Hydrogen", "api:4 Hydrogen",
                    "aistudio:4 Hydrogen", "web:4 Hydrogen",
                ]
                assert structured["writing"]["cli"]["model_ids"] == [
                    "gemini-3.8-flash-high", "gemini-4-argon-high",
                    "gemini-4-carbon-high", "gemini-4-hydrogen-high",
                    "gemini-4-neon-xhigh",
                ]
                assert structured["writing"]["api_thinking_levels"] == ["high"]
                assert structured["network_calls"] == 0
                assert structured["paid_model_calls"] == 0
                assert "credentials" in structured
                # Status must not contain any credential value.
                text = json.dumps(structured)
                assert "AIza" not in text

                invalid = await session.call_tool("gemini_write", {
                    "prompt": "write", "cwd": str(tmp_path), "file_paths": [str(tmp_path)],
                })
                assert invalid.structuredContent["ok"] is False
                assert invalid.structuredContent["error"]["code"] == "invalid_files"

    asyncio.run(asyncio.wait_for(run(), timeout=25))


@pytest.mark.mcp_smoke
def test_mcp_server_client_exit_does_not_kill_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys as _sys

    from gemini_baton.config import Settings
    from gemini_baton.service import RunService
    from tests.conftest import FAKE_KEY, wait_done

    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = _server_env(state_dir)
    env["GEMINI_API_KEY"] = FAKE_KEY
    env["GEMINI_BATON_FAKE_SCENARIO"] = "delayed_final"
    helper = Path(__file__).parent / "support" / "fake_mcp_server.py"
    params = StdioServerParameters(
        command=_sys.executable,
        args=[str(helper.resolve())],
        env=env,
        cwd=str(Path.cwd()),
    )

    async def start_run() -> str:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "gemini_send",
                    {
                        "message": "survive the MCP server exiting",
                        "cwd": str(workspace),
                    },
                )
                assert result.isError is False
                structured = result.structuredContent
                assert structured and structured["run_id"]
                return structured["run_id"]

    run_id = asyncio.run(asyncio.wait_for(start_run(), timeout=20))

    monkeypatch.setenv("GEMINI_BATON_STATE_DIR", str(state_dir))
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    service = RunService(
        Settings.from_env(),
        worker_command=[_sys.executable, str(Path(__file__).parent / "support" / "fake_worker.py")],
    )
    try:
        wait = wait_done(service, run_id, timeout=20)
        assert wait["status"] == "succeeded"
        assert service.result(run_id)["final_text"] == "survived mcp exit"
    finally:
        for run in service.store().active_runs():
            try:
                service.cancel(run["id"])
            except Exception:
                pass

    # Restart a normal production MCP server (with no credential env) and query
    # the persisted result. Querying does not need or make a model call.
    restarted_env = _server_env(state_dir)
    restarted_params = StdioServerParameters(
        command=_sys.executable,
        args=["-m", "gemini_baton.cli", "mcp"],
        env=restarted_env,
        cwd=str(Path.cwd()),
    )

    async def read_after_restart() -> dict[str, Any]:
        async with stdio_client(restarted_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "gemini_result", {"run_id": run_id}
                )
                assert result.isError is False
                assert result.structuredContent is not None
                return result.structuredContent

    restarted = asyncio.run(asyncio.wait_for(read_after_restart(), timeout=20))
    assert restarted["status"] == "succeeded"
    assert restarted["final_text"] == "survived mcp exit"
    assert restarted["artifacts"]["result"]


def test_mcp_send_missing_credential_returns_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gemini_baton import mcp_server, service as service_module
    from gemini_baton.config import Settings
    from gemini_baton.service import RunService

    monkeypatch.setenv("GEMINI_BATON_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        service_module,
        "credential_status",
        lambda: {"present": False, "source": None, "env_file": {"configured": False}},
    )
    fake_service = RunService(Settings.from_env())
    monkeypatch.setattr(mcp_server, "_service", fake_service)
    result = mcp_server.gemini_send("hello", cwd=str(tmp_path))
    assert result["ok"] is False
    assert result["error"]["code"] == "credentials_missing"
    assert "GEMINI_API_KEY" in result["error"]["message"]


@pytest.mark.mcp_smoke
def test_mcp_write_returns_full_result_and_reuses_request_id(tmp_path: Path) -> None:
    helper = Path(__file__).parent / "support/fake_mcp_server.py"
    params = StdioServerParameters(command=sys.executable, args=[str(helper.resolve())],
                                   env=_server_env(tmp_path / "state"), cwd=str(Path.cwd()))

    async def run() -> None:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                arguments = {"prompt": "write", "cwd": str(tmp_path), "request_id": "mcp-writing-id", "wait_seconds": 5}
                first = (await session.call_tool("gemini_write", arguments)).structuredContent
                assert first["done"] and first["ok"]
                assert first["text"] == "writing worker survived"
                again = (await session.call_tool("gemini_write", arguments)).structuredContent
                assert again["job_id"] == first["job_id"]
                result = (await session.call_tool("gemini_write_result", {"job_id": first["job_id"]})).structuredContent
                assert result["channel"] == "cli"
                assert result["actual_model_version"] == "4 Argon"
                assert result["effort"] == "high"
                assert result["requested_effort"] == "high"
                assert result["effective_effort"] == "high"
                assert len(result["attempts"]) == 1

    asyncio.run(asyncio.wait_for(run(), timeout=20))
