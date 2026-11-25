from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_unknown_cli_failure_retains_recovery_diagnostics():
    from gemini_baton.writing_routes import cli_receipt_from_stream
    from gemini_baton.errors import BridgeError
    with pytest.raises(BridgeError) as caught:
        cli_receipt_from_stream(
            [{"event": "result", "result": {"status": "ERROR", "error": "transport closed",
                                             "conversation_id": "recover-existing"}}],
            malformed=0, exit_code=1, requested_model="gemini-4-argon-high",
            requested_effort="high", cwd="/tmp")
    assert caught.value.code == "outcome_unknown"
    assert caught.value.details["retry_safe"] is False
    assert caught.value.details["conversation_id"] == "recover-existing"
    assert caught.value.details["error"] == "transport closed"

from gemini_baton.config import Settings
from gemini_baton.errors import BridgeError
from gemini_baton.writing import WritingService
from gemini_baton.writing_files import cli_preflight, cli_prompt, selected_files, snapshot_files
from gemini_baton.writing_routes import (CLI_DENY_RULES, CLI_MODEL_IDS, CLI_SETTINGS,
                                         WritingTransport, api_effort_supported,
                                         cli_receipt_from_stream, fallback_scope,
                                         parse_cli_stream, supported_api_efforts)

FAKE_AGY = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

argv = sys.argv[1:]
def value(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None

prompt = json.loads(sys.stdin.readline())["message"]["content"]
assert prompt not in argv
settings = json.loads(value("--settings") or "{}")
capture = {
    "cwd": os.getcwd(),
    "cwd_entries": sorted(os.listdir(".")),
    "prompt": prompt,
    "model": value("--model"),
    "effort": value("--effort"),
    "output_format": value("--output-format"),
    "settings": settings,
    "sandbox": "--sandbox" in argv,
    "mode": value("--mode"),
    "disable_slash_commands": "--disable-slash-commands" in argv,
    "print_timeout": value("--print-timeout"),
    "env_api_key": "GEMINI_API_KEY" in os.environ,
    "env_google_key": "GOOGLE_API_KEY" in os.environ,
    "env_baton_file": "GEMINI_BATON_ENV_FILE" in os.environ,
    "env_application_credentials": "GOOGLE_APPLICATION_CREDENTIALS" in os.environ,
}
Path(os.environ["FAKE_AGY_CAPTURE"]).write_text(json.dumps(capture))
mode = os.environ.get("FAKE_AGY_MODE", "success")
model = value("--model") or "gemini-4-argon-high"

def emit(value):
    print(json.dumps(value), flush=True)

if mode == "sleep_no_init":
    time.sleep(10)
    raise SystemExit(0)
if mode == "sleep_after_init":
    emit({"event": "init", "init": {"model": model, "cwd": os.getcwd(),
                                    "permission_mode": "strict", "tools": ["run_command"]}})
    time.sleep(10)
    raise SystemExit(0)
if mode in {"pre_submit_model", "pre_submit_auth"}:
    error = ("invalid model selection (--model x): model is not recognized"
             if mode == "pre_submit_model" else "not logged in; sign in first")
    emit({"event": "result", "result": {"conversation_id": "", "status": "ERROR",
          "response": "", "error": error, "duration_seconds": 0, "num_turns": 0,
          "usage": {"input_tokens": 0, "output_tokens": 0,
                    "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 0}}})
    raise SystemExit(1)
if mode == "no_init":
    emit({"event": "result", "result": {"conversation_id": "conv-no-init",
          "status": "SUCCESS", "response": "unverified\n", "duration_seconds": 1,
          "num_turns": 1, "usage": {}}})
    raise SystemExit(0)

init_model = "gemini-4-carbon-high" if mode == "wrong_model" else model
emit({"event": "init", "init": {"model": init_model, "cwd": os.getcwd(),
                                "permission_mode": "strict", "tools": ["run_command"]}})
emit({"event": "step_update", "step_update": {"step_type": "user_input", "state": "DONE"}})
if mode == "tool_step":
    emit({"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE",
          "tool_name": "run_command"}})
if mode == "post_init_error":
    emit({"event": "result", "result": {"conversation_id": "conv-post-error",
          "status": "ERROR", "response": "", "error": "generation failed",
          "duration_seconds": 2, "num_turns": 1, "usage": {}}})
    raise SystemExit(1)
emit({"event": "step_update", "step_update": {"step_type": "agent_response",
      "state": "DONE", "text_delta": "SYNTHETIC_OK\n"}})
emit({"event": "result", "result": {"conversation_id": "conv-fake",
      "status": "SUCCESS", "response": "SYNTHETIC_OK\n", "duration_seconds": 1.0,
      "num_turns": 1, "usage": {"input_tokens": 1, "output_tokens": 1}}})
'''


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_BATON_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("GEMINI_BATON_AGY", raising=False)
    return WritingService(Settings.from_env(), worker_command=[sys.executable, "-c", "pass"])


def install_fake_agy(tmp_path: Path, monkeypatch, mode: str = "success") -> Path:
    script = tmp_path / "fake-agy"
    script.write_text(FAKE_AGY)
    script.chmod(0o755)
    capture = tmp_path / "fake-agy-capture.json"
    monkeypatch.setenv("GEMINI_BATON_AGY", str(script))
    monkeypatch.setenv("FAKE_AGY_MODE", mode)
    monkeypatch.setenv("FAKE_AGY_CAPTURE", str(capture))
    return capture


def make_job(tmp_path: Path, *, prompt: str = "Write a synthetic paragraph.",
             file_text: str | None = "SELECTED_EVIDENCE_ONLY") -> dict:
    files = []
    if file_text is not None:
        source = tmp_path / "source.md"
        source.write_text(file_text)
        files = snapshot_files(selected_files([str(source)]), tmp_path / "inputs")
    return {"job_id": "write_" + "b" * 32, "prompt": prompt, "files": files,
            "route_timeout_seconds": 30, "conversation_id": None}


def start(service: WritingService, tmp_path: Path, **kwargs) -> str:
    return service.submit("Write from selected evidence", cwd=str(tmp_path),
                          wait_seconds=0, **kwargs)["job_id"]


class Outcomes:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def _call(self, channel, job, model, effort):
        self.calls.append((channel, model, effort))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return {"text": value, "model": model, "actual_model_version": model,
                "requested_effort": effort, "effective_effort": effort,
                "model_evidence": f"{channel}_test"}

    def cli(self, job, model, effort="high"):
        return self._call("cli", job, model, effort)

    def api(self, job, model, effort="high"):
        return self._call("api", job, model, effort)

    def aistudio(self, job, model, effort="high"):
        return self._call("aistudio", job, model, effort)

    def web(self, job, model, effort="high"):
        return self._call("web", job, model, effort)


def test_cli_success_receipt_isolated_prompt_snapshot_and_deny_all(tmp_path, monkeypatch, service):
    capture_path = install_fake_agy(tmp_path, monkeypatch, "success")
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-private")
    monkeypatch.setenv("GOOGLE_API_KEY", "unit-test-private")
    monkeypatch.setenv("GEMINI_BATON_ENV_FILE", "/do-not-read/.env")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/do-not-read/credentials.json")
    job = make_job(tmp_path)

    receipt = WritingTransport(service.settings).cli(job, "4 Argon", "high")
    captured = json.loads(capture_path.read_text())

    assert captured["model"] == "gemini-4-argon-high"
    assert captured["effort"] == "high"
    assert captured["output_format"] == "stream-json"
    assert captured["sandbox"] is True
    assert captured["mode"] == "plan"
    assert captured["disable_slash_commands"] is True
    assert captured["print_timeout"] == "30s"
    assert captured["cwd_entries"] == []
    assert captured["cwd"] != str(Path.cwd())
    assert "SELECTED_EVIDENCE_ONLY" in captured["prompt"]
    assert "AGENTS.md" not in captured["prompt"]
    assert captured["settings"]["toolPermission"] == "strict"
    assert captured["settings"]["agentMode"] == "plan"
    assert captured["settings"]["allowNonWorkspaceAccess"] is False
    assert captured["settings"]["disableSlashCommands"] is True
    assert captured["settings"]["permissions"]["deny"] == list(CLI_DENY_RULES)
    assert captured["env_api_key"] is False
    assert captured["env_google_key"] is False
    assert captured["env_baton_file"] is False
    assert captured["env_application_credentials"] is False

    assert receipt["text"] == "SYNTHETIC_OK"
    assert receipt["actual_model_version"] == "gemini-4-argon-high"
    assert receipt["requested_model"] == "gemini-4-argon-high"
    assert receipt["model_evidence"] == "cli_init_model"
    assert receipt["requested_effort"] == "high"
    assert receipt["effective_effort"] == "high"
    assert receipt["effort_evidence"] == "cli_verified_model_slug"
    assert receipt["conversation_id"] is None
    assert receipt["cli_conversation_id"] == "conv-fake"
    assert receipt["request_id"] is None
    assert receipt["capability_status"]["permission_mode"] == "strict"
    assert receipt["capability_status"]["tool_steps"] == 0
    assert receipt["capability_status"]["sandbox"] is True
    assert receipt["capability_status"]["model_verified"] is True


def test_cli_wrong_init_model_is_rejected_and_stops(tmp_path, monkeypatch, service):
    install_fake_agy(tmp_path, monkeypatch, "wrong_model")
    with pytest.raises(BridgeError) as error:
        WritingTransport(service.settings).cli(make_job(tmp_path), "4 Argon", "high")
    assert error.value.code == "model_unverified"
    assert error.value.details["phase"] == "SUBMITTED"
    assert error.value.details["outcome"] == "unknown"
    assert error.value.details["actual_model_version"] == "gemini-4-carbon-high"
    assert fallback_scope(error.value.as_dict(), "cli") == "stop"


def test_cli_success_without_init_model_evidence_is_rejected(tmp_path, monkeypatch, service):
    install_fake_agy(tmp_path, monkeypatch, "no_init")
    with pytest.raises(BridgeError) as error:
        WritingTransport(service.settings).cli(make_job(tmp_path), "4 Argon", "high")
    assert error.value.code == "model_unverified"
    assert error.value.details["phase"] == "SUBMITTED"
    assert fallback_scope(error.value.as_dict(), "cli") == "stop"


def test_cli_pre_submit_model_error_is_safe_model_fallback(tmp_path, monkeypatch, service):
    install_fake_agy(tmp_path, monkeypatch, "pre_submit_model")
    with pytest.raises(BridgeError) as error:
        WritingTransport(service.settings).cli(make_job(tmp_path), "4 Argon", "high")
    assert error.value.code == "MODEL_UNAVAILABLE"
    assert error.value.details["phase"] == "PRE_SUBMIT"
    assert error.value.details["retry_safe"] is True
    assert error.value.details["exit_code"] == 1
    assert fallback_scope(error.value.as_dict(), "cli") == "model"


def test_cli_pre_submit_auth_error_skips_channel(tmp_path, monkeypatch, service):
    install_fake_agy(tmp_path, monkeypatch, "pre_submit_auth")
    with pytest.raises(BridgeError) as error:
        WritingTransport(service.settings).cli(make_job(tmp_path), "4 Argon", "high")
    assert error.value.code == "AUTHORIZATION_REQUIRED"
    assert error.value.details["phase"] == "PRE_SUBMIT"
    assert fallback_scope(error.value.as_dict(), "cli") == "channel"


def test_cli_post_init_error_is_outcome_unknown_and_stops(tmp_path, monkeypatch, service):
    install_fake_agy(tmp_path, monkeypatch, "post_init_error")
    with pytest.raises(BridgeError) as error:
        WritingTransport(service.settings).cli(make_job(tmp_path), "4 Argon", "high")
    assert error.value.code == "outcome_unknown"
    assert error.value.details["phase"] == "SUBMITTED"
    assert error.value.details["retry_safe"] is False
    assert error.value.details["actual_model_version"] == "gemini-4-argon-high"
    assert fallback_scope(error.value.as_dict(), "cli") == "stop"


def test_cli_tool_step_is_rejected_even_when_result_succeeds(tmp_path, monkeypatch, service):
    install_fake_agy(tmp_path, monkeypatch, "tool_step")
    with pytest.raises(BridgeError) as error:
        WritingTransport(service.settings).cli(make_job(tmp_path), "4 Argon", "high")
    assert error.value.code == "tool_access_unexpected"
    assert error.value.details["tools"] == ["run_command"]
    assert error.value.details["phase"] == "SUBMITTED"
    assert fallback_scope(error.value.as_dict(), "cli") == "stop"


def test_cli_timeout_after_init_is_unknown(tmp_path, monkeypatch, service):
    install_fake_agy(tmp_path, monkeypatch, "sleep_after_init")
    monkeypatch.setattr("gemini_baton.writing_routes.CLI_PROCESS_GRACE_SECONDS", 0)
    job = make_job(tmp_path)
    job["route_timeout_seconds"] = 1
    with pytest.raises(BridgeError) as error:
        WritingTransport(service.settings).cli(job, "4 Argon", "high")
    assert error.value.code == "outcome_unknown"
    assert error.value.details["phase"] == "SUBMITTED"
    assert error.value.details["cli_status"] == "TIMEOUT"
    assert fallback_scope(error.value.as_dict(), "cli") == "stop"


def test_cli_timeout_without_init_retains_unknown_delivery(tmp_path, monkeypatch, service):
    install_fake_agy(tmp_path, monkeypatch, "sleep_no_init")
    monkeypatch.setattr("gemini_baton.writing_routes.CLI_PROCESS_GRACE_SECONDS", 0)
    job = make_job(tmp_path)
    job["route_timeout_seconds"] = 1
    with pytest.raises(BridgeError) as error:
        WritingTransport(service.settings).cli(job, "4 Argon", "high")
    assert error.value.code == "outcome_unknown"
    assert error.value.details["phase"] == "SUBMITTED"
    assert error.value.details["retry_safe"] is False
    assert fallback_scope(error.value.as_dict(), "cli") == "stop"


def test_cli_missing_binary_is_pre_submit_failure(tmp_path, monkeypatch, service):
    monkeypatch.setenv("GEMINI_BATON_AGY", str(tmp_path / "missing-agy"))
    with pytest.raises(BridgeError) as error:
        WritingTransport(service.settings).cli(make_job(tmp_path), "4 Argon", "high")
    assert error.value.code == "cli_unavailable"
    assert error.value.details["phase"] == "PRE_SUBMIT"
    assert fallback_scope(error.value.as_dict(), "cli") == "channel"


def test_cli_prompt_budget_and_unsupported_files_are_pre_submit(tmp_path):
    assert cli_preflight("x" * 120_001, [])["eligible"] is False
    assert cli_preflight("x" * 120_001, [])["reasons"][0]["reason"] == "cli_prompt_too_long"
    with pytest.raises(BridgeError) as error:
        cli_prompt("x" * 120_001, [])
    assert error.value.code == "cli_input_unsupported"
    assert error.value.details["phase"] == "PRE_SUBMIT"
    assert error.value.details["retry_safe"] is True

    pdf = tmp_path / "selected.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    files = snapshot_files(selected_files([str(pdf)]), tmp_path / "inputs")
    preflight = cli_preflight("write", files)
    assert preflight["eligible"] is False
    assert preflight["reasons"][0]["reason"] == "unsupported_format"


def test_cli_preflight_filters_default_route_and_explicit_route_stops(service, tmp_path):
    pdf = tmp_path / "selected.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    job_id = start(service, tmp_path, file_paths=[str(pdf)])
    job = service.read(job_id)
    assert job["cli_preflight"]["eligible"] is False
    assert all(route[0] != "cli" for route in job["routes"])
    assert job["routes"][0][0] == "api"  # API can carry the PDF as inline data.

    explicit = service.submit("write", cwd=str(tmp_path), file_paths=[str(pdf)],
                              route="cli:4 Argon", wait_seconds=0)
    assert explicit["done"] and not explicit["ok"]
    assert explicit["error"]["code"] == "cli_input_unsupported"
    assert explicit["error"]["details"]["phase"] == "PRE_SUBMIT"
    assert explicit["attempts"] == []


def test_installed_sdk_effort_detection_is_explicit():
    from google.genai import types
    expected = tuple(
        effort for effort in ("high", "xhigh")
        if effort.upper() in types.ThinkingLevel.__members__
    )
    assert supported_api_efforts() == expected
    assert "high" in expected


def test_api_adapter_passes_supported_thinking_level_and_receipts(service, tmp_path, monkeypatch):
    from gemini_baton import writing_routes
    job = make_job(tmp_path, file_text=None)
    capture = {}

    class Backend:
        def __init__(self, **kwargs):
            capture.update(kwargs)

        def generate(self, **kwargs):
            capture.update(kwargs)
            return SimpleNamespace(model_version="gemini-4-argon-001", finish_reason="STOP",
                                   text="api draft", function_calls=[], usage={"total": 3},
                                   response_id="response-1")

    monkeypatch.setattr(writing_routes, "GeminiBackend", Backend)
    monkeypatch.setattr(writing_routes, "load_credential",
                        lambda: SimpleNamespace(key="unit-key", source="fake"))
    receipt = WritingTransport(service.settings).api(job, "4 Argon", "high")
    assert capture["model"] == "gemini-4-argon"
    assert capture["thinking_level"] == "high"
    assert capture["tools"] == []
    assert capture["retry_attempts"] == 1
    assert receipt["actual_model_version"] == "gemini-4-argon-001"
    assert receipt["model"] == "4 Argon"
    assert receipt["requested_model"] == "gemini-4-argon"
    assert receipt["requested_effort"] == "high"
    assert receipt["effective_effort"] == "high"
    assert receipt["effort_evidence"] == "api_thinking_level_request"
    assert receipt["thinking_level"] == "HIGH"
    assert receipt["model_evidence"] == "api_model_version"
    assert receipt["capability_status"]["thinking_level_supported"] is True


def test_api_xhigh_is_rejected_pre_submit_before_credentials(service, tmp_path, monkeypatch):
    if api_effort_supported("xhigh"):
        pytest.skip("installed SDK exposes xhigh")
    from gemini_baton import writing_routes

    def fail_credential():
        raise AssertionError("credential loading must not happen for an unsupported effort")

    monkeypatch.setattr(writing_routes, "load_credential", fail_credential)
    with pytest.raises(BridgeError) as error:
        WritingTransport(service.settings).api(make_job(tmp_path, file_text=None), "4 Neon", "xhigh")
    assert error.value.code == "reasoning_unavailable"
    assert error.value.details["phase"] == "PRE_SUBMIT"
    assert error.value.details["retry_safe"] is True
    assert error.value.details["supported_efforts"] == list(supported_api_efforts())
    assert fallback_scope(error.value.as_dict(), "api") == "model"


def test_explicit_api_xhigh_route_is_rejected_without_creating_a_job(service, tmp_path):
    if api_effort_supported("xhigh"):
        pytest.skip("installed SDK exposes xhigh")
    with pytest.raises(BridgeError) as error:
        service.submit("write", cwd=str(tmp_path), route="api:gemini-4-neon", wait_seconds=0)
    assert error.value.code == "reasoning_unavailable"
    assert error.value.details["phase"] == "PRE_SUBMIT"
    assert not service.root.exists() or not list(service.root.glob("write_*"))


def test_default_chain_skips_api_xhigh_without_submitting(service, tmp_path, monkeypatch):
    monkeypatch.setattr("gemini_baton.writing.api_effort_supported", lambda effort: effort == "high")
    job_id = start(service, tmp_path)
    adapter = Outcomes([
        BridgeError("MODEL_UNAVAILABLE", "cli1", details={"phase": "PRE_SUBMIT"}),
        BridgeError("MODEL_UNAVAILABLE", "cli2", details={"phase": "PRE_SUBMIT"}),
        BridgeError("MODEL_UNAVAILABLE", "cli3", details={"phase": "PRE_SUBMIT"}),
        BridgeError("MODEL_UNAVAILABLE", "cli4", details={"phase": "PRE_SUBMIT"}),
        BridgeError("gemini_api_error", "api1", details={"http_status": 429, "outcome": "confirmed_failure"}),
        BridgeError("gemini_api_error", "api2", details={"http_status": 429, "outcome": "confirmed_failure"}),
        BridgeError("gemini_api_error", "api3", details={"http_status": 429, "outcome": "confirmed_failure"}),
        "aistudio draft",
    ])
    service.execute(job_id, transport=adapter)
    result = service.result(job_id)
    assert [call[:2] for call in adapter.calls] == [
        ("cli", "4 Argon"), ("cli", "4 Carbon"), ("cli", "4 Neon"), ("cli", "3.8 Flash"),
        ("api", "4 Argon"), ("api", "4 Carbon"), ("api", "3.8 Flash"),
        ("aistudio", "4 Argon"),
    ]
    skipped = [attempt for attempt in result["attempts"] if attempt["status"] == "skipped"]
    assert len(skipped) == 1
    assert skipped[0]["model"] == "4 Neon"
    assert skipped[0]["effort"] == "xhigh"
    assert skipped[0]["phase"] == "PRE_SUBMIT"
    assert result["channel"] == "aistudio"


def test_browser_status_requires_reasoning_effort_support(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path, route="web:4 Argon")
    adapter = WritingTransport(service.settings)
    calls = []

    def web(command, **kwargs):
        calls.append(command)
        status = {"browser": {"supportedModels": ["4 Argon"], "concurrencySupported": True,
                              "maxConcurrentRequests": 3, "recoverySupported": True,
                              "quotaFallbackSupported": True,
                              "attachments": {"enabled": True}}}
        return status

    monkeypatch.setattr(adapter, "_web", web)
    with pytest.raises(BridgeError) as error:
        adapter.web(service.read(job_id), "4 Argon", "high")
    assert error.value.code == "web_unavailable"
    assert error.value.details["retry_safe"] is True
    assert calls == ["status"]
    assert fallback_scope(error.value.as_dict(), "web") == "channel"


def test_browser_effort_is_sent_and_receipted(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path, route="web:4 Neon")
    job = service.read(job_id)
    adapter = WritingTransport(service.settings)
    calls = []

    def web(command, **kwargs):
        calls.append((command, kwargs))
        if command == "status":
            return {"browser": {"supportedModels": ["4 Argon", "4 Carbon", "4 Neon", "3.8 Flash"],
                                "reasoningEffortSupported": True,
                                "concurrencySupported": True, "maxConcurrentRequests": 3,
                                "recoverySupported": True, "quotaFallbackSupported": True,
                                "attachments": {"enabled": True}}}
        return {"model": "4 Neon", "reasoning_effort": "xhigh",
                "answer": "draft\n\n" + __import__("gemini_baton.writing_routes",
                                                   fromlist=["completion_marker"]).completion_marker(job),
                "request_id": "web-effort", "attachments": []}

    monkeypatch.setattr(adapter, "_web", web)
    receipt = adapter.web(job, "4 Neon", "xhigh")
    assert calls[1][1]["payload"]["model"] == "4 Neon"
    assert calls[1][1]["payload"]["reasoning_effort"] == "xhigh"
    assert receipt["requested_effort"] == "xhigh"
    assert receipt["effective_effort"] == "xhigh"
    assert receipt["effort_evidence"] == "browser_receipt"
    assert receipt["capability_status"]["ui_effort_verified"] is True


def test_browser_missing_effort_receipt_is_unverified(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path, route="web:4 Argon")
    adapter = WritingTransport(service.settings)

    def web(command, **kwargs):
        if command == "status":
            return {"browser": {"supportedModels": ["4 Argon"], "reasoningEffortSupported": True,
                                "concurrencySupported": True, "maxConcurrentRequests": 3,
                                "recoverySupported": True, "quotaFallbackSupported": True,
                                "attachments": {"enabled": True}}}
        job = service.read(job_id)
        from gemini_baton.writing_routes import completion_marker
        return {"model": "4 Argon", "answer": "draft\n\n" + completion_marker(job),
                "request_id": "missing-effort", "attachments": []}

    monkeypatch.setattr(adapter, "_web", web)
    with pytest.raises(BridgeError) as error:
        adapter.web(service.read(job_id), "4 Argon", "high")
    assert error.value.code == "reasoning_unverified"
    assert error.value.details["phase"] == "SUBMITTED"
    assert fallback_scope(error.value.as_dict(), "web") == "stop"


def test_reasoning_unavailable_is_safe_only_before_submit():
    assert fallback_scope({"code": "REASONING_UNAVAILABLE",
                           "details": {"phase": "PRE_SUBMIT"}}, "web") == "model"
    assert fallback_scope({"code": "REASONING_UNAVAILABLE",
                           "details": {"phase": "SUBMITTED", "retry_safe": True}}, "web") == "stop"


def test_legacy_route_history_executes_without_cli_and_normalizes(service, tmp_path):
    job_id = start(service, tmp_path)
    job = service.read(job_id)
    job["routes"] = [
        ["aistudio", "4 Argon"], ["aistudio", "4 Hydrogen"], ["aistudio", "3.8 Flash"],
        ["api", "gemini-4-argon"], ["api", "gemini-4-hydrogen"], ["api", "gemini-3.8-flash"],
        ["web", "4 Argon"], ["web", "4 Hydrogen"], ["web", "3.8 Flash"],
    ]
    job["status"] = "queued"
    job["attempts"] = []
    service.save(job)
    adapter = Outcomes(["legacy aistudio draft"])
    service.execute(job_id, transport=adapter)
    result = service.result(job_id)
    assert adapter.calls == [("aistudio", "4 Argon", "high")]
    assert result["channel"] == "aistudio"
    assert result["route_policy"][0] == {"channel": "aistudio", "model": "4 Argon", "effort": "high"}
    assert result["route_policy"][3] == {"channel": "api", "model": "4 Argon", "effort": "high"}


def test_cli_started_attempt_is_never_replayed(service, tmp_path):
    job_id = start(service, tmp_path)
    job = service.read(job_id)
    job["attempts"] = [{"channel": "cli", "model": "4 Argon", "effort": "high", "status": "started"}]
    service.save(job)
    adapter = Outcomes([])
    service.execute(job_id, transport=adapter)
    result = service.result(job_id)
    assert result["status"] == "outcome_unknown"
    assert adapter.calls == []

@pytest.mark.parametrize('exit_code', [0, 1, 137])
def test_missing_cli_output_never_proves_safe_resubmission(exit_code):
    with pytest.raises(BridgeError) as error:
        cli_receipt_from_stream([], malformed=0, exit_code=exit_code,
                                requested_model='gemini-4-argon-high', requested_effort='high', cwd='/tmp')
    assert error.value.code == 'outcome_unknown'
    assert fallback_scope(error.value.as_dict(), 'cli') == 'stop'


def test_confirmed_cli_model_refusal_advances_after_init():
    events = [
        {'event': 'init', 'init': {'model': 'gemini-4-argon-high'}},
        {'event': 'result', 'result': {'status': 'ERROR', 'error': 'Model is not available right now'}},
    ]
    with pytest.raises(BridgeError) as error:
        cli_receipt_from_stream(events, malformed=0, exit_code=3,
                                requested_model='gemini-4-argon-high', requested_effort='high', cwd='/tmp')
    assert fallback_scope(error.value.as_dict(), 'cli') == 'model'


def test_cli_post_launch_io_error_retains_unknown_delivery(service, tmp_path, monkeypatch):
    install_fake_agy(tmp_path, monkeypatch)
    class Broken:
        pid = 1234
        def communicate(self, *args, **kwargs):
            raise OSError('pipe failed after sending')
    monkeypatch.setattr('gemini_baton.writing_routes.subprocess.Popen', lambda *args, **kwargs: Broken())
    with pytest.raises(BridgeError) as error:
        WritingTransport(service.settings).cli(make_job(tmp_path), '4 Argon')
    assert error.value.code == 'outcome_unknown'
    assert fallback_scope(error.value.as_dict(), 'cli') == 'stop'


def test_cli_preflight_counts_exact_selected_file_framing(tmp_path):
    source = tmp_path / 'reference.md'
    source.write_text('selected material')
    files = snapshot_files(selected_files([str(source)]), tmp_path / 'inputs')
    prompt = cli_prompt('task', files, prefix='PREFIX\n\n')
    assert cli_preflight('task', files, extra_bytes=len('PREFIX\n\n'.encode()))['estimated_bytes'] == len(prompt.encode())
    assert fallback_scope({'code': 'cli_input_unsupported', 'details': {'phase': 'PRE_SUBMIT'}}, 'cli') == 'channel'


def test_cli_model_specific_quota_advances_one_model():
    events = [{'event': 'init', 'init': {'model': 'gemini-4-argon-high'}},
              {'event': 'result', 'result': {'status': 'ERROR', 'error': 'Quota exceeded for gemini-4-argon-high'}}]
    with pytest.raises(BridgeError) as error:
        cli_receipt_from_stream(events, malformed=0, exit_code=3, requested_model='gemini-4-argon-high', requested_effort='high', cwd='/tmp')
    assert fallback_scope(error.value.as_dict(), 'cli') == 'model'
