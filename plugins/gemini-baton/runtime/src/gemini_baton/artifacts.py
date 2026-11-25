from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .paths import (
    StateLayout,
    append_json_line,
    atomic_write_text,
    ensure_private_dir,
)
from .redact import redact_value
from .util import json_dumps, utc_now_iso


class RunArtifacts:
    def __init__(
        self,
        layout: StateLayout,
        run_id: str,
        *,
        secrets: Iterable[str] = (),
    ) -> None:
        self.layout = layout
        self.run_id = run_id
        self.dir = layout.run_dir(run_id)
        self.steps = ensure_private_dir(self.dir / "steps")
        self.tools = ensure_private_dir(self.dir / "tools")
        self.checks = ensure_private_dir(self.dir / "checks")
        self.events_path = self.dir / "events.jsonl"
        self.secrets = tuple(secret for secret in secrets if secret)

    def _safe(self, value: Any) -> Any:
        return redact_value(value, self.secrets)

    def event(self, event_type: str, **fields: Any) -> None:
        payload = {
            "ts": utc_now_iso(),
            "run_id": self.run_id,
            "event": event_type,
            **fields,
        }
        append_json_line(
            self.events_path, json_dumps(self._safe(payload), pretty=False)
        )

    def write_json(self, relative: str, value: Any) -> Path:
        path = self._inside(relative)
        atomic_write_text(path, json_dumps(self._safe(value), pretty=True))
        return path

    def write_text(self, relative: str, value: str) -> Path:
        path = self._inside(relative)
        atomic_write_text(path, self._safe(value))
        return path

    def _inside(self, relative: str) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("artifact path must stay inside the run directory")
        path = self.dir / candidate
        ensure_private_dir(path.parent)
        return path
