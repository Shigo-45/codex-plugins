from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .artifacts import RunArtifacts
from .backend import ModelBackend
from .errors import BridgeError
from .paths import ensure_private_dir
from .tools import WorkspaceTools
from .util import add_usage, preview_text, utc_now_iso

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_FINISH_FAILURES = {
    "MAX_TOKENS",
    "SAFETY",
    "RECITATION",
    "LANGUAGE",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "MALFORMED_FUNCTION_CALL",
    "IMAGE_SAFETY",
    "UNEXPECTED_TOOL_CALL",
    "TOO_MANY_TOOL_CALLS",
    "IMAGE_PROHIBITED_CONTENT",
    "IMAGE_RECITATION",
    "OTHER",
}


@dataclass
class AgentOutcome:
    success: bool
    final_text: str
    transcript: list[dict[str, Any]]
    usage: dict[str, int]
    actual_model_version: str | None
    finish_reason: str | None
    steps: int
    model_calls: int
    changed_files: list[str]
    error: dict[str, Any] | None = None
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: str | None = None


def build_system_instruction(
    *,
    cwd: str,
    permission_mode: str,
    allow_shell: bool,
) -> str:
    shell_text = (
        "enabled (argv only; there is no OS sandbox for commands)"
        if allow_shell
        else "disabled"
    )
    write_text = (
        "You may create or edit files only through write_file/edit_file."
        if permission_mode == "workspace-write"
        else "This is read-only; never claim to have modified files."
    )
    return f"""You are Gemini Baton, a Gemini API subagent invoked by Codex for writing, analysis, and coding tasks.

Selected workspace (fixed for this session): {cwd}
Permission mode: {permission_mode}
Shell: {shell_text}

Rules:
- Context is explicit. The bridge does not scan or upload the workspace automatically. Use list_files/read_file only for files you decide are needed.
- All file tool paths must be relative to the workspace. Do not attempt path traversal or sensitive files.
- {write_text}
- run_command is absent when shell is disabled. Do not pretend to run commands. When enabled, it takes an argv array, not a shell string.
- There is no OS sandbox. run_command can have side effects outside the workspace; use it only when needed.
- Never print, request, or store credentials. Do not read .env, credential, or key files.
- Work autonomously and keep the final answer concise: summarize what you did, files changed, and tests/checks run. Clearly state uncertainty or unfinished work.
"""


def _safe_call_slug(name: str, index: int) -> str:
    cleaned = _SAFE_NAME.sub("_", name)[:40].strip("_") or "tool"
    return f"{index:02d}-{cleaned}"


def _function_response_content(
    calls: list[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    return {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "name": name,
                    "response": response,
                }
            }
            for name, response in calls
        ],
    }


def _sanitize_tool_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, BridgeError):
        return {"ok": False, "error": exc.code, "message": exc.message}
    return {
        "ok": False,
        "error": "tool_internal_error",
        "message": f"{type(exc).__name__}",
    }


class AgentLoop:
    def __init__(
        self,
        *,
        backend: ModelBackend,
        model: str,
        cwd: str,
        permission_mode: str,
        allow_shell: bool,
        tools: WorkspaceTools,
        artifacts: RunArtifacts,
        max_steps: int,
        max_output_tokens: int,
        deadline_monotonic: float,
        control_check: Callable[[], str | None] | None = None,
    ) -> None:
        self.backend = backend
        self.model = model
        self.cwd = cwd
        self.permission_mode = permission_mode
        self.allow_shell = allow_shell
        self.tools = tools
        self.artifacts = artifacts
        self.max_steps = max_steps
        self.max_output_tokens = max_output_tokens
        self.deadline_monotonic = deadline_monotonic
        self.control_check = control_check or (lambda: None)

    def run(
        self,
        *,
        user_prompt: str,
        prior_transcript: list[dict[str, Any]],
    ) -> AgentOutcome:
        started_at = utc_now_iso()
        transcript = [dict(item) for item in prior_transcript]
        transcript.append(
            {"role": "user", "parts": [{"text": user_prompt}]}
        )
        usage_total: dict[str, int] = {}
        actual_model_version: str | None = None
        finish_reason: str | None = None
        steps = 0
        changed_files: list[str] = []
        final_text = ""
        error: dict[str, Any] | None = None
        declarations = self.tools.declarations()
        system_instruction = build_system_instruction(
            cwd=self.cwd,
            permission_mode=self.permission_mode,
            allow_shell=self.allow_shell,
        )

        self.artifacts.write_json(
            "turn.json",
            {
                "selected_model": self.model,
                "cwd": self.cwd,
                "permission_mode": self.permission_mode,
                "allow_shell": self.allow_shell,
                "max_steps": self.max_steps,
                "max_output_tokens": self.max_output_tokens,
                "tools": [item["name"] for item in declarations],
                "system_instruction": system_instruction,
                "user_prompt": user_prompt,
                "prior_content_count": len(prior_transcript),
                "created_at": started_at,
            },
        )
        self.artifacts.event(
            "turn_start",
            selected_model=self.model,
            cwd=self.cwd,
            permission_mode=self.permission_mode,
            allow_shell=self.allow_shell,
            max_steps=self.max_steps,
            tool_names=[item["name"] for item in declarations],
        )

        try:
            for step in range(1, self.max_steps + 1):
                control_status = self.control_check()
                if control_status is not None:
                    error = {
                        "code": "cancelled",
                        "message": f"Run control changed to {control_status}.",
                    }
                    break
                if time.monotonic() >= self.deadline_monotonic:
                    error = {
                        "code": "run_timeout",
                        "message": "The run deadline expired.",
                    }
                    break
                steps = step
                remaining = self.deadline_monotonic - time.monotonic()
                self.artifacts.event(
                    "model_request",
                    step=step,
                    model=self.model,
                    content_count=len(transcript),
                    remaining_seconds=round(remaining, 3),
                )
                response = self.backend.generate(
                    model=self.model,
                    contents=transcript,
                    tools=declarations,
                    system_instruction=system_instruction,
                    max_output_tokens=self.max_output_tokens,
                    timeout_seconds=max(0.1, remaining),
                )
                actual_model_version = response.model_version or actual_model_version
                finish_reason = response.finish_reason or finish_reason
                add_usage(usage_total, response.usage)
                transcript.append(response.content)
                model_artifact = self.artifacts.write_json(
                    f"steps/step-{step:02d}-model.json",
                    {
                        "step": step,
                        "selected_model": self.model,
                        "actual_model_version": response.model_version,
                        "response_id": response.response_id,
                        "finish_reason": response.finish_reason,
                        "finish_message": response.finish_message,
                        "usage": response.usage,
                        "content": response.content,
                        "text": response.text,
                        "function_calls": [
                            {"name": call.name, "args": call.args}
                            for call in response.function_calls
                        ],
                    },
                )
                self.artifacts.event(
                    "model_response",
                    step=step,
                    actual_model_version=response.model_version,
                    finish_reason=response.finish_reason,
                    function_call_count=len(response.function_calls),
                    text_preview=preview_text(response.text, 500),
                    usage=response.usage,
                    artifact=str(model_artifact),
                )
                if not response.function_calls:
                    final_text = response.text
                    if not final_text.strip():
                        error = {
                            "code": "empty_final_response",
                            "message": "Gemini returned no final text.",
                        }
                    elif (
                        response.finish_reason
                        and response.finish_reason in _FINISH_FAILURES
                    ):
                        error = {
                            "code": "incomplete_finish_reason",
                            "message": (
                                "Gemini finished with "
                                f"{response.finish_reason}; the answer may be incomplete."
                            ),
                        }
                    break
                if step >= self.max_steps:
                    error = {
                        "code": "step_limit",
                        "message": (
                            f"Model still requested tools after {self.max_steps} steps."
                        ),
                    }
                    break
                responses: list[tuple[str, dict[str, Any]]] = []
                for call_index, call in enumerate(
                    response.function_calls, start=1
                ):
                    control_status = self.control_check()
                    if control_status is not None:
                        error = {
                            "code": "cancelled",
                            "message": f"Run control changed to {control_status}.",
                        }
                        break
                    call_dirname = (
                        f"tools/step-{step:02d}-call-"
                        f"{_safe_call_slug(call.name, call_index)}"
                    )
                    call_dir = ensure_private_dir(self.artifacts.dir / call_dirname)
                    if not call.name:
                        tool_response = {
                            "ok": False,
                            "error": "invalid_tool_call",
                            "message": "Gemini returned a function call without a name.",
                        }
                    else:
                        try:
                            tool_response = self.tools.execute(
                                call.name,
                                call.args,
                                deadline_monotonic=self.deadline_monotonic,
                                artifact_dir=call_dir,
                            )
                        except BridgeError as exc:
                            tool_response = _sanitize_tool_error(exc)
                        except Exception as exc:
                            tool_response = _sanitize_tool_error(exc)
                    if (
                        call.name in {"write_file", "edit_file"}
                        and tool_response.get("ok")
                        and isinstance(tool_response.get("path"), str)
                    ):
                        path = tool_response["path"]
                        if path not in changed_files:
                            changed_files.append(path)
                    responses.append((call.name, tool_response))
                    payload = {
                        "ts": utc_now_iso(),
                        "step": step,
                        "call_index": call_index,
                        "name": call.name,
                        "args": call.args,
                        "response": tool_response,
                    }
                    artifact = self.artifacts.write_json(
                        f"{call_dirname}/tool.json", payload
                    )
                    self.artifacts.event(
                        "tool_call",
                        step=step,
                        call_index=call_index,
                        name=call.name,
                        args_preview=preview_text(
                            str(call.args), 1_000
                        ),
                        ok=bool(tool_response.get("ok")),
                        error=tool_response.get("error"),
                        output_paths={
                            key: tool_response[key]
                            for key in ("stdout_path", "stderr_path")
                            if isinstance(tool_response.get(key), str)
                        },
                        artifact=str(artifact),
                    )
                if error is not None:
                    break
                if responses:
                    transcript.append(_function_response_content(responses))
                else:
                    error = {
                        "code": "empty_tool_batch",
                        "message": "Gemini returned function calls but none were executable.",
                    }
                    break
            else:
                error = {
                    "code": "step_limit",
                    "message": f"Run exceeded {self.max_steps} model steps.",
                }
        except BridgeError as exc:
            error = exc.as_dict()
        except Exception as exc:
            error = {
                "code": "internal_error",
                "message": f"{type(exc).__name__}: {exc}",
            }

        success = error is None and bool(final_text.strip())
        ended_at = utc_now_iso()
        self.artifacts.event(
            "turn_end",
            success=success,
            steps=steps,
            error=error,
            actual_model_version=actual_model_version,
            finish_reason=finish_reason,
            changed_files=changed_files,
            usage=usage_total,
        )
        return AgentOutcome(
            success=success,
            final_text=final_text,
            transcript=transcript,
            usage=usage_total,
            actual_model_version=actual_model_version,
            finish_reason=finish_reason,
            steps=steps,
            model_calls=steps,
            changed_files=changed_files,
            error=error,
            started_at=started_at,
            ended_at=ended_at,
        )
