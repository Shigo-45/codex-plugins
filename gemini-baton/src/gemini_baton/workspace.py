from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .errors import BridgeError

_SENSITIVE_DIR_NAMES = {
    ".git",
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".docker",
    ".kube",
}
_SENSITIVE_FILE_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".envrc",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
    "client_secret",
    "client_secrets.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
_SENSITIVE_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
}


def resolve_workspace(cwd: str | os.PathLike[str]) -> Path:
    try:
        raw = Path(cwd).expanduser()
    except (TypeError, ValueError) as exc:
        raise BridgeError("invalid_cwd", "cwd is not a valid path.") from exc
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BridgeError(
            "invalid_cwd", "cwd does not exist or cannot be resolved."
        ) from exc
    if not resolved.is_dir():
        raise BridgeError("invalid_cwd", "cwd must be a directory.")
    return resolved


def is_sensitive_relative(relative: Path) -> bool:
    parts = relative.parts
    for part in parts:
        lower = part.lower()
        if lower in _SENSITIVE_DIR_NAMES:
            return True
        if lower == ".env" or lower.startswith(".env."):
            return True
        if lower in _SENSITIVE_FILE_NAMES:
            return True
        if lower.startswith("credentials.") or lower.startswith("secrets."):
            return True
        if lower.startswith("client_secret"):
            return True
        if (
            "service-account" in lower
            or "service_account" in lower
            or "gserviceaccount" in lower
        ):
            return True
        if any(
            marker in lower
            for marker in ("credential", "secret", "api_key", "apikey")
        ):
            return True
        if lower.endswith((".token", ".secrets")):
            return True
        if any(lower.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES):
            return True
        if lower.startswith("id_rsa") or lower.startswith("id_ed25519"):
            return True
    return False


def resolve_workspace_path(
    root: Path,
    user_path: str,
    *,
    must_exist: bool = False,
    for_write: bool = False,
    allow_directory: bool = False,
) -> tuple[Path, Path]:
    """Resolve a caller path under root, rejecting symlink escapes.

    Returns (absolute resolved path, relative path). The root itself is not a
    valid file target. This is a path check, not an OS sandbox.
    """

    if not isinstance(user_path, str) or not user_path.strip():
        raise BridgeError("invalid_path", "path must be a non-empty string.")
    if "\x00" in user_path:
        raise BridgeError("invalid_path", "path contains a NUL byte.")
    candidate_input = Path(user_path)
    if candidate_input.is_absolute():
        raise BridgeError(
            "path_outside_workspace", "Absolute paths are not accepted."
        )
    root_real = root.resolve()
    try:
        path = (root_real / candidate_input).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise BridgeError("invalid_path", "path cannot be resolved.") from exc
    try:
        relative = path.relative_to(root_real)
    except ValueError as exc:
        raise BridgeError(
            "path_outside_workspace",
            "Path resolves outside the selected workspace.",
        ) from exc
    if relative == Path("."):
        raise BridgeError("invalid_path", "The path must name a file.")
    if is_sensitive_relative(relative):
        raise BridgeError(
            "sensitive_path_denied",
            "This path is excluded because it looks like credentials or "
            "repository metadata.",
        )
    if must_exist and not path.exists():
        raise BridgeError("path_not_found", "Path does not exist.")
    if path.exists() and path.is_dir() and (for_write or not allow_directory):
        raise BridgeError("invalid_path", "Path is a directory.")
    if (
        not for_write
        and not allow_directory
        and path.exists()
        and not path.is_file()
    ):
        raise BridgeError("invalid_path", "Path is not a regular file.")
    if for_write:
        parent = path.parent
        if not parent.exists() or not parent.is_dir():
            raise BridgeError(
                "path_not_found",
                "Parent directory does not exist inside the workspace.",
            )
        parent_real = parent.resolve()
        try:
            parent_real.relative_to(root_real)
        except ValueError as exc:
            raise BridgeError(
                "path_outside_workspace",
                "Path parent resolves outside the selected workspace.",
            ) from exc
    return path, relative


def read_text_file(path: Path, max_bytes: int) -> tuple[str, int, bool]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise BridgeError("path_not_found", "File cannot be read.") from exc
    if not path.is_file():
        raise BridgeError("invalid_path", "Path is not a regular file.")
    truncated = stat.st_size > max_bytes
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes)
    except OSError as exc:
        raise BridgeError("read_failed", "File cannot be read.") from exc
    if b"\x00" in raw:
        raise BridgeError("binary_file", "Binary files are not supported.")
    try:
        text = raw.decode("utf-8", errors="replace" if truncated else "strict")
    except UnicodeDecodeError as exc:
        raise BridgeError(
            "binary_file", "File is not valid UTF-8 text."
        ) from exc
    if truncated:
        text += "\n\n[truncated by read limit]"
    return text, stat.st_size, truncated


def atomic_workspace_write(path: Path, text: str) -> None:
    parent = path.parent
    existing_mode: int | None = None
    try:
        if path.exists():
            existing_mode = path.stat().st_mode & 0o777
    except OSError:
        existing_mode = None
    fd: int | None = None
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        mode = existing_mode if existing_mode is not None else 0o644
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        fd = None
        os.replace(temp_name, path)
        temp_name = None
    except OSError as exc:
        raise BridgeError("write_failed", "File cannot be written.") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
