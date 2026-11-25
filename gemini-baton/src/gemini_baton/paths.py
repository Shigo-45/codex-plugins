from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .errors import BridgeError


def ensure_private_dir(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise BridgeError(
            "state_error", f"Cannot create or secure state directory: {path}"
        ) from exc
    return path


class StateLayout:
    def __init__(self, root: Path) -> None:
        self.root = ensure_private_dir(root)
        self.sessions = ensure_private_dir(root / "sessions")
        self.runs = ensure_private_dir(root / "runs")
        self.db = root / "gemini-baton.sqlite3"

    def session_dir(self, session_id: str) -> Path:
        safe = _safe_component(session_id)
        return ensure_private_dir(self.sessions / safe)

    def run_dir(self, run_id: str) -> Path:
        safe = _safe_component(run_id)
        return ensure_private_dir(self.runs / safe)

    def transcript_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "transcript.json"

    def open_log(self, path: Path) -> int:
        ensure_private_dir(path.parent)
        try:
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            os.chmod(path, 0o600)
            return fd
        except OSError as exc:
            raise BridgeError(
                "state_error", f"Cannot create log file: {path}"
            ) from exc


def _safe_component(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise BridgeError("invalid_id", "Invalid identifier.")
    return value


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, mode)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        tmp_name = None
    except OSError as exc:
        raise BridgeError("state_error", f"Cannot write artifact: {path}") from exc
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def atomic_write_text(
    path: Path, text: str, *, mode: int = 0o600, encoding: str = "utf-8"
) -> None:
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


def append_json_line(path: Path, value: str) -> None:
    """Append one already-serialized JSON value without logging secrets."""
    ensure_private_dir(path.parent)
    try:
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise BridgeError("state_error", f"Cannot append event log: {path}") from exc


def read_text_artifact(path: Path, limit: int | None = None) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(None if limit is None else limit)
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""
