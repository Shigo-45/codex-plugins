#!/usr/bin/env python3
"""Inspect checkout, skill installs, and MCP launch paths without model calls."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME_DIR = Path.home()


def run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=20, check=False
    )


def main() -> int:
    checks: list[dict[str, object]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    root_git = run("git", "rev-parse", "--show-toplevel")
    record("root_git", root_git.returncode == 0 and root_git.stdout.strip() == str(ROOT), str(ROOT))
    web = ROOT / "gemini-web-bridge"
    web_git = run("git", "rev-parse", "--show-toplevel", cwd=web)
    record("web_git", web_git.returncode == 0 and web_git.stdout.strip() == str(web), str(web))
    ignored = run("git", "check-ignore", "gemini-web-bridge/")
    owned_web = run("git", "ls-files", "--", "gemini-web-bridge")
    record("web_git_boundary", ignored.returncode == 0 and owned_web.returncode == 0 and not owned_web.stdout.strip(), "Web bridge is owned by its independent repository")
    plugin = web / "plugins/gemini-web-bridge"
    versions = [json.loads(path.read_text())["version"] for path in (
        web / "package.json", plugin / "package.json",
        plugin / "package-lock.json", plugin / ".codex-plugin/plugin.json",
    )]
    record("web_package_versions", len(set(versions)) == 1, ", ".join(versions))

    for name, plugin_name in (("write-to-me", "gemini-baton"), ("workhorse", "dsh-baton")):
        source = ROOT / "skills" / name
        manifest = json.loads((ROOT / "plugins" / plugin_name / "plugin.json").read_text())
        installed = HOME_DIR / ".codex/plugins/cache/shigo-plugins" / plugin_name / manifest["version"] / "skills" / name
        duplicate = HOME_DIR / ".codex/skills" / name
        record(f"skill:{name}", installed.is_dir() and (source / "SKILL.md").is_file() and not duplicate.is_symlink() and not duplicate.exists(), str(installed))

    config_path = HOME_DIR / ".codex/config.toml"
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    legacy = config.get("mcp_servers", {})
    record("plugin_mcp_ownership", not set(legacy).intersection({"gemini-baton", "gemini-web-backup", "dsh-baton", "image-analysis"}), "Target transports are owned by installed plugins")
    listed = run("codex", "mcp", "list", "--json")
    servers = {item["name"]: item for item in json.loads(listed.stdout)}
    for name in ("gemini-baton", "dsh-baton", "image-analysis"):
        server = servers.get(name, {})
        transport = server.get("transport", {})
        cache = HOME_DIR / ".codex/plugins/cache/shigo-plugins" / name
        owned = str(cache) in (transport.get("cwd") or "") if name != "image-analysis" else transport.get("url") == "http://127.0.0.1:3001/mcp"
        record(f"mcp:{name}", bool(owned and server.get("enabled") and config.get("plugins", {}).get(f"{name}@shigo-plugins", {}).get("enabled")), "Enabled marketplace MCP registration")

    api = ROOT / "gemini-baton"
    python = api / ".venv/bin/python"
    imported = run(str(python), "-c", "import gemini_baton; print(gemini_baton.__file__)")
    record("api_editable_install", imported.returncode == 0 and Path(imported.stdout.strip()).resolve() == api / "src/gemini_baton/__init__.py", "Python imports the current checkout")
    cli = run(str(api / ".venv/bin/gemini-baton"), "--version")
    record("api_entrypoint", cli.returncode == 0, cli.stdout.strip())
    writing_cli = run(str(api / ".venv/bin/gemini-baton"), "write", "--help")
    record("writing_entrypoint", writing_cli.returncode == 0 and "--wait-seconds" in writing_cli.stdout, "Unified writing CLI is installed")
    pytest = run(str(api / ".venv/bin/pytest"), "--version")
    record("pytest_entrypoint", pytest.returncode == 0, pytest.stdout.strip())
    activation = (api / ".venv/bin/activate").read_text()
    record("venv_activation", str(api / ".venv") in activation, "Activation points to the current checkout")

    obsolete = tuple(str(ROOT.parent / name) for name in ("gemini-baton", "gemini-web-bridge"))
    for path in (ROOT / "skills/write-to-me/SKILL.md", web / "plugins/gemini-web-bridge/skills/use-gemini-web/SKILL.md", api / "README.md"):
        record(f"paths:{path.relative_to(ROOT)}", not any(old in path.read_text() for old in obsolete), "Maintained instructions use current paths")
    for name in ("gemini-baton", "gemini-web-bridge"):
        link = ROOT.parent / name
        valid = not link.exists() and not link.is_symlink() or link.is_symlink() and link.resolve() == ROOT / name
        record(f"compatibility:{name}", valid, f"Optional legacy alias: {link}")

    ok = all(check["ok"] for check in checks)
    print(json.dumps({"ok": ok, "root": str(ROOT), "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
