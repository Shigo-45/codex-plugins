from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gemini_baton.config import Settings
from gemini_baton.service import RunService

FAKE_KEY = "test-only-not-a-real-gemini-key-0000000000"
FAKE_WORKER = Path(__file__).parent / "support" / "fake_worker.py"


@dataclass
class FakeEnvironment:
    state_dir: Path
    workspace: Path
    key: str


@pytest.fixture
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeEnvironment:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("GEMINI_BATON_STATE_DIR", str(state))
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_BATON_ENV_FILE", raising=False)
    monkeypatch.delenv("GEMINI_BATON_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_BATON_FAKE_SCENARIO", raising=False)
    monkeypatch.delenv("GEMINI_BATON_MAX_ACTIVE_RUNS", raising=False)
    return FakeEnvironment(state_dir=state, workspace=workspace, key=FAKE_KEY)


@pytest.fixture
def service_factory(
    fake_env: FakeEnvironment, monkeypatch: pytest.MonkeyPatch
) -> Any:
    created: list[RunService] = []

    def make() -> RunService:
        service = RunService(
            Settings.from_env(),
            worker_command=[sys.executable, str(FAKE_WORKER.resolve())],
        )
        created.append(service)
        return service

    yield make

    for service in created:
        try:
            for run in service.store().active_runs():
                try:
                    service.cancel(run["id"])
                except Exception:
                    pass
            service.recover_stale()
        except Exception:
            pass


def wait_done(
    service: RunService, run_id: str, *, timeout: float = 20.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = service.wait(run_id, timeout_seconds=0.5)
        if last.get("done"):
            return last
    raise AssertionError(f"run did not finish in {timeout}s: {last}")


def wait_until(predicate: Any, *, timeout: float = 10.0, interval: float = 0.05) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition was not met before timeout")
