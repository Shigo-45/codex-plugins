from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from gemini_baton.config import Settings
from gemini_baton.errors import BridgeError
from gemini_baton.writing import WritingService
from gemini_baton.writing_files import api_contents, selected_files, snapshot_files
from gemini_baton.writing_routes import (BROWSER_PROMPT_LIMIT, CLI_MODEL_IDS, OPT_IN_ROUTES,
                                         ROUTES, SYSTEM, WritingTransport, api_effort_supported,
                                         browser_preflight, browser_prompt,
                                         cli_receipt_from_stream, completion_marker,
                                         fallback_scope, parse_cli_stream, utf16_units)


TEST_JOB_ID = "write_" + "0" * 32


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_BATON_STATE_DIR", str(tmp_path / "state"))
    return WritingService(Settings.from_env(), worker_command=[sys.executable, "-c", "pass"])


class Routes:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []
        self.efforts = []

    def call(self, channel, job, model, effort):
        self.calls.append((channel, model))
        self.efforts.append(effort)
        value = next(self.outcomes)
        if isinstance(value, Exception):
            raise value
        return {"text": value, "model": model, "reasoning_effort": effort,
                "actual_model_version": model + ("-test" if channel == "api" else ""),
                "requested_effort": effort, "effective_effort": effort,
                "model_evidence": f"{channel}_test", "capability_status": {"test": True}}

    def cli(self, job, model, effort="high"):
        return self.call("cli", job, model, effort)

    def api(self, job, model, effort="high"):
        return self.call("api", job, model, effort)

    def web(self, job, model, effort="high"):
        return self.call("web", job, model, effort)

    def aistudio(self, job, model, effort="high"):
        return self.call("aistudio", job, model, effort)


def api_failure(code=429):
    return BridgeError("gemini_api_error", "confirmed rejection", details={"http_status": code, "outcome": "confirmed_failure"})


def start(service, tmp_path, prompt="Write from selected evidence", **kwargs):
    return service.submit(prompt, cwd=str(tmp_path), wait_seconds=0, **kwargs)["job_id"]


def prompt_with_effective_units(units, job_id=TEST_JOB_ID):
    prefix = utf16_units(browser_prompt("x", job_id=job_id)) - 1
    return "x" * (units - prefix)


def browser_answer(job, text):
    return text + "\n\n" + completion_marker(job)


def missing_marker_error(partial_text="truncated draft", *, delivery="confirmed_complete",
                         recovery_outcome="COMPLETED", request_id="browser-request"):
    return BridgeError("writing_incomplete", "Browser completed but the draft omitted its marker.", details={
        "reason": "completion_marker_missing",
        "partial_text": partial_text,
        "request_id": request_id,
        "phase": "SUBMITTED",
        "delivery": delivery,
        "recovery": {"outcome": recovery_outcome},
        "browser_receipt": {"outcome": recovery_outcome, "request_id": request_id},
    })


def test_default_route_policy_is_sixteen_with_expected_efforts():
    assert ROUTES == (
        ("cli", "4 Argon", "high"), ("cli", "4 Carbon", "high"),
        ("cli", "4 Neon", "xhigh"), ("cli", "3.8 Flash", "high"),
        ("api", "4 Argon", "high"), ("api", "4 Carbon", "high"),
        ("api", "4 Neon", "xhigh"), ("api", "3.8 Flash", "high"),
        ("aistudio", "4 Argon", "high"), ("aistudio", "4 Carbon", "high"),
        ("aistudio", "4 Neon", "xhigh"), ("aistudio", "3.8 Flash", "high"),
        ("web", "4 Argon", "high"), ("web", "4 Carbon", "high"),
        ("web", "4 Neon", "xhigh"), ("web", "3.8 Flash", "high"),
    )
    assert all(model != "4 Hydrogen" for _channel, model, _effort in ROUTES)
    assert OPT_IN_ROUTES == ("cli:4 Hydrogen", "api:4 Hydrogen",
                             "aistudio:4 Hydrogen", "web:4 Hydrogen")


def model_unavailable(message="model unavailable"):
    return BridgeError("MODEL_UNAVAILABLE", message, details={
        "phase": "PRE_SUBMIT", "retry_safe": True})


def test_fallback_is_owned_by_worker_in_exact_order(service, tmp_path, monkeypatch):
    monkeypatch.setattr("gemini_baton.writing.api_effort_supported", lambda effort: True)
    job_id = start(service, tmp_path)
    adapter = Routes([
        model_unavailable(), model_unavailable(), model_unavailable(), model_unavailable(),
        api_failure(), api_failure(), api_failure(), api_failure(503),
        model_unavailable(), model_unavailable(), model_unavailable(), model_unavailable(),
        model_unavailable(), model_unavailable(), model_unavailable(), "finished",
    ])
    service.execute(job_id, transport=adapter)
    result = service.result(job_id)
    expected = [
        ("cli", "4 Argon"), ("cli", "4 Carbon"), ("cli", "4 Neon"), ("cli", "3.8 Flash"),
        ("api", "4 Argon"), ("api", "4 Carbon"), ("api", "4 Neon"), ("api", "3.8 Flash"),
        ("aistudio", "4 Argon"), ("aistudio", "4 Carbon"), ("aistudio", "4 Neon"), ("aistudio", "3.8 Flash"),
        ("web", "4 Argon"), ("web", "4 Carbon"), ("web", "4 Neon"), ("web", "3.8 Flash"),
    ]
    assert adapter.calls == expected
    assert adapter.efforts == ["high", "high", "xhigh", "high"] * 4
    assert result["text"] == "finished"
    assert result["channel"] == "web"
    assert result["model"] == "3.8 Flash"
    assert result["effort"] == "high"
    assert result["actual_model_version"] == "3.8 Flash"
    assert result["done"] and result["ok"]
    assert [a["fallback"] for a in result["attempts"][:-1]] == ["model"] * 15


def test_cli_success_stops_chain_with_verified_effort(service, tmp_path):
    job_id = start(service, tmp_path)
    adapter = Routes(["first draft"])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("cli", "4 Argon")]
    assert adapter.efforts == ["high"]
    result = service.result(job_id)
    assert result["text"] == "first draft"
    assert result["channel"] == "cli"
    assert result["model"] == "4 Argon"
    assert result["effort"] == "high"
    assert result["requested_effort"] == "high"
    assert result["effective_effort"] == "high"
    assert result["attempts"][0]["capability_status"] == {"test": True}


def test_cli_model_failure_then_api_channel_failure_falls_back_to_browser(service, tmp_path):
    job_id = start(service, tmp_path)
    adapter = Routes([
        model_unavailable(), model_unavailable(), model_unavailable(), model_unavailable(),
        BridgeError("gemini_api_error", "DNS failure", details={"outcome": "not_submitted"}),
        model_unavailable(), model_unavailable(), model_unavailable(), model_unavailable(),
        "web draft",
    ])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [
        ("cli", "4 Argon"), ("cli", "4 Carbon"), ("cli", "4 Neon"), ("cli", "3.8 Flash"),
        ("api", "4 Argon"),
        ("aistudio", "4 Argon"), ("aistudio", "4 Carbon"), ("aistudio", "4 Neon"), ("aistudio", "3.8 Flash"),
        ("web", "4 Argon"),
    ]
    assert service.result(job_id)["channel"] == "web"


def test_whole_channel_failure_skips_other_models(service, tmp_path):
    job_id = start(service, tmp_path)
    adapter = Routes([
        BridgeError("RATE_LIMITED", "cli quota", details={"phase": "PRE_SUBMIT"}),
        BridgeError("credentials_missing", "local credentials absent"),
        BridgeError("WEB_QUOTA_EXHAUSTED", "studio quota", details={"phase": "PRE_SUBMIT"}),
        "web draft",
    ])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("cli", "4 Argon"), ("api", "4 Argon"),
                             ("aistudio", "4 Argon"), ("web", "4 Argon")]


@pytest.mark.parametrize("error", [
    BridgeError("gemini_api_error", "transport timeout", details={"outcome": "unknown"}),
    BridgeError("writing_incomplete", "MAX_TOKENS"),
    BridgeError("model_unverified", "missing model evidence"),
    BridgeError("gemini_api_error", "bad input", details={"http_status": 400, "outcome": "confirmed_failure"}),
])
def test_unresolved_or_content_failure_stops_without_replay(service, tmp_path, error):
    job_id = start(service, tmp_path, route="api:gemini-4-argon")
    adapter = Routes([error])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("api", "4 Argon")]
    result = service.result(job_id)
    assert result["done"] and not result["ok"]
    service.execute(job_id, transport=adapter)
    assert len(adapter.calls) == 1


def test_exhaustion_reports_all_sixteen_attempts_including_api_xhigh_skip(service, tmp_path):
    job_id = start(service, tmp_path)
    web = BridgeError("MODEL_UNAVAILABLE", "disabled", details={"retry_safe": True})
    adapter = Routes([web, web, web, web, api_failure(), api_failure(), api_failure(503),
                      web, web, web, web, web, web, web, web])
    service.execute(job_id, transport=adapter)
    result = service.result(job_id)
    assert result["error"]["code"] == "routes_exhausted"
    assert len(result["attempts"]) == 16
    skipped = [attempt for attempt in result["attempts"] if attempt["status"] == "skipped"]
    assert len(skipped) == 1
    assert skipped[0]["channel"] == "api"
    assert skipped[0]["model"] == "4 Neon"
    assert skipped[0]["effort"] == "xhigh"
    assert skipped[0]["reason"] == "reasoning_unavailable"
    assert skipped[0]["phase"] == "PRE_SUBMIT"


def test_explicit_route_pins_model(service, tmp_path):
    job_id = start(service, tmp_path, route="web:3.8 Flash")
    adapter = Routes([BridgeError("MODEL_UNAVAILABLE", "disabled", details={"retry_safe": True})])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("web", "3.8 Flash")]
    assert adapter.efforts == ["high"]


def test_explicit_aistudio_route_pins_provider(service, tmp_path):
    job_id = start(service, tmp_path, route="aistudio:4 Argon")
    adapter = Routes(["studio draft"])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("aistudio", "4 Argon")]
    assert service.result(job_id)["channel"] == "aistudio"
    assert ROUTES[0] == ("cli", "4 Argon", "high")


@pytest.mark.parametrize("route,expected", [
    ("aistudio:4 Hydrogen", ("aistudio", "4 Hydrogen", "high")),
    ("aistudio:gemini-4-hydrogen", ("aistudio", "4 Hydrogen", "high")),
    ("web:4 Hydrogen", ("web", "4 Hydrogen", "high")),
    ("web:gemini-4-hydrogen", ("web", "4 Hydrogen", "high")),
    ("api:gemini-4-hydrogen", ("api", "4 Hydrogen", "high")),
    ("api:4 Hydrogen", ("api", "4 Hydrogen", "high")),
    ("cli:4 Hydrogen", ("cli", "4 Hydrogen", "high")),
    ("cli:gemini-4-hydrogen-high", ("cli", "4 Hydrogen", "high")),
    ("cli:gemini-4-hydrogen", ("cli", "4 Hydrogen", "high")),
])
def test_hydrogen_pinned_route_aliases_canonicalize(service, tmp_path, route, expected):
    job_id = start(service, tmp_path, route=route)
    assert service.read(job_id)["routes"] == [list(expected)]
    adapter = Routes(["hydrogen draft"])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [(expected[0], expected[1])]
    assert adapter.efforts == [expected[2]]


def test_aistudio_hydrogen_receipt_is_exact(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path, route="aistudio:4 Hydrogen")
    job = service.read(job_id)
    adapter = WritingTransport(service.settings)
    monkeypatch.setattr(adapter, "_web", lambda command, **kwargs: studio_status() if command == "status" else {
        "provider": "aistudio", "billing_mode": "subscription", "model": "4 Hydrogen", "reasoning_effort": "high",
        "model_id": "gemini-4-hydrogen", "billing": {"mode": "subscription", "pro": True, "apiKeySelected": False},
        "answer": browser_answer(job, "hydrogen draft"), "request_id": "hydrogen-request", "attachments": [],
    })
    result = adapter.aistudio(job, "4 Hydrogen", "high")
    assert result["actual_model_version"] == "4 Hydrogen"
    assert result["model_id"] == "gemini-4-hydrogen"
    assert result["requested_effort"] == "high"
    assert result["effective_effort"] == "high"
    assert result["effort_evidence"] == "browser_receipt"
    assert result["billing"]["mode"] == "subscription"
    assert result["billing"]["pro"] is True
    assert result["billing"]["apiKeySelected"] is False
    assert result["provider"] == "aistudio"
    assert result["billing_mode"] == "subscription"


def test_conversation_followup_skips_api_and_reuses_web_handle(service, tmp_path, monkeypatch):
    conversation_id = "conv_followup123"
    job_id = start(service, tmp_path, conversation_id=conversation_id)
    job = service.read(job_id)
    assert job["conversation_id"] == conversation_id
    assert job["routes"] == [
        ["aistudio", "4 Argon", "high"], ["aistudio", "4 Carbon", "high"],
        ["aistudio", "4 Neon", "xhigh"], ["aistudio", "3.8 Flash", "high"],
        ["web", "4 Argon", "high"], ["web", "4 Carbon", "high"],
        ["web", "4 Neon", "xhigh"], ["web", "3.8 Flash", "high"],
    ]

    adapter = WritingTransport(service.settings)
    calls = []

    def web(command, **kwargs):
        calls.append((command, kwargs))
        if command == "status":
            return studio_status()
        return {"provider": "aistudio", "billing_mode": "subscription", "model": "4 Argon", "reasoning_effort": "high",
                "answer": browser_answer(job, "continued draft"), "conversation_id": conversation_id,
                "request_id": "followup-request", "attachments": []}

    monkeypatch.setattr(adapter, "_web", web)
    result = adapter.aistudio(job, "4 Argon", "high")
    assert result["text"] == "continued draft"
    assert completion_marker(job) not in result["text"]
    assert result["conversation_id"] == conversation_id
    assert calls[1][1]["payload"]["conversation_id"] == conversation_id
    assert calls[1][1]["payload"]["close_after"] is False
    assert calls[1][1]["payload"]["reasoning_effort"] == "high"


def test_aistudio_permission_failure_is_retained_without_switching_provider(service, tmp_path):
    job_id = start(service, tmp_path, route="aistudio:4 Argon")
    adapter = Routes([BridgeError("PERMISSION_DENIED", "account access denied", details={"phase": "SUBMITTED"})])
    service.execute(job_id, transport=adapter)
    result = service.result(job_id)
    assert adapter.calls == [("aistudio", "4 Argon")]
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert result["status"] == "failed"


def test_idempotency_reuses_job_and_rejects_different_material(service, tmp_path, monkeypatch):
    launch = Mock()
    launch.return_value.pid = 1
    monkeypatch.setattr("gemini_baton.writing.subprocess.Popen", launch)
    job_id = start(service, tmp_path, request_id="stable-task")
    assert start(service, tmp_path, request_id="stable-task") == job_id
    assert launch.call_count == 1
    with pytest.raises(BridgeError, match="different inputs"):
        service.submit("different task", cwd=str(tmp_path), request_id="stable-task", wait_seconds=0)


def test_snapshots_keep_inputs_identical_across_fallback(service, tmp_path):
    source = tmp_path / "reference.md"
    source.write_text("original evidence")
    job_id = start(service, tmp_path, file_paths=[str(source)])
    source.write_text("changed evidence")
    job = service.read(job_id)
    contents = api_contents(job["prompt"], job["files"])
    assert "original evidence" in str(contents)
    assert "changed evidence" not in str(contents)
    assert not (Path(job["files"][0]["snapshot"]).stat().st_mode & 0o077)


def test_pdf_and_office_are_carried_as_selected_input(tmp_path):
    pdf = tmp_path / "selected.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    docx = tmp_path / "draft.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", '<document><p><t>Evidence from draft</t></p></document>')
    files = snapshot_files(selected_files([str(pdf), str(docx)]), tmp_path / "inputs")
    contents = api_contents("write", files)
    parts = contents[0]["parts"]
    inline = next(p["inlineData"] for p in parts if "inlineData" in p)
    assert base64.b64decode(inline["data"]) == pdf.read_bytes()
    assert "Evidence from draft" in str(parts)


def test_office_visuals_use_original_web_attachment_and_natural_order(tmp_path):
    from gemini_baton.writing_files import office_text
    pptx = tmp_path / "figures.pptx"
    with zipfile.ZipFile(pptx, "w") as archive:
        for number in [1, 2, 10]:
            archive.writestr(f"ppt/slides/slide{number}.xml", f"<slide><t>Page {number}</t></slide>")
        archive.writestr("ppt/media/image1.png", b"image evidence")
    selected = selected_files([str(pptx)])
    text = office_text(selected[0]["data"], ".pptx", require_text_only=False)
    assert text.index("Page 1") < text.index("Page 2") < text.index("Page 10")
    files = snapshot_files(selected, tmp_path / "inputs")
    with pytest.raises(BridgeError) as error:
        api_contents("write", files)
    assert error.value.code == "api_input_unsupported"
    assert error.value.details["reason"] == "office_visual_or_embedded_content"


def test_default_submission_preflights_api_and_starts_with_browser_route_for_visual_office(service, tmp_path):
    pptx = tmp_path / "figures.pptx"
    with zipfile.ZipFile(pptx, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", "<slide><t>Page 1</t></slide>")
        archive.writestr("ppt/media/image1.png", b"image evidence")
    job_id = start(service, tmp_path, file_paths=[str(pptx)])
    job = service.read(job_id)
    assert job["routes"][0] == ["aistudio", "4 Argon", "high"]
    assert job["api_preflight"]["eligible"] is False
    assert job["api_preflight"]["reasons"][0]["reason"] == "office_visual_or_embedded_content"
    assert job["cli_preflight"]["eligible"] is False
    assert job["cli_preflight"]["reasons"][0]["reason"] == "office_visual_or_embedded_content"
    adapter = Routes(["studio draft"])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("aistudio", "4 Argon")]


def test_unsupported_api_input_falls_back_from_aistudio_directly_to_web(service, tmp_path):
    pptx = tmp_path / "figures.pptx"
    with zipfile.ZipFile(pptx, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", "<slide><t>Page 1</t></slide>")
        archive.writestr("ppt/media/image1.png", b"image evidence")
    job_id = start(service, tmp_path, file_paths=[str(pptx)])
    adapter = Routes([BridgeError("UPLOAD_UNSUPPORTED", "studio rejected format", details={"phase": "PRE_SUBMIT"}),
                      "web draft"])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("aistudio", "4 Argon"), ("web", "4 Argon")]
    assert service.result(job_id)["text"] == "web draft"


@pytest.mark.parametrize("name", [".env", ".env-local", "cookies.json", "credentials.json", "private.key", "auth.json"])
def test_sensitive_paths_are_rejected_before_model_calls(tmp_path, name):
    path = tmp_path / name
    path.write_text("sensitive")
    with pytest.raises(BridgeError):
        selected_files([str(path)])


def test_symlink_to_sensitive_file_is_rejected(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("sensitive")
    alias = tmp_path / "reference.txt"
    alias.symlink_to(secret)
    with pytest.raises(BridgeError):
        selected_files([str(alias)])


def test_credential_pattern_and_directory_are_rejected(service, tmp_path):
    with pytest.raises(BridgeError, match="credential pattern"):
        service.submit("AIza" + "x" * 30, cwd=str(tmp_path))
    with pytest.raises(BridgeError):
        selected_files([str(tmp_path)])


def test_worker_restart_never_replays_started_route(service, tmp_path):
    job_id = start(service, tmp_path)
    job = service.read(job_id)
    job["attempts"] = [{"channel": "api", "model": "gemini-4-argon", "status": "started"}]
    service.save(job)
    adapter = Routes([])
    service.execute(job_id, transport=adapter)
    assert service.result(job_id)["status"] == "outcome_unknown"
    assert adapter.calls == []


def test_dead_worker_receipt_becomes_unknown(service, tmp_path):
    job_id = start(service, tmp_path)
    job = service.read(job_id)
    job["created_at"] = time.time() - 30
    service.save(job)
    assert service.result(job_id)["error"]["code"] == "worker_lost"


def test_detached_job_survives_submitting_process_exit(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("GEMINI_BATON_STATE_DIR", str(state))
    worker = Path(__file__).parent / "support/fake_writing_worker.py"
    code = """import json, sys
from gemini_baton.config import Settings
from gemini_baton.writing import WritingService
s = WritingService(Settings.from_env(), worker_command=[sys.executable, sys.argv[1]])
print(json.dumps(s.submit('write', cwd=sys.argv[2], wait_seconds=0)))
"""
    completed = subprocess.run([sys.executable, "-c", code, str(worker.resolve()), str(tmp_path)],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    job_id = json.loads(completed.stdout)["job_id"]
    result = WritingService(Settings.from_env()).result(job_id, wait_seconds=5)
    assert result["text"] == "writing worker survived"
    assert result["status"] == "succeeded"


@pytest.mark.parametrize("outcome", ["SETTLED_UNKNOWN", "DETACHED_GENERATING", "COMPLETED", "OUTCOME_UNKNOWN"])
def test_web_unresolved_recovery_stops_even_with_quota_error(outcome):
    error = {"code": "RATE_LIMITED", "details": {"retry_safe": True, "recovery": {"outcome": outcome}}}
    assert fallback_scope(error, "web") == "stop"


def test_web_confirmed_failure_can_fallback():
    assert fallback_scope({"code": "RATE_LIMITED", "details": {"recovery": {"outcome": "FAILED"}}}, "web") == "model"
    assert fallback_scope({"code": "OUTCOME_UNKNOWN", "details": {"recovery": {
        "outcome": "FAILED", "failure": "RATE_LIMITED"}}}, "web") == "model"

@pytest.mark.parametrize("outcome,expected", [
    ("FAILED", "model"),
    ("UPLOAD_ABANDONED", "model"),
    ("OUTCOME_UNKNOWN", "stop"),
    ("DETACHED_GENERATING", "stop"),
    ("SETTLED_UNKNOWN", "stop"),
    ("COMPLETED", "stop"),
])
def test_aistudio_model_unavailable_uses_only_confirmed_recovery(outcome, expected):
    error = {"code": "MODEL_UNAVAILABLE", "details": {
        "phase": "SUBMITTED", "recovery": {"outcome": outcome},
    }}
    assert fallback_scope(error, "aistudio") == expected


def test_aistudio_model_unavailable_without_terminal_evidence_stops():
    assert fallback_scope({"code": "MODEL_UNAVAILABLE", "details": {"phase": "SUBMITTED"}}, "aistudio") == "stop"


def test_aistudio_model_unavailable_confirmed_failure_advances_to_carbon(service, tmp_path):
    job_id = start(service, tmp_path)
    unavailable = BridgeError(
        "MODEL_UNAVAILABLE",
        "Model isn't available right now. Please wait a minute and try again.",
        details={"phase": "SUBMITTED", "recovery": {"outcome": "FAILED", "failure": "MODEL_UNAVAILABLE"}},
    )
    adapter = Routes([model_unavailable(), model_unavailable(), model_unavailable(), model_unavailable(),
                      api_failure(), api_failure(), api_failure(503),
                      unavailable, "carbon draft"])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [
        ("cli", "4 Argon"), ("cli", "4 Carbon"), ("cli", "4 Neon"), ("cli", "3.8 Flash"),
        ("api", "4 Argon"), ("api", "4 Carbon"), ("api", "3.8 Flash"),
        ("aistudio", "4 Argon"), ("aistudio", "4 Carbon"),
    ]
    result = service.result(job_id)
    assert result["channel"] == "aistudio"
    assert result["model"] == "4 Carbon"
    assert result["text"] == "carbon draft"


def test_aistudio_model_unavailable_unknown_recovery_holds_chain(service, tmp_path):
    job_id = start(service, tmp_path)
    unavailable = BridgeError(
        "MODEL_UNAVAILABLE",
        "Model isn't available right now. Please wait a minute and try again.",
        details={"phase": "SUBMITTED", "recovery": {"outcome": "OUTCOME_UNKNOWN"}},
    )
    adapter = Routes([model_unavailable(), model_unavailable(), model_unavailable(), model_unavailable(),
                      api_failure(), api_failure(), api_failure(503),
                      unavailable, "must not run"])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [
        ("cli", "4 Argon"), ("cli", "4 Carbon"), ("cli", "4 Neon"), ("cli", "3.8 Flash"),
        ("api", "4 Argon"), ("api", "4 Carbon"), ("api", "3.8 Flash"),
        ("aistudio", "4 Argon"),
    ]
    assert service.result(job_id)["ok"] is False


def test_aistudio_model_unavailable_receipt_recovers_before_model_fallback(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path, route="aistudio:4 Argon")
    job = service.read(job_id)
    adapter = WritingTransport(service.settings)
    calls = []

    def web(command, **kwargs):
        calls.append(command)
        if command == "status":
            return studio_status()
        if command == "ask":
            return {
                "error": {
                    "code": "MODEL_UNAVAILABLE",
                    "message": "Model isn't available right now. Please wait a minute and try again.",
                    "phase": "SUBMITTED",
                    "retry_safe": False,
                },
                "request_id": "ais-model-unavailable",
            }
        return {"recovery": {"outcome": "FAILED", "failure": "MODEL_UNAVAILABLE"}}

    monkeypatch.setattr(adapter, "_web", web)
    with pytest.raises(BridgeError) as error:
        adapter.aistudio(job, "4 Argon")
    assert error.value.code == "MODEL_UNAVAILABLE"
    assert error.value.details["recovery"]["outcome"] == "FAILED"
    assert calls == ["status", "ask", "recover"]
    assert fallback_scope(error.value.as_dict(), "aistudio") == "model"



def web_status():
    return {"browser": {"supportedModels": ["4 Argon", "4 Carbon", "4 Neon", "3.8 Flash", "4 Hydrogen"],
                        "reasoningEffortSupported": True,
                        "concurrencySupported": True,
                        "maxConcurrentRequests": 3, "recoverySupported": True, "quotaFallbackSupported": True,
                        "attachments": {"enabled": True}}}


@pytest.mark.parametrize("proxy", [None, "", "direct", "http://127.0.0.1:10808"])
def test_web_process_preserves_explicit_network_selection_and_excludes_api_credentials(service, monkeypatch, proxy):
    monkeypatch.delenv("GEMINI_WEB_BRIDGE_PROXY", raising=False)
    if proxy is not None:
        monkeypatch.setenv("GEMINI_WEB_BRIDGE_PROXY", proxy)
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BATON_ENV_FILE"):
        monkeypatch.setenv(name, "test-credential-placeholder")
    process = Mock()
    process.communicate.return_value = ('{"status":"completed"}', None)
    popen = Mock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", popen)
    adapter = WritingTransport(service.settings)
    monkeypatch.setattr(adapter, "_web_command", lambda: ["node", "fake-web-cli.mjs"])
    assert adapter._web("status")["status"] == "completed"
    env = popen.call_args.kwargs["env"]
    if proxy is None:
        assert "GEMINI_WEB_BRIDGE_PROXY" not in env
    else:
        assert env["GEMINI_WEB_BRIDGE_PROXY"] == proxy
    assert not {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BATON_ENV_FILE"}.intersection(env)


def studio_status():
    status = web_status()
    status["browser"]["providers"] = {
        "aistudio": {"supportedModels": ["4 Argon", "4 Carbon", "4 Neon", "3.8 Flash", "4 Hydrogen"],
                     "attachments": {"enabled": False}}}
    return status


@pytest.mark.parametrize("receipt", [
    {"provider": "aistudio", "billing_mode": "paid_api_key"},
    {"provider": "gemini", "billing_mode": "subscription"},
    {"provider": "aistudio"},
])
def test_aistudio_requires_subscription_and_provider_receipts(service, tmp_path, monkeypatch, receipt):
    job_id = start(service, tmp_path, route="aistudio:4 Argon")
    adapter = WritingTransport(service.settings)
    monkeypatch.setattr(adapter, "_web", lambda command, **kwargs: studio_status() if command == "status" else
                        {"model": "4 Argon", "reasoning_effort": "high", "answer": "draft", **receipt})
    with pytest.raises(BridgeError) as error:
        adapter.aistudio(service.read(job_id), "4 Argon")
    assert error.value.code == "billing_unverified"


@pytest.mark.parametrize("actual_model", ["4 Argon", "gemini-4-argon"])
def test_aistudio_passes_explicit_provider_and_preserves_billing_evidence(service, tmp_path, monkeypatch, actual_model):
    job_id = start(service, tmp_path, route="aistudio:4 Argon")
    job = service.read(job_id)
    adapter = WritingTransport(service.settings)
    calls = []

    def web(command, **kwargs):
        calls.append((command, kwargs))
        return studio_status() if command == "status" else {
            "provider": "aistudio", "billing_mode": "subscription", "model": actual_model, "reasoning_effort": "high",
            "answer": browser_answer(job, "verified draft"), "request_id": "studio-request", "attachments": [],
        }

    monkeypatch.setattr(adapter, "_web", web)
    result = adapter.aistudio(job, "4 Argon")
    assert calls[1][1]["payload"]["provider"] == "aistudio"
    assert calls[1][1]["payload"]["close_after"] is True
    assert result["provider"] == "aistudio"
    assert result["billing_mode"] == "subscription"
    assert result["actual_model_version"] == actual_model


def test_aistudio_old_bridge_rejected_before_ask(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path, route="aistudio:4 Argon")
    adapter = WritingTransport(service.settings)
    calls = []
    monkeypatch.setattr(adapter, "_web", lambda command, **kwargs: calls.append(command) or web_status())
    with pytest.raises(BridgeError) as error:
        adapter.aistudio(service.read(job_id), "4 Argon")
    assert error.value.code == "web_unavailable"
    assert calls == ["status"]


def test_aistudio_unsupported_attachments_rejected_before_ask(service, tmp_path, monkeypatch):
    source = tmp_path / "selected.md"
    source.write_text("selected evidence")
    job_id = start(service, tmp_path, route="aistudio:4 Argon", file_paths=[str(source)])
    adapter = WritingTransport(service.settings)
    calls = []
    monkeypatch.setattr(adapter, "_web", lambda command, **kwargs: calls.append(command) or studio_status())
    with pytest.raises(BridgeError) as error:
        adapter.aistudio(service.read(job_id), "4 Argon")
    assert error.value.code == "UPLOAD_UNSUPPORTED"
    assert calls == ["status"]


@pytest.mark.parametrize("receipt_matches", [True, False])
def test_aistudio_uploads_snapshot_and_checks_attachment_receipt(service, tmp_path, monkeypatch, receipt_matches):
    source = tmp_path / "context.md"
    source.write_text("selected context")
    job_id = start(service, tmp_path, route="aistudio:4 Argon", file_paths=[str(source)])
    job = service.read(job_id)
    adapter = WritingTransport(service.settings)
    calls = []

    def web(command, **kwargs):
        calls.append((command, kwargs))
        if command == "status":
            status = studio_status()
            status["browser"]["providers"]["aistudio"]["attachments"]["enabled"] = True
            return status
        return {"provider": "aistudio", "billing_mode": "subscription", "model": "4 Argon", "reasoning_effort": "high",
                "answer": browser_answer(job, "context-based draft"), "request_id": "studio-upload",
                "attachments": [{"name": "context.md", "size": source.stat().st_size}] if receipt_matches else []}

    monkeypatch.setattr(adapter, "_web", web)
    if receipt_matches:
        result = adapter.aistudio(job, "4 Argon")
        assert result["attachments"] == [{"name": "context.md", "size": source.stat().st_size}]
    else:
        with pytest.raises(BridgeError) as error:
            adapter.aistudio(job, "4 Argon")
        assert error.value.code == "attachments_unverified"
    payload = calls[1][1]["payload"]
    assert payload["provider"] == "aistudio"
    assert payload["file_paths"] == [job["files"][0]["snapshot"]]
    assert Path(payload["file_paths"][0]).read_text() == "selected context"


def test_web_adapter_recovers_own_request_before_deciding(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path)
    adapter = WritingTransport(service.settings)
    calls = []

    def web(command, **kwargs):
        calls.append((command, kwargs))
        if command == "status":
            return web_status()
        if command == "ask":
            return {"error": {"code": "OUTCOME_UNKNOWN", "phase": "SUBMITTED"}, "request_id": "own-request"}
        return {"recovery": {"outcome": "DETACHED_GENERATING"}}

    monkeypatch.setattr(adapter, "_web", web)
    with pytest.raises(BridgeError) as error:
        adapter.web(service.read(job_id), "4 Argon")
    assert calls[-1] == ("recover", {"request_id": "own-request", "timeout": 60})
    assert fallback_scope(error.value.as_dict(), "web") == "stop"


def test_web_attachment_receipt_is_verified(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path)
    adapter = WritingTransport(service.settings)
    monkeypatch.setattr(adapter, "_web", lambda command, **kwargs: web_status() if command == "status" else
                        {"model": "4 Argon", "reasoning_effort": "high", "answer": "draft", "attachments": [{"name": "extra.md", "size": 2}]})
    with pytest.raises(BridgeError, match="attachment receipt"):
        adapter.web(service.read(job_id), "4 Argon")


def test_recovery_failure_retains_original_request_receipt(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path)
    adapter = WritingTransport(service.settings)

    def web(command, **kwargs):
        if command == "status":
            return web_status()
        if command == "ask":
            return {"error": {"code": "OUTCOME_UNKNOWN", "phase": "SUBMITTED"},
                    "request_id": "original-request", "conversation_id": "original-conversation"}
        raise BridgeError("outcome_unknown", "recovery transport failed")

    monkeypatch.setattr(adapter, "_web", web)
    with pytest.raises(BridgeError) as error:
        adapter.web(service.read(job_id), "4 Argon")
    details = error.value.details
    assert details["request_id"] == "original-request"
    assert details["conversation_id"] == "original-conversation"
    assert details["recovery_error"]["code"] == "outcome_unknown"
    assert fallback_scope(error.value.as_dict(), "web") == "stop"


def test_api_adapter_has_no_tools_and_disables_sdk_retries(service, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from gemini_baton import writing_routes
    capture = {}

    class Backend:
        def __init__(self, **kwargs):
            capture.update(kwargs)

        def generate(self, **kwargs):
            capture.update(kwargs)
            return SimpleNamespace(model_version="gemini-4-argon-001", finish_reason="STOP", text="draft",
                                   function_calls=[], usage={}, response_id="response")

    monkeypatch.setattr(writing_routes, "GeminiBackend", Backend)
    monkeypatch.setattr(writing_routes, "load_credential", lambda: SimpleNamespace(key="unit-key", source="fake"))
    job_id = start(service, tmp_path)
    result = WritingTransport(service.settings).api(service.read(job_id), "gemini-4-argon")
    assert capture["tools"] == []
    assert capture["retry_attempts"] == 1
    assert result["actual_model_version"] == "gemini-4-argon-001"


def test_web_cli_adapter_uses_same_files_and_strips_api_credentials(service, tmp_path, monkeypatch):
    import shutil
    if not shutil.which("node"):
        pytest.skip("Node.js is required for the Web adapter integration")
    cli = tmp_path / "fake-web.mjs"
    cli.write_text('''
import {readFileSync} from "node:fs";
const command=process.argv[2];
if(command==="status") {
 console.log(JSON.stringify({browser:{supportedModels:["4 Argon","4 Carbon","4 Neon","3.8 Flash","4 Hydrogen"],
 reasoningEffortSupported:true,
 concurrencySupported:true,maxConcurrentRequests:3,recoverySupported:true,
 quotaFallbackSupported:true,attachments:{enabled:true}}}));
} else {
 const input=JSON.parse(readFileSync(0,"utf8"));
 if(process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY || process.env.GEMINI_BATON_ENV_FILE) process.exit(9);
 const source=readFileSync(input.file_paths[0],"utf8");
 const marker=input.prompt.trimEnd().split("\\n").at(-1);
 console.log(JSON.stringify({model:input.model,reasoning_effort:input.reasoning_effort,
 answer:source+"\\n\\n"+marker,
 attachments:[{name:"source.md",size:Buffer.byteLength(source)}],request_id:"web-receipt"}));
}
''')
    monkeypatch.setenv("GEMINI_BATON_WEB_CLI", str(cli))
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-private-value")
    monkeypatch.setenv("GOOGLE_API_KEY", "another-unit-test-private-value")
    monkeypatch.setenv("GEMINI_BATON_ENV_FILE", "/do-not-read/.env")
    source = tmp_path / "source.md"
    source.write_text("selected immutable reference")
    job_id = start(service, tmp_path, file_paths=[str(source)])
    source.write_text("later modification")

    class RealWebFakeApi(WritingTransport):
        def cli(self, job, model, effort="high"):
            raise BridgeError("cli_unavailable", "not installed for this test",
                              details={"phase": "PRE_SUBMIT", "retry_safe": True})

        def api(self, job, model, effort="high"):
            raise BridgeError("credentials_missing", "local credentials absent")

    service.execute(job_id, transport=RealWebFakeApi(service.settings))
    result = service.result(job_id)
    assert result["text"] == "selected immutable reference"
    assert result["channel"] == "web"
    assert result["request_id"] == "web-receipt"
    assert result["channel"] == "web"
    assert result["effort"] == "high"
    assert result["effective_effort"] == "high"
    assert len(result["attempts"]) == 4
    assert [a["channel"] for a in result["attempts"]] == ["cli", "api", "aistudio", "web"]
    assert result["attempts"][0]["fallback"] == "channel"
    assert result["attempts"][1]["fallback"] == "channel"
    assert result["attempts"][2]["fallback"] == "channel"


def test_relative_credential_path_resolved_before_worker_cwd_change(service, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_BATON_ENV_FILE", "selected-config.env")
    launch = Mock()
    launch.return_value.pid = 1
    monkeypatch.setattr("gemini_baton.writing.subprocess.Popen", launch)
    workspace = tmp_path / "other"
    workspace.mkdir()
    start(service, workspace)
    assert launch.call_args.kwargs["env"]["GEMINI_BATON_ENV_FILE"] == str(tmp_path / "selected-config.env")


def test_cli_result_reads_saved_job(service, tmp_path, monkeypatch, capsys):
    from gemini_baton.cli import main
    job_id = start(service, tmp_path)
    service.execute(job_id, transport=Routes(["CLI draft"]))
    assert main(["write-result", job_id]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["text"] == "CLI draft"
    assert result["done"]

@pytest.mark.parametrize("error", [
    BridgeError("PERMISSION_DENIED", "confirmed access rejection", details={"phase": "SUBMITTED", "recovery": {"outcome": "FAILED", "failure": "PERMISSION_DENIED"}}),
    BridgeError("BILLING_UNVERIFIED", "subscription not verified", details={"phase": "PRE_SUBMIT"}),
    BridgeError("PAID_KEY_ACTIVE", "paid key selected", details={"phase": "PRE_SUBMIT"}),
    BridgeError("UPLOAD_UNSUPPORTED", "format unsupported", details={"phase": "PRE_SUBMIT"}),
])
def test_confirmed_studio_failure_falls_back_to_web(service, tmp_path, error):
    job_id = start(service, tmp_path)
    adapter = Routes([
        BridgeError("RATE_LIMITED", "cli quota", details={"phase": "PRE_SUBMIT"}),
        BridgeError("credentials_missing", "local credentials absent"),
        error, "web draft",
    ])
    service.execute(job_id, transport=adapter)
    result = service.result(job_id)
    assert adapter.calls == [("cli", "4 Argon"), ("api", "4 Argon"),
                             ("aistudio", "4 Argon"), ("web", "4 Argon")]
    assert result["channel"] == "web"
    assert result["text"] == "web draft"


@pytest.mark.parametrize("outcome", ["OUTCOME_UNKNOWN", "SETTLED_UNKNOWN", "DETACHED_GENERATING", "COMPLETED"])
def test_unresolved_studio_failure_holds_default_chain(service, tmp_path, outcome):
    job_id = start(service, tmp_path)
    adapter = Routes([
        BridgeError("RATE_LIMITED", "cli quota", details={"phase": "PRE_SUBMIT"}),
        BridgeError("credentials_missing", "local credentials absent"),
        BridgeError("PERMISSION_DENIED", "unsettled", details={"phase": "SUBMITTED", "recovery": {"outcome": outcome}}),
    ])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("cli", "4 Argon"), ("api", "4 Argon"), ("aistudio", "4 Argon")]
    assert service.result(job_id)["ok"] is False


def test_default_policy_change_preserves_existing_request_receipt(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path, request_id="saved-policy")
    old_routes = service.read(job_id)["routes"]
    monkeypatch.setattr("gemini_baton.writing.ROUTES", tuple(reversed(ROUTES)))
    assert start(service, tmp_path, request_id="saved-policy") == job_id
    assert service.read(job_id)["routes"] == old_routes


def test_overlong_browser_prompt_skips_only_browser_routes(service, tmp_path):
    prompt = prompt_with_effective_units(BROWSER_PROMPT_LIMIT + 1)
    job_id = start(service, tmp_path, prompt=prompt)
    job = service.read(job_id)
    assert job["browser_preflight"]["eligible"] is False
    assert job["browser_preflight"]["effective_chars"] == BROWSER_PROMPT_LIMIT + 1
    assert job["browser_preflight"]["reasons"] == [{
        "reason": "prompt_too_long", "limit": BROWSER_PROMPT_LIMIT,
        "effective_chars": BROWSER_PROMPT_LIMIT + 1, "overflow_units": 1,
    }]
    assert job["browser_preflight"]["skipped_routes"] == [
        ["aistudio", "4 Argon", "high"], ["aistudio", "4 Carbon", "high"],
        ["aistudio", "4 Neon", "xhigh"], ["aistudio", "3.8 Flash", "high"],
        ["web", "4 Argon", "high"], ["web", "4 Carbon", "high"],
        ["web", "4 Neon", "xhigh"], ["web", "3.8 Flash", "high"]]
    assert job["routes"] == [
        ["cli", "4 Argon", "high"], ["cli", "4 Carbon", "high"],
        ["cli", "4 Neon", "xhigh"], ["cli", "3.8 Flash", "high"],
        ["api", "4 Argon", "high"], ["api", "4 Carbon", "high"],
        ["api", "4 Neon", "xhigh"], ["api", "3.8 Flash", "high"],
    ]

    adapter = Routes([model_unavailable(), model_unavailable(), model_unavailable(), model_unavailable(),
                      "api draft"])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [
        ("cli", "4 Argon"), ("cli", "4 Carbon"), ("cli", "4 Neon"), ("cli", "3.8 Flash"),
        ("api", "4 Argon"),
    ]
    result = service.result(job_id)
    assert result["channel"] == "api"
    assert result["text"] == "api draft"


def test_exact_browser_prompt_boundary_keeps_all_sixteen_routes(service, tmp_path):
    prompt = prompt_with_effective_units(BROWSER_PROMPT_LIMIT)
    job_id = start(service, tmp_path, prompt=prompt)
    job = service.read(job_id)
    assert job["browser_preflight"]["eligible"] is True
    assert job["browser_preflight"]["effective_chars"] == BROWSER_PROMPT_LIMIT
    assert len(job["routes"]) == 16
    assert job["routes"][0] == ["cli", "4 Argon", "high"]
    assert job["routes"][8] == ["aistudio", "4 Argon", "high"]

    adapter = Routes(["cli draft"])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("cli", "4 Argon")]
    assert service.result(job_id)["channel"] == "cli"


def test_browser_prompt_limit_counts_utf16_units_not_code_points(service, tmp_path):
    astral = "😀" * 7990 + "x" * 20
    assert len(astral) == 8010
    assert len(astral.encode("utf-16-le")) // 2 == BROWSER_PROMPT_LIMIT
    marker_prefix = utf16_units(browser_prompt("x", job_id=TEST_JOB_ID)) - 1
    assert browser_preflight(astral, job_id=TEST_JOB_ID)["eligible"] is False
    assert browser_preflight(astral, job_id=TEST_JOB_ID)["effective_chars"] == marker_prefix + 16000

    valid = "😀" * 7600 + "x" * 100
    assert len(valid) < BROWSER_PROMPT_LIMIT
    assert browser_preflight(valid, job_id=TEST_JOB_ID)["eligible"] is True

    job_id = start(service, tmp_path, prompt=astral)
    job = service.read(job_id)
    assert job["routes"] == [
        ["cli", "4 Argon", "high"], ["cli", "4 Carbon", "high"],
        ["cli", "4 Neon", "xhigh"], ["cli", "3.8 Flash", "high"],
        ["api", "4 Argon", "high"], ["api", "4 Carbon", "high"],
        ["api", "4 Neon", "xhigh"], ["api", "3.8 Flash", "high"],
    ]
    adapter = Routes([model_unavailable(), model_unavailable(), model_unavailable(), model_unavailable(),
                      "api draft"])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [
        ("cli", "4 Argon"), ("cli", "4 Carbon"), ("cli", "4 Neon"), ("cli", "3.8 Flash"),
        ("api", "4 Argon"),
    ]


def test_overlong_conversation_continuation_stops_before_browser_submission(service, tmp_path):
    conversation_id = "conv_overlong123"
    prompt = prompt_with_effective_units(BROWSER_PROMPT_LIMIT + 1)
    result = service.submit(prompt, cwd=str(tmp_path), conversation_id=conversation_id, wait_seconds=0)
    assert result["done"] and not result["ok"]
    assert result["error"]["code"] == "browser_prompt_too_long"
    assert result["error"]["details"]["reason"] == "prompt_too_long"
    assert result["error"]["details"]["conversation_id"] == conversation_id
    assert result["browser_preflight"]["continuation"] is True
    assert result["browser_preflight"]["skipped_routes"] == [
        ["aistudio", "4 Argon", "high"], ["aistudio", "4 Carbon", "high"],
        ["aistudio", "4 Neon", "xhigh"], ["aistudio", "3.8 Flash", "high"],
        ["web", "4 Argon", "high"], ["web", "4 Carbon", "high"],
        ["web", "4 Neon", "xhigh"], ["web", "3.8 Flash", "high"]]
    assert result["attempts"] == []

    adapter = Routes([])
    service.execute(result["job_id"], transport=adapter)
    assert adapter.calls == []


def test_continuation_payload_uses_effective_browser_prompt_at_boundary(service, tmp_path, monkeypatch):
    conversation_id = "conv_boundary123"
    prompt = prompt_with_effective_units(BROWSER_PROMPT_LIMIT)
    job_id = start(service, tmp_path, prompt=prompt, conversation_id=conversation_id)
    job = service.read(job_id)
    assert job["browser_preflight"]["eligible"] is True
    assert job["browser_preflight"]["continuation"] is True
    assert job["routes"] == [
        ["aistudio", "4 Argon", "high"], ["aistudio", "4 Carbon", "high"],
        ["aistudio", "4 Neon", "xhigh"], ["aistudio", "3.8 Flash", "high"],
        ["web", "4 Argon", "high"], ["web", "4 Carbon", "high"],
        ["web", "4 Neon", "xhigh"], ["web", "3.8 Flash", "high"],
    ]

    adapter = WritingTransport(service.settings)
    calls = []

    def web(command, **kwargs):
        calls.append((command, kwargs))
        if command == "status":
            return studio_status()
        return {"provider": "aistudio", "billing_mode": "subscription", "model": "4 Argon", "reasoning_effort": "high",
                "answer": browser_answer(job, "continued boundary draft"), "conversation_id": conversation_id,
                "request_id": "boundary-request", "attachments": []}

    monkeypatch.setattr(adapter, "_web", web)
    result = adapter.aistudio(job, "4 Argon")
    assert result["text"] == "continued boundary draft"
    assert result["conversation_id"] == conversation_id
    payload = calls[1][1]["payload"]
    assert payload["prompt"] == browser_prompt(prompt, job_id=job["job_id"])
    assert payload["prompt"].endswith(completion_marker(job))
    assert payload["conversation_id"] == conversation_id
    assert payload["close_after"] is False
    assert len(payload["prompt"].encode("utf-16-le")) // 2 == BROWSER_PROMPT_LIMIT


@pytest.mark.parametrize("channel", ["aistudio", "web"])
def test_prompt_overflow_receipt_is_channel_scoped(channel):
    error = {"code": "INVALID_INPUT",
             "message": "Prompt is too long; keep it within 16000 characters.",
             "details": {"phase": "PRE_SUBMIT", "retry_safe": False}}
    assert fallback_scope(error, channel) == "channel"


def test_structured_prompt_overflow_receipt_falls_back_to_web(service, tmp_path):
    job_id = start(service, tmp_path)
    adapter = Routes([
        BridgeError("RATE_LIMITED", "cli quota", details={"phase": "PRE_SUBMIT"}),
        BridgeError("credentials_missing", "local credentials absent"),
        BridgeError("INVALID_INPUT", "Prompt is too long; keep it within 16000 characters.",
                    details={"phase": "PRE_SUBMIT", "retry_safe": False}),
        "web draft",
    ])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("cli", "4 Argon"), ("api", "4 Argon"),
                             ("aistudio", "4 Argon"), ("web", "4 Argon")]
    result = service.result(job_id)
    assert result["channel"] == "web"
    assert result["text"] == "web draft"
    assert result["attempts"][2]["fallback"] == "channel"


@pytest.mark.parametrize("message", [
    "Prompt cannot be empty.",
    "Invalid task slot.",
    "No recovery record exists for this request_id.",
])
def test_other_invalid_input_remains_terminal(service, tmp_path, message):
    job_id = start(service, tmp_path, route="aistudio:4 Argon")
    error = BridgeError("INVALID_INPUT", message, details={"phase": "PRE_SUBMIT"})
    adapter = Routes([error])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("aistudio", "4 Argon")]
    result = service.result(job_id)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_INPUT"
    assert fallback_scope(error.as_dict(), "aistudio") == "stop"


def test_web_adapter_marks_prompt_overflow_without_prompt_content(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path)
    job = service.read(job_id)
    adapter = WritingTransport(service.settings)

    def web(command, **kwargs):
        if command == "status":
            return studio_status()
        return {"error": {"code": "INVALID_INPUT",
                          "message": "Prompt is too long; keep it within 16000 characters.",
                          "phase": "PRE_SUBMIT", "retry_safe": False}}

    monkeypatch.setattr(adapter, "_web", web)
    with pytest.raises(BridgeError) as error:
        adapter.aistudio(job, "4 Argon")
    details = error.value.details
    assert details["reason"] == "prompt_too_long"
    assert details["limit"] == BROWSER_PROMPT_LIMIT
    assert details["effective_chars"] == job["browser_preflight"]["effective_chars"]
    assert "Write from selected evidence" not in json.dumps(details)
    assert fallback_scope(error.value.as_dict(), "aistudio") == "channel"


def test_browser_completion_marker_is_stable_stripped_and_not_delivered(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path, route="aistudio:4 Argon")
    job = service.read(job_id)
    marker = completion_marker(job)
    prompt = browser_prompt(job["prompt"], job_id=job["job_id"])
    assert prompt.endswith(marker)
    assert completion_marker(job) == marker

    adapter = WritingTransport(service.settings)
    calls = []

    def web(command, **kwargs):
        calls.append((command, kwargs))
        if command == "status":
            return studio_status()
        return {"provider": "aistudio", "billing_mode": "subscription", "model": "4 Argon", "reasoning_effort": "high",
                "answer": "final draft\n\n" + marker + "\n\n", "request_id": "marker-request",
                "attachments": [], "browser_closed": True}

    monkeypatch.setattr(adapter, "_web", web)
    result = adapter.aistudio(job, "4 Argon")
    assert result["text"] == "final draft"
    assert marker not in result["text"]
    assert calls[1][1]["payload"]["prompt"] == prompt


def test_completion_marker_is_per_job(service, tmp_path):
    first = service.read(start(service, tmp_path, route="aistudio:4 Argon"))
    second = service.read(start(service, tmp_path, route="aistudio:4 Argon"))
    assert first["prompt"] == second["prompt"]
    assert completion_marker(first) != completion_marker(second)


def test_missing_browser_marker_preserves_partial_text_and_terminal_receipt(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path, route="aistudio:4 Argon")
    job = service.read(job_id)
    partial = "A draft that ends mid-sentence RESULTS"
    adapter = WritingTransport(service.settings)

    def web(command, **kwargs):
        if command == "status":
            return studio_status()
        return {"provider": "aistudio", "billing_mode": "subscription", "model": "4 Argon", "reasoning_effort": "high",
                "answer": partial, "request_id": "truncated-request", "attachments": [],
                "browser_closed": True}

    monkeypatch.setattr(adapter, "_web", web)
    with pytest.raises(BridgeError) as error:
        adapter.aistudio(job, "4 Argon")
    assert error.value.code == "writing_incomplete"
    details = error.value.details
    assert details["reason"] == "completion_marker_missing"
    assert details["partial_text"] == partial
    assert details["request_id"] == "truncated-request"
    assert details["phase"] == "SUBMITTED"
    assert details["delivery"] == "confirmed_complete"
    assert details["recovery"]["outcome"] == "COMPLETED"
    assert details["browser_receipt"]["outcome"] == "COMPLETED"
    assert details["browser_receipt"]["request_id"] == "truncated-request"
    assert details["browser_receipt"]["model"] == "4 Argon"
    assert details["browser_receipt"]["browser_closed"] is True
    assert fallback_scope(error.value.as_dict(), "aistudio") == "model"


@pytest.mark.parametrize("channel", ["aistudio", "web"])
def test_confirmed_missing_marker_is_model_scoped(channel):
    assert fallback_scope(missing_marker_error().as_dict(), channel) == "model"


def test_recovery_only_completion_is_still_confirmed_for_marker_fallback():
    error = BridgeError("writing_incomplete", "missing marker", details={
        "reason": "completion_marker_missing",
        "partial_text": "partial",
        "recovery": {"outcome": "COMPLETED"},
    })
    assert fallback_scope(error.as_dict(), "aistudio") == "model"


def test_missing_browser_marker_falls_back_only_after_confirmed_completion(service, tmp_path):
    job_id = start(service, tmp_path)
    adapter = Routes([
        model_unavailable(), model_unavailable(), model_unavailable(), model_unavailable(),
        api_failure(), api_failure(), api_failure(503),
        missing_marker_error(partial_text="truncated partial"), "carbon draft",
    ])
    service.execute(job_id, transport=adapter)
    assert adapter.calls[-2:] == [("aistudio", "4 Argon"), ("aistudio", "4 Carbon")]
    result = service.result(job_id)
    assert result["channel"] == "aistudio"
    assert result["model"] == "4 Carbon"
    assert result["text"] == "carbon draft"
    assert result["attempts"][8]["fallback"] == "model"
    assert result["attempts"][8]["error"]["details"]["partial_text"] == "truncated partial"


@pytest.mark.parametrize("error", [
    missing_marker_error(recovery_outcome="OUTCOME_UNKNOWN"),
    missing_marker_error(recovery_outcome="SETTLED_UNKNOWN"),
    missing_marker_error(delivery="unknown"),
    BridgeError("writing_incomplete", "missing marker", details={
        "reason": "completion_marker_missing", "partial_text": "partial",
        "recovery": {"outcome": "OUTCOME_UNKNOWN"}}),
    BridgeError("OUTCOME_UNKNOWN", "uncertain delivery", details={
        "phase": "SUBMITTED", "recovery": {"outcome": "OUTCOME_UNKNOWN"}}),
])
def test_uncertain_browser_delivery_never_falls_back(service, tmp_path, error):
    job_id = start(service, tmp_path)
    adapter = Routes([
        BridgeError("RATE_LIMITED", "cli quota", details={"phase": "PRE_SUBMIT"}),
        BridgeError("credentials_missing", "local credentials absent"),
        error, "web draft",
    ])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("cli", "4 Argon"), ("api", "4 Argon"), ("aistudio", "4 Argon")]
    result = service.result(job_id)
    assert result["done"] and not result["ok"]


def test_continuation_marker_and_missing_marker_web_fallback(service, tmp_path):
    conversation_id = "conv_marker123"
    job_id = start(service, tmp_path, prompt="Continue the draft", conversation_id=conversation_id)
    job = service.read(job_id)
    marker = completion_marker(job)
    assert job["routes"] == [
        ["aistudio", "4 Argon", "high"], ["aistudio", "4 Carbon", "high"],
        ["aistudio", "4 Neon", "xhigh"], ["aistudio", "3.8 Flash", "high"],
        ["web", "4 Argon", "high"], ["web", "4 Carbon", "high"],
        ["web", "4 Neon", "xhigh"], ["web", "3.8 Flash", "high"],
    ]
    assert browser_prompt(job["prompt"], job_id=job["job_id"]).endswith(marker)
    assert completion_marker(job) == marker

    adapter = Routes([missing_marker_error(partial_text="continued partial"), "carbon continuation draft"])
    service.execute(job_id, transport=adapter)
    assert adapter.calls == [("aistudio", "4 Argon"), ("aistudio", "4 Carbon")]
    result = service.result(job_id)
    assert result["channel"] == "aistudio"
    assert result["model"] == "4 Carbon"
    assert result["text"] == "carbon continuation draft"
    assert result["attempts"][0]["fallback"] == "model"


def test_marker_instruction_overhead_counts_toward_browser_limit(service, tmp_path):
    bare_prefix = utf16_units(SYSTEM + "\n\n")
    prompt = "x" * (BROWSER_PROMPT_LIMIT - bare_prefix)
    assert utf16_units(SYSTEM + "\n\n" + prompt) == BROWSER_PROMPT_LIMIT
    preflight = browser_preflight(prompt, job_id=TEST_JOB_ID)
    assert preflight["eligible"] is False
    assert preflight["effective_chars"] > BROWSER_PROMPT_LIMIT

    job_id = start(service, tmp_path, prompt=prompt)
    job = service.read(job_id)
    assert job["browser_preflight"]["eligible"] is False
    assert job["routes"] == [
        ["cli", "4 Argon", "high"], ["cli", "4 Carbon", "high"],
        ["cli", "4 Neon", "xhigh"], ["cli", "3.8 Flash", "high"],
        ["api", "4 Argon", "high"], ["api", "4 Carbon", "high"],
        ["api", "4 Neon", "xhigh"], ["api", "3.8 Flash", "high"],
    ]
    assert browser_prompt(prompt, job_id=job["job_id"]).endswith(completion_marker(job))


def test_explicit_pinned_browser_route_overlong_still_stops_without_api(service, tmp_path):
    prompt = prompt_with_effective_units(BROWSER_PROMPT_LIMIT + 1)
    result = service.submit(prompt, cwd=str(tmp_path), route="aistudio:4 Argon", wait_seconds=0)
    assert result["done"] and not result["ok"]
    assert result["error"]["code"] == "browser_prompt_too_long"
    assert result["browser_preflight"]["skipped_routes"] == [["aistudio", "4 Argon", "high"]]

    adapter = Routes(["api draft"])
    service.execute(result["job_id"], transport=adapter)
    assert adapter.calls == []


def test_completion_marker_must_be_the_exact_final_suffix(service, tmp_path, monkeypatch):
    job_id = start(service, tmp_path, route="aistudio:4 Argon")
    job = service.read(job_id)
    marker = completion_marker(job)
    answers = iter([
        "draft\n\n" + marker + "\ntrailing text",
        "draft\n\n" + marker[:-1],
        "draft\n\n" + marker + ".",
    ])
    adapter = WritingTransport(service.settings)

    def web(command, **kwargs):
        if command == "status":
            return studio_status()
        return {"provider": "aistudio", "billing_mode": "subscription", "model": "4 Argon", "reasoning_effort": "high",
                "answer": next(answers), "request_id": "exact-marker-request", "attachments": []}

    monkeypatch.setattr(adapter, "_web", web)
    for _ in range(3):
        with pytest.raises(BridgeError) as error:
            adapter.aistudio(job, "4 Argon")
        assert error.value.code == "writing_incomplete"
        assert error.value.details["reason"] == "completion_marker_missing"


def test_completion_marker_must_be_one_standalone_final_line():
    from gemini_baton.writing_routes import strip_completion_marker
    marker = "GEMINI_BATON_COMPLETE_synthetic"
    assert strip_completion_marker("draft\n" + marker + "\n\n", marker) == "draft"
    for response in ["draft" + marker, "draft\n" + marker + "\nextra",
                     "draft\n" + marker + "\n" + marker, marker]:
        assert strip_completion_marker(response, marker) is None
