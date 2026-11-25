from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import BridgeError
from .paths import StateLayout

SCHEMA_VERSION = 1

ACTIVE_STATUSES = ("running", "cancelling")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    cwd TEXT NOT NULL,
    model TEXT NOT NULL,
    permission_mode TEXT NOT NULL,
    allow_shell INTEGER NOT NULL,
    transcript_json TEXT NOT NULL,
    transcript_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_id TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    status TEXT NOT NULL,
    prompt TEXT NOT NULL,
    cwd TEXT NOT NULL,
    model TEXT NOT NULL,
    selected_model TEXT NOT NULL,
    permission_mode TEXT NOT NULL,
    allow_shell INTEGER NOT NULL,
    checks_json TEXT NOT NULL,
    timeout_seconds REAL NOT NULL,
    max_steps INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    max_tool_output_chars INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    updated_at TEXT NOT NULL,
    deadline_at TEXT,
    worker_token_hash TEXT,
    worker_pid INTEGER,
    worker_pgid INTEGER,
    worker_identity TEXT,
    worker_started_at TEXT,
    active_command_pid INTEGER,
    active_command_note TEXT,
    model_calls INTEGER NOT NULL DEFAULT 0,
    steps INTEGER NOT NULL DEFAULT 0,
    actual_model_version TEXT,
    finish_reason TEXT,
    usage_json TEXT,
    final_text TEXT,
    error_json TEXT,
    changed_files_json TEXT,
    checks_result_json TEXT,
    artifact_dir TEXT,
    cancel_requested_at TEXT,
    exit_code INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_session
ON runs(session_id)
WHERE status IN ('running', 'cancelling');

CREATE INDEX IF NOT EXISTS runs_by_session
ON runs(session_id, created_at);

CREATE INDEX IF NOT EXISTS runs_by_status
ON runs(status, created_at);
"""


class Store:
    """SQLite access with short-lived connections and explicit transactions."""

    def __init__(self, layout: StateLayout) -> None:
        self.layout = layout
        self.path = layout.db
        self._ensure_database()

    def _ensure_database(self) -> None:
        path = Path(self.path)
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
        except OSError as exc:
            raise BridgeError(
                "state_error", "Cannot initialize the state database."
            ) from exc
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise BridgeError(
                    "state_error",
                    f"Unsupported state schema version {version}; expected "
                    f"{SCHEMA_VERSION}.",
                )
            if version == 0:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("PRAGMA journal_mode=WAL")
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                try:
                    os.chmod(candidate, 0o600)
                except OSError:
                    pass

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        try:
            conn = sqlite3.connect(
                self.path,
                isolation_level=None,
                timeout=30.0,
            )
        except sqlite3.Error as exc:
            raise BridgeError(
                "state_error", "Cannot open the state database."
            ) from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    # -- reads -----------------------------------------------------------
    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_run_for_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE session_id = ? ORDER BY created_at DESC "
                "LIMIT 1",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None

    def active_runs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status IN ('running','cancelling') "
                "ORDER BY created_at"
            ).fetchall()
            return [dict(row) for row in rows]

    def active_run_for_session(
        self, session_id: str, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        if conn is None:
            with self.connect() as own:
                row = own.execute(
                    "SELECT * FROM runs WHERE session_id = ? AND status IN "
                    "('running','cancelling')",
                    (session_id,),
                ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM runs WHERE session_id = ? AND status IN "
                "('running','cancelling')",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def count_active_runs(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE status IN ('running','cancelling')"
        ).fetchone()
        return int(row[0])

    def list_sessions(self, limit: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*,
                       r.id AS latest_run_id,
                       r.status AS latest_run_status,
                       r.created_at AS latest_run_created_at
                FROM sessions s
                LEFT JOIN runs r ON r.id = (
                    SELECT id FROM runs WHERE session_id = s.id
                    ORDER BY created_at DESC LIMIT 1
                )
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
