from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

from . import __version__
from .config import Settings
from .credentials import save_key_to_keychain
from .errors import BridgeError
from .plugin_settings import SettingsStore, launch_settings_panel, merge_writing_status
from .redact import redact_text, redact_value
from .service import RunService
from .writing import WritingService


def _print_json(value: Any, *, stream: Any = None) -> None:
    print(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        file=sys.stdout if stream is None else stream,
    )


def _service() -> RunService:
    return RunService(Settings.from_env())


def _check_arg(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(
            "check must be a JSON array of strings, for example "
            "[\"pytest\",\"-q\"]"
        ) from exc
    if (
        not isinstance(parsed, list)
        or not parsed
        or not all(isinstance(item, str) for item in parsed)
    ):
        raise argparse.ArgumentTypeError(
            "check must be a non-empty JSON array of strings"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gemini-baton",
        description=(
            "Durable Gemini API subagent bridge. Background workers survive the "
            "calling CLI/MCP process; state is persisted in SQLite."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status", help="local credential/model/version status; no network calls"
    )
    status.set_defaults(handler=_cmd_status)

    write = sub.add_parser("write", help="end-to-end writing with Antigravity CLI -> API -> AI Studio -> Web fallback; task JSON on stdin")
    write.add_argument("--wait-seconds", type=float, default=45)
    write.set_defaults(handler=_cmd_write)

    write_result = sub.add_parser("write-result", help="read a durable writing job without resubmitting")
    write_result.add_argument("job_id")
    write_result.add_argument("--wait-seconds", type=float, default=0)
    write_result.set_defaults(handler=_cmd_write_result)

    settings = sub.add_parser("settings", help="open the local writing settings panel")
    settings.add_argument("--status", action="store_true", help="print effective settings without opening a browser")
    settings.set_defaults(handler=_cmd_settings)

    auth = sub.add_parser(
        "auth-set",
        help="prompt for an API key with getpass and store it in macOS Keychain",
    )
    auth.set_defaults(handler=_cmd_auth_set)

    send = sub.add_parser(
        "send", help="start a background run and print run_id/session_id"
    )
    send.add_argument("message", help="prompt text, or '-' to read stdin")
    send.add_argument("--cwd", help="workspace directory; required for a new session")
    send.add_argument("--session-id", help="continue this session")
    send.add_argument("--model", help="per-run/session model override")
    send.add_argument(
        "--permission-mode",
        choices=["read-only", "workspace-write"],
        help="permission mode; inherited on continuation when omitted",
    )
    shell_group = send.add_mutually_exclusive_group()
    shell_group.add_argument(
        "--allow-shell",
        dest="allow_shell",
        action="store_true",
        default=None,
        help="enable run_command/checks (requires workspace-write)",
    )
    shell_group.add_argument(
        "--no-shell",
        dest="allow_shell",
        action="store_false",
        help="explicitly disable run_command/checks for this send",
    )
    send.add_argument("--timeout-seconds", type=int)
    send.add_argument("--max-steps", type=int)
    send.add_argument(
        "--check",
        action="append",
        type=_check_arg,
        metavar='JSON_ARGV',
        help='final check argv as JSON, e.g. --check \'["pytest","-q"]\'',
    )
    send.set_defaults(handler=_cmd_send)

    wait = sub.add_parser(
        "wait", help="wait up to 50 seconds (hard MCP-safe cap)"
    )
    wait.add_argument("run_id")
    wait.add_argument("--timeout-seconds", type=float, default=20.0)
    wait.set_defaults(handler=_cmd_wait)

    result = sub.add_parser("result", help="read a persisted run result")
    result.add_argument("run_id")
    result.add_argument(
        "--full", action="store_true", help="include the full final text"
    )
    result.set_defaults(handler=_cmd_result)

    cancel = sub.add_parser(
        "cancel", help="cancel a run and its worker process group"
    )
    cancel.add_argument("run_id")
    cancel.set_defaults(handler=_cmd_cancel)

    models = sub.add_parser(
        "list-models", help="explicit network query of available Gemini models"
    )
    models.set_defaults(handler=_cmd_list_models)

    sessions = sub.add_parser("list-sessions", help="list persisted sessions")
    sessions.add_argument("--limit", type=int, default=20)
    sessions.set_defaults(handler=_cmd_list_sessions)

    mcp = sub.add_parser("mcp", help="run the MCP server on stdin/stdout")
    mcp.set_defaults(handler=_cmd_mcp)
    return parser


def _cmd_status(_args: argparse.Namespace) -> int:
    service = _service()
    _print_json(merge_writing_status(service.status(), service.settings))
    return 0


def _cmd_settings(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.status:
        _print_json(SettingsStore(settings).public_status())
        return 0
    result = launch_settings_panel(settings)
    _print_json(result)
    return 0 if result.get("ok") else 1


def _cmd_auth_set(_args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        raise BridgeError(
            "unsupported_platform",
            "auth-set is macOS-only. Use GEMINI_API_KEY or GOOGLE_API_KEY.",
        )
    try:
        first = getpass.getpass("Gemini API key (input hidden): ")
        second = getpass.getpass("Repeat API key: ")
    except (KeyboardInterrupt, EOFError):
        raise BridgeError("auth_cancelled", "API key entry was cancelled.") from None
    if not first or first != second:
        raise BridgeError(
            "invalid_credential", "Keys were empty or did not match; nothing stored."
        )
    save_key_to_keychain(first)
    _print_json(
        {
            "ok": True,
            "stored": "macOS Keychain",
            "service": "gemini-baton",
            "account": "GEMINI_API_KEY",
            "note": (
                "The key was read interactively and is not included in output. "
                "GEMINI_API_KEY/GOOGLE_API_KEY environment variables take "
                "precedence over Keychain."
            ),
        }
    )
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    message = sys.stdin.read() if args.message == "-" else args.message
    payload = _service().send(
        message,
        cwd=args.cwd,
        session_id=args.session_id,
        model=args.model,
        permission_mode=args.permission_mode,
        allow_shell=args.allow_shell,
        timeout_seconds=args.timeout_seconds,
        max_steps=args.max_steps,
        checks=args.check,
    )
    _print_json(payload)
    return 0 if payload.get("status") != "failed" else 1


def _cmd_write(args: argparse.Namespace) -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        raise BridgeError("invalid_input", "Expected a writing task JSON object on stdin.") from None
    allowed = {"prompt", "cwd", "file_paths", "request_id", "conversation_id", "route", "route_timeout_seconds"}
    if not isinstance(payload, dict) or not {"prompt", "cwd"} <= payload.keys() or payload.keys() - allowed:
        raise BridgeError("invalid_input", "Provide prompt, cwd and optional file_paths, request_id, conversation_id, route, route_timeout_seconds.")
    result = WritingService(Settings.from_env()).submit(**payload, wait_seconds=args.wait_seconds)
    _print_json(result)
    return 0 if result["ok"] else 1


def _cmd_write_result(args: argparse.Namespace) -> int:
    result = WritingService(Settings.from_env()).result(args.job_id, wait_seconds=args.wait_seconds)
    _print_json(result)
    return 0 if result["ok"] else 1


def _cmd_wait(args: argparse.Namespace) -> int:
    _print_json(_service().wait(args.run_id, args.timeout_seconds))
    return 0


def _cmd_result(args: argparse.Namespace) -> int:
    _print_json(_service().result(args.run_id, compact=not args.full))
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    _print_json(_service().cancel(args.run_id))
    return 0


def _cmd_list_models(_args: argparse.Namespace) -> int:
    _print_json(_service().list_models())
    return 0


def _cmd_list_sessions(args: argparse.Namespace) -> int:
    _print_json(_service().list_sessions(args.limit))
    return 0


def _cmd_mcp(_args: argparse.Namespace) -> int:
    from .mcp_server import run

    run()
    return 0


_HANDLERS = {
    "status": _cmd_status,
    "auth-set": _cmd_auth_set,
    "send": _cmd_send,
    "wait": _cmd_wait,
    "result": _cmd_result,
    "settings": _cmd_settings,
    "cancel": _cmd_cancel,
    "list-models": _cmd_list_models,
    "list-sessions": _cmd_list_sessions,
    "mcp": _cmd_mcp,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except BridgeError as exc:
        _print_json(
            {"ok": False, "error": redact_value(exc.as_dict())},
            stream=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": "unexpected_error",
                    "message": redact_text(f"{type(exc).__name__}: {exc}"),
                },
            },
            stream=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
