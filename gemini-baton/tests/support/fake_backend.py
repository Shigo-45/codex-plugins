from __future__ import annotations

import base64
import os
import sys
import time
from typing import Any

from gemini_baton.backend import FunctionCallRequest, ModelResponse


def _sig(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _text_content(text: str, signature: str | None = None) -> dict[str, Any]:
    part: dict[str, Any] = {"text": text}
    if signature:
        part["thoughtSignature"] = _sig(signature)
    return {"role": "model", "parts": [part]}


def _call_content(
    calls: list[tuple[str, dict[str, Any]]],
    *,
    signature: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"text": text})
    for index, (name, args) in enumerate(calls):
        part: dict[str, Any] = {"functionCall": {"name": name, "args": args}}
        if index == 0 and signature:
            part["thoughtSignature"] = _sig(signature)
        parts.append(part)
    return {"role": "model", "parts": parts}


def _scan_contents(contents: list[dict[str, Any]]) -> tuple[int, bool, bool]:
    function_responses = 0
    any_signature = False
    for content in contents:
        for part in content.get("parts", []):
            if "functionResponse" in part:
                function_responses += 1
            if part.get("thoughtSignature"):
                any_signature = True
    last = contents[-1] if contents else {}
    last_has_function_response = any(
        "functionResponse" in part for part in last.get("parts", [])
    )
    return function_responses, any_signature, last_has_function_response


class FakeBackend:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.calls = 0

    def generate(
        self,
        *,
        model: str,
        contents: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_instruction: str,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ModelResponse:
        self.calls += 1
        function_responses, has_signature, last_has_function_response = (
            _scan_contents(contents)
        )
        if self.scenario == "secret_error":
            key = os.environ.get("GEMINI_API_KEY", "")
            raise RuntimeError(
                f"fake failure key={key} "
                "https://example.invalid/x?key=" + key
            )
        if self.scenario == "slow":
            time.sleep(60)
            return self._final("slow finished")
        if self.scenario == "long":
            return self._final("z" * 9000)
        if self.scenario == "delayed_final":
            time.sleep(1.0)
            return self._final("survived mcp exit")
        if self.scenario == "deadline_probe":
            return self._final(f"timeout_seconds={timeout_seconds:.3f}")
        if self.scenario == "honor_timeout":
            time.sleep(min(max(timeout_seconds, 0.1), 5.0))
            raise RuntimeError("fake backend honoured its timeout")
        if self.scenario == "session_echo":
            return self._final(
                "prior_signature_present="
                + ("true" if has_signature else "false")
                + f"; prior_function_responses={function_responses}"
            )
        if self.scenario == "slow_tool":
            code = (
                "import subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(300)'])\n"
                "print('child_pid='+str(child.pid), flush=True)\n"
                "time.sleep(300)\n"
            )
            return self._calls(
                [
                    (
                        "run_command",
                        {
                            "argv": [sys.executable, "-c", code],
                            "timeout_seconds": 300,
                        },
                    )
                ],
                signature="sig-slow-tool",
            )
        if self.scenario == "tool_write":
            if not last_has_function_response:
                return self._calls(
                    [
                        (
                            "write_file",
                            {
                                "path": "written.txt",
                                "content": "written by fake model\n",
                            },
                        )
                    ],
                    signature="sig-tool-write-1",
                )
            return self._final(
                "tool_write completed; prior_signature_present="
                + ("true" if has_signature else "false")
                + f"; responses={function_responses}"
            )
        if self.scenario == "readme_then_final":
            if not last_has_function_response:
                return self._calls(
                    [
                        ("read_file", {"path": "input.txt"}),
                    ],
                    signature="sig-read",
                )
            return self._final("read complete")
        if self.scenario == "empty":
            return ModelResponse(
                content={"role": "model", "parts": [{"text": ""}]},
                text="",
                function_calls=(),
                finish_reason="STOP",
                usage={},
                model_version="fake-3.8-flash",
            )
        return self._final("fake final answer")

    def _final(self, text: str) -> ModelResponse:
        return ModelResponse(
            content=_text_content(text, signature="sig-final"),
            text=text,
            function_calls=(),
            finish_reason="STOP",
            usage={
                "prompt_token_count": 10,
                "candidates_token_count": 4,
                "total_token_count": 14,
            },
            model_version="fake-3.8-flash",
        )

    def _calls(
        self, calls: list[tuple[str, dict[str, Any]]], *, signature: str
    ) -> ModelResponse:
        return ModelResponse(
            content=_call_content(calls, signature=signature),
            text="",
            function_calls=tuple(
                FunctionCallRequest(name=name, args=args) for name, args in calls
            ),
            finish_reason="STOP",
            usage={
                "prompt_token_count": 10,
                "candidates_token_count": 4,
                "total_token_count": 14,
            },
            model_version="fake-3.8-flash",
        )


def fake_factory(settings: Any) -> FakeBackend:
    return FakeBackend(os.environ.get("GEMINI_BATON_FAKE_SCENARIO", "final"))
