from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import gemini_baton.service as service_module
from gemini_baton import credentials
from gemini_baton.config import Settings
from gemini_baton.errors import BridgeError
from gemini_baton.paths import StateLayout
from gemini_baton.processes import (
    group_alive,
    read_worker_lock,
    worker_lock_held,
    worker_lock_path,
)
from gemini_baton.store import Store
from tests.conftest import FAKE_KEY, wait_done, wait_until


def start(
    service: Any,
    message: str,
    workspace: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    return service.send(message, cwd=str(workspace), **kwargs)


def finished(
    service: Any, message: str, workspace: Path, **kwargs: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    info = start(service, message, workspace, **kwargs)
    wait = wait_done(service, info["run_id"])
    result = service.result(info["run_id"])
    return info, result


def test_successful_run_persists_artifacts_permissions_and_session(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    service = service_factory()
    info, result = finished(service, "hello", fake_env.workspace)

    assert result["status"] == "succeeded"
    assert result["final_text"] == "fake final answer"
    assert result["selected_model"] == "gemini-4-argon"
    assert result["actual_model_version"] == "fake-3.8-flash"
    assert result["finish_reason"] == "STOP"
    assert result["usage"]["total_token_count"] == 14
    assert result["final_text_path"] and Path(result["final_text_path"]).read_text() == (
        "fake final answer"
    )
    run_dir = Path(result["artifacts"]["dir"])
    assert (run_dir / "events.jsonl").exists()
    assert (run_dir / "steps" / "step-01-model.json").exists()
    assert (run_dir / "result.json").exists()
    assert (run_dir / "worker.lock").exists()
    lock_metadata = json.loads((run_dir / "worker.lock").read_text())
    assert lock_metadata["run_id"] == info["run_id"]
    assert lock_metadata["identity"]
    assert result["worker"]["identity"] == lock_metadata["identity"]
    run_row = Store(StateLayout(fake_env.state_dir)).get_run(info["run_id"])
    assert lock_metadata["token_fingerprint"] == run_row["worker_token_hash"]
    transcript = Path(result["artifacts"]["transcript"])
    assert transcript.exists()
    transcript_data = json.loads(transcript.read_text())
    assert transcript_data[0]["role"] == "user"
    assert transcript_data[-1]["role"] == "model"

    assert (fake_env.state_dir.stat().st_mode & 0o777) == 0o700
    assert (fake_env.state_dir / "runs").stat().st_mode & 0o777 == 0o700
    assert (fake_env.state_dir / "sessions").stat().st_mode & 0o777 == 0o700
    assert run_dir.stat().st_mode & 0o777 == 0o700
    for path in (
        fake_env.state_dir / "gemini-baton.sqlite3",
        transcript,
        run_dir / "final.md",
        run_dir / "events.jsonl",
        run_dir / "worker.log",
        run_dir / "worker.lock",
    ):
        assert (path.stat().st_mode & 0o777) == 0o600, path

    store = Store(StateLayout(fake_env.state_dir))
    session = store.get_session(info["session_id"])
    assert session is not None
    assert session["last_run_id"] == info["run_id"]
    run_row = store.get_run(info["run_id"])
    assert run_row["worker_token_hash"]
    assert len(run_row["worker_token_hash"]) == 64
    assert session["transcript_sha256"] == __import__("hashlib").sha256(
        session["transcript_json"].encode()
    ).hexdigest()


def test_result_survives_new_service_instance(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    service = service_factory()
    info, _ = finished(service, "persist me", fake_env.workspace)
    restarted = service_factory()
    result = restarted.result(info["run_id"])
    assert result["status"] == "succeeded"
    assert result["final_text"] == "fake final answer"


def test_tool_session_thought_signature_and_changed_files(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "tool_write")
    service = service_factory()
    info, result = finished(
        service,
        "write a file",
        fake_env.workspace,
        permission_mode="workspace-write",
    )
    assert result["status"] == "succeeded"
    assert result["changed_files"] == ["written.txt"]
    assert "prior_signature_present=true" in result["final_text"]
    assert (fake_env.workspace / "written.txt").read_text() == "written by fake model\n"
    store = Store(StateLayout(fake_env.state_dir))
    transcript = json.loads(
        store.get_session(info["session_id"])["transcript_json"]
    )
    signatures = [
        part["thoughtSignature"]
        for content in transcript
        for part in content.get("parts", [])
        if "thoughtSignature" in part
    ]
    assert signatures
    assert any("functionResponse" in part for content in transcript for part in content["parts"])

    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "session_echo")
    second = service.send("second turn", session_id=info["session_id"])
    wait_done(service, second["run_id"])
    second_result = service.result(second["run_id"])
    assert second_result["status"] == "succeeded"
    assert "prior_signature_present=true" in second_result["final_text"]
    assert "prior_function_responses=1" in second_result["final_text"]


def test_session_defaults_cwd_immutability_model_and_permissions(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    service = service_factory()
    first = start(service, "first", fake_env.workspace)
    wait_done(service, first["run_id"])
    assert first["permission_mode"] == "read-only"
    assert first["allow_shell"] is False

    other = fake_env.workspace.parent / "other"
    other.mkdir()
    with pytest.raises(BridgeError) as excinfo:
        service.send(
            "move it", cwd=str(other), session_id=first["session_id"]
        )
    assert excinfo.value.code == "cwd_mismatch"

    second = service.send(
        "upgrade",
        session_id=first["session_id"],
        model="gemini-explicit-other",
        permission_mode="workspace-write",
        allow_shell=True,
    )
    wait_done(service, second["run_id"])
    assert second["selected_model"] == "gemini-explicit-other"
    assert second["session_defaults"] == {
        "cwd": str(fake_env.workspace.resolve()),
        "model": "gemini-explicit-other",
        "permission_mode": "workspace-write",
        "allow_shell": True,
    }
    third = service.send("inherit", session_id=first["session_id"])
    assert third["selected_model"] == "gemini-explicit-other"
    assert third["permission_mode"] == "workspace-write"
    assert third["allow_shell"] is True
    wait_done(service, third["run_id"])


def test_busy_session_rejects_concurrent_send(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "slow")
    service = service_factory()
    first = start(service, "slow", fake_env.workspace, timeout_seconds=60)
    try:
        with pytest.raises(BridgeError) as excinfo:
            service.send("second", session_id=first["session_id"])
        assert excinfo.value.code == "session_busy"
    finally:
        service.cancel(first["run_id"])


def test_global_concurrency_limit(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_MAX_ACTIVE_RUNS", "1")
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "slow")
    service = service_factory()
    first = start(service, "slow one", fake_env.workspace, timeout_seconds=60)
    try:
        with pytest.raises(BridgeError) as excinfo:
            start(service, "slow two", fake_env.workspace, timeout_seconds=60)
        assert excinfo.value.code == "concurrency_limit"
    finally:
        service.cancel(first["run_id"])


def test_cancel_terminates_worker_group_and_child(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "slow_tool")
    service = service_factory()
    info = start(
        service,
        "spawn a sleeper",
        fake_env.workspace,
        permission_mode="workspace-write",
        allow_shell=True,
        timeout_seconds=60,
    )
    run_id = info["run_id"]
    tool_logs = list(
        (fake_env.state_dir / "runs" / run_id / "tools").glob(
            "**/command.stdout.txt"
        )
    )

    def child_started() -> bool:
        nonlocal tool_logs
        tool_logs = list(
            (fake_env.state_dir / "runs" / run_id / "tools").glob(
                "**/command.stdout.txt"
            )
        )
        return bool(tool_logs and "child_pid=" in tool_logs[0].read_text())

    wait_until(child_started)
    child_pid = int(
        tool_logs[0].read_text().split("child_pid=", 1)[1].splitlines()[0]
    )
    row = service.store().get_run(run_id)
    pgid = row["worker_pgid"]
    assert worker_lock_held(worker_lock_path(fake_env.state_dir / "runs" / run_id))

    result = service.cancel(run_id)
    assert result["status"] == "cancelled"
    assert result["termination"]["confirmed"] is True
    assert result["termination"]["worker_lock_released"] is True
    assert group_alive(pgid) is False
    wait_until(lambda: not worker_lock_held(
        worker_lock_path(fake_env.state_dir / "runs" / run_id)
    ))
    try:
        os.kill(child_pid, 0)
        child_alive = True
    except ProcessLookupError:
        child_alive = False
    assert child_alive is False
    final = service.result(run_id)
    assert final["status"] == "cancelled"
    assert "cancelled" in final["error"]["code"]
    assert (fake_env.state_dir / "runs" / run_id / "cancel.json").exists()

    # A cancelled turn is not checkpointed; the session remains usable.
    session = service.store().get_session(info["session_id"])
    assert json.loads(session["transcript_json"]) == []
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    follow = service.send("after cancel", session_id=info["session_id"])
    wait_done(service, follow["run_id"])
    assert service.result(follow["run_id"])["status"] == "succeeded"


def test_dead_worker_is_recovered_and_session_reusable(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "slow")
    service = service_factory()
    info = start(service, "slow", fake_env.workspace, timeout_seconds=60)
    run_id = info["run_id"]
    lock = worker_lock_path(fake_env.state_dir / "runs" / run_id)
    wait_until(lambda: worker_lock_held(lock))
    row = service.store().get_run(run_id)
    os.killpg(row["worker_pgid"], signal.SIGKILL)

    def dead() -> bool:
        return not worker_lock_held(lock)

    wait_until(dead)
    # A fresh service instance has no Popen handle for this run; recovery must
    # work purely from the worker lock/state, as it does after MCP restarts.
    restarted = service_factory()
    result = restarted.result(run_id)
    assert result["status"] == "interrupted"
    assert result["error"]["code"] == "worker_died"
    assert result["worker"]["alive"] is False

    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    follow = restarted.send("reuse", session_id=info["session_id"])
    wait_done(restarted, follow["run_id"])
    assert restarted.result(follow["run_id"])["status"] == "succeeded"


def test_supervisor_timeout_kills_stuck_worker(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "slow")
    monkeypatch.setattr(service_module, "TIMEOUT_KILL_GRACE_SECONDS", 0.25)
    service = service_factory()
    info = start(service, "slow", fake_env.workspace, timeout_seconds=1)
    wait = wait_done(service, info["run_id"], timeout=12)
    assert wait["status"] == "failed"
    result = service.result(info["run_id"])
    assert result["error"]["code"] == "run_timeout"
    assert result["worker"]["alive"] is False
    assert not worker_lock_held(
        worker_lock_path(fake_env.state_dir / "runs" / info["run_id"])
    )


def test_checks_success_and_failure_semantics(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    service = service_factory()
    ok_check = [sys.executable, "-c", "print('checks-ok')"]
    info, result = finished(
        service,
        "with checks",
        fake_env.workspace,
        permission_mode="workspace-write",
        allow_shell=True,
        checks=[ok_check],
    )
    assert result["status"] == "succeeded"
    assert result["checks"][0]["ok"] is True
    assert result["checks"][0]["exit_code"] == 0
    assert "checks-ok" in result["checks"][0]["stdout_preview"]
    assert Path(result["checks"][0]["stdout_path"]).exists()
    store = Store(StateLayout(fake_env.state_dir))
    transcript_after_success = store.get_session(info["session_id"])[
        "transcript_json"
    ]

    bad_check = [
        sys.executable,
        "-c",
        "print('checks-bad'); raise SystemExit(3)",
    ]
    info2, result2 = finished(
        service,
        "failing checks",
        fake_env.workspace,
        permission_mode="workspace-write",
        allow_shell=True,
        checks=[bad_check],
    )
    assert result2["status"] == "failed"
    assert result2["error"]["code"] == "checks_failed"
    assert result2["checks"][0]["exit_code"] == 3
    assert result2["final_text"] == "fake final answer"
    session = store.get_session(info2["session_id"])
    assert json.loads(session["transcript_json"]) == []
    assert transcript_after_success != session["transcript_json"]


def test_checks_require_shell_and_workspace_write(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    service = service_factory()
    with pytest.raises(BridgeError) as excinfo:
        start(
            service,
            "bad checks",
            fake_env.workspace,
            checks=[[sys.executable, "-c", "print(1)"]],
        )
    assert excinfo.value.code == "invalid_checks"


def test_error_redaction_in_db_artifacts_and_logs(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "secret_error")
    service = service_factory()
    info = start(service, "boom", fake_env.workspace)
    wait_done(service, info["run_id"])
    result = service.result(info["run_id"])
    assert result["status"] == "failed"
    encoded = json.dumps(result)
    assert FAKE_KEY not in encoded
    assert "[REDACTED]" in result["error"]["message"] + encoded
    run_dir = Path(result["artifacts"]["dir"])
    for path in (
        run_dir / "worker.log",
        run_dir / "events.jsonl",
        run_dir / "result.json",
    ):
        assert FAKE_KEY not in path.read_text(errors="replace"), path
    db_bytes = (fake_env.state_dir / "gemini-baton.sqlite3").read_bytes()
    assert FAKE_KEY.encode() not in db_bytes


def test_missing_credential_is_clear_and_does_not_create_run(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_factory()
    monkeypatch.setattr(
        service_module,
        "credential_status",
        lambda: {"present": False, "source": None, "env_file": {"configured": True}},
    )
    with pytest.raises(BridgeError) as excinfo:
        start(service, "no key", fake_env.workspace)
    assert excinfo.value.code == "credentials_missing"
    assert "GEMINI_API_KEY" in excinfo.value.message
    assert FAKE_KEY not in json.dumps(excinfo.value.as_dict())


def test_status_is_local_and_does_not_expose_key(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_factory()
    payload = service.status()
    assert payload["network_calls"] == 0
    assert payload["paid_model_calls"] == 0
    assert payload["credentials"]["present"] is True
    assert payload["credentials"]["source"] == "env:GEMINI_API_KEY"
    assert FAKE_KEY not in json.dumps(payload)
    assert payload["model"]["fallback_allowed"] is False

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_BATON_ENV_FILE", raising=False)
    monkeypatch.setattr(credentials, "_keychain_exists", lambda: False)
    no_key = service.status()
    assert no_key["credentials"]["present"] is False
    assert no_key["credentials"]["source"] is None


def test_list_models_is_explicit_and_mockable(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"n": 0}

    def fake_listing() -> dict[str, Any]:
        called["n"] += 1
        return {
            "models": [{"name": "models/gemini-3.8-flash"}],
            "count": 1,
            "credential_source": "env:GEMINI_API_KEY",
            "service_version": "0.1.0",
        }

    monkeypatch.setattr(service_module, "list_available_models", fake_listing)
    service = service_factory()
    status = service.status()
    assert called["n"] == 0
    listing = service.list_models()
    assert called["n"] == 1
    assert listing["network_calls"] == 1
    assert status["paid_model_calls"] == 0


def test_wait_cap_and_result_compaction(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "long")
    service = service_factory()
    info = start(service, "long", fake_env.workspace)
    with pytest.raises(BridgeError) as excinfo:
        service.wait(info["run_id"], 50.5)
    assert excinfo.value.code == "invalid_arguments"
    wait_done(service, info["run_id"])
    compact = service.result(info["run_id"], compact=True)
    assert compact["final_text_truncated"] is True
    assert len(compact["final_text"]) < 9000
    assert compact["final_text_path"]
    full = service.result(info["run_id"], compact=False)
    assert len(full["final_text"]) == 9000


def test_unknown_ids_and_session_are_clear(
    fake_env: Any,
    service_factory: Any,
) -> None:
    service = service_factory()
    with pytest.raises(BridgeError) as excinfo:
        service.result("run_missing")
    assert excinfo.value.code == "unknown_run"
    with pytest.raises(BridgeError) as excinfo:
        service.send("hi", session_id="ses_missing")
    assert excinfo.value.code == "unknown_session"


def test_env_file_credential_is_used_by_detached_worker(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    env_file = tmp_path / "explicit-credentials.env"
    file_secret = "file-only-test-key-123456789"
    env_file.write_text(
        "# only auth keys are read\n"
        f"GEMINI_API_KEY={file_secret}\n"
        "OTHER_SECRET=must-not-be-used\n",
        encoding="utf-8",
    )
    relative = os.path.relpath(env_file, Path.cwd())
    monkeypatch.setenv("GEMINI_BATON_ENV_FILE", relative)
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    service = service_factory()
    status = service.status()
    assert status["credentials"]["present"] is True
    assert status["credentials"]["source"] == "env-file:GEMINI_API_KEY"
    assert status["credentials"]["env_file"] == {
        "configured": True,
        "readable": True,
        "contains_supported_key": True,
    }
    info, result = finished(service, "env-file worker", fake_env.workspace)
    assert result["status"] == "succeeded"
    assert file_secret not in json.dumps(result)
    assert file_secret not in (
        fake_env.state_dir / "runs" / info["run_id"] / "worker.log"
    ).read_text(errors="replace")
    assert file_secret.encode() not in (
        fake_env.state_dir / "gemini-baton.sqlite3"
    ).read_bytes()
    for state_file in fake_env.state_dir.rglob("*"):
        if state_file.is_file():
            assert file_secret.encode() not in state_file.read_bytes(), state_file


def test_list_sessions_reports_latest_status(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    service = service_factory()
    info, _ = finished(service, "session list", fake_env.workspace)
    listing = service.list_sessions(limit=10)
    assert listing["ok"] is True
    matching = [
        item for item in listing["sessions"] if item["session_id"] == info["session_id"]
    ]
    assert len(matching) == 1
    assert matching[0]["last_run_id"] == info["run_id"]
    assert matching[0]["last_run_status"] == "succeeded"


def test_worker_and_result_survive_caller_process_exit(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    script = f"""
import json, os, sys
sys.path.insert(0, {str(Path.cwd())!r})
from gemini_baton.config import Settings
from gemini_baton.service import RunService
svc = RunService(
    Settings.from_env(),
    worker_command=[sys.executable, {str(Path('tests/support/fake_worker.py').resolve())!r}],
)
info = svc.send("survive caller exit", cwd={str(fake_env.workspace)!r}, timeout_seconds=30)
print(json.dumps(info))
sys.stdout.flush()
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        env=os.environ.copy(),
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    info = json.loads(completed.stdout.strip().splitlines()[-1])
    restarted = service_factory()
    wait = wait_done(restarted, info["run_id"], timeout=20)
    assert wait["status"] == "succeeded"
    result = restarted.result(info["run_id"])
    assert result["final_text"] == "fake final answer"
    assert result["worker"]["alive"] is False


def test_cancel_reaches_in_flight_check_process(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    service = service_factory()
    check_code = (
        "import time\n"
        "print('check-started', flush=True)\n"
        "time.sleep(300)\n"
    )
    info = start(
        service,
        "run a slow check",
        fake_env.workspace,
        permission_mode="workspace-write",
        allow_shell=True,
        timeout_seconds=60,
        checks=[[sys.executable, "-c", check_code]],
    )
    check_stdout = (
        fake_env.state_dir
        / "runs"
        / info["run_id"]
        / "checks"
        / "check-01"
        / "check.stdout.txt"
    )

    def check_started() -> bool:
        return check_stdout.exists() and "check-started" in check_stdout.read_text()

    wait_until(check_started)
    row = service.store().get_run(info["run_id"])
    assert worker_lock_held(
        worker_lock_path(fake_env.state_dir / "runs" / info["run_id"])
    )
    cancelled = service.cancel(info["run_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["termination"]["confirmed"] is True
    assert group_alive(row["worker_pgid"]) is False
    result = service.result(info["run_id"])
    assert result["status"] == "cancelled"
    assert json.loads(
        service.store().get_session(info["session_id"])["transcript_json"]
    ) == []


def test_configured_model_is_explicit_and_not_silently_replaced(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_MODEL", "gemini-explicit-configured")
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    service = service_factory()
    status = service.status()
    assert status["model"] == {
        "configured": "gemini-explicit-configured",
        "source": "env:GEMINI_BATON_MODEL",
        "fallback_allowed": False,
    }
    info, result = finished(service, "model config", fake_env.workspace)
    assert info["selected_model"] == "gemini-explicit-configured"
    assert result["selected_model"] == "gemini-explicit-configured"
    assert result["actual_model_version"] == "fake-3.8-flash"


def test_worker_receives_monotonic_remaining_timeout(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "deadline_probe")
    service = service_factory()
    info = start(service, "deadline", fake_env.workspace, timeout_seconds=5)
    wait_done(service, info["run_id"])
    result = service.result(info["run_id"])
    assert result["status"] == "succeeded"
    reported = float(result["final_text"].split("=", 1)[1])
    assert 0 < reported <= 5


def test_worker_itself_finishes_after_api_timeout_without_polling(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "honor_timeout")
    service = service_factory()
    info = start(service, "timeout", fake_env.workspace, timeout_seconds=1)
    # Read SQLite directly so service.wait/recover does not act as a supervisor.
    store = service.store()
    row = None
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        row = store.get_run(info["run_id"])
        if row and row["status"] not in ("running", "cancelling"):
            break
        time.sleep(0.1)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_json"]
    assert "fake backend honoured" in row["error_json"]


def test_cancel_fails_closed_on_identity_mismatch_then_succeeds_after_restore(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "slow")
    spawning_service = service_factory()
    info = start(spawning_service, "slow", fake_env.workspace, timeout_seconds=60)
    run_id = info["run_id"]
    lock = worker_lock_path(fake_env.state_dir / "runs" / run_id)

    def metadata_ready() -> bool:
        return bool(read_worker_lock(lock) and read_worker_lock(lock).get("identity"))

    wait_until(metadata_ready)
    original = lock.read_text()
    metadata = json.loads(original)
    metadata["identity"] = "deliberately-mismatched-identity"
    with lock.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(metadata))
        handle.flush()
        os.fsync(handle.fileno())

    # A fresh service has no Popen handle, so only lock metadata can
    # authenticate the worker. The mismatch must prevent signalling.
    fresh = service_factory()
    with pytest.raises(BridgeError) as excinfo:
        fresh.cancel(run_id)
    assert excinfo.value.code == "cancel_unconfirmed"
    assert worker_lock_held(lock)
    assert group_alive(fresh.store().get_run(run_id)["worker_pgid"])

    with lock.open("w", encoding="utf-8") as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())

    terminated = fresh.cancel(run_id)
    assert terminated["status"] == "cancelled"
    assert not worker_lock_held(lock)


def test_status_reconciles_dead_worker_locally(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "slow")
    service = service_factory()
    info = start(service, "slow", fake_env.workspace, timeout_seconds=60)
    lock = worker_lock_path(fake_env.state_dir / "runs" / info["run_id"])
    wait_until(lambda: worker_lock_held(lock))
    row = service.store().get_run(info["run_id"])
    os.killpg(row["worker_pgid"], signal.SIGKILL)
    wait_until(lambda: not worker_lock_held(lock))
    status = service.status()
    assert status["network_calls"] == 0
    recovered = service.store().get_run(info["run_id"])
    assert recovered["status"] == "interrupted"
    assert recovered["error_json"]


def test_session_cwd_symlink_replacement_is_rejected(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "final")
    service = service_factory()
    info, _ = finished(service, "first", fake_env.workspace)
    moved = fake_env.workspace.parent / "moved-workspace"
    fake_env.workspace.rename(moved)
    replacement = fake_env.workspace.parent / "replacement"
    replacement.mkdir()
    fake_env.workspace.symlink_to(replacement, target_is_directory=True)
    with pytest.raises(BridgeError) as excinfo:
        service.send("follow symlink", session_id=info["session_id"])
    assert excinfo.value.code == "cwd_changed"


def test_supervisor_timeout_does_not_hide_live_worker_on_unconfirmed_kill(
    fake_env: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gemini_baton.processes import GroupTermination

    monkeypatch.setenv("GEMINI_BATON_FAKE_SCENARIO", "slow")
    monkeypatch.setattr(service_module, "TIMEOUT_KILL_GRACE_SECONDS", 0.1)
    service = service_factory()
    info = start(service, "slow timeout", fake_env.workspace, timeout_seconds=1)
    run_id = info["run_id"]
    lock = worker_lock_path(fake_env.state_dir / "runs" / run_id)
    wait_until(lambda: worker_lock_held(lock))
    time.sleep(1.35)
    unconfirmed = GroupTermination(
        requested=True,
        confirmed=False,
        group_alive_before=True,
        group_alive_after=True,
        worker_lock_released=False,
        detail="test-only unconfirmed kill",
    )
    real_terminate = service_module.terminate_process_group
    monkeypatch.setattr(
        service_module,
        "terminate_process_group",
        lambda *args, **kwargs: unconfirmed,
    )
    service.recover_stale()
    run = service.store().get_run(run_id)
    assert run["status"] == "running"
    assert worker_lock_held(lock)

    monkeypatch.setattr(service_module, "terminate_process_group", real_terminate)
    wait_done(service, run_id, timeout=10)
    final = service.result(run_id)
    assert final["status"] == "failed"
    assert final["error"]["code"] == "run_timeout"
    assert final["worker"]["alive"] is False
