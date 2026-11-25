from __future__ import annotations

import argparse

from .config import Settings
from .plugin_settings import install_saved_credential_fallback
from .writing import WritingService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    install_saved_credential_fallback()
    WritingService(Settings.from_env()).execute(args.job_id)


if __name__ == "__main__":
    main()
