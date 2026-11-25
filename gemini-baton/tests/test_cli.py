from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gemini_baton import cli


def clean_env(state_dir: Path) -> dict[str, str]:
    blocked = {
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_BATON_ENV_FILE",
        "GEMINI_BATON_STATE_DIR",
        "GEMINI_BATON_MODEL",
    }
    env = {key: value for key, value in os.environ.items() if key not in blocked}
    env["GEMINI_BATON_STATE_DIR"] = str(state_dir)
    return env


def test_cli_help_and_safe_status_without_network(tmp_path: Path) -> None:
    help_result = subprocess.run(
        [sys.executable, "-m", "gemini_baton.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert help_result.returncode == 0
    assert "gemini-baton" in help_result.stdout
    assert "mcp" in help_result.stdout

    result = subprocess.run(
        [sys.executable, "-m", "gemini_baton.cli", "status"],
        capture_output=True,
        text=True,
        timeout=20,
        env=clean_env(tmp_path / "state"),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["network_calls"] == 0
    assert payload["paid_model_calls"] == 0
    assert "credentials" in payload
    assert "AIza" not in result.stdout
    assert "GEMINI_API_KEY=" not in result.stdout


def test_auth_set_uses_getpass_and_never_prints_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "test-key-entered-interactively"
    saved: list[str] = []

    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: secret)
    monkeypatch.setattr(cli, "save_key_to_keychain", lambda value: saved.append(value))
    assert cli.main(["auth-set"]) == 0
    assert saved == [secret]
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert "Keychain" in captured.out
