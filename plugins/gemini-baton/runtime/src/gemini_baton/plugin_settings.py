"""Private writing settings and the authenticated local settings panel.

The settings JSON lives in the bridge's private external state directory.  It
contains only the ordered enabled writing route names and, when the user saves
one, an API key.  Public status never includes the key.

The browser panel is deliberately small and local: it binds 127.0.0.1, checks
the exact Host and Origin, uses an HttpOnly session cookie plus a CSRF token,
and accepts mutations only as authenticated JSON POST requests.
"""
from __future__ import annotations

import argparse
import fcntl
import hmac
import html
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import parse_qs, quote, urlsplit

from .config import Settings
from .credentials import Credential, credential_status, load_credential
from .errors import BridgeError
from .paths import atomic_write_text, ensure_private_dir
from .writing_routes import ROUTES, resolve_route

SETTINGS_VERSION = 1
SETTINGS_DIR_NAME = "writing-settings"
SETTINGS_FILE_NAME = "settings.json"
MAX_SETTINGS_BYTES = 128 * 1024
MAX_API_KEY_CHARS = 8192
DEFAULT_ROUTE_NAMES = tuple(f"{channel}:{model}" for channel, model, _effort in ROUTES)
ROUTE_BY_NAME = {f"{channel}:{model}": (channel, model, effort)
                 for channel, model, effort in ROUTES}
CHANNEL_LABELS = {
    "cli": "Antigravity CLI",
    "api": "Gemini API",
    "aistudio": "AI Studio (Web)",
    "web": "Gemini Web",
}
LOGIN_PROVIDERS = (
    {"id": "gemini", "label": "Gemini Web"},
    {"id": "aistudio", "label": "AI Studio"},
)
COOKIE_NAME = "gemini_baton_settings_session"
CSRF_HEADER = "X-CSRF-Token"
MAX_BODY_BYTES = 64 * 1024
PANEL_IDLE_TIMEOUT_SECONDS = 600
SESSION_TTL_SECONDS = 8 * 60 * 60
LOGIN_PROBE_SECONDS = 0.4
WEB_CLI_RELATIVE = "gemini-web-bridge/plugins/gemini-web-bridge/dist/gemini-web-cli.mjs"
_CREDENTIAL_ENV_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
_CHILD_SECRET_ENV = {
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_BATON_ENV_FILE",
    "GOOGLE_APPLICATION_CREDENTIALS",
}
_UNSET = object()

ANTIGRAVITY_LOGIN_COMMAND = "agy acp --login"
# Verified against agy 1.5.17: `agy --help` has no `login` subcommand, while
# `agy acp --help` documents --login as "Log in with Google interactively,
# then exit (ACP terminal auth)".  Keep the command exactly as documented.
ANTIGRAVITY_LOGIN_INSTRUCTIONS = (
    "For Antigravity CLI login, run agy acp --login in a terminal. "
    "Complete Google sign-in in the browser, then reopen the panel."
)


@dataclass(frozen=True)
class LoadedSettings:
    routes: tuple[str, ...]
    api_key: str | None
    source: str
    routes_source: str
    updated_at: float | None


def _has_env_credential(env: Mapping[str, str]) -> bool:
    for name in _CREDENTIAL_ENV_NAMES:
        value = env.get(name)
        if value is not None and value.strip():
            return True
    raw_file = env.get("GEMINI_BATON_ENV_FILE")
    return bool(raw_file and raw_file.strip())


def _validate_api_key(value: Any) -> str:
    if not isinstance(value, str):
        raise BridgeError("invalid_settings", "The API key must be text; it was not saved.")
    value = value.strip()
    if not value or len(value) > MAX_API_KEY_CHARS:
        raise BridgeError(
            "invalid_settings",
            "The API key must be non-empty and within the supported length; it was not saved.",
        )
    if any(ord(char) < 32 for char in value):
        raise BridgeError(
            "invalid_settings",
            "The API key contains control characters; it was not saved.",
        )
    return value


def _canonical_setting_route(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeError("invalid_settings", "Every enabled route name must be non-empty text.")
    resolved = resolve_route(value.strip())
    if resolved is None or resolved not in ROUTES:
        raise BridgeError(
            "invalid_settings",
            "Only the sixteen default writing routes can be enabled; use an explicit route to pin Hydrogen.",
        )
    return f"{resolved[0]}:{resolved[1]}"


def _validate_route_names(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise BridgeError("invalid_settings", "routes must be a list of enabled route names.")
    if not values:
        raise BridgeError("invalid_settings", "Enable at least one writing route.")
    canonical: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _canonical_setting_route(value)
        if name in seen:
            raise BridgeError("invalid_settings", "A writing route is enabled more than once.")
        seen.add(name)
        canonical.append(name)
    return tuple(canonical)


def _normalize_default_routes(values: Sequence[Any] | None) -> tuple[str, ...]:
    candidates = DEFAULT_ROUTE_NAMES if values is None else values
    names: list[str] = []
    for value in candidates:
        if isinstance(value, str):
            name = _canonical_setting_route(value)
        elif isinstance(value, (list, tuple)) and 2 <= len(value) <= 3:
            channel, model = str(value[0]), str(value[1])
            resolved = resolve_route(f"{channel}:{model}")
            if resolved is None or resolved not in ROUTES:
                raise BridgeError("invalid_settings", "The built-in writing route policy is invalid.")
            name = f"{resolved[0]}:{resolved[1]}"
        else:
            raise BridgeError("invalid_settings", "The built-in writing route policy is invalid.")
        names.append(name)
    return tuple(dict.fromkeys(names))


def route_triples(names: Sequence[str]) -> list[tuple[str, str, str]]:
    triples: list[tuple[str, str, str]] = []
    for name in names:
        resolved = resolve_route(name)
        if resolved is None:
            raise BridgeError("invalid_settings", "A configured writing route is no longer supported.")
        triples.append(resolved)
    return triples


def route_status_rows() -> list[dict[str, Any]]:
    rows = []
    for channel, model, effort in ROUTES:
        rows.append({
            "name": f"{channel}:{model}",
            "channel": channel,
            "channel_label": CHANNEL_LABELS.get(channel, channel),
            "model": model,
            "effort": effort,
        })
    return rows


def antigravity_login_status() -> dict[str, Any]:
    configured = os.environ.get("GEMINI_BATON_AGY", "").strip()
    if configured:
        available = Path(configured).expanduser().is_file()
    else:
        available = shutil.which("agy") is not None
    return {
        "available": available,
        "command": ANTIGRAVITY_LOGIN_COMMAND,
        "instructions": ANTIGRAVITY_LOGIN_INSTRUCTIONS,
        "verified": True,
    }


def _web_cli_command() -> list[str]:
    default = Path(__file__).resolve().parents[3] / WEB_CLI_RELATIVE
    path = Path(os.environ.get("GEMINI_BATON_WEB_CLI", str(default))).expanduser()
    node = shutil.which("node")
    if not node or not path.is_file():
        raise BridgeError(
            "login_unavailable",
            "The Web bridge bundle and Node.js are required to open a Gemini Web or AI Studio login window.",
        )
    return [node, str(path)]


def launch_web_login(provider: str, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Open an existing Web CLI login window with an argument-only argv."""
    if provider not in {"gemini", "aistudio"}:
        raise BridgeError("invalid_provider", "Login provider must be gemini or aistudio.")
    argv = _web_cli_command() + ["login", "--provider", provider]
    parent_env = os.environ if env is None else env
    child_env = {key: value for key, value in parent_env.items() if key not in _CHILD_SECRET_ENV}
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            start_new_session=True,
            shell=False,
        )
    except OSError:
        raise BridgeError(
            "login_unavailable",
            "The Gemini Web login window could not be started; no login was performed.",
        ) from None
    # A fast non-zero exit usually means a resident browser owns the profile or
    # Node could not start.  Report a safe failure instead of a phantom window.
    try:
        code = process.wait(timeout=LOGIN_PROBE_SECONDS)
    except subprocess.TimeoutExpired:
        code = None
    if code is None:
        def reap() -> None:
            try:
                process.wait()
            except Exception:
                pass

        threading.Thread(target=reap, name="settings-login-reaper", daemon=True).start()
        return {"provider": provider, "status": "launched", "pid": process.pid}
    if code == 0:
        return {"provider": provider, "status": "completed", "pid": process.pid}
    raise BridgeError(
        "login_failed",
        "The Gemini Web login window closed before sign-in started. Stop any resident bridge browser and retry.",
    )


def _key_status(
    store: "SettingsStore",
    *,
    env: Mapping[str, str] | None = None,
    credentials: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    env = os.environ if env is None else env
    for name in _CREDENTIAL_ENV_NAMES:
        value = env.get(name)
        if value is not None and value.strip():
            return {"configured": True, "source": f"environment:{name}", "saved": False}
    raw_file = env.get("GEMINI_BATON_ENV_FILE")
    if raw_file and raw_file.strip():
        # The existing loader treats an explicit env file as authoritative for
        # environment credentials.  Keep the saved key below that boundary.
        if credentials is None:
            credentials = credential_status(env)
        if credentials.get("present"):
            return {"configured": True, "source": credentials.get("source"), "saved": False}
        return {"configured": False, "source": "env-file", "saved": False}
    saved = store.saved_api_key()
    if saved:
        return {"configured": True, "source": "settings:private", "saved": True}
    if credentials is None:
        credentials = credential_status(env)
    if credentials.get("present"):
        return {"configured": True, "source": credentials.get("source"), "saved": False}
    return {"configured": False, "source": None, "saved": False}


class SettingsStore:
    """Read and atomically replace the private writing settings document."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = ensure_private_dir(settings.state_dir / SETTINGS_DIR_NAME)
        self.path = self.root / SETTINGS_FILE_NAME
        self.lock_path = self.root / "settings.lock"
        self._mutex = threading.Lock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._mutex:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _read_document(self) -> dict[str, Any]:
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {}
        except OSError:
            raise BridgeError(
                "settings_error",
                "The private writing settings file could not be read safely.",
            ) from None
        try:
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("settings path is not a regular file")
                raw = handle.read(MAX_SETTINGS_BYTES + 1)
        except OSError:
            raise BridgeError(
                "settings_error",
                "The private writing settings file could not be read safely.",
            ) from None
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        if len(raw) > MAX_SETTINGS_BYTES:
            raise BridgeError("invalid_settings", "The private writing settings file is too large.")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BridgeError("invalid_settings", "The private writing settings JSON is invalid.") from None
        if not isinstance(document, dict):
            raise BridgeError("invalid_settings", "The private writing settings must be a JSON object.")
        allowed = {"version", "routes", "api_key", "updated_at"}
        if document.keys() - allowed:
            raise BridgeError("invalid_settings", "The private writing settings contain unsupported fields.")
        version = document.get("version", SETTINGS_VERSION)
        if isinstance(version, bool) or not isinstance(version, int) or version != SETTINGS_VERSION:
            raise BridgeError("invalid_settings", "The private writing settings version is unsupported.")
        updated_at = document.get("updated_at")
        if updated_at is not None and (
            isinstance(updated_at, bool) or not isinstance(updated_at, (int, float))
        ):
            raise BridgeError("invalid_settings", "The private writing settings timestamp is invalid.")
        return document

    def load(self) -> LoadedSettings:
        document = self._read_document()
        routes_source = "settings" if "routes" in document else "defaults"
        if routes_source == "settings":
            routes = _validate_route_names(document["routes"])
        else:
            routes = _normalize_default_routes(None)
        api_key = None
        if "api_key" in document:
            api_key = _validate_api_key(document["api_key"])
        source = "settings" if self.path.exists() else "defaults"
        updated_at = document.get("updated_at")
        return LoadedSettings(
            routes=routes,
            api_key=api_key,
            source=source,
            routes_source=routes_source,
            updated_at=float(updated_at) if updated_at is not None else None,
        )

    def _read_api_key(self) -> str | None:
        try:
            document = self._read_document()
        except BridgeError:
            return None
        if "api_key" not in document:
            return None
        try:
            return _validate_api_key(document["api_key"])
        except BridgeError:
            return None

    def saved_api_key(self) -> str | None:
        return self.load().api_key

    def save(
        self,
        *,
        routes: Sequence[str],
        api_key: Any = _UNSET,
        clear_api_key: bool = False,
    ) -> LoadedSettings:
        canonical = _validate_route_names(list(routes))
        if clear_api_key and api_key is not _UNSET and api_key not in (None, ""):
            raise BridgeError("invalid_settings", "Choose either a new API key or removal, not both.")
        with self._locked():
            # Preserve a valid key even when the saved route list needs repair;
            # a wholly unparseable document still cannot carry a value forward.
            existing_key = self._read_api_key()
            if clear_api_key:
                key = None
            elif api_key is _UNSET or api_key is None or api_key == "":
                key = existing_key
            else:
                key = _validate_api_key(api_key)
            document: dict[str, Any] = {
                "version": SETTINGS_VERSION,
                "routes": list(canonical),
                "updated_at": time.time(),
            }
            if key is not None:
                document["api_key"] = key
            atomic_write_text(self.path, json.dumps(document, ensure_ascii=False))
        return self.load()

    def effective_routes_with_source(
        self,
        *,
        default_routes: Sequence[Any] | None = None,
    ) -> tuple[tuple[tuple[str, str, str], ...], str]:
        loaded = self.load()
        names = loaded.routes if loaded.routes_source == "settings" else _normalize_default_routes(default_routes)
        return tuple(route_triples(names)), loaded.routes_source

    def public_status(
        self,
        *,
        allow_invalid: bool = False,
        credentials: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings_error = None
        try:
            loaded = self.load()
            routes = route_triples(loaded.routes)
            source = loaded.source
            routes_source = loaded.routes_source
            api_key_status = _key_status(self, credentials=credentials)
            updated_at = loaded.updated_at
        except BridgeError as exc:
            if not allow_invalid:
                raise
            settings_error = {"code": exc.code, "message": exc.message}
            default_names = _normalize_default_routes(None)
            routes = route_triples(default_names)
            source = "defaults"
            routes_source = "defaults"
            api_key_status = {"configured": False, "source": None, "saved": False}
            updated_at = None
        enabled = [f"{channel}:{model}" for channel, model, _effort in routes]
        disabled = [name for name in DEFAULT_ROUTE_NAMES if name not in set(enabled)]
        return {
            "ok": True,
            "source": source,
            "routes_source": routes_source,
            "routes": enabled,
            "route_order": enabled + disabled,
            "route_policy": [
                {"channel": channel, "model": model, "effort": effort}
                for channel, model, effort in routes
            ],
            "all_routes": route_status_rows(),
            "api_key": api_key_status,
            "antigravity": antigravity_login_status(),
            "login_providers": list(LOGIN_PROVIDERS),
            "settings_path": str(self.path),
            "updated_at": updated_at,
            "settings_error": settings_error,
        }


def merge_writing_status(
    payload: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    """Replace static status route defaults with the effective saved policy."""
    writing = payload.setdefault("writing", {})
    try:
        store = SettingsStore(settings)
        status = store.public_status(credentials=payload.get("credentials"))
        writing["routes"] = status["routes"]
        writing["route_policy"] = status["route_policy"]
        writing["routes_source"] = status["routes_source"]
        writing["settings"] = {
            "source": status["source"],
            "api_key": status["api_key"],
            "settings_path": status["settings_path"],
        }
    except BridgeError as exc:
        writing["routes_source"] = "defaults"
        writing["settings_error"] = {"code": exc.code, "message": exc.message}
        writing["settings"] = {
            "source": "defaults",
            "api_key": {"configured": False, "source": None, "saved": False},
        }
    return payload


def load_credential_with_private(
    *,
    env: Mapping[str, str] | None = None,
    original: Any = None,
) -> Credential:
    """Load a credential with environment sources ahead of the saved key.

    Precedence: GEMINI_API_KEY/GOOGLE_API_KEY, GEMINI_BATON_ENV_FILE (the
    existing loader owns both), then the private settings key, then the
    loader's remaining sources (macOS Keychain).
    """
    env = os.environ if env is None else env
    loader = original or load_credential
    if _has_env_credential(env):
        return loader(env)
    saved = SettingsStore(Settings.from_env(env)).saved_api_key()
    if saved:
        return Credential(source="settings:private", key=saved)
    return loader(env)


def install_saved_credential_fallback() -> None:
    """Install the private-key fallback inside the writing worker process."""
    from . import writing_routes

    current = writing_routes.load_credential
    if getattr(current, "_gemini_baton_private_settings", False):
        return

    def loader(env: Mapping[str, str] | None = None) -> Credential:
        return load_credential_with_private(env=env, original=current)

    loader._gemini_baton_private_settings = True  # type: ignore[attr-defined]
    writing_routes.load_credential = loader


class _SettingsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], panel: "SettingsPanel") -> None:
        self.panel = panel
        super().__init__(address, handler)


class SettingsPanel:
    """In-process panel used by tests and by the detached server child."""

    def __init__(
        self,
        settings: Settings,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        idle_timeout: float = PANEL_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        if host != "127.0.0.1":
            raise BridgeError("invalid_config", "The settings panel must bind to loopback.")
        self.settings = settings
        self.store = SettingsStore(settings)
        self.idle_timeout = idle_timeout
        self.httpd = _SettingsHTTPServer((host, port), _SettingsHandler, self)
        self.port = int(self.httpd.server_address[1])
        self.exact_host = f"127.0.0.1:{self.port}"
        self.exact_origin = f"http://{self.exact_host}"
        self.bootstrap_token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_hex(32)
        self.session_started = time.time()
        self.last_activity = time.time()
        self._token_lock = threading.Lock()
        self._stop = threading.Event()
        self.serving = threading.Event()
        self._monitor = threading.Thread(target=self._idle_monitor, name="settings-panel-idle", daemon=True)
        self.runtime_path = self.store.root / f"panel-{os.getpid()}.json"

    @property
    def base_url(self) -> str:
        return f"http://{self.exact_host}/"

    @property
    def bootstrap_url(self) -> str:
        return f"http://{self.exact_host}/auth?token={quote(self.bootstrap_token)}"

    def touch(self) -> None:
        self.last_activity = time.time()

    def host_ok(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value or "", self.exact_host)

    def origin_ok(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value or "", self.exact_origin)

    def consume_bootstrap(self, token: str | None) -> bool:
        if not token:
            return False
        with self._token_lock:
            current = self.bootstrap_token
            if current is None or not hmac.compare_digest(token, current):
                return False
            self.bootstrap_token = None
            return True

    def authenticated(self, cookie_header: str | None) -> bool:
        if not cookie_header:
            return False
        try:
            cookies = SimpleCookie()
            cookies.load(cookie_header)
        except Exception:
            return False
        morsel = cookies.get(COOKIE_NAME)
        if morsel is None:
            return False
        if time.time() - self.session_started > SESSION_TTL_SECONDS:
            return False
        return hmac.compare_digest(morsel.value, self.session_token)

    def csrf_ok(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value or "", self.csrf_token)

    def write_runtime(self) -> None:
        atomic_write_text(
            self.runtime_path,
            json.dumps({"pid": os.getpid(), "port": self.port, "started_at": time.time()}),
        )

    def cleanup_runtime(self) -> None:
        try:
            self.runtime_path.unlink()
        except OSError:
            pass

    def _idle_monitor(self) -> None:
        while not self._stop.wait(0.5):
            if self.idle_timeout > 0 and time.time() - self.last_activity > self.idle_timeout:
                self.httpd.shutdown()
                return

    def serve_forever(self) -> None:
        self.serving.set()
        self._monitor.start()
        try:
            self.httpd.serve_forever(poll_interval=0.2)
        finally:
            self._stop.set()
            self.httpd.server_close()
            self.cleanup_runtime()

    def shutdown(self) -> None:
        self._stop.set()
        if self.serving.is_set():
            self.httpd.shutdown()
        else:
            self.httpd.server_close()

    def html(self) -> str:
        template = _load_html_template()
        return template.replace("{{CSRF_TOKEN}}", html.escape(self.csrf_token, quote=True))


def _load_html_template() -> str:
    try:
        return resource_files(__package__).joinpath("settings_panel.html").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        path = Path(__file__).with_name("settings_panel.html")
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            raise BridgeError(
                "settings_panel_missing",
                "The local settings panel resource is missing from this installation.",
            ) from None


class _SettingsHandler(BaseHTTPRequestHandler):
    server_version = "GeminiBatonSettings/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Never log request targets: the bootstrap path contains a one-time
        # token, and query strings are otherwise unnecessary.
        return

    @property
    def panel(self) -> SettingsPanel:
        return self.server.panel  # type: ignore[attr-defined]

    def _common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; form-action 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )

    def _send(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str = "application/json; charset=utf-8",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, status: int, value: Mapping[str, Any], *, headers: Mapping[str, str] | None = None) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), headers=headers)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"ok": False, "error": {"code": code, "message": message}})

    def _reject_host(self) -> bool:
        if self.panel.host_ok(self.headers.get("Host")):
            return False
        self._error(HTTPStatus.MISDIRECTED_REQUEST, "forbidden_host", "The request Host is not the loopback settings panel.")
        return True

    def _request_target(self) -> tuple[str, dict[str, list[str]]] | None:
        target = urlsplit(self.path)
        if target.scheme or target.netloc or not target.path.startswith("/"):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_target", "The request target is invalid.")
            return None
        return target.path, parse_qs(target.query, keep_blank_values=True)

    def _authenticated(self) -> bool:
        if self.panel.authenticated(self.headers.get("Cookie")):
            self.panel.touch()
            return True
        self._error(HTTPStatus.UNAUTHORIZED, "authentication_required", "Open the panel from gemini_settings.")
        return False

    def _origin_and_csrf(self) -> bool:
        if not self.panel.origin_ok(self.headers.get("Origin")):
            self._error(HTTPStatus.FORBIDDEN, "forbidden_origin", "The request Origin is not the loopback settings panel.")
            return False
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site not in {"same-origin", "none"}:
            self._error(HTTPStatus.FORBIDDEN, "forbidden_origin", "Cross-site requests are not accepted.")
            return False
        if not self.panel.csrf_ok(self.headers.get(CSRF_HEADER)):
            self._error(HTTPStatus.FORBIDDEN, "invalid_csrf_token", "The request token is missing or invalid.")
            return False
        return True

    def _json_body(self) -> dict[str, Any] | None:
        if self.headers.get("Transfer-Encoding"):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_body", "Chunked request bodies are not accepted.")
            return None
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "invalid_content_type", "Send application/json.")
            return None
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._error(HTTPStatus.LENGTH_REQUIRED, "invalid_body", "A Content-Length is required.")
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_body", "The request body length is invalid.")
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "invalid_body", "The request body is empty or too large.")
            return None
        raw = self.rfile.read(length)
        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", "The request body is not a valid JSON object.")
            return None
        if not isinstance(value, dict):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", "The request body must be a JSON object.")
            return None
        return value

    def do_GET(self) -> None:  # noqa: N802
        if self._reject_host():
            return
        target = self._request_target()
        if target is None:
            return
        path, query = target
        if path == "/auth":
            token_values = query.get("token") or []
            token = token_values[0] if len(token_values) == 1 else None
            if not self.panel.consume_bootstrap(token):
                self._send(HTTPStatus.FORBIDDEN, b"Invalid or already-used settings link.\n",
                           content_type="text/plain; charset=utf-8")
                return
            cookie = (
                f"{COOKIE_NAME}={self.panel.session_token}; Path=/; HttpOnly; "
                f"SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}"
            )
            self._send(
                HTTPStatus.SEE_OTHER,
                b"",
                content_type="text/plain; charset=utf-8",
                headers={"Location": "/", "Set-Cookie": cookie},
            )
            return
        if query:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_target", "Unexpected query parameters.")
            return
        if not self._authenticated():
            return
        if path == "/":
            page = self.panel.html().encode("utf-8")
            self._send(HTTPStatus.OK, page, content_type="text/html; charset=utf-8")
            return
        if path == "/api/settings":
            self._json(HTTPStatus.OK, self.panel.store.public_status(allow_invalid=True))
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "Not found.")

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_host():
            return
        target = self._request_target()
        if target is None:
            return
        path, query = target
        if query:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_target", "Unexpected query parameters.")
            return
        if not self._authenticated():
            return
        if not self._origin_and_csrf():
            return
        body = self._json_body()
        if body is None:
            return
        try:
            if path == "/api/settings":
                self._save_settings(body)
            elif path == "/api/login":
                self._login(body)
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
        except BridgeError as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc.code, exc.message)
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "settings_error", "The settings operation failed.")

    def _save_settings(self, body: dict[str, Any]) -> None:
        allowed = {"routes", "api_key", "clear_api_key"}
        if body.keys() - allowed:
            raise BridgeError("invalid_body", "The settings request contains unsupported fields.")
        if "routes" not in body:
            raise BridgeError("invalid_body", "The settings request must include routes.")
        clear = body.get("clear_api_key", False)
        if not isinstance(clear, bool):
            raise BridgeError("invalid_body", "clear_api_key must be boolean.")
        api_key: Any = _UNSET
        if "api_key" in body:
            if not isinstance(body["api_key"], str):
                raise BridgeError("invalid_body", "api_key must be text.")
            api_key = body["api_key"]
        self.panel.store.save(routes=body["routes"], api_key=api_key, clear_api_key=clear)
        self._json(HTTPStatus.OK, self.panel.store.public_status())

    def _login(self, body: dict[str, Any]) -> None:
        if body.keys() - {"provider"}:
            raise BridgeError("invalid_body", "The login request contains unsupported fields.")
        provider = body.get("provider")
        if provider not in {"gemini", "aistudio"}:
            raise BridgeError("invalid_provider", "Login provider must be gemini or aistudio.")
        result = launch_web_login(provider)
        self._json(HTTPStatus.OK, {"ok": True, "login": result})

    def _method_not_allowed(self) -> None:
        if self._reject_host():
            return
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Only GET and POST are accepted.")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_HEAD(self) -> None:  # noqa: N802
        self._method_not_allowed()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite number")


def _wait_for_panel_port(settings: Settings, pid: int, *, timeout: float = 5.0) -> int | None:
    store = SettingsStore(settings)
    path = store.root / f"panel-{pid}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            port = value.get("port")
            if isinstance(port, int) and 1 <= port <= 65535:
                return port
        except (OSError, ValueError, AttributeError):
            pass
        time.sleep(0.05)
    return None


def launch_settings_panel(settings: Settings) -> dict[str, Any]:
    """Start the panel in a detached child and open its authenticated URL."""
    env = dict(os.environ)
    env["GEMINI_BATON_STATE_DIR"] = str(settings.state_dir)
    argv = [sys.executable, "-m", "gemini_baton.plugin_settings", "--serve"]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            shell=False,
        )
    except OSError:
        raise BridgeError("settings_panel_failed", "The local settings panel could not be started.") from None

    def reap_panel() -> None:
        try:
            process.wait()
        except Exception:
            pass

    threading.Thread(target=reap_panel, name="settings-panel-reaper", daemon=True).start()
    port = _wait_for_panel_port(settings, process.pid)
    if port is None:
        code = process.poll()
        if code is not None:
            raise BridgeError("settings_panel_failed", "The local settings panel exited before it was ready.")
        return {
            "ok": True,
            "opened": False,
            "pid": process.pid,
            "message": "The settings panel is starting; run gemini_settings again if no browser window appears.",
        }
    return {
        "ok": True,
        "opened": True,
        "pid": process.pid,
        "url": f"http://127.0.0.1:{port}/",
        "expires_in_seconds": PANEL_IDLE_TIMEOUT_SECONDS,
    }


def run_panel(
    settings: Settings,
    *,
    port: int = 0,
    idle_timeout: float = PANEL_IDLE_TIMEOUT_SECONDS,
    open_browser: bool = True,
) -> None:
    panel = SettingsPanel(settings, port=port, idle_timeout=idle_timeout)
    try:
        panel.write_runtime()
        if open_browser:
            webbrowser.open(panel.bootstrap_url)
        panel.serve_forever()
    finally:
        panel.cleanup_runtime()
        if not panel.serving.is_set():
            panel.httpd.server_close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gemini-baton-settings")
    parser.add_argument("--serve", action="store_true", help="run the private loopback settings panel")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--idle-timeout", type=float, default=PANEL_IDLE_TIMEOUT_SECONDS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not args.serve:
        parser.error("--serve is required")
    try:
        settings = Settings.from_env()
        run_panel(
            settings,
            port=args.port,
            idle_timeout=args.idle_timeout,
            open_browser=not args.no_browser,
        )
    except BridgeError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
