from __future__ import annotations

import threading
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .errors import BridgeError
from .plugin_settings import SettingsStore, launch_settings_panel, merge_writing_status
from .redact import redact_text, redact_value
from .service import RunService
from .writing import WritingService
from .writing_routes import ROUTE_NAMES

_INSTRUCTIONS = """Gemini Baton runs durable Gemini tasks for Codex.

Use gemini_write for end-to-end human-facing writing. Supply the task, cwd,
selected file_paths and a stable request_id; pass conversation_id to continue
an existing AI Studio/Web conversation. The plugin owns the Antigravity CLI ->
API -> AI Studio -> Web fallback chain (16 routes): each channel tries 4 Argon
(High), 4 Carbon (High), 4 Neon (xHigh), then 3.8 Flash (High), with exact
model/effort receipts such as cli:gemini-4-argon-high, api:gemini-4-argon, or
aistudio:4 Argon. Hydrogen remains available only through an explicit pinned
route. The CLI runs in an empty temporary cwd with all tools, files, MCP and
slash commands denied, and its init model is verified before a draft is
accepted. The plugin also owns file conversion and unresolved-delivery
protection. It returns the full result or error, or a durable job_id if still
running; then use gemini_write_result. Selected files and prompts are sent to
Google. Writing exposes no shell or workspace tools. An explicit route pins a
single channel/model, including official model-ID aliases such as
aistudio:gemini-4-hydrogen, api:gemini-4-hydrogen, or
cli:gemini-4-hydrogen-high. Use route="aistudio:4 Argon" to select AI Studio
Web with subscription billing verification.

Use gemini_settings to open the authenticated loopback settings panel, or
action="status" to read the effective ordered route policy and private-key
presence without starting a server or exposing a key. Saved settings are
snapshotted into each new writing job; existing jobs retain their recorded
routes. gemini_status reports the current effective writing routes.

Use gemini_send to start a task. It returns immediately with a run_id and
session_id; a detached worker keeps running if this MCP server exits. Poll with
gemini_wait (<=50 seconds) or call gemini_result later. Continue a conversation
by passing session_id; cwd is immutable, and model/permissions persist unless
explicitly changed. A busy session rejects another send (there is no queue).

Sessions start read-only with shell disabled. Pass permission_mode and
allow_shell=true only when shell execution is necessary. Shell commands use argv
arrays and have no OS sandbox. checks run once, in argv form, after the final
answer and only when shell is enabled. gemini_status makes no network call;
gemini_list_models makes an explicit network request.
"""

mcp = FastMCP(
    name="gemini-baton",
    instructions=_INSTRUCTIONS,
    log_level="ERROR",
)

_service_lock = threading.Lock()
_service: RunService | None = None


def get_service() -> RunService:
    global _service
    with _service_lock:
        if _service is None:
            _service = RunService(Settings.from_env())
        return _service


def _error(exc: BridgeError) -> dict[str, Any]:
    return {"ok": False, "error": redact_value(exc.as_dict())}


def _unexpected(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "bridge_error",
            "message": redact_text(f"{type(exc).__name__}: {exc}"),
        },
    }


RouteName = Literal[ROUTE_NAMES]


@mcp.tool(
    name="gemini_write",
    description=(
        "End-to-end writing with selected files and Antigravity CLI -> API -> AI Studio -> Web fallback. "
        "Sixteen default routes try 4 Argon High, 4 Carbon High, 4 Neon xHigh and 3.8 Flash High in each channel. "
        "The CLI runs in an empty temporary cwd with tools, files, MCP and slash commands denied; "
        "its actual init model and effort are verified. "
        "Pass prompt, cwd, optional absolute file_paths, and stable request_id for "
        "idempotency. Pass conversation_id to continue an existing AI Studio/Web conversation. "
        "Returns full text and route/model/effort/attachment evidence or error; "
        "a running job returns job_id for gemini_write_result. Wait is capped at 50s. "
        "Task materials are sent to Google. Explicit route pins one supported route."
    ),
)
def gemini_write(
    prompt: str,
    cwd: str,
    file_paths: list[str] | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
    route: RouteName | None = None,
    wait_seconds: float = 45,
    route_timeout_seconds: int = 300,
) -> dict[str, Any]:
    try:
        return WritingService(get_service().settings).submit(
            prompt, cwd=cwd, file_paths=file_paths, request_id=request_id, route=route,
            conversation_id=conversation_id,
            wait_seconds=wait_seconds, route_timeout_seconds=route_timeout_seconds,
        )
    except BridgeError as exc:
        return _error(exc)
    except Exception as exc:
        return _unexpected(exc)


@mcp.tool(
    name="gemini_write_result",
    description=(
        "Read or wait for the same durable writing job; never resubmits a model request. "
        "Returns full text, actual model, route attempts, files and errors. "
        "Use wait_seconds=30 for an active job (maximum 50)."
    ),
)
def gemini_write_result(job_id: str, wait_seconds: float = 0) -> dict[str, Any]:
    try:
        return WritingService(get_service().settings).result(job_id, wait_seconds=wait_seconds)
    except BridgeError as exc:
        return _error(exc)
    except Exception as exc:
        return _unexpected(exc)


SettingsAction = Literal["open", "status"]


@mcp.tool(
    name="gemini_settings",
    description=(
        "Open the local authenticated settings panel in a browser (default), "
        "or return effective writing route settings with action=\"status\". "
        "The panel binds 127.0.0.1 only, uses a one-time loopback session and "
        "CSRF token, and saves ordered enabled routes plus an optional API key "
        "in private external state. API keys are never returned by status, "
        "HTML, logs or errors. Login buttons delegate the existing Gemini Web "
        "bridge login; Antigravity instructions use the verified CLI ACP login. "
        "No model or network request is made by this tool."
    ),
)
def gemini_settings(action: SettingsAction = "open") -> dict[str, Any]:
    try:
        service = get_service()
        if action == "status":
            status = SettingsStore(service.settings).public_status()
            return {"ok": True, "action": "status", **status}
        if action == "open":
            result = launch_settings_panel(service.settings)
            return {"action": "open", **result}
        raise BridgeError("invalid_action", "action must be open or status.")
    except BridgeError as exc:
        return _error(exc)
    except Exception as exc:
        return _unexpected(exc)


@mcp.tool(
    name="gemini_status",
    description=(
        "Local-only status: credential presence/source (never the secret), "
        "installed SDK versions, configured model, effective writing routes, "
        "state directory, and limits. Reconciles already-persisted dead "
        "workers; makes no network or paid model call."
    ),
)
def gemini_status() -> dict[str, Any]:
    try:
        service = get_service()
        return merge_writing_status(service.status(), service.settings)
    except BridgeError as exc:
        return _error(exc)
    except Exception as exc:
        return _unexpected(exc)


@mcp.tool(
    name="gemini_list_models",
    description=(
        "Explicitly query the Gemini models endpoint and return models available "
        "to the configured API key. Requires a network call and credentials, but "
        "does not generate content."
    ),
)
def gemini_list_models() -> dict[str, Any]:
    try:
        return get_service().list_models()
    except BridgeError as exc:
        return _error(exc)
    except Exception as exc:
        return _unexpected(exc)


@mcp.tool(
    name="gemini_send",
    description=(
        "Start a background Gemini subagent run and return immediately. cwd is "
        "required for a new session and immutable thereafter; omit it when "
        "continuing. permission_mode is read-only or workspace-write. Shell "
        "defaults off, requires workspace-write, and is inherited on continuation "
        "unless explicitly supplied. checks are argv arrays run once after the "
        "final answer (shell required). One active run per session; no queue."
    ),
)
def gemini_send(
    message: str,
    cwd: str | None = None,
    session_id: str | None = None,
    model: str | None = None,
    permission_mode: Literal["read-only", "workspace-write"] | None = None,
    allow_shell: bool | None = None,
    timeout_seconds: int | None = None,
    max_steps: int | None = None,
    checks: list[list[str]] | None = None,
) -> dict[str, Any]:
    try:
        return get_service().send(
            message,
            cwd=cwd,
            session_id=session_id,
            model=model,
            permission_mode=permission_mode,
            allow_shell=allow_shell,
            timeout_seconds=timeout_seconds,
            max_steps=max_steps,
            checks=checks,
        )
    except BridgeError as exc:
        return _error(exc)
    except Exception as exc:
        return _unexpected(exc)


@mcp.tool(
    name="gemini_wait",
    description=(
        "Wait for a run for at most 50 seconds. Returns status, a short preview, "
        "and artifact paths. It is safe to call repeatedly; 50 seconds is a hard "
        "cap to fit MCP/Codex tool timeouts."
    ),
)
def gemini_wait(run_id: str, timeout_seconds: float = 20.0) -> dict[str, Any]:
    try:
        return get_service().wait(run_id, timeout_seconds)
    except BridgeError as exc:
        return _error(exc)
    except Exception as exc:
        return _unexpected(exc)


@mcp.tool(
    name="gemini_result",
    description=(
        "Read a persisted run result after restart or completion. compact=true "
        "(default) truncates the final text but always includes full-text and "
        "artifact paths, model/usage/finish reason, changed files, checks, and "
        "errors. compact=false returns the full final text."
    ),
)
def gemini_result(run_id: str, compact: bool = True) -> dict[str, Any]:
    try:
        return get_service().result(run_id, compact)
    except BridgeError as exc:
        return _error(exc)
    except Exception as exc:
        return _unexpected(exc)


@mcp.tool(
    name="gemini_cancel",
    description=(
        "Cancel a run: mark cancellation intent, terminate the worker process "
        "group (including in-flight commands/checks), verify it is gone, and "
        "persist status=cancelled. The current turn is not checkpointed."
    ),
)
def gemini_cancel(run_id: str) -> dict[str, Any]:
    try:
        return get_service().cancel(run_id)
    except BridgeError as exc:
        return _error(exc)
    except Exception as exc:
        return _unexpected(exc)


@mcp.tool(
    name="gemini_list_sessions",
    description=(
        "List persisted sessions with cwd, model, permission defaults, and the "
        "latest run status. Recovers workers that died."
    ),
)
def gemini_list_sessions(limit: int = 20) -> dict[str, Any]:
    try:
        return get_service().list_sessions(limit)
    except BridgeError as exc:
        return _error(exc)
    except Exception as exc:
        return _unexpected(exc)


def run() -> None:
    # Reconcile dead workers before serving when a state database already
    # exists. This is local; it makes no Gemini call and requires no credential.
    try:
        service = get_service()
        if service.settings.db_path.exists():
            service.recover_stale()
    except Exception:
        pass
    mcp.run(transport="stdio")
