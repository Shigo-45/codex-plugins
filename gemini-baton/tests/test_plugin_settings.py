from __future__ import annotations

import http.client
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from gemini_baton import plugin_settings
from gemini_baton.config import Settings
from gemini_baton.credentials import Credential
from gemini_baton.errors import BridgeError
from gemini_baton.plugin_settings import (
    COOKIE_NAME,
    DEFAULT_ROUTE_NAMES,
    SettingsPanel,
    SettingsStore,
    install_saved_credential_fallback,
    launch_settings_panel,
    load_credential_with_private,
    merge_writing_status,
)
from gemini_baton.writing import WritingService
from gemini_baton.writing_routes import ROUTES


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("GEMINI_BATON_STATE_DIR", str(tmp_path / "state"))
    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_BATON_ENV_FILE",
        "GEMINI_BATON_AGY",
        "GEMINI_BATON_WEB_CLI",
    ):
        monkeypatch.delenv(name, raising=False)
    return Settings.from_env()


@pytest.fixture
def service(settings: Settings) -> WritingService:
    return WritingService(settings, worker_command=[sys.executable, "-c", "pass"])


@pytest.fixture
def panel(settings: Settings):
    value = SettingsPanel(settings, idle_timeout=30)
    thread = threading.Thread(target=value.serve_forever, daemon=True)
    thread.start()
    assert value.serving.wait(2)
    try:
        yield value
    finally:
        value.shutdown()
        thread.join(timeout=5)


def _request(
    panel: SettingsPanel,
    method: str,
    path: str,
    *,
    body: Any = None,
    headers: dict[str, str] | None = None,
    cookie: str | None = None,
):
    connection = http.client.HTTPConnection("127.0.0.1", panel.port, timeout=5)
    request_headers = {"Host": panel.exact_host}
    if cookie is not None:
        request_headers["Cookie"] = f"{COOKIE_NAME}={cookie}"
    if headers:
        request_headers.update(headers)
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    payload = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    return response.status, response_headers, payload


def _auth_cookie(panel: SettingsPanel) -> str:
    target = urlsplit(panel.bootstrap_url)
    status, headers, _ = _request(panel, "GET", target.path + "?" + target.query)
    assert status == 303
    set_cookie = headers["Set-Cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    return set_cookie.split(";", 1)[0].split("=", 1)[1]


def _post_headers(panel: SettingsPanel, **overrides: str) -> dict[str, str]:
    headers = {
        "Origin": panel.exact_origin,
        "X-CSRF-Token": panel.csrf_token,
        "Content-Type": "application/json",
    }
    headers.update(overrides)
    return headers


def test_default_settings_are_the_existing_sixteen_routes(settings: Settings) -> None:
    store = SettingsStore(settings)
    status = store.public_status()
    assert status["source"] == "defaults"
    assert status["routes"] == list(DEFAULT_ROUTE_NAMES)
    assert len(status["routes"]) == 16
    assert status["api_key"] == {"configured": False, "source": None, "saved": False}
    assert [row["name"] for row in status["all_routes"]] == list(DEFAULT_ROUTE_NAMES)


def test_saved_routes_preserve_order_canonicalize_aliases_and_keep_key_private(settings: Settings) -> None:
    store = SettingsStore(settings)
    loaded = store.save(
        routes=["web:4 Argon", "api:gemini-4-argon", "cli:4 Carbon"],
        api_key="  private-key-value  ",
    )
    assert loaded.routes == ("web:4 Argon", "api:4 Argon", "cli:4 Carbon")
    assert loaded.source == "settings"
    assert loaded.routes_source == "settings"
    assert loaded.api_key == "private-key-value"
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    public = store.public_status()
    assert "private-key-value" not in json.dumps(public)
    assert public["api_key"] == {"configured": True, "source": "settings:private", "saved": True}


def test_saving_routes_without_a_key_preserves_it_and_clear_removes_it(settings: Settings) -> None:
    store = SettingsStore(settings)
    store.save(routes=["api:4 Argon"], api_key="keep-this-key")
    store.save(routes=["web:4 Argon"])
    assert store.saved_api_key() == "keep-this-key"
    store.save(routes=["web:4 Argon"], clear_api_key=True)
    assert store.saved_api_key() is None

    store.path.write_text(
        json.dumps({"version": 1, "routes": ["not-a-route"], "api_key": "repair-keeps-key"}),
        encoding="utf-8",
    )
    store.save(routes=["web:4 Argon"])
    assert store.saved_api_key() == "repair-keeps-key"


@pytest.mark.parametrize("routes", [
    [],
    ["web:4 Argon", "web:4 Argon"],
    ["not-a-route"],
    ["cli:4 Hydrogen"],
    ["web:4 Argon", 3],
])
def test_route_validation_rejects_empty_duplicates_unknown_and_hydrogen(
    settings: Settings, routes: list[object]
) -> None:
    store = SettingsStore(settings)
    with pytest.raises(BridgeError) as error:
        store.save(routes=routes, api_key="must-not-leak")
    assert error.value.code == "invalid_settings"
    assert "must-not-leak" not in json.dumps(error.value.as_dict())
    assert not store.path.exists()


def test_invalid_settings_file_fails_closed_and_does_not_echo_contents(settings: Settings) -> None:
    store = SettingsStore(settings)
    store.path.write_text('{"api_key": "secret-in-corrupt-file",', encoding="utf-8")
    with pytest.raises(BridgeError) as error:
        store.public_status()
    assert error.value.code == "invalid_settings"
    assert "secret-in-corrupt-file" not in error.value.message
    public = store.public_status(allow_invalid=True)
    assert public["routes"] == list(DEFAULT_ROUTE_NAMES)
    assert public["settings_error"]["code"] == "invalid_settings"
    assert "secret-in-corrupt-file" not in json.dumps(public)


def test_settings_symlink_is_rejected(settings: Settings, tmp_path: Path) -> None:
    store = SettingsStore(settings)
    target = tmp_path / "outside.json"
    target.write_text('{"routes": ["api:4 Argon"]}', encoding="utf-8")
    store.path.symlink_to(target)
    with pytest.raises(BridgeError) as error:
        store.load()
    assert error.value.code == "settings_error"


def test_job_snapshots_effective_routes_and_reports_route_policy(
    service: WritingService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(service.settings)
    store.save(routes=["api:4 Argon", "web:4 Carbon"])
    launch = Mock()
    launch.return_value.pid = 9876
    monkeypatch.setattr("gemini_baton.writing.subprocess.Popen", launch)
    job = service.submit("write", cwd=str(tmp_path), wait_seconds=0)
    job_id = job["job_id"]
    assert service.read(job_id)["routes"] == [
        ["api", "4 Argon", "high"], ["web", "4 Carbon", "high"]]
    assert service.read(job_id)["route_policy_source"] == "settings"
    result = service.result(job_id)
    assert result["route_policy"] == [
        {"channel": "api", "model": "4 Argon", "effort": "high"},
        {"channel": "web", "model": "4 Carbon", "effort": "high"},
    ]
    assert result["route_policy_source"] == "settings"

    store.save(routes=["cli:3.8 Flash"])
    assert service.read(job_id)["routes"] == [
        ["api", "4 Argon", "high"], ["web", "4 Carbon", "high"]]
    new_job = service.submit("different task", cwd=str(tmp_path), wait_seconds=0)
    assert service.read(new_job["job_id"])["routes"] == [["cli", "3.8 Flash", "high"]]


def test_explicit_route_pin_ignores_settings_and_hydrogen_remains_pinnable(
    service: WritingService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(service.settings)
    store.save(routes=["web:4 Argon"])
    launch = Mock()
    launch.return_value.pid = 9877
    monkeypatch.setattr("gemini_baton.writing.subprocess.Popen", launch)
    pinned = service.submit("write", cwd=str(tmp_path), route="api:gemini-4-argon", wait_seconds=0)
    assert service.read(pinned["job_id"])["routes"] == [["api", "4 Argon", "high"]]
    assert service.read(pinned["job_id"])["route_policy_source"] == "explicit"
    hydrogen = service.submit("write again", cwd=str(tmp_path), route="cli:4 Hydrogen", wait_seconds=0)
    assert service.read(hydrogen["job_id"])["routes"] == [["cli", "4 Hydrogen", "high"]]


def test_conversation_routing_uses_only_enabled_browser_routes(
    service: WritingService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(service.settings)
    store.save(routes=["cli:4 Argon", "api:4 Argon", "aistudio:4 Argon", "web:4 Carbon"])
    launch = Mock()
    launch.return_value.pid = 9878
    monkeypatch.setattr("gemini_baton.writing.subprocess.Popen", launch)
    job = service.submit("continue", cwd=str(tmp_path), conversation_id="conv_followup123", wait_seconds=0)
    assert service.read(job["job_id"])["routes"] == [
        ["aistudio", "4 Argon", "high"], ["web", "4 Carbon", "high"]]


def test_conversation_without_enabled_browser_routes_fails_without_submission(
    service: WritingService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(service.settings)
    store.save(routes=["cli:4 Argon", "api:4 Argon"])
    launch = Mock()
    launch.return_value.pid = 9879
    monkeypatch.setattr("gemini_baton.writing.subprocess.Popen", launch)
    result = service.submit("continue", cwd=str(tmp_path), conversation_id="conv_followup123", wait_seconds=0)
    assert result["done"] is True
    assert result["ok"] is False
    assert result["error"]["code"] == "no_eligible_route"
    assert launch.call_count == 0


def test_saved_key_is_used_after_environment_sources_and_before_keychain(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(settings)
    store.save(routes=["api:4 Argon"], api_key="saved-key")
    state_env = {"GEMINI_BATON_STATE_DIR": str(settings.state_dir)}
    calls: list[dict[str, str]] = []

    def original(env=None):
        calls.append(dict(env or {}))
        if env and env.get("GEMINI_API_KEY"):
            return Credential(source="env:GEMINI_API_KEY", key=env["GEMINI_API_KEY"])
        if env and env.get("GEMINI_BATON_ENV_FILE"):
            return Credential(source="env-file:GEMINI_API_KEY", key="file-key")
        return Credential(source="macos-keychain", key="keychain-key")

    credential = load_credential_with_private(env=state_env, original=original)
    assert credential == Credential(source="settings:private", key="saved-key")
    assert calls == []

    env_key = {**state_env, "GEMINI_API_KEY": "environment-key"}
    credential = load_credential_with_private(env=env_key, original=original)
    assert credential == Credential(source="env:GEMINI_API_KEY", key="environment-key")
    assert calls

    env_file = {**state_env, "GEMINI_BATON_ENV_FILE": str(settings.state_dir / "explicit.env")}
    calls.clear()

    def file_loader(env=None):
        calls.append(dict(env or {}))
        return Credential(source="env-file:GEMINI_API_KEY", key="file-key")

    credential = load_credential_with_private(env=env_file, original=file_loader)
    assert credential == Credential(source="env-file:GEMINI_API_KEY", key="file-key")
    assert calls

    store.save(routes=["api:4 Argon"], clear_api_key=True)
    credential = load_credential_with_private(env=state_env, original=original)
    assert credential == Credential(source="macos-keychain", key="keychain-key")


def test_install_saved_credential_fallback_patches_only_the_worker_loader(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(settings)
    store.save(routes=["api:4 Argon"], api_key="worker-key")
    from gemini_baton import writing_routes

    def original(env=None):
        raise AssertionError("the keychain loader must not run")

    monkeypatch.setattr(writing_routes, "load_credential", original)
    install_saved_credential_fallback()
    credential = writing_routes.load_credential({"GEMINI_BATON_STATE_DIR": str(settings.state_dir)})
    assert credential == Credential(source="settings:private", key="worker-key")


def test_merge_writing_status_uses_effective_routes_without_key(settings: Settings) -> None:
    store = SettingsStore(settings)
    store.save(routes=["api:4 Argon"], api_key="merge-secret")
    payload = {
        "ok": True,
        "writing": {"routes": ["cli:4 Argon"]},
        "credentials": {"present": False, "source": None},
    }
    merged = merge_writing_status(payload, settings)
    assert merged["writing"]["routes"] == ["api:4 Argon"]
    assert merged["writing"]["routes_source"] == "settings"
    assert merged["writing"]["settings"]["api_key"]["saved"] is True
    assert "merge-secret" not in json.dumps(merged)


def test_panel_requires_bootstrap_session_exact_host_and_single_use_token(panel: SettingsPanel) -> None:
    status, _, _ = _request(panel, "GET", "/")
    assert status == 401
    status, _, _ = _request(
        panel, "GET", "/", headers={"Host": f"localhost:{panel.port}"})
    assert status == 421
    status, _, _ = _request(panel, "GET", "/auth?token=wrong")
    assert status == 403
    cookie = _auth_cookie(panel)
    status, _, body = _request(panel, "GET", "/", cookie=cookie)
    assert status == 200
    assert b"Writing routes" in body
    assert panel.csrf_token.encode() in body
    status, _, _ = _request(panel, "GET", f"/auth?token={panel.bootstrap_token}")
    assert status == 403


def test_panel_rejects_cross_origin_csrf_content_type_and_bad_bodies(
    panel: SettingsPanel, settings: Settings
) -> None:
    cookie = _auth_cookie(panel)
    valid = {"routes": ["api:4 Argon"]}

    status, _, _ = _request(
        panel, "POST", "/api/settings", body=valid,
        headers={"X-CSRF-Token": panel.csrf_token, "Content-Type": "application/json"},
        cookie=cookie,
    )
    assert status == 403

    status, _, _ = _request(
        panel, "POST", "/api/settings", body=valid,
        headers=_post_headers(panel, Origin="https://evil.example"),
        cookie=cookie,
    )
    assert status == 403

    status, _, _ = _request(
        panel, "POST", "/api/settings", body=valid,
        headers=_post_headers(panel, **{"X-CSRF-Token": "wrong"}),
        cookie=cookie,
    )
    assert status == 403

    status, _, _ = _request(
        panel, "POST", "/api/settings", body=valid,
        headers=_post_headers(panel, **{"Sec-Fetch-Site": "cross-site"}),
        cookie=cookie,
    )
    assert status == 403

    status, _, _ = _request(
        panel, "POST", "/api/settings", body=b'{"routes": ["api:4 Argon"]}',
        headers={
            "Origin": panel.exact_origin,
            "X-CSRF-Token": panel.csrf_token,
            "Content-Type": "text/plain",
        },
        cookie=cookie,
    )
    assert status == 415

    status, _, _ = _request(
        panel, "POST", "/api/settings",
        body=b'{"routes": ["api:4 Argon"], "routes": ["web:4 Argon"]}',
        headers=_post_headers(panel), cookie=cookie,
    )
    assert status == 400

    status, _, _ = _request(
        panel, "POST", "/api/settings",
        body={"routes": ["api:4 Argon"], "unexpected": True},
        headers=_post_headers(panel), cookie=cookie,
    )
    assert status == 400

    status, _, body = _request(
        panel, "POST", "/api/settings",
        body={"routes": [], "api_key": "bad-key"},
        headers=_post_headers(panel), cookie=cookie,
    )
    assert status == 400
    assert b"bad-key" not in body
    assert b"bad-key" not in _request(
        panel, "GET", "/api/settings", cookie=cookie)[2]

    status, _, _ = _request(panel, "OPTIONS", "/api/settings", cookie=cookie)
    assert status == 405
    assert not (settings.state_dir / "writing-settings" / "settings.json").exists()


def test_panel_saves_private_settings_and_never_echoes_the_key(
    panel: SettingsPanel, settings: Settings
) -> None:
    cookie = _auth_cookie(panel)
    status, _, body = _request(
        panel, "POST", "/api/settings",
        body={"routes": ["web:4 Argon", "api:4 Argon"], "api_key": "panel-secret-key"},
        headers=_post_headers(panel), cookie=cookie,
    )
    assert status == 200
    assert b"panel-secret-key" not in body
    loaded = SettingsStore(settings).load()
    assert loaded.routes == ("web:4 Argon", "api:4 Argon")
    assert loaded.api_key == "panel-secret-key"

    status, _, body = _request(panel, "GET", "/api/settings", cookie=cookie)
    assert status == 200
    public = json.loads(body)
    assert public["routes"] == ["web:4 Argon", "api:4 Argon"]
    assert "panel-secret-key" not in json.dumps(public)

    status, _, body = _request(panel, "GET", "/", cookie=cookie)
    assert status == 200
    assert b"panel-secret-key" not in body

    status, _, body = _request(
        panel, "POST", "/api/settings",
        body={"routes": ["web:4 Argon"], "clear_api_key": True},
        headers=_post_headers(panel), cookie=cookie,
    )
    assert status == 200
    assert SettingsStore(settings).saved_api_key() is None


def test_panel_login_delegates_web_cli_argv_without_shell_or_secrets(
    panel: SettingsPanel, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie = _auth_cookie(panel)
    process = Mock()
    process.pid = 4242
    process.wait.side_effect = subprocess.TimeoutExpired(cmd="login", timeout=0.1)
    popen = Mock(return_value=process)
    monkeypatch.setattr(plugin_settings.subprocess, "Popen", popen)
    monkeypatch.setattr(
        plugin_settings, "_web_cli_command",
        lambda: ["/usr/local/bin/node", "/tmp/gemini-web-cli.mjs"])
    monkeypatch.setenv("GEMINI_API_KEY", "login-must-not-see-this")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/secret.json")

    status, _, body = _request(
        panel, "POST", "/api/login", body={"provider": "aistudio"},
        headers=_post_headers(panel), cookie=cookie,
    )
    assert status == 200
    assert b"login-must-not-see-this" not in body
    argv = popen.call_args.args[0]
    assert argv == ["/usr/local/bin/node", "/tmp/gemini-web-cli.mjs", "login", "--provider", "aistudio"]
    kwargs = popen.call_args.kwargs
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert "GEMINI_API_KEY" not in kwargs["env"]
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in kwargs["env"]

    status, _, _ = _request(
        panel, "POST", "/api/login", body={"provider": "other"},
        headers=_post_headers(panel), cookie=cookie,
    )
    assert status == 400


def test_panel_antigravity_uses_verified_acp_login(panel: SettingsPanel, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        plugin_settings.shutil, "which",
        lambda name: "/usr/local/bin/agy" if name == "agy" else None)
    cookie = _auth_cookie(panel)
    status, _, body = _request(panel, "GET", "/api/settings", cookie=cookie)
    assert status == 200
    payload = json.loads(body)
    assert payload["antigravity"]["available"] is True
    assert payload["antigravity"]["command"] == "agy acp --login"
    assert "agy login" not in payload["antigravity"]["command"]
    assert "Google sign-in" in payload["antigravity"]["instructions"]


def test_panel_bind_is_loopback_only(settings: Settings) -> None:
    with pytest.raises(BridgeError):
        SettingsPanel(settings, host="0.0.0.0")


def test_launch_settings_panel_is_detached_and_returns_no_token(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = Mock()
    process.pid = 4321
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(plugin_settings.subprocess, "Popen", popen)
    monkeypatch.setattr(plugin_settings, "_wait_for_panel_port", lambda settings, pid: 45678)
    result = launch_settings_panel(settings)
    assert result["ok"] is True
    assert result["opened"] is True
    assert result["url"] == "http://127.0.0.1:45678/"
    assert "token" not in json.dumps(result).lower()
    kwargs = popen.call_args.kwargs
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["env"]["GEMINI_BATON_STATE_DIR"] == str(settings.state_dir)


def test_settings_panel_resource_is_packaged_with_csrf_placeholder() -> None:
    from importlib.resources import files

    text = files("gemini_baton").joinpath("settings_panel.html").read_text(encoding="utf-8")
    assert "{{CSRF_TOKEN}}" in text
    assert "api-key" in text
    assert "gemini_settings" in text


def test_mcp_settings_status_and_status_merge(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(settings)
    store.save(routes=["api:4 Argon"], api_key="mcp-private-key")
    from gemini_baton import mcp_server

    class FakeService:
        def __init__(self) -> None:
            self.settings = settings

        def status(self) -> dict:
            return {
                "ok": True,
                "writing": {"routes": ["cli:4 Argon"]},
                "credentials": {"present": False, "source": None},
            }

    monkeypatch.setattr(mcp_server, "_service", FakeService())
    status = mcp_server.gemini_settings("status")
    assert status["ok"] is True
    assert status["routes"] == ["api:4 Argon"]
    assert "mcp-private-key" not in json.dumps(status)
    merged = mcp_server.gemini_status()
    assert merged["writing"]["routes"] == ["api:4 Argon"]
    assert merged["writing"]["routes_source"] == "settings"
    assert "mcp-private-key" not in json.dumps(merged)

    monkeypatch.setattr(
        mcp_server, "launch_settings_panel",
        lambda value: {"ok": True, "opened": True, "pid": 11, "url": "http://127.0.0.1:9/"})
    opened = mcp_server.gemini_settings("open")
    assert opened == {
        "action": "open", "ok": True, "opened": True, "pid": 11,
        "url": "http://127.0.0.1:9/",
    }
    assert mcp_server.gemini_settings("bogus")["ok"] is False


def test_cli_settings_and_status_use_saved_routes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = SettingsStore(settings)
    store.save(routes=["web:4 Argon"], api_key="cli-private-key")
    from gemini_baton.cli import main

    assert main(["settings", "--status"]) == 0
    settings_output = json.loads(capsys.readouterr().out)
    assert settings_output["routes"] == ["web:4 Argon"]
    assert "api_key" in settings_output
    assert "cli-private-key" not in json.dumps(settings_output)

    assert main(["status"]) == 0
    status_output = json.loads(capsys.readouterr().out)
    assert status_output["writing"]["routes"] == ["web:4 Argon"]
    assert status_output["writing"]["routes_source"] == "settings"
    assert "cli-private-key" not in json.dumps(status_output)


def test_default_route_triples_match_writing_routes() -> None:
    assert DEFAULT_ROUTE_NAMES == tuple(f"{channel}:{model}" for channel, model, _effort in ROUTES)
