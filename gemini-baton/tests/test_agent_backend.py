from __future__ import annotations

import base64
import copy
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from gemini_baton.agent import AgentLoop
from gemini_baton.artifacts import RunArtifacts
from gemini_baton.backend import (
    FunctionCallRequest,
    GeminiBackend,
    ModelResponse,
)
from gemini_baton.errors import BridgeError
from gemini_baton.paths import StateLayout
from gemini_baton.tools import WorkspaceTools


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def text_content(text: str, signature: str | None = None) -> dict[str, Any]:
    part: dict[str, Any] = {"text": text}
    if signature:
        part["thoughtSignature"] = b64(signature)
    return {"role": "model", "parts": [part]}


def call_content(
    name: str,
    args: dict[str, Any],
    *,
    text: str = "",
    signature: str = "call-sig",
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"text": text})
    parts.append(
        {
            "functionCall": {"name": name, "args": args},
            "thoughtSignature": b64(signature),
        }
    )
    return {"role": "model", "parts": parts}


class RecordingBackend:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> ModelResponse:
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


def response(
    content: dict[str, Any],
    *,
    calls: list[tuple[str, dict[str, Any]]] | None = None,
    text: str = "",
    finish: str = "STOP",
) -> ModelResponse:
    return ModelResponse(
        content=content,
        text=text,
        function_calls=tuple(
            FunctionCallRequest(name=name, args=args)
            for name, args in (calls or [])
        ),
        finish_reason=finish,
        usage={"prompt_token_count": 3, "candidates_token_count": 2},
        model_version="gemini-3.8-flash-test",
        response_id="resp-test",
    )


def build_loop(
    tmp_path: Path,
    backend: RecordingBackend,
    *,
    permission_mode: str = "read-only",
    allow_shell: bool = False,
    max_steps: int = 6,
    deadline_seconds: float = 10,
) -> AgentLoop:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "input.txt").write_text("input data\n", encoding="utf-8")
    layout = StateLayout(tmp_path / "state")
    artifacts = RunArtifacts(layout, "run_test")
    tools = WorkspaceTools(
        workspace,
        permission_mode=permission_mode,
        allow_shell=allow_shell,
        read_max_bytes=4096,
        max_output_chars=4096,
        command_timeout_seconds=5,
    )
    return AgentLoop(
        backend=backend,
        model="gemini-3.8-flash",
        cwd=str(workspace),
        permission_mode=permission_mode,
        allow_shell=allow_shell,
        tools=tools,
        artifacts=artifacts,
        max_steps=max_steps,
        max_output_tokens=1024,
        deadline_monotonic=time.monotonic() + deadline_seconds,
    )


def test_manual_tool_loop_preserves_full_content_and_signature(
    tmp_path: Path,
) -> None:
    first = call_content("read_file", {"path": "input.txt"}, signature="sig-one")
    backend = RecordingBackend(
        [
            response(
                first,
                calls=[("read_file", {"path": "input.txt"})],
                text="reading",
            ),
            response(
                text_content("done", signature="final-signature"),
                text="done",
            ),
        ]
    )
    loop = build_loop(tmp_path, backend)
    outcome = loop.run(user_prompt="read it", prior_transcript=[])
    assert outcome.success is True
    assert outcome.final_text == "done"
    assert len(backend.calls) == 2
    second_contents = backend.calls[1]["contents"]
    model_turns = [c for c in second_contents if c.get("role") == "model"]
    assert model_turns
    assert model_turns[-1]["parts"][0]["thoughtSignature"] == b64("sig-one")
    response_turn = second_contents[-1]
    assert response_turn["role"] == "user"
    assert "functionResponse" in response_turn["parts"][0]
    assert response_turn["parts"][0]["functionResponse"]["name"] == "read_file"
    # The complete final Content, including its signature, is in the transcript.
    assert outcome.transcript[-1]["parts"][0]["thoughtSignature"] == b64(
        "final-signature"
    )
    assert backend.calls[0]["system_instruction"].startswith("You are Gemini Baton")
    assert backend.calls[0]["max_output_tokens"] == 1024
    artifacts = RunArtifacts(StateLayout(tmp_path / "state"), "run_test")
    assert (artifacts.dir / "steps" / "step-01-model.json").exists()
    assert (artifacts.dir / "tools").exists()


def test_permission_refusal_is_returned_to_model(tmp_path: Path) -> None:
    backend = RecordingBackend(
        [
            response(
                call_content(
                    "write_file", {"path": "out.txt", "content": "nope"}
                ),
                calls=[("write_file", {"path": "out.txt", "content": "nope"})],
            ),
            response(text_content("read-only respected"), text="read-only respected"),
        ]
    )
    loop = build_loop(tmp_path, backend, permission_mode="read-only")
    outcome = loop.run(user_prompt="try to write", prior_transcript=[])
    assert outcome.success is True
    assert not (tmp_path / "workspace" / "out.txt").exists()
    tool_events = [
        line
        for line in (
            RunArtifacts(StateLayout(tmp_path / "state"), "run_test")
            .events_path.read_text()
            .splitlines()
        )
        if "tool_call" in line
    ]
    assert tool_events
    assert "permission_denied" in tool_events[0]


def test_session_continuation_sends_previous_signature(tmp_path: Path) -> None:
    first_backend = RecordingBackend(
        [response(text_content("first answer", signature="sess-sig"), text="first")]
    )
    outcome1 = build_loop(tmp_path, first_backend).run(
        user_prompt="first", prior_transcript=[]
    )
    assert outcome1.success
    second_backend = RecordingBackend(
        [response(text_content("second answer"), text="second")]
    )
    outcome2 = build_loop(tmp_path, second_backend).run(
        user_prompt="second", prior_transcript=outcome1.transcript
    )
    assert outcome2.success
    contents = second_backend.calls[0]["contents"]
    assert any(
        part.get("thoughtSignature") == b64("sess-sig")
        for content in contents
        for part in content.get("parts", [])
    )
    assert contents[-1]["parts"][0]["text"] == "second"


def test_step_limit_is_failed_not_silently_truncated(tmp_path: Path) -> None:
    loop = build_loop(
        tmp_path,
        RecordingBackend(
            [
                response(
                    call_content("read_file", {"path": "input.txt"}),
                    calls=[("read_file", {"path": "input.txt"})],
                ),
                response(
                    call_content("read_file", {"path": "input.txt"}),
                    calls=[("read_file", {"path": "input.txt"})],
                ),
            ]
        ),
        max_steps=2,
    )
    outcome = loop.run(user_prompt="loop", prior_transcript=[])
    assert outcome.success is False
    assert outcome.error["code"] == "step_limit"


def test_finish_reason_failure_is_reported(tmp_path: Path) -> None:
    loop = build_loop(
        tmp_path,
        RecordingBackend(
            [
                response(
                    text_content("partial"),
                    text="partial",
                    finish="MAX_TOKENS",
                )
            ]
        ),
    )
    outcome = loop.run(user_prompt="long", prior_transcript=[])
    assert outcome.success is False
    assert outcome.error["code"] == "incomplete_finish_reason"
    assert outcome.final_text == "partial"
    assert outcome.finish_reason == "MAX_TOKENS"


def test_backend_disables_automatic_function_calling_and_keeps_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client"] = kwargs

        def close(self) -> None:
            captured["closed"] = True

        class Models:
            def generate_content(self, **kwargs: Any) -> Any:
                captured["generate"] = kwargs
                content = types.Content(
                    role="model",
                    parts=[types.Part(text="hello", thought_signature=b"sig-xyz")],
                )
                return SimpleNamespace(
                    candidates=[
                        SimpleNamespace(
                            content=content,
                            finish_reason=types.FinishReason.STOP,
                            finish_message=None,
                        )
                    ],
                    usage_metadata=types.GenerateContentResponseUsageMetadata(
                        prompt_token_count=4,
                        candidates_token_count=2,
                        total_token_count=6,
                    ),
                    model_version="gemini-3.8-flash-001",
                    response_id="response-id",
                    prompt_feedback=None,
                )

        @property
        def models(self) -> Any:
            return self.Models()

    import google.genai

    monkeypatch.setattr(google.genai, "Client", FakeClient)
    backend = GeminiBackend(api_key="unit-test-key", api_key_source="test")
    result = backend.generate(
        model="gemini-3.8-flash",
        contents=[{"role": "user", "parts": [{"text": "hi"}]}],
        tools=[
            {
                "name": "read_file",
                "description": "read",
                "parameters_json_schema": {"type": "object", "properties": {}},
            }
        ],
        system_instruction="system",
        max_output_tokens=99,
        timeout_seconds=5,
    )
    config = captured["generate"]["config"]
    assert config.automatic_function_calling.disable is True
    assert config.max_output_tokens == 99
    assert config.tools[0].function_declarations[0].name == "read_file"
    assert result.content["parts"][0]["thoughtSignature"] == b64("sig-xyz")
    assert result.model_version == "gemini-3.8-flash-001"
    assert result.usage["total_token_count"] == 6
    assert captured["closed"] is True


def test_backend_errors_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "AIza" + "x" * 30

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

        @property
        def models(self) -> Any:
            class Models:
                def generate_content(self, **kwargs: Any) -> Any:
                    raise RuntimeError(
                        f"bad key {secret} https://example.test/x?key={secret}"
                    )

            return Models()

    import google.genai

    monkeypatch.setattr(google.genai, "Client", FakeClient)
    backend = GeminiBackend(api_key=secret, api_key_source="test")
    with pytest.raises(BridgeError) as excinfo:
        backend.generate(
            model="gemini-3.8-flash",
            contents=[],
            tools=[],
            system_instruction="system",
            max_output_tokens=10,
            timeout_seconds=1,
        )
    assert secret not in excinfo.value.message
    assert "[REDACTED]" in excinfo.value.message


@pytest.mark.parametrize("connect_failure", [True, False])
def test_backend_distinguishes_preconnect_failure_from_unknown_delivery(monkeypatch, connect_failure):
    import httpx
    import google.genai

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = self

        def close(self):
            pass

        def generate_content(self, **kwargs):
            if connect_failure:
                raise httpx.ConnectError("DNS lookup failed")
            raise httpx.ReadTimeout("read timed out after submission")

    monkeypatch.setattr(google.genai, "Client", FakeClient)
    backend = GeminiBackend(api_key="unit-key", api_key_source="test", retry_attempts=1)
    with pytest.raises(BridgeError) as error:
        backend.generate(model="gemini-4-argon", contents=[], tools=[], system_instruction="write",
                         max_output_tokens=10, timeout_seconds=1)
    assert error.value.details["outcome"] == ("not_submitted" if connect_failure else "unknown")


def test_only_explicit_prompt_and_prior_history_are_sent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "should-not-be-auto-read.txt").write_text("AUTO-SCAN-SECRET")
    backend = RecordingBackend(
        [response(text_content("no scan needed"), text="no scan needed")]
    )
    loop = build_loop(tmp_path, backend)
    outcome = loop.run(user_prompt="answer directly", prior_transcript=[])
    assert outcome.success is True
    contents = backend.calls[0]["contents"]
    assert contents == [
        {"role": "user", "parts": [{"text": "answer directly"}]}
    ]
    serialized = str(contents)
    assert "AUTO-SCAN-SECRET" not in serialized
    assert "should-not-be-auto-read" not in serialized
    assert "answer directly" in serialized


def test_multiple_sequential_tool_rounds_keep_all_signatures(
    tmp_path: Path,
) -> None:
    first = call_content("read_file", {"path": "input.txt"}, signature="sig-a")
    second = call_content("list_files", {"path": "."}, signature="sig-b")
    backend = RecordingBackend(
        [
            response(
                first,
                calls=[("read_file", {"path": "input.txt"})],
            ),
            response(
                second,
                calls=[("list_files", {"path": "."})],
            ),
            response(text_content("finished", signature="sig-final"), text="finished"),
        ]
    )
    outcome = build_loop(tmp_path, backend).run(
        user_prompt="two tool rounds", prior_transcript=[]
    )
    assert outcome.success is True
    assert outcome.steps == 3
    assert outcome.model_calls == 3
    assert len(backend.calls) == 3
    contents = backend.calls[2]["contents"]
    signatures = [
        part["thoughtSignature"]
        for content in contents
        for part in content.get("parts", [])
        if "thoughtSignature" in part
    ]
    assert b64("sig-a") in signatures
    assert b64("sig-b") in signatures
    function_responses = [
        part["functionResponse"]["name"]
        for content in contents
        for part in content.get("parts", [])
        if "functionResponse" in part
    ]
    assert function_responses == ["read_file", "list_files"]
    assert outcome.usage["prompt_token_count"] == 9


def test_list_available_models_queries_models_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace as NS

    import google.genai

    from gemini_baton import backend as backend_module
    from gemini_baton.credentials import Credential

    captured: dict[str, Any] = {}

    class FakeModels:
        def list(self, config: dict[str, Any]) -> list[Any]:
            captured["config"] = config
            return [
                NS(
                    name="models/zeta",
                    display_name="Zeta",
                    description="z",
                    version="1",
                    input_token_limit=100,
                    output_token_limit=10,
                    supported_actions=["generateContent"],
                ),
                NS(
                    name="models/alpha",
                    display_name="Alpha",
                    description="a",
                    version="1",
                    input_token_limit=100,
                    output_token_limit=10,
                    supported_actions=["generateContent"],
                ),
            ]

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client"] = kwargs
            self.models = FakeModels()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(
        backend_module,
        "load_credential",
        lambda: Credential(source="env:GEMINI_API_KEY", key="unit-key"),
    )
    monkeypatch.setattr(google.genai, "Client", FakeClient)
    listing = backend_module.list_available_models()
    assert captured["config"] == {"page_size": 100}
    assert captured["closed"] is True
    assert [item["name"] for item in listing["models"]] == [
        "models/alpha",
        "models/zeta",
    ]
    assert listing["credential_source"] == "env:GEMINI_API_KEY"
    assert listing["count"] == 2
    assert "unit-key" not in str(listing)


def test_agent_prechecks_expired_deadline(tmp_path: Path) -> None:
    backend = RecordingBackend([response(text_content("should not happen"))])
    loop = build_loop(tmp_path, backend, deadline_seconds=-1)
    outcome = loop.run(user_prompt="expired", prior_transcript=[])
    assert outcome.success is False
    assert outcome.error["code"] == "run_timeout"
    assert backend.calls == []
