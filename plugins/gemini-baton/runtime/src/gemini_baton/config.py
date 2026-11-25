from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import BridgeError

APP_NAME = "gemini-baton"

DEFAULT_MODEL = "gemini-4-argon"
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_STEPS = 12
DEFAULT_MAX_OUTPUT_TOKENS = 16_384
DEFAULT_MAX_TOOL_OUTPUT_CHARS = 32_768
DEFAULT_MAX_ACTIVE_RUNS = 4
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120
DEFAULT_READ_MAX_BYTES = 262_144
MAX_WAIT_SECONDS = 50
MAX_TIMEOUT_SECONDS = 86_400
MAX_STEPS_LIMIT = 50
MAX_ACTIVE_RUNS_LIMIT = 64
MAX_MESSAGE_CHARS = 1_000_000
MAX_CHECKS = 20
MAX_CHECK_ARGV = 128
MAX_ARG_CHARS = 32_768
MAX_WRITE_BYTES = 1_048_576
MAX_READ_LIMIT_BYTES = 4_194_304
STARTUP_GRACE_SECONDS = 15
TIMEOUT_KILL_GRACE_SECONDS = 10
FINAL_PREVIEW_CHARS = 4_000
EVENT_PREVIEW_CHARS = 2_000
WAIT_POLL_INTERVAL_SECONDS = 0.2
CANCEL_TERM_GRACE_SECONDS = 4.0
CANCEL_KILL_GRACE_SECONDS = 4.0


def default_state_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = env.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME
    base = env.get("XDG_STATE_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "state" / APP_NAME


def _positive_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int,
) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise BridgeError(
            "invalid_config", f"{name} must be an integer."
        ) from exc
    if not minimum <= value <= maximum:
        raise BridgeError(
            "invalid_config",
            f"{name} must be between {minimum} and {maximum}.",
        )
    return value


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    model: str
    model_source: str
    default_timeout_seconds: int
    default_max_steps: int
    max_output_tokens: int
    max_tool_output_chars: int
    max_active_runs: int
    command_timeout_seconds: int
    read_max_bytes: int
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        raw_state = env.get("GEMINI_BATON_STATE_DIR")
        if raw_state is not None and not raw_state.strip():
            raise BridgeError(
                "invalid_config", "GEMINI_BATON_STATE_DIR cannot be empty."
            )
        state_dir = (
            Path(raw_state).expanduser().resolve()
            if raw_state is not None
            else default_state_dir(env).expanduser().resolve()
        )
        raw_model = env.get("GEMINI_BATON_MODEL")
        if raw_model is not None:
            model = raw_model.strip()
            if not model:
                raise BridgeError(
                    "invalid_config", "GEMINI_BATON_MODEL cannot be empty."
                )
            model_source = "env:GEMINI_BATON_MODEL"
        else:
            model = DEFAULT_MODEL
            model_source = "built-in-default"
        return cls(
            state_dir=state_dir,
            model=model,
            model_source=model_source,
            default_timeout_seconds=_positive_int(
                env,
                "GEMINI_BATON_TIMEOUT_SECONDS",
                DEFAULT_TIMEOUT_SECONDS,
                maximum=MAX_TIMEOUT_SECONDS,
            ),
            default_max_steps=_positive_int(
                env,
                "GEMINI_BATON_MAX_STEPS",
                DEFAULT_MAX_STEPS,
                maximum=MAX_STEPS_LIMIT,
            ),
            max_output_tokens=_positive_int(
                env,
                "GEMINI_BATON_MAX_OUTPUT_TOKENS",
                DEFAULT_MAX_OUTPUT_TOKENS,
                maximum=131_072,
            ),
            max_tool_output_chars=_positive_int(
                env,
                "GEMINI_BATON_MAX_TOOL_OUTPUT_CHARS",
                DEFAULT_MAX_TOOL_OUTPUT_CHARS,
                minimum=1_024,
                maximum=1_000_000,
            ),
            max_active_runs=_positive_int(
                env,
                "GEMINI_BATON_MAX_ACTIVE_RUNS",
                DEFAULT_MAX_ACTIVE_RUNS,
                maximum=MAX_ACTIVE_RUNS_LIMIT,
            ),
            command_timeout_seconds=_positive_int(
                env,
                "GEMINI_BATON_COMMAND_TIMEOUT_SECONDS",
                DEFAULT_COMMAND_TIMEOUT_SECONDS,
                maximum=3_600,
            ),
            read_max_bytes=_positive_int(
                env,
                "GEMINI_BATON_READ_MAX_BYTES",
                DEFAULT_READ_MAX_BYTES,
                minimum=1_024,
                maximum=MAX_READ_LIMIT_BYTES,
            ),
        )

    @property
    def db_path(self) -> Path:
        return self.state_dir / "gemini-baton.sqlite3"

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    def limits(self) -> dict[str, int | float]:
        return {
            "default_timeout_seconds": self.default_timeout_seconds,
            "default_max_steps": self.default_max_steps,
            "max_output_tokens_per_step": self.max_output_tokens,
            "max_tool_output_chars": self.max_tool_output_chars,
            "max_active_runs": self.max_active_runs,
            "command_timeout_seconds": self.command_timeout_seconds,
            "read_max_bytes": self.read_max_bytes,
            "wait_max_seconds": MAX_WAIT_SECONDS,
            "startup_grace_seconds": STARTUP_GRACE_SECONDS,
        }
