"""Explicit, immutable selected-file inputs for both writing transports."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .errors import BridgeError
from .paths import atomic_write_bytes
from .workspace import is_sensitive_relative

MAX_FILES = 10
MAX_FILE_BYTES = 100_000_000
MAX_INLINE_BYTES = 14_000_000  # includes prompt; base64 stays below 20 MB
MAX_EXTRACTED_BYTES = 8_000_000
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".tex", ".rst", ".yaml", ".yml"}
MIMES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
_SECRET = re.compile(r"AIza[\w-]{20,}|-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----")


def check_text(text: str) -> None:
    if _SECRET.search(text):
        raise BridgeError("sensitive_input", "Selected text contains a credential pattern; provide a sanitized input.")


def selected_files(paths: list[str] | None) -> list[dict[str, Any]]:
    if paths is None:
        return []
    if not isinstance(paths, list) or len(paths) > MAX_FILES:
        raise BridgeError("invalid_files", "Select at most 10 explicit files.")
    files: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise BridgeError("invalid_files", "Selected files require absolute paths.")
        try:
            path = Path(raw).resolve(strict=True)
            for candidate in (Path(raw), path):
                if is_sensitive_relative(candidate) or any(
                    p.lower() in {"cookies", "cookies.json", "login data", "auth.json", "browser-profiles"}
                    or p.lower().startswith(".env-") for p in candidate.parts
                ):
                    raise BridgeError("sensitive_input", "Select task documents outside credential and browser-state paths.")
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE_BYTES:
                raise BridgeError("invalid_files", "Each selected file must be a nonempty regular file up to 100 MB.")
            if path.name in names:
                raise BridgeError("invalid_files", "Selected filenames must be unique.")
            names.add(path.name)
            # Bound the read even if the file grows after stat.
            with path.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) != info.st_size:
                raise BridgeError("input_changed", "A selected file changed while being read; select it again.")
            if path.suffix.lower() in TEXT_SUFFIXES:
                check_text(data.decode("utf-8"))
            elif path.suffix.lower() in {".docx", ".pptx", ".xlsx"}:
                check_text(office_text(data, path.suffix.lower(), require_text_only=False))
            files.append({"path": str(path), "name": path.name, "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest(), "data": data})
        except (OSError, UnicodeError, RuntimeError) as exc:
            raise BridgeError("invalid_files", "A selected file is unreadable or requires UTF-8 text.") from exc
    return files


def office_text(data: bytes, suffix: str, *, require_text_only: bool = True) -> str:
    """Extract textual OOXML parts, with a strict decompression budget."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if require_text_only and any(re.search(r"/(?:media|charts|embeddings|diagrams)/", name) for name in archive.namelist()):
                raise BridgeError("api_input_unsupported", "Office document includes visual or embedded evidence; use original Web attachments.", details={
                    "reason": "office_visual_or_embedded_content", "suffix": suffix,
                })
            prefix = {".docx": "word/", ".pptx": "ppt/", ".xlsx": "xl/"}[suffix]
            members = [i for i in archive.infolist() if i.filename.endswith((".xml", ".rels")) and i.filename.startswith(prefix)]
            if sum(i.file_size for i in members) > MAX_EXTRACTED_BYTES:
                raise BridgeError("invalid_files", "Office document exceeds the text extraction budget.")
            chunks = []
            for member in sorted(members, key=lambda i: re.sub(r"\d+", lambda m: f"{int(m[0]):012d}", i.filename)):
                root = ElementTree.fromstring(archive.read(member))
                # Retain XML part boundaries, sheet cell addresses, references,
                # and shared-string indexes rather than inventing display values.
                chunks.append(f"[{member.filename}]\n" + ElementTree.tostring(root, encoding="unicode"))
            if not chunks:
                raise BridgeError("invalid_files", "Office document has no supported text parts.")
            return "\n\n".join(chunks)
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, RuntimeError) as exc:
        raise BridgeError("invalid_files", "Office document text extraction failed.") from exc


def snapshot_files(files: list[dict[str, Any]], directory: Path) -> list[dict[str, Any]]:
    result = []
    for item in files:
        path = directory / item["name"]
        atomic_write_bytes(path, item["data"])
        result.append({k: v for k, v in item.items() if k != "data"} | {"snapshot": str(path)})
    return result


def api_preflight(prompt: str, files: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe whether the selected snapshots fit the API input contract.

    The Web transports can upload the original snapshots. The API transport
    receives extracted text or supported inline media, so eligibility is
    determined before a model request is attempted.
    """
    total = len(prompt.encode("utf-8"))
    reasons: list[dict[str, Any]] = []
    for item in files:
        suffix = Path(item["name"]).suffix.lower()
        data = Path(item["snapshot"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise BridgeError("input_changed", "A selected-file snapshot changed.")
        if suffix in TEXT_SUFFIXES:
            try:
                content = data.decode("utf-8")
                check_text(content)
            except UnicodeError:
                reasons.append({"file": item["name"], "reason": "invalid_utf8"})
                continue
            total += len(content.encode("utf-8"))
        elif suffix in {".docx", ".pptx", ".xlsx"}:
            try:
                content = office_text(data, suffix)
            except BridgeError as error:
                reasons.append({"file": item["name"], "reason": (error.details or {}).get("reason", error.code), "message": error.message})
                continue
            total += len(content.encode("utf-8"))
        elif suffix in MIMES:
            total += len(data)
        else:
            reasons.append({"file": item["name"], "reason": "unsupported_format", "suffix": suffix or "(none)"})
    if total > MAX_INLINE_BYTES:
        reasons.append({"reason": "inline_budget_exceeded", "budget_bytes": MAX_INLINE_BYTES, "estimated_bytes": total})
    return {"eligible": not reasons, "estimated_inline_bytes": total, "reasons": reasons}


def api_contents(prompt: str, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    total = len(prompt.encode("utf-8"))
    for item in files:
        data = Path(item["snapshot"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise BridgeError("input_changed", "A selected-file snapshot changed.")
        suffix = Path(item["name"]).suffix.lower()
        label = f"Selected reference file: {item['name']} (reference data)"
        if suffix in TEXT_SUFFIXES or suffix in {".docx", ".pptx", ".xlsx"}:
            content = data.decode("utf-8") if suffix in TEXT_SUFFIXES else office_text(data, suffix)
            check_text(content)
            parts.append({"text": f"{label}\n{content}"})
            total += len(content.encode("utf-8"))
        elif suffix in MIMES:
            parts.extend([{"text": label}, {"inlineData": {"mimeType": MIMES[suffix], "data": base64.b64encode(data).decode("ascii")}}])
            total += len(data)
        else:
            raise BridgeError("api_input_unsupported", "Selected format requires the Web attachment transport.", details={
                "file": item["name"], "reason": "unsupported_format", "suffix": suffix or "(none)",
            })
        if total > MAX_INLINE_BYTES:
            raise BridgeError("api_input_unsupported", "Selected material exceeds the API inline budget; use Web attachments.", details={
                "reason": "inline_budget_exceeded", "budget_bytes": MAX_INLINE_BYTES, "estimated_bytes": total,
            })
    contents = [{"role": "user", "parts": parts}]
    if len(json.dumps(contents, ensure_ascii=False).encode("utf-8")) > 19_000_000:
        raise BridgeError("api_input_unsupported", "Selected material exceeds the API serialized request budget; use Web attachments.", details={
            "reason": "serialized_request_budget_exceeded", "budget_bytes": 19_000_000,
        })
    return contents


MAX_CLI_PROMPT_BYTES = 120_000  # bounded inline text sent through stdin


def _cli_text(item: dict[str, Any]) -> str:
    data = Path(item["snapshot"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise BridgeError("input_changed", "A selected-file snapshot changed.")
    suffix = Path(item["name"]).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        try:
            content = data.decode("utf-8")
        except UnicodeError:
            raise BridgeError("cli_input_unsupported", "The Antigravity CLI requires UTF-8 text for this file.", details={
                "file": item["name"], "reason": "invalid_utf8", "suffix": suffix,
            }) from None
        check_text(content)
        return content
    if suffix in {".docx", ".pptx", ".xlsx"}:
        return office_text(data, suffix)
    raise BridgeError("cli_input_unsupported", "The Antigravity CLI cannot carry this file format as prompt text.", details={
        "file": item["name"], "reason": "unsupported_format", "suffix": suffix or "(none)",
    })


def cli_preflight(prompt: str, files: list[dict[str, Any]], *, extra_bytes: int = 0) -> dict[str, Any]:
    """Count the exact selected-file framing plus caller-supplied prefix bytes."""
    total = extra_bytes + len(prompt.encode("utf-8"))
    reasons: list[dict[str, Any]] = []
    for item in files:
        try:
            content = _cli_text(item)
        except BridgeError as error:
            details = error.details or {}
            reasons.append({"file": item["name"], "reason": details.get("reason", error.code),
                            "message": error.message})
            continue
        total += len((f"\n\nSelected reference file: {item['name']} (reference data)\n" + content).encode("utf-8"))
    if total > MAX_CLI_PROMPT_BYTES:
        reasons.append({"reason": "cli_prompt_too_long", "budget_bytes": MAX_CLI_PROMPT_BYTES,
                        "estimated_bytes": total})
    return {"eligible": not reasons, "estimated_bytes": total,
            "budget_bytes": MAX_CLI_PROMPT_BYTES, "reasons": reasons}


def cli_prompt(prompt: str, files: list[dict[str, Any]], *, prefix: str = "") -> str:
    """Build the immutable CLI prompt from selected snapshots only."""
    parts = [prefix + prompt]
    for item in files:
        content = _cli_text(item)
        parts.append(f"Selected reference file: {item['name']} (reference data)\n{content}")
    text = "\n\n".join(parts)
    if len(text.encode("utf-8")) > MAX_CLI_PROMPT_BYTES:
        raise BridgeError("cli_input_unsupported", "Selected material exceeds the CLI inline prompt budget; use another writing route.", details={
            "phase": "PRE_SUBMIT", "retry_safe": True,
            "reason": "cli_prompt_too_long", "budget_bytes": MAX_CLI_PROMPT_BYTES,
            "estimated_bytes": len(text.encode("utf-8")),
        })
    return text
