from __future__ import annotations

import re
from typing import Any, Iterable

_REDACTED = "[REDACTED]"

_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token)=)"
    r"([^&#\s]+)"
)
_HEADER_SECRET = re.compile(
    r"(?i)((?:authorization\s*:\s*bearer|x-goog-api-key\s*:)\s*)(\S+)"
)
_GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]+=*")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|client[_-]?secret)\b"
    r"(\s*[:=]\s*[\"']?)([^\s,\"'}]+)"
)


def redact_text(text: Any, secrets: Iterable[str] = ()) -> str:
    """Best-effort removal of credentials from user-visible text.

    This is defense in depth, not a guarantee that an arbitrary command's
    output is safe. The bridge never intentionally writes API keys to logs.
    """

    if text is None:
        return ""
    value = str(text)
    for secret in secrets:
        if secret:
            value = value.replace(secret, _REDACTED)
    value = _QUERY_SECRET.sub(r"\1" + _REDACTED, value)
    value = _HEADER_SECRET.sub(r"\1" + _REDACTED, value)
    value = _GOOGLE_KEY.sub(_REDACTED, value)
    value = _BEARER.sub("Bearer " + _REDACTED, value)
    value = _SECRET_ASSIGNMENT.sub(r"\1\2" + _REDACTED, value)
    return value


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, secrets) for item in value)
    if isinstance(value, dict):
        return {
            redact_text(key, secrets): redact_value(item, secrets)
            for key, item in value.items()
        }
    return value
