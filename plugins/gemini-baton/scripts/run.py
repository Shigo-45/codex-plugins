#!/usr/bin/env python3
"""Bootstrap the bundled bridge into a private, content-addressed environment."""
from __future__ import annotations

import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import time
import venv

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ENV_KEYS = frozenset({
    "GEMINI_BATON_ENV_FILE", "GEMINI_BATON_MODEL", "GEMINI_WEB_BRIDGE_PROXY",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
})


def runtime_environment(env: dict[str, str]) -> dict[str, str]:
    """Load user-local transport options without embedding them in the plugin."""
    if env.get("GEMINI_BATON_STATE_DIR"):
        state = Path(env["GEMINI_BATON_STATE_DIR"]).expanduser()
    elif sys.platform == "darwin":
        state = Path.home() / "Library/Application Support/gemini-baton"
    elif os.name == "nt":
        state = Path(env.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "gemini-baton"
    else:
        state = Path(env.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "gemini-baton"
    path = state / "runtime-env.json"
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError()
        saved = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(saved, dict) or saved.keys() - RUNTIME_ENV_KEYS
                or any(not isinstance(value, str) or "\x00" in value for value in saved.values())):
            raise ValueError()
    except FileNotFoundError:
        return dict(env)
    except (OSError, ValueError, UnicodeError):
        raise RuntimeError("Invalid private Gemini runtime-env.json configuration.") from None
    return {**env, **saved}


def cache_root() -> Path:
    override = os.environ.get("WRITE_TO_ME_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/write-to-me"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "write-to-me/cache"
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "write-to-me"


def fingerprint() -> str:
    digest = hashlib.sha256(repr((sys.version_info[:2], sys.platform, platform.machine())).encode())
    for path in sorted((ROOT / "runtime").rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(ROOT)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:24]


def executable(directory: Path, name: str) -> Path:
    return directory / ("Scripts" if os.name == "nt" else "bin") / (name + ".exe" if os.name == "nt" else name)


def checked(argv: list[str], env: dict[str, str]) -> None:
    subprocess.run(argv, check=True, env=env, stdout=sys.stderr, stderr=sys.stderr)


def main() -> int:
    if sys.version_info < (3, 11):
        raise RuntimeError("Write to me requires Python 3.11 or newer.")
    os.umask(0o077)
    os.environ.update(runtime_environment(dict(os.environ)))
    cache = cache_root()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = cache / fingerprint()
    lock = cache / "bootstrap.lock"
    # File locks release automatically after a crash; serialize installation only.
    with lock.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.write(b"0")
            handle.flush()
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.2)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        python = executable(environment, "python")
        marker = environment / ".ready"
        if not marker.is_file() or not python.is_file():
            uv = shutil.which("uv")
            if not uv:
                bootstrap = cache / "bootstrap-uv-0.10.11"
                uv_path = executable(bootstrap, "uv")
                if not uv_path.is_file():
                    venv.create(bootstrap, with_pip=True)
                    checked([str(executable(bootstrap, "python")), "-m", "pip", "install", "uv==0.10.11"], dict(os.environ))
                uv = str(uv_path)
            setup_env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(environment))
            checked([uv, "sync", "--locked", "--no-dev", "--no-editable", "--python", sys.executable,
                     "--project", str(ROOT / "runtime")], setup_env)
            marker.touch(mode=0o600)
    env = dict(os.environ)
    env["GEMINI_BATON_WEB_CLI"] = str(ROOT / "web/gemini-web-cli.mjs")
    args = sys.argv[1:] or ["mcp"]
    os.execve(str(python), [str(python), "-m", "gemini_baton.cli", *args], env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
