from __future__ import annotations

import hashlib
import importlib.metadata
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .backend import list_available_models
from .config import (
    FINAL_PREVIEW_CHARS,
    MAX_CHECKS,
    MAX_MESSAGE_CHARS,
    MAX_STEPS_LIMIT,
    MAX_TIMEOUT_SECONDS,
    MAX_WAIT_SECONDS,
    STARTUP_GRACE_SECONDS,
    TIMEOUT_KILL_GRACE_SECONDS,
    WAIT_POLL_INTERVAL_SECONDS,
    Settings,
)
from .writing_routes import CLI_MODEL_IDS, OPT_IN_ROUTES, ROUTES, supported_api_efforts
from .credentials import credential_status
from .errors import BridgeError
from .paths import StateLayout, atomic_write_text
from .processes import (
    group_alive,
    read_worker_lock,
    reap_child,
    terminate_process_group,
    worker_lock_held,
    worker_lock_path,
)
from .redact import redact_text, redact_value
from .store import ACTIVE_STATUSES, Store
from .tools import _validate_argv
from .util import (
    elapsed_seconds,
    json_dumps,
    json_loads,
    parse_iso,
    preview_text,
    truncate_text,
    utc_now,
    utc_now_iso,
)
from .artifacts import RunArtifacts

_READ_ONLY = "read-only"
_WORKSPACE_WRITE = "workspace-write"
_VALID_PERMISSIONS = {_READ_ONLY, _WORKSPACE_WRITE}

_LIMITATIONS = [
    "No OS sandbox. Built-in file tools use heuristic path filtering, but "
    "run_command (when explicitly enabled) can read or modify anything the "
    "invoking user can, including credentials outside the workspace.",
    "Sensitive-path checks are heuristic and not race-free (TOCTOU).",
    "changed_files tracks successful write_file/edit_file calls only; it cannot "
    "observe files changed by run_command.",
    "The session transcript checkpoints only after a run succeeds and all "
    "requested checks pass. A failed or cancelled turn is not appended to the "
    "conversation, although files written by its tools remain on disk.",
    "A busy session rejects concurrent sends. There is no queue or retry.",
    "Worker lifecycle uses POSIX process groups and advisory locks; command "
    "descendant cleanup uses /bin/ps when the host permits process enumeration. "
    "Worker lifecycle management is supported on macOS and Linux, not Windows.",
    "Dead-worker recovery is event-driven (MCP startup or a later tool call), "
    "not a continuously running watchdog.",
    "Cancelled runs may not have aggregate usage/actual-model/finish-reason "
    "fields because the worker is terminated before finalization.",
]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _default_transcript() -> list[dict[str, Any]]:
    return []


def _parse_transcript(raw: str) -> list[dict[str, Any]]:
    value = json_loads(raw, default=None)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _session_defaults(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "cwd": row["cwd"],
        "model": row["model"],
        "permission_mode": row["permission_mode"],
        "allow_shell": bool(row["allow_shell"]),
    }


def _versions() -> dict[str, str]:
    def version(name: str) -> str:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return "unknown"

    return {
        "gemini-baton": __version__,
        "google-genai": version("google-genai"),
        "mcp": version("mcp"),
        "python": sys.version.split()[0],
    }


def _worker_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _age_seconds(iso: str | None) -> float | None:
    parsed = parse_iso(iso)
    if parsed is None:
        return None
    return max(0.0, (utc_now() - parsed).total_seconds())


class RunService:
    def __init__(
        self,
        settings: Settings,
        *,
        worker_command: Sequence[str] | None = None,
    ) -> None:
        self.settings = settings
        self._layout: StateLayout | None = None
        self._store: Store | None = None
        self._state_lock = threading.RLock()
        self._children: dict[str, Any] = {}
        self._children_lock = threading.Lock()
        # Internal dependency injection point used by tests. MCP/CLI never set it.
        self._worker_command = list(worker_command) if worker_command else None

    # -- setup -----------------------------------------------------------
    def layout(self) -> StateLayout:
        with self._state_lock:
            if self._layout is None:
                self._layout = StateLayout(self.settings.state_dir)
            return self._layout

    def store(self) -> Store:
        with self._state_lock:
            if self._store is None:
                self._store = Store(self.layout())
            return self._store

    def status(self) -> dict[str, Any]:
        # Status remains local/no-network, but if state already exists it also
        # reconciles dead workers so a crash does not stay "running" forever
        # behind an innocuous status call.
        if self.settings.db_path.exists():
            try:
                self.recover_stale()
            except Exception:
                # Status itself must stay available even if state is damaged.
                pass
        credentials = credential_status()
        credentials["precedence"] = [
            "GEMINI_API_KEY environment variable",
            "GOOGLE_API_KEY environment variable",
            "GEMINI_BATON_ENV_FILE (only GEMINI_API_KEY/GOOGLE_API_KEY, "
            "parsed as data)",
            "macOS Keychain service gemini-baton / account GEMINI_API_KEY",
        ]
        return {
            "ok": True,
            "service": "gemini-baton",
            "version": __version__,
            "credentials": credentials,
            "model": {
                "configured": self.settings.model,
                "source": self.settings.model_source,
                "fallback_allowed": False,
            },
            "versions": _versions(),
            "state_dir": str(self.settings.state_dir),
            "writing": {
                "entrypoint": "gemini_write",
                "result_tool": "gemini_write_result",
                "fallback_managed": True,
                "routes": [f"{channel}:{model}" for channel, model, _effort in ROUTES],
                "route_policy": [
                    {"channel": channel, "model": model, "effort": effort}
                    for channel, model, effort in ROUTES
                ],
                "opt_in_routes": list(OPT_IN_ROUTES),
                "cli": {
                    "binary_on_path": shutil.which("agy") is not None,
                    "model_ids": sorted(set(CLI_MODEL_IDS.values())),
                    "sandbox": True,
                    "tools_disabled": True,
                },
                "api_thinking_levels": list(supported_api_efforts()),
                "api_max_concurrent_writing_calls": 1,
                "web_max_concurrent_requests": 3,
            },
            "limits": self.settings.limits(),
            "network_calls": 0,
            "paid_model_calls": 0,
        }

    # -- send ------------------------------------------------------------
    def send(
        self,
        message: str,
        *,
        cwd: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        permission_mode: str | None = None,
        allow_shell: bool | None = None,
        timeout_seconds: int | None = None,
        max_steps: int | None = None,
        checks: list[list[str]] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip():
            raise BridgeError("invalid_message", "message must be non-empty text.")
        if len(message) > MAX_MESSAGE_CHARS:
            raise BridgeError(
                "invalid_message",
                f"message exceeds the {MAX_MESSAGE_CHARS}-character limit.",
            )
        if session_id is not None and not isinstance(session_id, str):
            raise BridgeError("invalid_arguments", "session_id must be a string.")
        if model is not None:
            if not isinstance(model, str) or not model.strip():
                raise BridgeError(
                    "invalid_arguments", "model must be a non-empty string."
                )
            model = model.strip()
        if permission_mode is not None and permission_mode not in _VALID_PERMISSIONS:
            raise BridgeError(
                "invalid_permission_mode",
                "permission_mode must be read-only or workspace-write.",
            )
        if allow_shell is not None and not isinstance(allow_shell, bool):
            raise BridgeError("invalid_arguments", "allow_shell must be boolean.")
        timeout = self._bounded_int(
            timeout_seconds,
            "timeout_seconds",
            self.settings.default_timeout_seconds,
            1,
            MAX_TIMEOUT_SECONDS,
        )
        steps = self._bounded_int(
            max_steps,
            "max_steps",
            self.settings.default_max_steps,
            1,
            MAX_STEPS_LIMIT,
        )
        validated_checks = self._validate_checks(checks)
        credentials = credential_status()
        if not credentials.get("present"):
            raise BridgeError(
                "credentials_missing",
                "No usable Gemini API credential. Set GEMINI_API_KEY or "
                "GOOGLE_API_KEY, set GEMINI_BATON_ENV_FILE to an explicit .env "
                "file containing one of those names, or run `gemini-baton "
                "auth-set` on macOS.",
                details={"credential_env_file": credentials.get("env_file")},
            )

        supplied_cwd: Path | None = None
        if cwd is not None:
            from .workspace import resolve_workspace

            supplied_cwd = resolve_workspace(cwd)

        store = self.store()
        self.recover_stale()
        now = utc_now()
        now_iso = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        deadline_iso = (now + timedelta(seconds=timeout)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        run_id = f"run_{secrets.token_hex(16)}"
        new_session_id = f"ses_{secrets.token_hex(16)}"
        artifact_dir = self.layout().run_dir(run_id)
        checks_json = json_dumps(validated_checks)
        token = secrets.token_urlsafe(32)
        token_hash = _worker_token_hash(token)

        try:
            with store.transaction() as conn:
                if session_id is not None:
                    row = conn.execute(
                        "SELECT * FROM sessions WHERE id = ?", (session_id,)
                    ).fetchone()
                    if row is None:
                        raise BridgeError(
                            "unknown_session", "No such session.",
                            details={"session_id": session_id},
                        )
                    session = dict(row)
                    if bool(
                        conn.execute(
                            "SELECT 1 FROM runs WHERE session_id = ? AND status "
                            "IN ('running','cancelling')",
                            (session_id,),
                        ).fetchone()
                    ):
                        active = conn.execute(
                            "SELECT id FROM runs WHERE session_id = ? AND status "
                            "IN ('running','cancelling')",
                            (session_id,),
                        ).fetchone()
                        raise BridgeError(
                            "session_busy",
                            "The session already has an active run; concurrent "
                            "sends are refused (no queue).",
                            details={
                                "session_id": session_id,
                                "active_run_id": active[0] if active else None,
                            },
                        )
                    if supplied_cwd is not None and str(supplied_cwd) != session["cwd"]:
                        raise BridgeError(
                            "cwd_mismatch",
                            "A session's cwd is immutable. Omit cwd to reuse it or "
                            "start a new session.",
                            details={"session_cwd": session["cwd"]},
                        )
                    session_cwd = Path(session["cwd"])
                    if not session_cwd.is_dir():
                        raise BridgeError(
                            "cwd_unavailable",
                            "The session's original cwd no longer exists.",
                        )
                    # The session stores a canonical realpath. If the path has
                    # since become a symlink (or its parents now resolve
                    # elsewhere), reject instead of silently changing workspace.
                    from .workspace import resolve_workspace

                    try:
                        current_cwd = resolve_workspace(session["cwd"])
                    except BridgeError:
                        raise BridgeError(
                            "cwd_unavailable",
                            "The session's original cwd cannot be resolved.",
                        ) from None
                    if str(current_cwd) != session["cwd"]:
                        raise BridgeError(
                            "cwd_changed",
                            "The session cwd no longer resolves to its original "
                            "real path; start a new session for the new location.",
                            details={"session_cwd": session["cwd"]},
                        )
                    effective_model = model or session["model"]
                    effective_mode = permission_mode or session["permission_mode"]
                    if allow_shell is None:
                        effective_shell = bool(session["allow_shell"])
                        if effective_mode == _READ_ONLY:
                            effective_shell = False
                    else:
                        effective_shell = allow_shell
                    if effective_shell and effective_mode != _WORKSPACE_WRITE:
                        raise BridgeError(
                            "invalid_permission_mode",
                            "allow_shell=true requires workspace-write mode.",
                        )
                    if validated_checks and not effective_shell:
                        raise BridgeError(
                            "invalid_checks",
                            "checks require allow_shell=true and workspace-write.",
                        )
                    if conn.execute(
                        "SELECT COUNT(*) FROM runs WHERE status IN "
                        "('running','cancelling')"
                    ).fetchone()[0] >= self.settings.max_active_runs:
                        raise BridgeError(
                            "concurrency_limit",
                            f"At most {self.settings.max_active_runs} active runs "
                            "are allowed; no queue is implemented.",
                        )
                    conn.execute(
                        "UPDATE sessions SET model=?, permission_mode=?, "
                        "allow_shell=?, updated_at=?, last_run_id=? WHERE id=?",
                        (
                            effective_model,
                            effective_mode,
                            int(effective_shell),
                            now_iso,
                            run_id,
                            session_id,
                        ),
                    )
                    run_session_id = str(session_id)
                    model_source = (
                        "send:model" if model else "session"
                    )
                    current_session = dict(
                        conn.execute(
                            "SELECT * FROM sessions WHERE id=?", (session_id,)
                        ).fetchone()
                    )
                else:
                    if supplied_cwd is None:
                        raise BridgeError(
                            "invalid_cwd",
                            "cwd is required when starting a new session.",
                        )
                    effective_model = model or self.settings.model
                    effective_mode = permission_mode or _READ_ONLY
                    if allow_shell is None:
                        effective_shell = False
                    else:
                        effective_shell = allow_shell
                    if effective_shell and effective_mode != _WORKSPACE_WRITE:
                        raise BridgeError(
                            "invalid_permission_mode",
                            "allow_shell=true requires workspace-write mode.",
                        )
                    if validated_checks and not effective_shell:
                        raise BridgeError(
                            "invalid_checks",
                            "checks require allow_shell=true and workspace-write.",
                        )
                    if conn.execute(
                        "SELECT COUNT(*) FROM runs WHERE status IN "
                        "('running','cancelling')"
                    ).fetchone()[0] >= self.settings.max_active_runs:
                        raise BridgeError(
                            "concurrency_limit",
                            f"At most {self.settings.max_active_runs} active runs "
                            "are allowed; no queue is implemented.",
                        )
                    empty_transcript = json_dumps(_default_transcript())
                    transcript_sha = _sha256_text(empty_transcript)
                    run_session_id = new_session_id
                    model_source = (
                        "send:model" if model else self.settings.model_source
                    )
                    conn.execute(
                        "INSERT INTO sessions (id, cwd, model, permission_mode, "
                        "allow_shell, transcript_json, transcript_sha256, "
                        "created_at, updated_at, last_run_id) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?)",
                        (
                            new_session_id,
                            str(supplied_cwd),
                            effective_model,
                            effective_mode,
                            int(effective_shell),
                            empty_transcript,
                            transcript_sha,
                            now_iso,
                            now_iso,
                            run_id,
                        ),
                    )
                    current_session = dict(
                        conn.execute(
                            "SELECT * FROM sessions WHERE id=?", (new_session_id,)
                        ).fetchone()
                    )
                conn.execute(
                    "INSERT INTO runs (id, session_id, status, prompt, cwd, model, "
                    "selected_model, permission_mode, allow_shell, checks_json, "
                    "timeout_seconds, max_steps, max_output_tokens, "
                    "max_tool_output_chars, created_at, updated_at, deadline_at, "
                    "worker_token_hash, artifact_dir) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        run_session_id,
                        "running",
                        message,
                        str(supplied_cwd or current_session["cwd"]),
                        effective_model,
                        effective_model,
                        effective_mode,
                        int(effective_shell),
                        checks_json,
                        float(timeout),
                        steps,
                        self.settings.max_output_tokens,
                        self.settings.max_tool_output_chars,
                        now_iso,
                        now_iso,
                        deadline_iso,
                        token_hash,
                        str(artifact_dir),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise BridgeError(
                "session_busy",
                "The session already has an active run; concurrent sends are "
                "refused (no queue).",
            ) from exc
        except BridgeError:
            raise

        prompt_path = artifact_dir / "prompt.txt"
        try:
            atomic_write_text(prompt_path, message)
            atomic_write_text(artifact_dir / "checks.json", json_dumps(validated_checks, pretty=True))
            artifacts = RunArtifacts(self.layout(), run_id)
            artifacts.event(
                "run_created",
                session_id=run_session_id,
                selected_model=effective_model,
                permission_mode=effective_mode,
                allow_shell=bool(effective_shell),
                timeout_seconds=timeout,
                max_steps=steps,
            )
        except BridgeError as exc:
            self._mark_spawn_failure(run_id, token_hash, exc.as_dict())
            return self._send_payload(
                store.get_run(run_id),  # type: ignore[arg-type]
                current_session,
                model_source,
                spawn_error=exc.as_dict(),
            )

        spawn_error: dict[str, Any] | None = None
        try:
            self._spawn_worker(run_id, token)
        except Exception as exc:
            spawn_error = {
                "code": "worker_spawn_failed",
                "message": redact_text(
                    f"{type(exc).__name__}: could not start background worker"
                ),
            }
            self._mark_spawn_failure(run_id, token_hash, spawn_error)

        row = store.get_run(run_id)
        if row is None:
            raise BridgeError("state_error", "Run row disappeared after creation.")
        return self._send_payload(
            row, current_session, model_source, spawn_error=spawn_error
        )

    def _spawn_worker(self, run_id: str, token: str) -> None:
        if os.name != "posix":
            raise BridgeError(
                "unsupported_platform",
                "Background worker process groups require POSIX (macOS/Linux).",
            )
        artifact_dir = self.layout().run_dir(run_id)
        log_path = artifact_dir / "worker.log"
        log_fd = self.layout().open_log(log_path)
        command = list(self._worker_command) if self._worker_command else [
            sys.executable,
            "-m",
            "gemini_baton.worker",
        ]
        command.extend(
            ["--run-id", run_id, "--state-dir", str(self.settings.state_dir)]
        )
        env = os.environ.copy()
        env["GEMINI_BATON_STATE_DIR"] = str(self.settings.state_dir)
        env["GEMINI_BATON_WORKER_TOKEN"] = token
        env["PYTHONUNBUFFERED"] = "1"
        # The worker runs with state_dir as its process cwd. Resolve an explicit
        # credential env-file path in the caller's context so relative paths mean
        # the same thing in parent and worker.
        env_file = env.get("GEMINI_BATON_ENV_FILE")
        if env_file:
            env["GEMINI_BATON_ENV_FILE"] = str(
                Path(env_file).expanduser().resolve()
            )
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.settings.state_dir),
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(log_fd)
        with self._children_lock:
            self._children[run_id] = process
        started_at = utc_now_iso()
        with self.store().transaction() as conn:
            conn.execute(
                "UPDATE runs SET worker_pid=?, worker_pgid=?, "
                "worker_started_at=?, started_at=COALESCE(started_at, ?), "
                "updated_at=? WHERE id=? AND status='running'",
                (
                    process.pid,
                    process.pid,
                    started_at,
                    started_at,
                    started_at,
                    run_id,
                ),
            )
        RunArtifacts(self.layout(), run_id).event(
            "worker_spawned",
            pid=process.pid,
            pgid=process.pid,
            log_path=str(log_path),
            liveness="worker-held advisory lock",
        )

    def _mark_spawn_failure(
        self, run_id: str, token_hash: str, error: dict[str, Any]
    ) -> None:
        now = utc_now_iso()
        try:
            with self.store().transaction() as conn:
                conn.execute(
                    "UPDATE runs SET status='failed', ended_at=?, updated_at=?, "
                    "error_json=? WHERE id=? AND status='running' AND "
                    "worker_token_hash=?",
                    (now, now, json_dumps(error), run_id, token_hash),
                )
        except BridgeError:
            pass

    def _send_payload(
        self,
        row: dict[str, Any],
        session: dict[str, Any],
        model_source: str,
        *,
        spawn_error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        artifacts = self.layout().run_dir(row["id"])
        payload: dict[str, Any] = {
            "ok": True,
            "run_id": row["id"],
            "session_id": row["session_id"],
            "status": row["status"],
            "selected_model": row["selected_model"],
            "model_source": model_source,
            "cwd": row["cwd"],
            "permission_mode": row["permission_mode"],
            "allow_shell": bool(row["allow_shell"]),
            "timeout_seconds": row["timeout_seconds"],
            "max_steps": row["max_steps"],
            "session_defaults": _session_defaults(session),
            "artifact_dir": str(artifacts),
            "events_path": str(artifacts / "events.jsonl"),
        }
        if spawn_error is not None:
            payload["status"] = "failed"
            payload["ok"] = False
            payload["error"] = spawn_error
        if row["status"] in ("failed", "interrupted", "cancelled"):
            payload["ok"] = False
            payload["error"] = json_loads(row.get("error_json"), default={})
        return payload

    @staticmethod
    def _bounded_int(
        value: int | None,
        name: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise BridgeError("invalid_arguments", f"{name} must be an integer.")
        if not minimum <= value <= maximum:
            raise BridgeError(
                "invalid_arguments",
                f"{name} must be between {minimum} and {maximum}.",
            )
        return value

    def _validate_checks(
        self, checks: list[list[str]] | None
    ) -> list[list[str]]:
        if checks is None:
            return []
        if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
            raise BridgeError(
                "invalid_checks",
                f"checks must be a list of at most {MAX_CHECKS} argv arrays.",
            )
        result: list[list[str]] = []
        for index, argv in enumerate(checks):
            try:
                result.append(_validate_argv(argv, field=f"checks[{index}]"))
            except BridgeError as exc:
                raise BridgeError("invalid_checks", exc.message) from None
        return result

    # -- reads -----------------------------------------------------------
    def list_sessions(self, limit: int = 20) -> dict[str, Any]:
        limit = self._bounded_int(limit, "limit", 20, 1, 100)
        self.recover_stale()
        rows = self.store().list_sessions(limit)
        sessions = []
        for row in rows:
            sessions.append(
                {
                    "session_id": row["id"],
                    "cwd": row["cwd"],
                    "model": row["model"],
                    "permission_mode": row["permission_mode"],
                    "allow_shell": bool(row["allow_shell"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "last_run_id": row["latest_run_id"],
                    "last_run_status": row["latest_run_status"],
                }
            )
        return {"ok": True, "sessions": sessions, "count": len(sessions)}

    def wait(self, run_id: str, timeout_seconds: float = 20.0) -> dict[str, Any]:
        if not isinstance(timeout_seconds, (int, float)) or isinstance(
            timeout_seconds, bool
        ):
            raise BridgeError(
                "invalid_arguments", "timeout_seconds must be a number."
            )
        timeout_seconds = float(timeout_seconds)
        if not 0 <= timeout_seconds <= MAX_WAIT_SECONDS:
            raise BridgeError(
                "invalid_arguments",
                f"timeout_seconds must be between 0 and {MAX_WAIT_SECONDS}.",
            )
        run = self._get_run_required(run_id)
        deadline = time.monotonic() + timeout_seconds
        while True:
            self.recover_stale()
            run = self._get_run_required(run_id)
            if run["status"] not in ACTIVE_STATUSES:
                return self._wait_payload(run, timed_out=False)
            if time.monotonic() >= deadline:
                return self._wait_payload(run, timed_out=True)
            time.sleep(
                min(WAIT_POLL_INTERVAL_SECONDS, max(0.02, deadline - time.monotonic()))
            )

    def _wait_payload(
        self, row: dict[str, Any], *, timed_out: bool
    ) -> dict[str, Any]:
        final_text = row.get("final_text") or ""
        final_path = self.layout().run_dir(row["id"]) / "final.md"
        error = json_loads(row.get("error_json"), default=None)
        preview = ""
        if final_text:
            preview = preview_text(
                final_text,
                FINAL_PREVIEW_CHARS,
                path=str(final_path) if final_path.exists() else None,
            )
        elif error:
            preview = redact_text(error.get("message", ""))
        artifacts = self.layout().run_dir(row["id"])
        return {
            "ok": True,
            "run_id": row["id"],
            "session_id": row["session_id"],
            "status": row["status"],
            "done": row["status"] not in ACTIVE_STATUSES,
            "timed_out": timed_out,
            "selected_model": row["selected_model"],
            "actual_model_version": row.get("actual_model_version"),
            "finish_reason": row.get("finish_reason"),
            "preview": preview,
            "error": redact_value(error) if error else None,
            "result_path": str(artifacts / "result.json"),
            "events_path": str(artifacts / "events.jsonl"),
            "final_text_path": str(final_path) if final_path.exists() else None,
        }

    def result(self, run_id: str, compact: bool = True) -> dict[str, Any]:
        if not isinstance(compact, bool):
            raise BridgeError("invalid_arguments", "compact must be boolean.")
        self.recover_stale()
        row = self._get_run_required(run_id)
        if row["status"] in ACTIVE_STATUSES:
            # Re-inspect once in case the worker exited during this request.
            self.recover_stale()
            row = self._get_run_required(run_id)
        run_dir = self.layout().run_dir(run_id)
        final_text = row.get("final_text") or ""
        final_path = run_dir / "final.md"
        final_available = final_path.exists()
        final_output = final_text
        truncated = False
        if compact and final_text:
            final_output, truncated = truncate_text(final_text, FINAL_PREVIEW_CHARS)
            if truncated and final_available:
                final_output += f"\n\n[full text: {final_path}]"
        error = json_loads(row.get("error_json"), default=None)
        checks = json_loads(row.get("checks_result_json"), default=[]) or []
        result_path = run_dir / "result.json"
        payload: dict[str, Any] = {
            "ok": True,
            "run_id": row["id"],
            "session_id": row["session_id"],
            "status": row["status"],
            "done": row["status"] not in ACTIVE_STATUSES,
            "selected_model": row["selected_model"],
            "actual_model_version": row.get("actual_model_version"),
            "finish_reason": row.get("finish_reason"),
            "usage": json_loads(row.get("usage_json"), default={}),
            "final_text": final_output,
            "final_text_truncated": truncated,
            "final_text_path": str(final_path) if final_available else None,
            "changed_files": json_loads(row.get("changed_files_json"), default=[]) or [],
            "checks": checks,
            "error": redact_value(error) if error else None,
            "model_calls": row.get("model_calls", 0),
            "steps": row.get("steps", 0),
            "created_at": row["created_at"],
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
            "elapsed_seconds": round(
                elapsed_seconds(row.get("started_at") or row["created_at"], row.get("ended_at")),
                3,
            ),
            "cwd": row["cwd"],
            "permission_mode": row["permission_mode"],
            "allow_shell": bool(row["allow_shell"]),
            "worker": {
                "pid": row.get("worker_pid"),
                "pgid": row.get("worker_pgid"),
                "identity": row.get("worker_identity"),
                "alive": self._worker_is_alive(row),
                "liveness_source": "worker lock + token",
            },
            "artifacts": {
                "dir": str(run_dir),
                "events": str(run_dir / "events.jsonl"),
                "result": str(result_path) if result_path.exists() else None,
                "final_text": str(final_path) if final_available else None,
                "worker_log": str(run_dir / "worker.log"),
                "transcript": str(self.layout().transcript_path(row["session_id"])),
            },
            "limitations": list(_LIMITATIONS),
        }
        if not compact:
            payload["final_text"] = final_text
            payload["final_text_truncated"] = False
        if row["status"] == "running" and row.get("worker_pid"):
            payload["preview"] = preview_text(final_text, FINAL_PREVIEW_CHARS)
        return payload

    def _worker_is_alive(self, row: dict[str, Any]) -> bool:
        lock_held = worker_lock_held(self._lock_path(row["id"]))
        if lock_held and self._worker_auth(row, lock_held=True)[0]:
            return True
        return self._known_live_child(row["id"], row.get("worker_pid")) is not None

    def _known_live_child(self, run_id: str, pid: int | None) -> Any:
        with self._children_lock:
            process = self._children.get(run_id)
        if process is None or pid is None or process.pid != pid:
            return None
        try:
            if process.poll() is None:
                return process
        except Exception:
            return None
        return None

    def _lock_path(self, run_id: str) -> Path:
        return worker_lock_path(self.layout().run_dir(run_id))

    def _worker_coordinates(
        self, row: dict[str, Any], *, lock_held: bool
    ) -> tuple[int | None, int | None]:
        pid = row.get("worker_pid")
        pgid = row.get("worker_pgid")
        if lock_held and (not pid or not pgid):
            metadata = read_worker_lock(self._lock_path(row["id"])) or {}
            if isinstance(metadata.get("pid"), int):
                pid = metadata["pid"]
            if isinstance(metadata.get("pgid"), int):
                pgid = metadata["pgid"]
        return pid, pgid

    def _worker_auth(
        self, row: dict[str, Any], *, lock_held: bool
    ) -> tuple[bool, dict[str, Any] | None]:
        """Verify the lock metadata, token fingerprint and start identity.

        A held advisory lock prevents ordinary stale-PID signalling, but we also
        bind the lock to the run id, the token hash stored in SQLite, the worker
        instance identity, and the recorded PID/PGID. This makes an accidental
        signal after PID reuse or against a mismatched worker fail closed.
        """
        if not lock_held:
            return False, None
        metadata = read_worker_lock(self._lock_path(row["id"]))
        if not isinstance(metadata, dict):
            return False, None
        if metadata.get("run_id") != row["id"]:
            return False, metadata
        expected_token = row.get("worker_token_hash")
        if expected_token and metadata.get("token_fingerprint") != expected_token:
            return False, metadata
        expected_identity = row.get("worker_identity")
        if expected_identity and metadata.get("identity") != expected_identity:
            return False, metadata
        expected_pid = row.get("worker_pid")
        if expected_pid is not None and metadata.get("pid") != expected_pid:
            return False, metadata
        expected_pgid = row.get("worker_pgid")
        if expected_pgid is not None and metadata.get("pgid") != expected_pgid:
            return False, metadata
        return True, metadata

    def _get_run_required(self, run_id: str) -> dict[str, Any]:
        if not isinstance(run_id, str) or not run_id:
            raise BridgeError("invalid_arguments", "run_id must be non-empty.")
        row = self.store().get_run(run_id)
        if row is None:
            raise BridgeError(
                "unknown_run", "No such run.", details={"run_id": run_id}
            )
        return row

    # -- listing models --------------------------------------------------
    def list_models(self) -> dict[str, Any]:
        listing = list_available_models()
        return {
            "ok": True,
            **listing,
            "selected_model": self.settings.model,
            "network_calls": 1,
            "note": (
                "This call queried the Gemini API models endpoint. It does not "
                "generate content, but network access is required."
            ),
        }

    # -- cancellation ----------------------------------------------------
    def cancel(self, run_id: str) -> dict[str, Any]:
        self._reap_children()
        self.recover_stale()
        row = self._get_run_required(run_id)
        if row["status"] not in ACTIVE_STATUSES:
            return {
                "ok": True,
                "run_id": run_id,
                "status": row["status"],
                "cancelled": row["status"] == "cancelled",
                "message": "Run is already terminal.",
            }
        with self.store().transaction() as conn:
            current = conn.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if current is None:
                raise BridgeError("unknown_run", "No such run.")
            row = dict(current)
            if row["status"] not in ACTIVE_STATUSES:
                return {
                    "ok": True,
                    "run_id": run_id,
                    "status": row["status"],
                    "cancelled": row["status"] == "cancelled",
                    "message": "Run finished before cancellation took effect.",
                }
            if row["status"] == "running":
                now = utc_now_iso()
                conn.execute(
                    "UPDATE runs SET status='cancelling', cancel_requested_at=?, "
                    "updated_at=? WHERE id=? AND status='running'",
                    (now, now, run_id),
                )
        lock_path = self._lock_path(run_id)
        lock_held = worker_lock_held(lock_path)
        pid, pgid = self._worker_coordinates(row, lock_held=lock_held)
        # A just-spawned worker may not have taken its lock yet. If it is our
        # still-live direct child (same PID/PGID recorded at spawn), that child
        # is itself the authenticated ownership proof; a PID cannot be reused
        # while our child remains alive. Never signal an unauthenticated group.
        known_child = self._known_live_child(run_id, pid)
        if not lock_held and known_child and pgid and group_alive(pgid):
            lock_held = True
        authenticated, _metadata = self._worker_auth(row, lock_held=lock_held)
        if lock_held and not authenticated and not known_child:
            # Give a just-started worker a moment to publish its lock metadata
            # and identity before concluding ownership cannot be authenticated.
            auth_deadline = time.monotonic() + 2.0
            while time.monotonic() < auth_deadline:
                time.sleep(0.05)
                lock_held = worker_lock_held(lock_path)
                authenticated, _metadata = self._worker_auth(
                    row, lock_held=lock_held
                )
                if authenticated or not lock_held:
                    break
        if lock_held and not authenticated and not known_child:
            termination = {
                "requested": False,
                "confirmed": False,
                "group_alive_before": group_alive(pgid),
                "group_alive_after": group_alive(pgid),
                "worker_lock_released": not lock_held,
                "detail": (
                    "worker lock metadata did not match the run token/start "
                    "identity; no signal sent"
                ),
            }
            with self.store().transaction() as conn:
                conn.execute(
                    "UPDATE runs SET error_json=?, updated_at=? WHERE id=? AND "
                    "status='cancelling'",
                    (
                        json_dumps(
                            {
                                "code": "cancel_unconfirmed",
                                "message": "Worker identity could not be "
                                "authenticated; no process group was signalled.",
                            }
                        ),
                        utc_now_iso(),
                        run_id,
                    ),
                )
            raise BridgeError(
                "cancel_unconfirmed",
                "Worker ownership could not be authenticated; no signal was sent.",
                details={"termination": termination},
            )
        termination: dict[str, Any]
        if lock_held and pgid:
            termination = terminate_process_group(
                int(pgid), worker_pid=pid, lock_path=lock_path
            ).as_dict()
            if not termination["confirmed"]:
                with self.store().transaction() as conn:
                    conn.execute(
                        "UPDATE runs SET error_json=?, updated_at=? WHERE id=? AND "
                        "status='cancelling'",
                        (
                            json_dumps(
                                {
                                    "code": "cancel_unconfirmed",
                                    "message": "Worker process group could not be "
                                    "confirmed dead; cancellation remains pending.",
                                }
                            ),
                            utc_now_iso(),
                            run_id,
                        ),
                    )
                raise BridgeError(
                    "cancel_unconfirmed",
                    "Cancellation was requested but the worker group could not be "
                    "confirmed gone; the run remains cancelling for a retry.",
                    details={"termination": termination},
                )
        else:
            live_group = group_alive(pgid)
            termination = {
                "requested": False,
                "confirmed": not lock_held and not live_group,
                "group_alive_before": live_group,
                "group_alive_after": live_group,
                "worker_lock_released": not lock_held,
                "detail": (
                    "worker lock was not held; no group signal was sent to avoid "
                    "a stale-PID/reused-group hazard"
                ),
            }
            if lock_held or live_group:
                raise BridgeError(
                    "cancel_unconfirmed",
                    "Worker ownership is uncertain; no group signal was sent. "
                    "Refusing to target a process group we cannot authenticate.",
                    details={"termination": termination},
                )
        self._reap_children()
        now = utc_now_iso()
        with self.store().transaction() as conn:
            current = conn.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if current is None:
                raise BridgeError("unknown_run", "No such run.")
            row = dict(current)
            if row["status"] not in ACTIVE_STATUSES:
                return {
                    "ok": True,
                    "run_id": run_id,
                    "status": row["status"],
                    "cancelled": row["status"] == "cancelled",
                    "termination": termination,
                    "message": "Run finished while cancellation was in progress.",
                }
            conn.execute(
                "UPDATE runs SET status='cancelled', ended_at=?, updated_at=?, "
                "error_json=? WHERE id=? AND status='cancelling'",
                (
                    now,
                    now,
                    json_dumps(
                        {
                            "code": "cancelled",
                            "message": "Cancelled by caller.",
                        }
                    ),
                    run_id,
                ),
            )
        artifacts = RunArtifacts(self.layout(), run_id)
        artifacts.event("cancelled_by_parent", termination=termination)
        artifacts.write_json(
            "cancel.json",
            {
                "cancelled_at": now,
                "termination": termination,
                "session_checkpointed": False,
            },
        )
        return {
            "ok": True,
            "run_id": run_id,
            "session_id": row["session_id"],
            "status": "cancelled",
            "cancelled": True,
            "termination": termination,
            "artifacts": {
                "dir": str(artifacts.dir),
                "events": str(artifacts.events_path),
                "cancel": str(artifacts.dir / "cancel.json"),
            },
        }

    # -- recovery --------------------------------------------------------
    def recover_stale(self) -> int:
        self._reap_children()
        recovered = 0
        for run in self.store().active_runs():
            if self._recover_one(run):
                recovered += 1
        return recovered

    def _recover_one(self, run: dict[str, Any]) -> bool:
        status = run["status"]
        lock_path = self._lock_path(run["id"])
        lock_held = worker_lock_held(lock_path)
        pid, pgid = self._worker_coordinates(run, lock_held=lock_held)
        age = _age_seconds(run.get("created_at"))
        if not lock_held:
            if (
                not run.get("worker_identity")
                and age is not None
                and age < STARTUP_GRACE_SECONDS
            ):
                return False
            live_group = group_alive(pgid)
            extra = {
                "orphan_process_group": int(pgid) if live_group and pgid else None,
                "liveness_source": "worker lock released",
            }
            return self._mark_orphaned(
                run,
                new_status="interrupted" if status == "running" else "cancelled",
                code="worker_died",
                message=(
                    "The background worker exited without recording a terminal "
                    "state. No stale PID/process group was signalled."
                ),
                extra=extra,
            )
        if not pgid:
            if age is not None and age < STARTUP_GRACE_SECONDS:
                return False
            return self._mark_orphaned(
                run,
                new_status="interrupted" if status == "running" else "cancelled",
                code="worker_coordinates_missing",
                message=(
                    "The worker holds its lock but did not publish a process "
                    "group before the startup grace expired; no group was signalled."
                ),
            )
        deadline = parse_iso(run.get("deadline_at"))
        past_deadline = bool(
            deadline is not None
            and (utc_now() - deadline).total_seconds()
            > TIMEOUT_KILL_GRACE_SECONDS
        )
        authenticated, _metadata = self._worker_auth(run, lock_held=lock_held)
        if not authenticated:
            # Fail closed rather than signal an unverified group. Normal active
            # runs do not need a kill, so remain visible until identity is
            # readable or a human intervenes.
            if status == "cancelling" or past_deadline:
                RunArtifacts(self.layout(), run["id"]).event(
                    "supervisor_auth_failed",
                    detail="worker lock identity/token metadata did not match",
                )
            return False
        if status == "cancelling":
            termination = terminate_process_group(
                int(pgid), worker_pid=pid, lock_path=lock_path
            ).as_dict()
            if termination["confirmed"]:
                self._reap_children()
                self._mark_orphaned(
                    run,
                    new_status="cancelled",
                    code="cancelled",
                    message="Cancelled by caller.",
                    extra={"termination": termination},
                )
                return True
            return False
        if past_deadline:
            termination = terminate_process_group(
                int(pgid), worker_pid=pid, lock_path=lock_path
            ).as_dict()
            self._reap_children()
            RunArtifacts(self.layout(), run["id"]).event(
                "supervisor_timeout_kill", termination=termination
            )
            if termination["confirmed"]:
                # Kill first, then write the terminal row. A live worker must
                # never be hidden behind a "failed" status.
                return self._mark_orphaned(
                    run,
                    new_status="failed",
                    code="run_timeout",
                    message=(
                        "Worker exceeded the run deadline and was killed by the "
                        "supervisor."
                    ),
                    extra={"termination": termination},
                )
            return False
        return False

    def _mark_orphaned(
        self,
        run: dict[str, Any],
        *,
        new_status: str,
        code: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        if new_status not in {"failed", "cancelled", "interrupted"}:
            return False
        now = utc_now_iso()
        error: dict[str, Any] = {"code": code, "message": message}
        if extra:
            error["details"] = extra
        with self.store().transaction() as conn:
            current = conn.execute(
                "SELECT status FROM runs WHERE id=?", (run["id"],)
            ).fetchone()
            if current is None or current[0] not in ACTIVE_STATUSES:
                return False
            conn.execute(
                "UPDATE runs SET status=?, ended_at=?, updated_at=?, error_json=? "
                "WHERE id=? AND status=?",
                (
                    new_status,
                    now,
                    now,
                    json_dumps(error),
                    run["id"],
                    current[0],
                ),
            )
        return True

    def _reap_children(self) -> None:
        with self._children_lock:
            for run_id, process in list(self._children.items()):
                try:
                    if process.poll() is not None:
                        self._children.pop(run_id, None)
                        reap_child(process.pid)
                except Exception:
                    self._children.pop(run_id, None)
