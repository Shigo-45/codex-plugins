from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from gemini_baton.config import Settings  # noqa: E402
from gemini_baton import mcp_server  # noqa: E402
from gemini_baton.service import RunService  # noqa: E402

mcp_server._service = RunService(
    Settings.from_env(),
    worker_command=[sys.executable, str((Path(__file__).parent / "fake_worker.py").resolve())],
)

_WritingService = mcp_server.WritingService


def fake_writing_service(settings):
    return _WritingService(settings, worker_command=[
        sys.executable, str((Path(__file__).parent / "fake_writing_worker.py").resolve()),
    ])


mcp_server.WritingService = fake_writing_service

if __name__ == "__main__":
    mcp_server.mcp.run(transport="stdio")
