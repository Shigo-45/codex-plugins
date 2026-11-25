from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .agent import AgentLoop, AgentOutcome
from .artifacts import RunArtifacts
from .backend import GeminiBackend, ModelBackend
from .config import Settings
from .credentials import load_credential
from .errors import BridgeError
from .paths import StateLayout, atomic_write_text, ensure_private_dir
from .processes import (
    acquire_worker_lock,
    current_process_coordinates,
    new_worker_identity,
    worker_lock_path,
)
from .redact import redact_text, redact_value
from .store import Store
from .tools import WorkspaceTools
from .util import (
    json_dumps,
    json_loads,
    parse_iso,
    preview_text,
    utc_now_iso,
)
from .workspace import resolve_workspace

_LOG = logging.getLogger("gemini_baton.worker")
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--state-dir", required=True)
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    backend_factory: Callable[[Settings], ModelBackend] | None = None,
) -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    os.environ["GEMINI_BATON_STATE_DIR"] = str(Path(args.state_dir).resolve())
    settings = Settings.from_env()
    layout = StateLayout(settings.state_dir)
    store = Store(layout)
    token = os.environ.get("GEMINI_BATON_WORKER_TOKEN", "")
    if not token:
        _LOG.error("worker token missing")
        return 2
    run = store.get_run(args.run_id)
    if run is None:
        _LOG.error("run not found")
        return 2
    expected = run.get("worker_token_hash") or ""
    if not expected or not hmac.compare_digest(_token_hash(token), expected):
        _LOG.error("worker token did not match the run")
        return 2
    if run["status"] != "running":
        _LOG.info("run is no longer running; worker exits")
        return 0

    pid, pgid = current_process_coordinates()
    identity = new_worker_identity()
    started_at = utc_now_iso()
    lock_handle: Any = None
    try:
        lock_handle = acquire_worker_lock(
            worker_lock_path(layout.run_dir(args.run_id)),
            {
                "run_id": args.run_id,
                "pid": pid,
                "pgid": pgid,
                "identity": identity,
                "token_fingerprint": expected,
                "started_at": started_at,
            },
        )
    except RuntimeError as exc:
        _LOG.error("could not acquire worker lock: %s", exc)
        return 2
    with store.transaction() as conn:
        updated = conn.execute(
            "UPDATE runs SET worker_pid=?, worker_pgid=?, worker_identity=?, "
            "worker_started_at=?, started_at=COALESCE(started_at, ?), "
            "updated_at=? WHERE id=? AND status='running' AND "
            "worker_token_hash=?",
            (
                pid,
                pgid,
                identity,
                started_at,
                started_at,
                started_at,
                args.run_id,
                expected,
            ),
        ).rowcount
    if updated != 1:
        if lock_handle is not None:
            lock_handle.close()
        return 0

    run = store.get_run(args.run_id)
    if run is None or run["status"] != "running":
        return 0

    key: str | None = None
    try:
        credential = load_credential()
        key = credential.key
        artifacts = RunArtifacts(
            layout, args.run_id, secrets=[credential.key]
        )
        _LOG.info(
            "worker started run=%s pid=%s pgid=%s credential_source=%s",
            args.run_id,
            pid,
            pgid,
            credential.source,
        )
        if backend_factory is None:
            backend = GeminiBackend(
                api_key=credential.key,
                api_key_source=credential.source,
            )
        else:
            backend = backend_factory(settings)
        result_code = _execute(
            settings=settings,
            layout=layout,
            store=store,
            run=run,
            artifacts=artifacts,
            backend=backend,
            secrets=[credential.key],
        )
        # Keep the advisory lock alive until the worker has fully finished.
        _ = lock_handle
        return result_code
    except BridgeError as exc:
        error = redact_value(exc.as_dict(), [key] if key else [])
        _LOG.error("run failed: %s", redact_text(error.get("message", ""), [key or ""]))
        _mark_failed_without_outcome(
            store=store,
            run=run,
            artifacts=(
                locals().get("artifacts")
                or RunArtifacts(layout, args.run_id, secrets=[key] if key else [])
            ),
            error=error,
            secrets=[key] if key else [],
        )
        return 1
    except Exception as exc:
        error = {
            "code": "worker_error",
            "message": redact_text(
                f"{type(exc).__name__}: {exc}", [key] if key else []
            ),
        }
        _LOG.error("worker error: %s", redact_text(error["message"], [key or ""]))
        _mark_failed_without_outcome(
            store=store,
            run=run,
            artifacts=(
                locals().get("artifacts")
                or RunArtifacts(layout, args.run_id, secrets=[key] if key else [])
            ),
            error=error,
            secrets=[key] if key else [],
        )
        return 1


def _execute(
    *,
    settings: Settings,
    layout: StateLayout,
    store: Store,
    run: dict[str, Any],
    artifacts: RunArtifacts,
    backend: ModelBackend,
    secrets: list[str],
) -> int:
    run_id = run["id"]
    session_id = run["session_id"]
    checks = json_loads(run.get("checks_json"), default=[]) or []
    if not isinstance(checks, list):
        checks = []
    deadline = parse_iso(run.get("deadline_at"))
    if deadline is not None:
        # AgentLoop/tools compare against time.monotonic(). Convert the stored
        # wall-clock deadline into the monotonic scale before passing it down.
        deadline_monotonic = time.monotonic() + max(
            0.0, deadline.timestamp() - time.time()
        )
    else:
        deadline_monotonic = time.monotonic() + 300.0
    session = store.get_session(session_id)
    if session is None:
        error = {"code": "state_error", "message": "Session row is missing."}
        _mark_failed_without_outcome(
            store=store, run=run, artifacts=artifacts, error=error, secrets=secrets
        )
        return 1
    initial_transcript_json = session["transcript_json"]
    initial_transcript_sha = session["transcript_sha256"]
    computed_initial_sha = hashlib.sha256(
        initial_transcript_json.encode("utf-8")
    ).hexdigest()
    if computed_initial_sha != initial_transcript_sha:
        error = {
            "code": "transcript_corrupt",
            "message": "Session transcript hash did not match its contents.",
        }
        _mark_failed_without_outcome(
            store=store, run=run, artifacts=artifacts, error=error, secrets=secrets
        )
        return 1
    transcript = json_loads(initial_transcript_json, default=None)
    if not isinstance(transcript, list):
        error = {
            "code": "transcript_corrupt",
            "message": "Session transcript is not a JSON list.",
        }
        _mark_failed_without_outcome(
            store=store, run=run, artifacts=artifacts, error=error, secrets=secrets
        )
        return 1
    try:
        cwd = resolve_workspace(run["cwd"])
        if str(cwd) != run["cwd"]:
            raise BridgeError(
                "cwd_changed",
                "The run cwd no longer resolves to its original real path.",
            )
        tools = WorkspaceTools(
            cwd,
            permission_mode=run["permission_mode"],
            allow_shell=bool(run["allow_shell"]),
            read_max_bytes=settings.read_max_bytes,
            max_output_chars=run["max_tool_output_chars"],
            command_timeout_seconds=settings.command_timeout_seconds,
        )
    except BridgeError as exc:
        _mark_failed_without_outcome(
            store=store,
            run=run,
            artifacts=artifacts,
            error=exc.as_dict(),
            secrets=secrets,
        )
        return 1

    def control_check() -> str | None:
        try:
            current = store.get_run(run_id)
        except BridgeError:
            return "state_unavailable"
        if current is None:
            return "missing"
        if current["status"] != "running":
            return str(current["status"])
        return None

    loop = AgentLoop(
        backend=backend,
        model=run["selected_model"],
        cwd=str(cwd),
        permission_mode=run["permission_mode"],
        allow_shell=bool(run["allow_shell"]),
        tools=tools,
        artifacts=artifacts,
        max_steps=run["max_steps"],
        max_output_tokens=run["max_output_tokens"],
        deadline_monotonic=deadline_monotonic,
        control_check=control_check,
    )
    outcome = loop.run(user_prompt=run["prompt"], prior_transcript=transcript)
    if outcome.error:
        outcome.error = redact_value(outcome.error, secrets)
    if outcome.success and time.monotonic() > deadline_monotonic:
        outcome.success = False
        outcome.error = {
            "code": "run_timeout",
            "message": "The run deadline expired before the final answer was "
            "checkpointed.",
        }

    check_evidence: list[dict[str, Any]] = []
    checks_ok = True
    if outcome.success and checks:
        checks_ok = _run_checks(
            checks=checks,
            tools=tools,
            artifacts=artifacts,
            deadline_monotonic=deadline_monotonic,
            secrets=secrets,
            evidence=check_evidence,
        )
        if any(item.get("timed_out") for item in check_evidence):
            outcome.success = False
            outcome.error = {
                "code": "run_timeout",
                "message": "The run deadline expired while running checks.",
            }
        elif not checks_ok:
            outcome.success = False
            failed_indexes = [
                item["index"]
                for item in check_evidence
                if not item.get("ok")
            ]
            outcome.error = {
                "code": "checks_failed",
                "message": "The final answer was produced, but one or more checks "
                "failed.",
                "details": {"failed_checks": failed_indexes},
            }
    if check_evidence:
        artifacts.write_json("checks.json", check_evidence)

    final_now = utc_now_iso()
    final_path = artifacts.dir / "final.md"
    if outcome.final_text:
        atomic_write_text(final_path, outcome.final_text)

    if outcome.success:
        return _finalize_success(
            store=store,
            layout=layout,
            artifacts=artifacts,
            run=run,
            outcome=outcome,
            check_evidence=check_evidence,
            initial_transcript_sha=initial_transcript_sha,
            final_now=final_now,
            secrets=secrets,
        )
    if outcome.success is False and outcome.error and outcome.error.get("code") == "cancelled":
        current = store.get_run(run_id)
        if current is not None and current["status"] != "running":
            _LOG.info("run was cancelled; worker leaves terminal state to caller")
            return 0
    return _finalize_failure(
        store=store,
        artifacts=artifacts,
        run=run,
        outcome=outcome,
        check_evidence=check_evidence,
        final_now=final_now,
        secrets=secrets,
    )


def _run_checks(
    *,
    checks: list[Any],
    tools: WorkspaceTools,
    artifacts: RunArtifacts,
    deadline_monotonic: float,
    secrets: list[str],
    evidence: list[dict[str, Any]],
) -> bool:
    all_ok = True
    for index, argv in enumerate(checks, start=1):
        check_dir = ensure_private_dir(artifacts.checks / f"check-{index:02d}")
        entry: dict[str, Any]
        if time.monotonic() >= deadline_monotonic:
            entry = {
                "index": index,
                "argv": argv,
                "ok": False,
                "exit_code": None,
                "timed_out": True,
                "error": "run deadline expired before this check",
                "stdout_path": None,
                "stderr_path": None,
            }
            all_ok = False
            evidence.append(entry)
            artifacts.write_json(f"checks/check-{index:02d}/result.json", entry)
            continue
        try:
            result = tools.run_check(
                argv,
                deadline_monotonic=deadline_monotonic,
                artifact_dir=check_dir,
            )
        except BridgeError as exc:
            result = {
                "ok": False,
                "argv": argv,
                "exit_code": None,
                "timed_out": False,
                "error": exc.message,
                "stdout_path": None,
                "stderr_path": None,
            }
        summary = {
            "index": index,
            "argv": argv,
            "ok": bool(result.get("ok")),
            "exit_code": result.get("exit_code"),
            "timed_out": bool(result.get("timed_out")),
            "output_limited": bool(result.get("output_limited")),
            "duration_ms": result.get("duration_ms"),
            "stdout_preview": result.get("stdout", ""),
            "stderr_preview": result.get("stderr", ""),
            "stdout_path": result.get("stdout_path"),
            "stderr_path": result.get("stderr_path"),
            "error": result.get("error"),
        }
        evidence.append(summary)
        artifacts.write_json(
            f"checks/check-{index:02d}/result.json",
            {"summary": summary, "raw": result},
        )
        artifacts.event(
            "check_result",
            index=index,
            ok=summary["ok"],
            exit_code=summary["exit_code"],
            timed_out=summary["timed_out"],
            stdout_path=summary["stdout_path"],
            stderr_path=summary["stderr_path"],
        )
        if not summary["ok"]:
            all_ok = False
    return all_ok


def _result_artifact(
    *,
    artifacts: RunArtifacts,
    run: dict[str, Any],
    outcome: AgentOutcome,
    status: str,
    error: dict[str, Any] | None,
    check_evidence: list[dict[str, Any]],
    final_now: str,
    secrets: list[str],
) -> dict[str, Any]:
    run_dir = artifacts.dir
    final_path = run_dir / "final.md"
    return {
        "run_id": run["id"],
        "session_id": run["session_id"],
        "status": status,
        "selected_model": run["selected_model"],
        "actual_model_version": outcome.actual_model_version,
        "finish_reason": outcome.finish_reason,
        "usage": outcome.usage,
        "final_text_path": str(final_path) if final_path.exists() else None,
        "final_text_preview": preview_text(
            outcome.final_text, 4_000, path=str(final_path)
        ),
        "changed_files": outcome.changed_files,
        "checks": check_evidence,
        "error": redact_value(error, secrets) if error else None,
        "steps": outcome.steps,
        "model_calls": outcome.model_calls,
        "ended_at": final_now,
        "artifacts": {
            "dir": str(run_dir),
            "events": str(run_dir / "events.jsonl"),
            "worker_log": str(run_dir / "worker.log"),
            "prompt": str(run_dir / "prompt.txt"),
        },
    }


def _finalize_success(
    *,
    store: Store,
    layout: StateLayout,
    artifacts: RunArtifacts,
    run: dict[str, Any],
    outcome: AgentOutcome,
    check_evidence: list[dict[str, Any]],
    initial_transcript_sha: str,
    final_now: str,
    secrets: list[str],
) -> int:
    try:
        from google.genai import types

        for item in outcome.transcript:
            types.Content.model_validate(item)
        new_transcript_json = json_dumps(outcome.transcript)
        new_transcript_sha = hashlib.sha256(
            new_transcript_json.encode("utf-8")
        ).hexdigest()
    except Exception as exc:
        error = {
            "code": "transcript_invalid",
            "message": redact_text(
                f"Cannot serialize full model content: {type(exc).__name__}",
                secrets,
            ),
        }
        outcome.success = False
        return _finalize_failure(
            store=store,
            artifacts=artifacts,
            run=run,
            outcome=outcome,
            check_evidence=check_evidence,
            final_now=final_now,
            secrets=secrets,
            error_override=error,
        )
    try:
        with store.transaction() as conn:
            current = conn.execute(
                "SELECT status FROM runs WHERE id=? AND worker_token_hash=?",
                (run["id"], run["worker_token_hash"]),
            ).fetchone()
            if current is None or current["status"] != "running":
                _LOG.info("run terminal state changed before checkpoint; not committing")
                return 0
            session_row = conn.execute(
                "SELECT transcript_sha256 FROM sessions WHERE id=?",
                (run["session_id"],),
            ).fetchone()
            if (
                session_row is None
                or session_row["transcript_sha256"] != initial_transcript_sha
            ):
                raise BridgeError(
                    "transcript_conflict",
                    "Session transcript changed while the run was active.",
                )
            session_update = conn.execute(
                "UPDATE sessions SET transcript_json=?, transcript_sha256=?, "
                "updated_at=? WHERE id=? AND transcript_sha256=?",
                (
                    new_transcript_json,
                    new_transcript_sha,
                    final_now,
                    run["session_id"],
                    initial_transcript_sha,
                ),
            ).rowcount
            if session_update != 1:
                raise BridgeError(
                    "transcript_conflict",
                    "Session transcript changed while the run was active.",
                )
            update = conn.execute(
                "UPDATE runs SET status='succeeded', ended_at=?, updated_at=?, "
                "actual_model_version=?, finish_reason=?, usage_json=?, "
                "final_text=?, error_json=NULL, changed_files_json=?, "
                "checks_result_json=?, steps=?, model_calls=?, exit_code=0 "
                "WHERE id=? AND status='running' AND worker_token_hash=?",
                (
                    final_now,
                    final_now,
                    outcome.actual_model_version,
                    outcome.finish_reason,
                    json_dumps(outcome.usage),
                    outcome.final_text,
                    json_dumps(outcome.changed_files),
                    json_dumps(check_evidence),
                    outcome.steps,
                    outcome.model_calls,
                    run["id"],
                    run["worker_token_hash"],
                ),
            ).rowcount
            if update != 1:
                raise BridgeError(
                    "checkpoint_race", "Run state changed during checkpoint."
                )
    except BridgeError as exc:
        error = exc.as_dict()
        _LOG.error("checkpoint failed: %s", redact_text(error["message"], secrets))
        outcome.success = False
        return _finalize_failure(
            store=store,
            artifacts=artifacts,
            run=run,
            outcome=outcome,
            check_evidence=check_evidence,
            final_now=final_now,
            secrets=secrets,
            error_override=error,
        )
    try:
        atomic_write_text(
            layout.transcript_path(run["session_id"]), new_transcript_json
        )
    except BridgeError as exc:
        _LOG.error(
            "transcript mirror write failed: %s",
            redact_text(exc.message, secrets),
        )
    result = _result_artifact(
        artifacts=artifacts,
        run=run,
        outcome=outcome,
        status="succeeded",
        error=None,
        check_evidence=check_evidence,
        final_now=final_now,
        secrets=secrets,
    )
    artifacts.write_json("result.json", result)
    artifacts.event("run_finished", status="succeeded", checks=len(check_evidence))
    _LOG.info("run succeeded")
    return 0


def _finalize_failure(
    *,
    store: Store,
    artifacts: RunArtifacts,
    run: dict[str, Any],
    outcome: AgentOutcome,
    check_evidence: list[dict[str, Any]],
    final_now: str,
    secrets: list[str],
    error_override: dict[str, Any] | None = None,
) -> int:
    error = redact_value(error_override or outcome.error or {
        "code": "unknown_failure",
        "message": "Run failed without an error detail.",
    }, secrets)
    with store.transaction() as conn:
        current = conn.execute(
            "SELECT status FROM runs WHERE id=? AND worker_token_hash=?",
            (run["id"], run["worker_token_hash"]),
        ).fetchone()
        if current is None or current["status"] != "running":
            _LOG.info("run terminal state changed; not overwriting")
            return 0
        conn.execute(
            "UPDATE runs SET status='failed', ended_at=?, updated_at=?, "
            "actual_model_version=?, finish_reason=?, usage_json=?, final_text=?, "
            "error_json=?, changed_files_json=?, checks_result_json=?, steps=?, "
            "model_calls=?, exit_code=1 WHERE id=? AND status='running' AND "
            "worker_token_hash=?",
            (
                final_now,
                final_now,
                outcome.actual_model_version,
                outcome.finish_reason,
                json_dumps(outcome.usage),
                outcome.final_text,
                json_dumps(error),
                json_dumps(outcome.changed_files),
                json_dumps(check_evidence),
                outcome.steps,
                outcome.model_calls,
                run["id"],
                run["worker_token_hash"],
            ),
        )
    result = _result_artifact(
        artifacts=artifacts,
        run=run,
        outcome=outcome,
        status="failed",
        error=error,
        check_evidence=check_evidence,
        final_now=final_now,
        secrets=secrets,
    )
    artifacts.write_json("result.json", result)
    artifacts.event("run_finished", status="failed", error=error)
    _LOG.info("run failed: %s", redact_text(error.get("message", ""), secrets))
    return 1


def _mark_failed_without_outcome(
    *,
    store: Store,
    run: dict[str, Any],
    artifacts: RunArtifacts,
    error: dict[str, Any],
    secrets: list[str],
) -> None:
    now = utc_now_iso()
    safe_error = redact_value(error, secrets)
    try:
        with store.transaction() as conn:
            current = conn.execute(
                "SELECT status FROM runs WHERE id=? AND worker_token_hash=?",
                (run["id"], run["worker_token_hash"]),
            ).fetchone()
            if current is None or current["status"] != "running":
                return
            conn.execute(
                "UPDATE runs SET status='failed', ended_at=?, updated_at=?, "
                "error_json=?, exit_code=1 WHERE id=? AND status='running' AND "
                "worker_token_hash=?",
                (
                    now,
                    now,
                    json_dumps(safe_error),
                    run["id"],
                    run["worker_token_hash"],
                ),
            )
    except BridgeError:
        return
    try:
        artifacts.write_json(
            "result.json",
            {
                "run_id": run["id"],
                "session_id": run["session_id"],
                "status": "failed",
                "selected_model": run["selected_model"],
                "error": safe_error,
                "ended_at": now,
            },
        )
        artifacts.event("run_finished", status="failed", error=safe_error)
    except BridgeError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
