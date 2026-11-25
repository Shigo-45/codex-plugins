from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gemini_baton import credentials
from gemini_baton.credentials import (
    credential_env_file_info,
    credential_status,
    load_credential,
)
from gemini_baton.errors import BridgeError


def test_env_var_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "GEMINI_API_KEY": "env-gemini",
        "GOOGLE_API_KEY": "env-google",
    }
    assert load_credential(env).source == "env:GEMINI_API_KEY"
    assert load_credential(env).key == "env-gemini"
    assert load_credential({"GOOGLE_API_KEY": "env-google"}).source == (
        "env:GOOGLE_API_KEY"
    )


def test_env_file_only_extracts_supported_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "GEMINI_API_KEY='file-gemini-key'\n"
        "GOOGLE_API_KEY=file-google-key\n"
        "OTHER_SECRET=must-not-be-imported\n"
        "AWS_SECRET_ACCESS_KEY=also-not-imported\n"
        "export UNRELATED=1\n",
        encoding="utf-8",
    )
    env = {"GEMINI_BATON_ENV_FILE": str(env_file)}
    before = dict(os.environ)
    credential = load_credential(env)
    assert credential.source == "env-file:GEMINI_API_KEY"
    assert credential.key == "file-gemini-key"
    # Parsed as data: no variables are exported into the process.
    assert os.environ == before
    assert "must-not-be-imported" not in os.environ.values()
    info = credential_env_file_info(env)
    assert info == {
        "configured": True,
        "readable": True,
        "contains_supported_key": True,
    }
    status = credential_status(env)
    assert status["present"] is True
    assert status["source"] == "env-file:GEMINI_API_KEY"
    assert "file-gemini-key" not in json.dumps(status)


def test_env_vars_beat_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=from-file\n", encoding="utf-8")
    env = {
        "GEMINI_BATON_ENV_FILE": str(env_file),
        "GEMINI_API_KEY": "from-env",
    }
    assert load_credential(env).key == "from-env"


def test_env_file_google_fallback_and_quotes(tmp_path: Path) -> None:
    env_file = tmp_path / "credentials.env"
    env_file.write_text(
        'export GOOGLE_API_KEY="quoted-google-key"\n', encoding="utf-8"
    )
    credential = load_credential({"GEMINI_BATON_ENV_FILE": str(env_file)})
    assert credential.source == "env-file:GOOGLE_API_KEY"
    assert credential.key == "quoted-google-key"


def test_missing_env_file_is_an_error_without_leaking_content(
    tmp_path: Path,
) -> None:
    with pytest.raises(BridgeError) as excinfo:
        load_credential({"GEMINI_BATON_ENV_FILE": str(tmp_path / "absent.env")})
    assert excinfo.value.code == "credential_env_file_error"
    assert "secret" not in excinfo.value.message.lower()


def test_status_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credentials, "_keychain_exists", lambda: False)
    status = credential_status({})
    assert status["present"] is False
    assert status["source"] is None
    assert status["env_file"]["configured"] is False


def test_control_characters_rejected() -> None:
    with pytest.raises(BridgeError):
        load_credential({"GEMINI_API_KEY": "key\ninjection"})


def test_keychain_read_keeps_secret_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        returncode = 0
        stdout = b"keychain-secret\n"
        stderr = b""

    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> Result:
        seen.append(argv)
        return Result()

    monkeypatch.setattr(credentials, "_keychain_available", lambda: True)
    monkeypatch.setattr(credentials.subprocess, "run", fake_run)
    credential = load_credential({})
    assert credential.key == "keychain-secret"
    assert credential.source.startswith("macos-keychain:")
    assert "-w" in seen[0]
    assert "keychain-secret" not in json.dumps(credential.source)


def test_auth_set_uses_security_stdin_without_key_in_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Result:
        returncode = 0

    def fake_run(argv: list[str], **kwargs: object) -> Result:
        calls.append((argv, kwargs))
        return Result()

    secret = "keychain-write-secret"
    monkeypatch.setattr(credentials, "_keychain_available", lambda: True)
    monkeypatch.setattr(credentials.subprocess, "run", fake_run)
    credentials.save_key_to_keychain(secret)
    argv, kwargs = calls[0]
    assert argv == [credentials._SECURITY, "-i"]
    assert secret not in argv
    assert all(secret not in arg for arg in argv)
    assert secret not in str(kwargs.get("input"))
    assert secret.encode("utf-8").hex() in str(kwargs.get("input"))
    assert kwargs.get("capture_output") is True


def test_unspecified_dotenv_in_cwd_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("GEMINI_API_KEY=must-not-be-auto-read\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(credentials, "_keychain_available", lambda: False)
    with pytest.raises(BridgeError) as excinfo:
        load_credential({})
    assert excinfo.value.code == "credentials_missing"
