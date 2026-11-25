#!/usr/bin/env python3
"""Vendor the redistributable Fiji plugin without importing its developer checkout."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "copilotj-fiji"
TARGET = ROOT / "plugins/image-analysis"


def main() -> None:
    if TARGET.exists():
        raise SystemExit("Destination exists; review changes and remove the generated snapshot before resyncing.")
    shutil.copytree(SOURCE / "codex-plugin", TARGET, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(SOURCE / "plugin/src", TARGET / "source/src")
    shutil.copyfile(SOURCE / "plugin/pom.xml", TARGET / "source/pom.xml")
    for name in ("LICENSE",):
        shutil.copyfile(SOURCE / name, TARGET / name)
    provenance = {
        "upstream": subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=SOURCE, text=True).strip(),
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip(),
        "includes_local_modifications": True,
        "sha256": {str(p.relative_to(TARGET)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(TARGET.rglob("*")) if p.is_file()},
    }
    (TARGET / "UPSTREAM.json").write_text(json.dumps(provenance, indent=2) + "\n")


if __name__ == "__main__":
    main()
