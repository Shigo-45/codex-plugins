from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fake_backend import fake_factory  # noqa: E402

from gemini_baton.worker import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(backend_factory=fake_factory))
