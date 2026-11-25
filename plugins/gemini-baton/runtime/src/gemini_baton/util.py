from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def json_dumps(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def json_loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def truncate_text(
    text: str, limit: int, *, marker: str = "\n\n[truncated]"
) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit <= len(marker):
        return text[: max(0, limit)], True
    return text[: limit - len(marker)] + marker, True


def preview_text(text: str | None, limit: int, *, path: str | None = None) -> str:
    if not text:
        return ""
    out, truncated = truncate_text(text, limit)
    if truncated and path:
        out += f"\n\n[full text: {path}]"
    return out


def usage_dict(usage: Any) -> dict[str, int]:
    """Convert a google-genai usage object (or dict) to plain counters."""
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        raw = usage.model_dump(exclude_none=True)
    elif isinstance(usage, dict):
        raw = usage
    else:
        return {}
    fields = (
        "prompt_token_count",
        "candidates_token_count",
        "thoughts_token_count",
        "tool_use_prompt_token_count",
        "cached_content_token_count",
        "total_token_count",
    )
    result: dict[str, int] = {}
    for field in fields:
        value = raw.get(field)
        if isinstance(value, int):
            result[field] = value
    return result


def add_usage(total: dict[str, int], step: dict[str, int]) -> None:
    for key, value in step.items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


def elapsed_seconds(start_iso: str | None, end_iso: str | None = None) -> float:
    start = parse_iso(start_iso)
    if start is None:
        return 0.0
    end = parse_iso(end_iso) or utc_now()
    return max(0.0, (end - start).total_seconds())
