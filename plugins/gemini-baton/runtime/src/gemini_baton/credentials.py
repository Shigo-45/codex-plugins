from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import BridgeError

KEYCHAIN_SERVICE = "gemini-baton"
KEYCHAIN_ACCOUNT = "GEMINI_API_KEY"
SECURITY = "/usr/bin/security"
_SECURITY = SECURITY
_ENV_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
ENV_FILE_VARIABLE = "GEMINI_BATON_ENV_FILE"
_MAX_ENV_FILE_BYTES = 262_144


@dataclass(frozen=True)
class Credential:
    source: str
    key: str


def _env_key(env: Mapping[str, str]) -> tuple[str, str] | None:
    for name in _ENV_NAMES:
        raw = env.get(name)
        if raw is not None and raw.strip():
            value = raw.strip()
            if any(ord(char) < 32 for char in value):
                raise BridgeError(
                    "invalid_credential",
                    f"{name} contains control characters.",
                )
            return f"env:{name}", value
    return None


def _validate_key(value: str, source: str) -> str:
    value = value.strip()
    if not value:
        raise BridgeError(
            "invalid_credential", f"{source} resolves to an empty key."
        )
    if any(ord(char) < 32 for char in value):
        raise BridgeError(
            "invalid_credential",
            f"The credential from {source} contains control characters.",
        )
    return value


def _parse_env_file(text: str) -> dict[str, str]:
    """Extract only the two supported names; never source/export the file."""
    found: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if name not in _ENV_NAMES:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.split("#", 1)[0].strip()
        if value:
            found[name] = _validate_key(value, ENV_FILE_VARIABLE)
    return found


def _read_credential_env_file(
    env: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, bool]]:
    raw_path = env.get(ENV_FILE_VARIABLE)
    info = {
        "configured": bool(raw_path and raw_path.strip()),
        "readable": False,
        "contains_supported_key": False,
    }
    if not info["configured"]:
        return {}, info
    try:
        path = Path(raw_path).expanduser()
        if path.stat().st_size > _MAX_ENV_FILE_BYTES:
            raise OSError("credential env file is too large")
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}, info
    info["readable"] = True
    try:
        values = _parse_env_file(text)
    except BridgeError:
        return {}, info
    info["contains_supported_key"] = any(
        name in values for name in _ENV_NAMES
    )
    return values, info


def credential_env_file_info(
    env: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    env = os.environ if env is None else env
    _, info = _read_credential_env_file(env)
    return info


def _keychain_available() -> bool:
    return sys.platform == "darwin" and shutil.which(_SECURITY) is not None


def _keychain_exists() -> bool:
    if not _keychain_available():
        return False
    try:
        result = subprocess.run(
            [
                _SECURITY,
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def credential_status(
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Non-secret credential presence/source information for status output."""
    env = os.environ if env is None else env
    env_file = credential_env_file_info(env)
    try:
        found = _env_key(env)
        if found:
            return {
                "present": True,
                "source": found[0],
                "env_file": env_file,
            }
    except BridgeError:
        return {
            "present": False,
            "source": "invalid-environment-value",
            "env_file": env_file,
        }
    file_values, _ = _read_credential_env_file(env)
    for name in _ENV_NAMES:
        if name in file_values:
            return {
                "present": True,
                "source": f"env-file:{name}",
                "env_file": env_file,
            }
    if _keychain_exists():
        return {
            "present": True,
            "source": "macos-keychain:gemini-baton/GEMINI_API_KEY",
            "env_file": env_file,
        }
    return {"present": False, "source": None, "env_file": env_file}


def credential_source(env: Mapping[str, str] | None = None) -> str | None:
    """Return a non-secret source label without sending a network request."""
    return credential_status(env)["source"]


def load_credential(env: Mapping[str, str] | None = None) -> Credential:
    """Load the key into memory. Never pass the return value to logging.

    Precedence: GEMINI_API_KEY env, GOOGLE_API_KEY env, supported entries in
    GEMINI_BATON_ENV_FILE, then macOS Keychain. The env file is parsed as data,
    never sourced; unsupported variables in it are ignored.
    """
    env = os.environ if env is None else env
    found = _env_key(env)
    if found:
        return Credential(source=found[0], key=found[1])
    file_values, file_info = _read_credential_env_file(env)
    if file_info["configured"] and not file_info["readable"]:
        raise BridgeError(
            "credential_env_file_error",
            "GEMINI_BATON_ENV_FILE is set but the file could not be read or is "
            "too large.",
        )
    for name in _ENV_NAMES:
        if name in file_values:
            return Credential(
                source=f"env-file:{name}", key=file_values[name]
            )
    if _keychain_available():
        try:
            result = subprocess.run(
                [
                    _SECURITY,
                    "find-generic-password",
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-a",
                    KEYCHAIN_ACCOUNT,
                    "-w",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BridgeError(
                "credential_error",
                "Could not query macOS Keychain for gemini-baton credentials.",
            ) from exc
        if result.returncode == 0:
            value = result.stdout.decode("utf-8", errors="replace").strip()
            if value:
                return Credential(
                    source="macos-keychain:gemini-baton/GEMINI_API_KEY",
                    key=value,
                )
            raise BridgeError(
                "credential_error",
                "The gemini-baton Keychain item is empty.",
            )
        if result.returncode == 44:
            raise BridgeError(
                "credentials_missing",
                "No GEMINI_API_KEY/GOOGLE_API_KEY environment variable and no "
                "gemini-baton Keychain item was found.",
            )
        raise BridgeError(
            "credential_error",
            "Could not read the gemini-baton Keychain item (security exit "
            f"{result.returncode}).",
        )
    raise BridgeError(
        "credentials_missing",
        "Set GEMINI_API_KEY or GOOGLE_API_KEY, set GEMINI_BATON_ENV_FILE to an "
        "explicit .env file containing one of those names, or on macOS run "
        "`gemini-baton auth-set`.",
    )


def save_key_to_keychain(key: str) -> None:
    """Save through security(1) interactive mode without key material in argv.

    The key is read with getpass by the CLI. It is hex-encoded and sent over
    security's stdin using ``-i``; the child's argv is only
    ``/usr/bin/security -i``. Captured output is discarded. The key is never
    written to the bridge database, configuration, or logs.
    """
    if not _keychain_available():
        raise BridgeError(
            "unsupported_platform",
            "auth-set is only available on macOS. Use GEMINI_API_KEY or "
            "GOOGLE_API_KEY on other platforms.",
        )
    if not key or not key.strip():
        raise BridgeError("invalid_credential", "The API key was empty.")
    key = key.strip()
    hex_key = key.encode("utf-8").hex()
    command = (
        "add-generic-password -U -s "
        f"{KEYCHAIN_SERVICE} -a {KEYCHAIN_ACCOUNT} -X {hex_key}\n"
    )
    try:
        result = subprocess.run(
            [_SECURITY, "-i"],
            input=command,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeError(
            "keychain_write_failed",
            "Could not write the API key to macOS Keychain.",
        ) from exc
    if result.returncode != 0:
        raise BridgeError(
            "keychain_write_failed",
            "macOS security(1) refused to store the Keychain item.",
        )
    # Verify existence without retrieving the secret; never log either result.
    try:
        check = subprocess.run(
            [
                _SECURITY,
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeError(
            "keychain_verify_failed",
            "The key was submitted to Keychain but could not be verified.",
        ) from exc
    if check.returncode != 0:
        raise BridgeError(
            "keychain_write_failed",
            "The Keychain write returned success but the item was not found.",
        )
