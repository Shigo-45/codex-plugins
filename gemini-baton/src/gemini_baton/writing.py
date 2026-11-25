"""Durable end-to-end writing jobs; the worker owns the entire fallback chain."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .config import Settings
from .errors import BridgeError
from .paths import atomic_write_text, ensure_private_dir
from .plugin_settings import SettingsStore
from .processes import acquire_worker_lock, reap_child, worker_lock_held
from .redact import redact_value
from .workspace import resolve_workspace
from .writing_files import api_preflight, check_text, cli_preflight, selected_files, snapshot_files
from .writing_routes import (ROUTES, SYSTEM, WritingTransport, api_effort_supported,
                             browser_preflight, fallback_scope, normalize_route,
                             resolve_route, route_model_id, supported_api_efforts)

TERMINAL = {"succeeded", "failed", "outcome_unknown"}


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


class WritingService:
    def __init__(self, settings: Settings, *, worker_command: Sequence[str] | None = None) -> None:
        self.settings = settings
        self.root = settings.state_dir / "writing"
        self._settings_store: SettingsStore | None = None
        self.worker_command = list(worker_command) if worker_command else [sys.executable, "-m", "gemini_baton.writing_worker"]

    @property
    def settings_store(self) -> SettingsStore:
        # Create the private settings directory lazily so read-only operations
        # (notably gemini_write_result) still work when state is read-only.
        if self._settings_store is None:
            self._settings_store = SettingsStore(self.settings)
        return self._settings_store

    def directory(self, job_id: str) -> Path:
        if not re.fullmatch(r"write_[a-f0-9]{32}", job_id):
            raise BridgeError("invalid_id", "Invalid writing job identifier.")
        return self.root / job_id

    def read(self, job_id: str) -> dict[str, Any]:
        try:
            return json.loads((self.directory(job_id) / "state.json").read_text())
        except FileNotFoundError:
            raise BridgeError("job_not_found", "Writing job was not found.") from None

    def save(self, job: dict[str, Any]) -> None:
        job["updated_at"] = time.time()
        atomic_write_text(self.directory(job["job_id"]) / "state.json", json.dumps(job, ensure_ascii=False))

    def submit(self, prompt: str, *, cwd: str, file_paths: list[str] | None = None,
               request_id: str | None = None, route: str | None = None,
               conversation_id: str | None = None,
               wait_seconds: float = 45, route_timeout_seconds: int = 300) -> dict[str, Any]:
        self.validate_wait(wait_seconds)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1_000_000:
            raise BridgeError("invalid_prompt", "Provide a nonempty writing task up to 1,000,000 characters.")
        check_text(prompt)
        workspace = str(resolve_workspace(cwd))
        if not isinstance(route_timeout_seconds, int) or isinstance(route_timeout_seconds, bool) or not 1 <= route_timeout_seconds <= 3600:
            raise BridgeError("invalid_timeout", "Route timeout must be between 1 and 3600 seconds.")
        explicit_route = route is not None
        resolved = None
        route_policy_source = "settings"
        if route is not None:
            resolved = resolve_route(route)
            if resolved is None:
                raise BridgeError("invalid_route", "Choose a supported channel:model route.")
            if resolved[0] == "api" and not api_effort_supported(resolved[2]):
                raise BridgeError(
                    "reasoning_unavailable",
                    "The installed google-genai SDK does not support that thinking level; nothing was submitted.",
                    details={"phase": "PRE_SUBMIT", "retry_safe": True,
                             "requested_model": route_model_id(*resolved),
                             "requested_effort": resolved[2],
                             "supported_efforts": list(supported_api_efforts())},
                )
            routes = [list(resolved)]  # an explicit override pins one route
            route_policy_source = "explicit"
        else:
            configured_routes, route_policy_source = self.settings_store.effective_routes_with_source(
                default_routes=ROUTES)
            routes = [list(r) for r in configured_routes]
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
            raise BridgeError("invalid_request_id", "request_id must be a stable string up to 128 characters.")
        if conversation_id is not None and (not isinstance(conversation_id, str) or not 8 <= len(conversation_id) <= 80):
            raise BridgeError("invalid_conversation_id", "conversation_id must be a stable Web conversation handle.")
        if conversation_id is not None and route is not None and route.startswith(("api:", "cli:")):
            raise BridgeError("invalid_route", "A Web conversation_id requires an AI Studio or Web route.")
        job_id = "write_" + (hashlib.sha256(request_id.encode()).hexdigest()[:32] if request_id else uuid.uuid4().hex)
        browser_eligibility = browser_preflight(prompt, job_id=job_id, conversation_id=conversation_id)
        files = selected_files(file_paths)
        # Preflight against the immutable selected bytes before a model worker
        # starts. The original snapshots are still created below for the job;
        # this check only decides which channels can accept the inputs.
        api_eligibility = {"eligible": True, "estimated_inline_bytes": len(prompt.encode("utf-8")), "reasons": []}
        cli_eligibility = cli_preflight(prompt, [], extra_bytes=len(SYSTEM.encode("utf-8")) + 2)
        if files:
            # Use temporary in-memory metadata for the same deterministic
            # checks; snapshot_files is called once the job directory exists.
            temp_dir = self.root / ".preflight" / uuid.uuid4().hex
            try:
                temp_files = snapshot_files(files, temp_dir)
                api_eligibility = api_preflight(prompt, temp_files)
                cli_eligibility = cli_preflight(
                    prompt, temp_files, extra_bytes=len(SYSTEM.encode("utf-8")) + 2)
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        if not explicit_route and not api_eligibility["eligible"]:
            routes = [r for r in routes if r[0] != "api"]
        if not cli_eligibility["eligible"]:
            routes = [r for r in routes if r[0] != "cli"]
        if not browser_eligibility["eligible"]:
            # The browser's 16000-unit limit is shared by AI Studio and Web.
            # CLI and API do not carry the browser marker, so they remain.
            browser_eligibility["skipped_routes"] = [
                list(r) for r in routes if r[0] in {"aistudio", "web"}]
            routes = [r for r in routes if r[0] not in {"aistudio", "web"}]
        if conversation_id is not None:
            # A Web conversation cannot be continued through the CLI or API.
            # The provider-aware Web bridge selects AI Studio or Gemini Web
            # from the saved conversation record.
            routes = [r for r in routes if r[0] not in {"api", "cli"}]
        preflight_error = None
        if explicit_route and not routes:
            channel = resolved[0]
            if channel == "cli":
                preflight_error = {
                    "code": "cli_input_unsupported",
                    "message": "Selected material cannot be carried by the Antigravity CLI prompt; nothing was submitted.",
                    "details": {"phase": "PRE_SUBMIT", "retry_safe": True,
                                "cli_preflight": cli_eligibility},
                }
            elif channel == "api":
                preflight_error = {
                    "code": "api_input_unsupported",
                    "message": "Selected material cannot be carried by the API route; nothing was submitted.",
                    "details": {"phase": "PRE_SUBMIT", "retry_safe": True,
                                "api_preflight": api_eligibility},
                }
        if preflight_error is None and not routes:
            if not browser_eligibility["eligible"]:
                preflight_error = {
                    "code": "browser_prompt_too_long",
                    "message": "Prompt exceeds the 16000-unit browser limit and no eligible writing route remains; nothing was submitted.",
                    "details": {"reason": "prompt_too_long", "limit": browser_eligibility["limit"],
                                "effective_chars": browser_eligibility["effective_chars"],
                                "conversation_id": conversation_id},
                }
            else:
                preflight_error = {
                    "code": "no_eligible_route",
                    "message": "No supported writing route can accept the selected inputs; nothing was submitted.",
                    "details": {"api_preflight": api_eligibility,
                                "cli_preflight": cli_eligibility},
                }
        identity = {"prompt": prompt, "cwd": workspace, "routes": routes, "conversation_id": conversation_id,
                    "route_timeout_seconds": route_timeout_seconds,
                    "api_preflight": api_eligibility,
                    "files": [{k: f[k] for k in ("path", "name", "size", "sha256")} for f in files]}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with file_lock(self.root / "submit.lock"):
            directory = self.directory(job_id)
            if (directory / "state.json").exists():
                job = self.read(job_id)
                if route is None:
                    # Existing jobs retain their recorded policy when defaults
                    # change; receipt retrieval must never submit a new route.
                    identity["routes"] = job["routes"]
                    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if job["fingerprint"] != fingerprint:
                    raise BridgeError("request_conflict", "This request_id already belongs to different inputs.")
            else:
                job = {"job_id": job_id, "fingerprint": fingerprint,
                       "status": "failed" if preflight_error else "queued", "prompt": prompt,
                       "cwd": workspace, "routes": routes,
                       "route_policy_source": route_policy_source,
                       "route_timeout_seconds": route_timeout_seconds,
                       "conversation_id": conversation_id,
                       "api_preflight": api_eligibility,
                       "cli_preflight": cli_eligibility,
                       "browser_preflight": browser_eligibility,
                       "files": snapshot_files(files, directory / "inputs"), "attempts": [],
                       "created_at": time.time(), "worker_pid": None}
                if preflight_error:
                    job["error"] = preflight_error
                self.save(job)
                if preflight_error is None:
                    env = dict(os.environ)
                    env["GEMINI_BATON_STATE_DIR"] = str(self.settings.state_dir)
                    env_file = env.get("GEMINI_BATON_ENV_FILE")
                    if env_file and env_file.strip():
                        env["GEMINI_BATON_ENV_FILE"] = str(Path(env_file).expanduser().resolve())
                    web_cli = env.get("GEMINI_BATON_WEB_CLI")
                    if web_cli and web_cli.strip():
                        env["GEMINI_BATON_WEB_CLI"] = str(Path(web_cli).expanduser().resolve())
                    agy = env.get("GEMINI_BATON_AGY")
                    if agy and agy.strip():
                        env["GEMINI_BATON_AGY"] = str(Path(agy).expanduser().resolve())
                    try:
                        process = subprocess.Popen(self.worker_command + ["--job-id", job_id], cwd=workspace, env=env,
                                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL, start_new_session=True)
                        # The worker owns all state updates after launch. Store the
                        # launch PID separately to avoid overwriting a fast result.
                        atomic_write_text(directory / "launch.json", json.dumps({"pid": process.pid}))
                    except OSError as exc:
                        job["status"] = "failed"
                        job["error"] = {"code": "worker_start_failed", "message": f"Writing worker launch failed: {type(exc).__name__}."}
                        self.save(job)
        return self.result(job_id, wait_seconds=wait_seconds)

    @staticmethod
    def validate_wait(wait_seconds: float) -> None:
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= 50:
            raise BridgeError("invalid_timeout", "Wait duration must be between 0 and 50 seconds.")

    def result(self, job_id: str, *, wait_seconds: float = 0) -> dict[str, Any]:
        self.validate_wait(wait_seconds)
        deadline = time.monotonic() + wait_seconds
        while True:
            job = self.read(job_id)
            directory = self.directory(job_id)
            if job["status"] not in TERMINAL and time.time() - job["created_at"] > 20:
                # Recheck while holding the same worker ownership lock: a
                # delayed startup and a reader must never both own recovery.
                if not worker_lock_held(directory / "worker.lock"):
                    try:
                        held = acquire_worker_lock(directory / "worker.lock", {"reconciler": os.getpid()})
                    except RuntimeError:
                        held = None
                    if held is not None:
                        with held:
                            job = self.read(job_id)
                            if job["status"] not in TERMINAL:
                                job["status"] = "outcome_unknown"
                                job["error"] = {"code": "worker_lost", "message": "Writing worker exited before persisting a final result. Original submissions are retained."}
                                self.save(job)
            if job["status"] in TERMINAL or time.monotonic() >= deadline:
                break
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        try:
            launch = json.loads((directory / "launch.json").read_text())
            reap_child(launch["pid"])
        except (OSError, ValueError, KeyError):
            pass
        public = {k: v for k, v in job.items() if k not in {"prompt", "fingerprint", "routes", "worker_pid", "files"}}
        public["files"] = [{k: f[k] for k in ("name", "size", "sha256")} for f in job["files"]]
        try:
            public["route_policy"] = [
                {"channel": channel, "model": model, "effort": effort}
                for channel, model, effort in (normalize_route(route) for route in job["routes"])
            ]
        except BridgeError:
            public["route_policy"] = []
        public["route_policy_source"] = job.get("route_policy_source") or (
            "explicit" if len(job["routes"]) == 1 else "recorded")
        public["ok"] = job["status"] not in {"failed", "outcome_unknown"}
        public["done"] = job["status"] in TERMINAL
        public["result_path"] = str(directory / "state.json")
        if not public["done"]:
            public["next"] = {"tool": "gemini_write_result", "job_id": job_id, "wait_seconds": 30}
        return redact_value(public)

    def execute(self, job_id: str, *, transport: WritingTransport | None = None) -> None:
        directory = self.directory(job_id)
        try:
            held = acquire_worker_lock(directory / "worker.lock", {"pid": os.getpid(), "job_id": job_id})
        except RuntimeError:
            return
        with held:
            job = self.read(job_id)
            if job["status"] in TERMINAL:
                return
            # A second invocation may inspect an interrupted job, but never
            # replay a route already recorded as started.
            if job["attempts"]:
                job["status"] = "outcome_unknown"
                job["error"] = {"code": "outcome_unknown", "message": "An existing writing attempt requires its saved receipt; automatic replay is stopped."}
                self.save(job)
                return
            job["worker_pid"] = os.getpid()
            job["status"] = "running"
            self.save(job)
            adapter = transport or WritingTransport(self.settings)
            browser_eligible = (job.get("browser_preflight") or {}).get("eligible", True)
            cli_eligible = (job.get("cli_preflight") or {}).get("eligible", True)
            normalized_routes = [normalize_route(route) for route in job["routes"]]
            if not browser_eligible and not any(
                    channel in {"cli", "api"} for channel, _model, _effort in normalized_routes):
                preflight = job["browser_preflight"]
                job.update(status="failed", error={
                    "code": "browser_prompt_too_long",
                    "message": "Prompt exceeds the 16000-unit browser limit and no eligible writing route remains; nothing was submitted.",
                    "details": {"reason": "prompt_too_long", "limit": preflight.get("limit"),
                                "effective_chars": preflight.get("effective_chars"),
                                "conversation_id": job.get("conversation_id")},
                })
                self.save(job)
                return
            skip_channels: set[str] = set()
            try:
                for route in job["routes"]:
                    channel, model, effort = normalize_route(route)
                    if channel in {"aistudio", "web"} and not browser_eligible:
                        continue
                    if channel == "cli" and not cli_eligible:
                        continue
                    if channel in skip_channels:
                        continue
                    requested_model = route_model_id(channel, model, effort)
                    if channel == "api" and not api_effort_supported(effort):
                        job["attempts"].append({
                            "channel": channel, "model": model, "effort": effort,
                            "requested_model": requested_model,
                            "status": "skipped", "reason": "reasoning_unavailable",
                            "phase": "PRE_SUBMIT", "fallback": "model",
                            "started_at": time.time(), "finished_at": time.time(),
                            "capability_status": {
                                "thinking_level_supported": False,
                                "supported_efforts": list(supported_api_efforts()),
                            },
                        })
                        self.save(job)
                        continue
                    attempt = {"channel": channel, "model": model, "effort": effort,
                               "requested_model": requested_model,
                               "status": "started", "started_at": time.time()}
                    job["attempts"].append(attempt)
                    self.save(job)
                    try:
                        if channel == "cli":
                            result = adapter.cli(job, model, effort)
                        elif channel == "api":
                            # Conservative cross-process writing API admission.
                            # Web retains its existing shared three-slot pool.
                            with file_lock(self.root / "api.lock"):
                                result = adapter.api(job, model, effort)
                        elif channel == "aistudio":
                            result = adapter.aistudio(job, model, effort)
                        else:
                            result = adapter.web(job, model, effort)
                        attempt.update({
                            "status": "succeeded", "finished_at": time.time(),
                            "actual_model_version": result["actual_model_version"],
                            "requested_effort": result.get("requested_effort", effort),
                            "effective_effort": result.get("effective_effort"),
                            "model_evidence": result.get("model_evidence"),
                            "capability_status": result.get("capability_status"),
                        })
                        job.update(status="succeeded", channel=channel,
                                   effort=effort, **result)
                        job.setdefault("model", model)
                        self.save(job)
                        return
                    except BridgeError as exc:
                        error = redact_value(exc.as_dict())
                        scope = fallback_scope(error, channel)
                        attempt.update(status="failed", finished_at=time.time(), error=error, fallback=scope)
                        self.save(job)
                        if scope == "stop":
                            details = error.get("details") or {}
                            uncertain = (error.get("code") in {"outcome_unknown", "OUTCOME_UNKNOWN"}
                                         or details.get("outcome") in {"unknown", "OUTCOME_UNKNOWN"})
                            job.update(status="outcome_unknown" if uncertain else "failed", error=error)
                            self.save(job)
                            return
                        if scope == "channel":
                            skip_channels.add(channel)
                job.update(status="failed", error={"code": "routes_exhausted", "message": "All eligible writing routes failed; see attempts for route evidence."})
                self.save(job)
            except Exception as exc:
                job.update(status="outcome_unknown", error={"code": "outcome_unknown", "message": f"Writing execution stopped with {type(exc).__name__}; saved attempt receipts are retained."})
                self.save(job)
