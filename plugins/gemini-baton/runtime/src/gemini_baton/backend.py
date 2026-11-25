from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import __version__
from .config import Settings
from .credentials import load_credential
from .errors import BridgeError
from .redact import redact_text
from .util import usage_dict

# The bridge renders SDK failures itself after redacting the API key and URL
# credentials. SDK transport debug logs could contain a key-bearing URL, so keep
# them out of stderr/worker logs.
for _logger_name in ("google_genai", "google.genai", "httpx", "httpcore"):
    logging.getLogger(_logger_name).setLevel(logging.CRITICAL)


@dataclass(frozen=True)
class FunctionCallRequest:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    content: dict[str, Any]
    text: str
    function_calls: tuple[FunctionCallRequest, ...]
    finish_reason: str | None
    usage: dict[str, int]
    model_version: str | None
    response_id: str | None = None
    finish_message: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)


class ModelBackend(Protocol):
    def generate(
        self,
        *,
        model: str,
        contents: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_instruction: str,
        max_output_tokens: int,
        timeout_seconds: float,
        thinking_level: str | None = None,
    ) -> ModelResponse: ...


def content_from_dict(value: dict[str, Any]) -> Any:
    from google.genai import types

    return types.Content.model_validate(value)


def content_to_dict(content: Any) -> dict[str, Any]:
    return content.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


class GeminiBackend:
    """Official google-genai transport with manual function-call handling."""

    def __init__(self, *, api_key: str, api_key_source: str, retry_attempts: int | None = None) -> None:
        self._api_key = api_key
        self.api_key_source = api_key_source
        self.retry_attempts = retry_attempts

    @classmethod
    def from_settings(cls, settings: Settings) -> "GeminiBackend":
        credential = load_credential()
        return cls(api_key=credential.key, api_key_source=credential.source)

    def generate(
        self,
        *,
        model: str,
        contents: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_instruction: str,
        max_output_tokens: int,
        timeout_seconds: float,
        thinking_level: str | None = None,
    ) -> ModelResponse:
        from google import genai
        from google.genai import types

        timeout_ms = max(100, int(timeout_seconds * 1000))
        client = None
        try:
            client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(
                    timeout=timeout_ms,
                    retry_options=(types.HttpRetryOptions(attempts=self.retry_attempts)
                                   if self.retry_attempts is not None else None),
                ),
            )
            declarations = [
                types.FunctionDeclaration(
                    name=tool["name"],
                    description=tool["description"],
                    parameters_json_schema=tool["parameters_json_schema"],
                )
                for tool in tools
            ]
            tool_list = (
                [types.Tool(function_declarations=declarations)]
                if declarations
                else None
            )
            thinking_config = None
            if thinking_level is not None:
                if getattr(types.ThinkingLevel, thinking_level.upper(), None) is None:
                    raise BridgeError(
                        "reasoning_unavailable",
                        "The installed google-genai SDK does not support that thinking level.",
                        details={"phase": "PRE_SUBMIT", "retry_safe": True,
                                 "requested_effort": thinking_level},
                    )
                thinking_config = types.ThinkingConfig(
                    thinking_level=thinking_level.upper()
                )
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=max_output_tokens,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
                tools=tool_list,
                thinking_config=thinking_config,
            )
            typed_contents = [
                types.Content.model_validate(item) for item in contents
            ]
            response = client.models.generate_content(
                model=model,
                contents=typed_contents,
                config=config,
            )
        except BridgeError:
            raise
        except Exception as exc:
            import httpx

            safe = redact_text(
                f"{type(exc).__name__}: {exc}", [self._api_key]
            )
            code = getattr(exc, "code", None)
            status = code if isinstance(code, int) else None
            # Preserve structured failure evidence for the writing router. A
            # transport timeout leaves delivery unknown and must not be replayed.
            details = {
                "selected_model": model,
                "http_status": status,
                "api_status": str(getattr(exc, "status", "") or ""),
                "outcome": "confirmed_failure" if status in {
                    400, 401, 403, 404, 413, 415, 429, 500, 502, 503
                } else "unknown",
            }
            if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)):
                details["outcome"] = "not_submitted"
            raise BridgeError(
                "gemini_api_error",
                f"Gemini API request failed: {safe}",
                details=details,
            ) from None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

        if not response.candidates:
            feedback = ""
            if response.prompt_feedback is not None:
                feedback = redact_text(str(response.prompt_feedback), [self._api_key])
            raise BridgeError(
                "gemini_no_candidate",
                "Gemini returned no candidate."
                + (f" Prompt feedback: {feedback}" if feedback else ""),
                details={"selected_model": model},
            )
        candidate = response.candidates[0]
        if candidate.content is None:
            raise BridgeError(
                "gemini_empty_content",
                "Gemini returned a candidate without model content.",
                details={"selected_model": model},
            )
        content = content_to_dict(candidate.content)
        text_parts: list[str] = []
        calls: list[FunctionCallRequest] = []
        for part in candidate.content.parts or []:
            if part.function_call is not None:
                name = part.function_call.name or ""
                args = part.function_call.args or {}
                calls.append(
                    FunctionCallRequest(
                        name=name,
                        args=dict(args) if isinstance(args, dict) else {},
                    )
                )
            elif part.text and not part.thought:
                text_parts.append(part.text)
        finish_reason = (
            candidate.finish_reason.value
            if candidate.finish_reason is not None
            else None
        )
        finish_message = redact_text(candidate.finish_message, [self._api_key])
        return ModelResponse(
            content=content,
            text="".join(text_parts),
            function_calls=tuple(calls),
            finish_reason=finish_reason,
            usage=usage_dict(response.usage_metadata),
            model_version=response.model_version,
            response_id=response.response_id,
            finish_message=finish_message or None,
        )


def list_available_models() -> dict[str, Any]:
    """Explicit network call. Never invoked by status."""
    from google import genai
    from google.genai import types

    credential = load_credential()
    client = None
    models: list[dict[str, Any]] = []
    try:
        client = genai.Client(
            api_key=credential.key,
            http_options=types.HttpOptions(timeout=30_000),
        )
        pager = client.models.list(config={"page_size": 100})
        for model in pager:
            name = model.name or ""
            models.append(
                {
                    "name": name,
                    "id": name[7:] if name.startswith("models/") else name,
                    "display_name": model.display_name,
                    "description": model.description,
                    "version": model.version,
                    "input_token_limit": model.input_token_limit,
                    "output_token_limit": model.output_token_limit,
                    "supported_actions": list(model.supported_actions or []),
                }
            )
            if len(models) >= 500:
                break
    except BridgeError:
        raise
    except Exception as exc:
        safe = redact_text(f"{type(exc).__name__}: {exc}", [credential.key])
        raise BridgeError(
            "gemini_api_error", f"Gemini model listing failed: {safe}"
        ) from None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    models.sort(key=lambda item: item["name"])
    return {
        "models": models,
        "count": len(models),
        "credential_source": credential.source,
        "service_version": __version__,
    }
