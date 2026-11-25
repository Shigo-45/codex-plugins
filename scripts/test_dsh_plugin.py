#!/usr/bin/env python3
"""Zero-model-request checks for the portable DSH Baton Codex plugin.

Every test stays local: manifests, vendored source closure, MCP initialize and
tools/list over stdio, and CLI status against temporary state directories. A fake
DSH launcher records any accidental invocation, and no test reads real Harness
sessions, credentials, or configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "dsh-baton"
APP = PLUGIN / "app"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
MIN_NODE = (22, 15)
VISIBLE_TOOLS = {
    "dsh_list_workspaces",
    "dsh_list_sessions",
    "dsh_list_models",
    "dsh_read_session",
    "dsh_send",
    "dsh_status",
    "dsh_wait",
    "dsh_answer",
    "dsh_cancel",
    "dsh_call",
}
PRIVATE_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:[\\/]Users[\\/])")
RELATIVE_IMPORT = re.compile(r"""(?:from\s+|import\s*\()\s*['"](\.{1,2}/[^'"]+)['"]""")


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def node_binary() -> str:
    found = shutil.which("node")
    if not found:
        raise unittest.SkipTest("node is not installed")
    version = subprocess.run(
        [found, "--version"], capture_output=True, text=True, timeout=15, check=True
    ).stdout.strip()
    major, minor = (int(part) for part in version.lstrip("v").split(".")[:2])
    if (major, minor) < MIN_NODE:
        raise unittest.SkipTest(f"node {version} is older than {MIN_NODE[0]}.{MIN_NODE[1]}")
    return found


def isolated_env(root: Path, fake_bin: Path) -> dict[str, str]:
    baton = root / "baton"
    env = {
        **os.environ,
        "HOME": str(root / "home"),
        "DSH_HOME": str(root / "dsh"),
        "DSH_BATON_HOME": str(baton),
        "DSH_BATON_SOCKET": str(baton / "host.sock"),
        "DSH_BATON_BACKEND": "headless",
        "DSH_BIN": str(fake_bin / "dsh"),
        "DSH_TEST_MARKER": str(root / "dsh-invoked"),
        "CODEX_HOME": str(root / "codex"),
        "CLAUDE_CONFIG_DIR": str(root / "claude"),
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
    }
    for directory in (root / "home", root / "dsh", baton, root / "codex", fake_bin):
        directory.mkdir(parents=True, exist_ok=True)
    return env


def write_fake_dsh(fake_bin: Path) -> None:
    fake_bin.mkdir(parents=True, exist_ok=True)
    path = fake_bin / "dsh"
    path.write_text(
        "#!/bin/sh\n"
        'printf invoked > "$DSH_TEST_MARKER"\n'
        "exit 99\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def write_run(baton_home: Path, run_id: str = "run-20260101000000-test") -> Path:
    run = baton_home / "runs" / run_id
    run.mkdir(parents=True)
    (run / "meta.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "session_id": "session-test",
                "cwd": str(run.parent),
                "started_at": "2026-01-01T00:00:00.000Z",
                "message_preview": "zero-task status fixture",
            }
        ),
        encoding="utf-8",
    )
    (run / "exit.json").write_text(
        json.dumps(
            {
                "ended_at": "2026-01-01T00:00:01.000Z",
                "exit_code": 0,
                "signal": None,
                "state": "completed",
            }
        ),
        encoding="utf-8",
    )
    (run / "events.ndjson").write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "sessionId": "session-test"}),
                json.dumps({"type": "status", "phase": "turn_end", "reason": "completed"}),
                json.dumps({"type": "final", "text": "zero-task status reply"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return run


def run_cli(node: str, app: Path, args: list[str], env: dict[str, str], timeout: int = 30):
    return subprocess.run(
        [node, str(app / "src" / "cli.mjs"), *args],
        cwd=str(app),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class ManifestTests(unittest.TestCase):
    def test_portable_plugin_manifest(self) -> None:
        manifest = load_json(PLUGIN / "plugin.json")
        self.assertEqual(manifest["$schema"], PLUGIN_SCHEMA)
        self.assertEqual(manifest["name"], "dsh-baton")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(manifest["description"], load_json(APP / "package.json")["description"])
        self.assertNotIn("skills", manifest)
        self.assertNotIn("mcpServers", manifest)
        self.assertNotIn("interface", manifest)
        openai = manifest["extensions"]["com.openai"]
        self.assertEqual(openai["onboardingSkill"], "./skills/dsh-baton-setup/SKILL.md")
        self.assertTrue((PLUGIN / openai["onboardingSkill"]).is_file())
        self.assertEqual(openai["interface"]["displayName"], "DSH Baton")
        self.assertEqual(openai["interface"]["category"], "Productivity")
        self.assertIsInstance(openai["interface"]["capabilities"], list)

    def test_compatibility_manifest_matches_identity(self) -> None:
        portable = load_json(PLUGIN / "plugin.json")
        overlay = load_json(PLUGIN / ".codex-plugin/plugin.json")
        for key in ("name", "version", "description", "license"):
            self.assertEqual(overlay[key], portable[key], key)
        self.assertEqual(overlay["skills"], "./skills/")
        self.assertEqual(overlay["mcpServers"], "./.mcp.json")
        self.assertEqual(overlay["onboardingSkill"], portable["extensions"]["com.openai"]["onboardingSkill"])
        self.assertTrue((PLUGIN / ".mcp.json").is_file())
        self.assertTrue((PLUGIN / "skills").is_dir())
        self.assertEqual(overlay["interface"]["displayName"], portable["extensions"]["com.openai"]["interface"]["displayName"])
        self.assertEqual(overlay["interface"]["category"], "Productivity")

    def test_portable_mcp_manifest_is_rooted_at_plugin_root(self) -> None:
        doc = load_json(PLUGIN / "mcp.json")
        self.assertEqual(doc["$schema"], MCP_SCHEMA)
        self.assertEqual(set(doc), {"$schema", "mcpServers"})
        server = doc["mcpServers"]["dsh-baton"]
        self.assertEqual(set(server), {"type", "command", "args", "cwd"})
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "node")
        self.assertEqual(server["cwd"], "${PLUGIN_ROOT}")
        self.assertEqual(server["args"], ["${PLUGIN_ROOT}/scripts/launch-mcp.mjs"])
        self.assertNotIn("..", server["args"][0])
        target = Path(server["args"][0].replace("${PLUGIN_ROOT}", str(PLUGIN)))
        self.assertTrue(target.is_file(), target)

    def test_legacy_mcp_manifest_matches_portable_entry(self) -> None:
        portable = load_json(PLUGIN / "mcp.json")["mcpServers"]["dsh-baton"]
        legacy = load_json(PLUGIN / ".mcp.json")["mcpServers"]["dsh-baton"]
        self.assertNotIn("$schema", load_json(PLUGIN / ".mcp.json"))
        for key in ("type", "command", "args", "cwd"):
            self.assertEqual(legacy[key], portable[key], key)
        self.assertGreaterEqual(legacy["startup_timeout_sec"], 30)
        self.assertGreaterEqual(legacy["tool_timeout_sec"], 300)

    def test_no_absolute_private_paths(self) -> None:
        offenders = []
        for path in PLUGIN.rglob("*"):
            if not path.is_file() or path.suffix in {".png", ".jpg", ".webp", ".zip"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in PRIVATE_PATH.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(PLUGIN)}:{line}:{match.group(0)}")
        self.assertEqual(offenders, [])

    def test_no_hook_or_auto_task_registration(self) -> None:
        for manifest_path in ("plugin.json", ".codex-plugin/plugin.json", ".mcp.json", "mcp.json"):
            text = (PLUGIN / manifest_path).read_text(encoding="utf-8")
            self.assertNotIn('"hooks"', text, manifest_path)
        self.assertFalse((PLUGIN / "hooks").exists())
        launcher = (PLUGIN / "scripts" / "launch-mcp.mjs").read_text(encoding="utf-8")
        self.assertNotIn("autoRegister", launcher)
        self.assertNotIn("setup(", launcher)

    def test_no_runtime_state_or_dependency_directories(self) -> None:
        forbidden_dirs = {"node_modules", ".git", "runs", "evidence", "profiles", "__pycache__"}
        forbidden_names = {
            ".env",
            "config.toml",
            "credentials.json",
            "settings.json",
            "pending.json",
            "alive",
            "session.v4.jsonl.zstd",
        }
        for path in PLUGIN.rglob("*"):
            self.assertNotIn(path.name, forbidden_dirs, path)
            self.assertNotIn(path.name, forbidden_names, path)
            self.assertFalse(path.name.endswith(".sock"), path)

    def test_upstream_provenance_and_license(self) -> None:
        upstream = load_json(PLUGIN / "UPSTREAM.json")
        self.assertEqual(upstream["version"], "0.6.1")
        self.assertEqual(upstream["license"], "MIT")
        self.assertEqual(upstream["upstream"], "https://github.com/JialaoLiu/dsh-baton")
        self.assertIn("JialaoLiu", upstream["copyright"])
        license_text = (PLUGIN / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", license_text)
        self.assertIn("Copyright (c) 2026 JialaoLiu", license_text)
        self.assertEqual((PLUGIN / "LICENSE").read_bytes(), (APP / "LICENSE").read_bytes())
        self.assertEqual((APP / "LICENSE").read_bytes(), (APP / "host" / "LICENSE").read_bytes())

        actual = {
            path.relative_to(PLUGIN).as_posix()
            for path in APP.rglob("*")
            if path.is_file()
        }
        self.assertEqual(set(upstream["sha256"]), actual)
        mismatched = [
            rel
            for rel, digest in upstream["sha256"].items()
            if sha256(PLUGIN / rel) != digest
        ]
        self.assertEqual(mismatched, [])

    def test_vendored_package_metadata(self) -> None:
        package = load_json(APP / "package.json")
        self.assertEqual(package["name"], "dsh-baton-mcp")
        self.assertEqual(package["version"], "0.6.1")
        self.assertEqual(package["license"], "MIT")
        self.assertEqual(package["engines"]["node"], ">=22.15")
        for relative in package["bin"].values():
            self.assertTrue((APP / relative).is_file(), relative)
        self.assertTrue((APP / "patches" / "account.patch.yml").is_file())
        self.assertTrue((APP / "host" / "lib" / "socket.js").is_file())


class SourceClosureTests(unittest.TestCase):
    def test_runtime_import_closure(self) -> None:
        missing = []
        bare = []
        for path in sorted((APP / "src").glob("*.mjs")):
            text = path.read_text(encoding="utf-8")
            for specifier in RELATIVE_IMPORT.findall(text):
                target = (path.parent / specifier).resolve()
                if not target.is_file():
                    missing.append(f"{path.name}: {specifier}")
            for match in re.finditer(r"from\s+['\"]([^'\"]+)['\"]", text):
                specifier = match.group(1)
                if not specifier.startswith((".", "node:")):
                    bare.append(f"{path.name}: {specifier}")
        self.assertEqual(missing, [])
        self.assertEqual(bare, [])

    def test_launcher_checks_runtime_floor_before_loading_server(self) -> None:
        text = (PLUGIN / "scripts" / "launch-mcp.mjs").read_text(encoding="utf-8")
        self.assertIn("[22, 15]", text)
        self.assertIn("process.versions.node", text)
        self.assertIn("import('../app/src/server.mjs')", text)
        self.assertLess(text.index("process.versions.node"), text.index("import('../app/src/server.mjs')"))

    def test_workhorse_copy_preserves_source_policy_files(self) -> None:
        source = REPO / "skills" / "workhorse"
        for relative in ("SKILL.md", "agents/openai.yaml", "scripts/dispatch.py", "scripts/workhorse.patch.yml"):
            self.assertEqual((PLUGIN / "skills" / "workhorse" / relative).read_bytes(), (source / relative).read_bytes(), relative)
        for relative in ("references/parallel-headless.md", "references/sdk-fallback.md"):
            text = (PLUGIN / "skills" / "workhorse" / relative).read_text(encoding="utf-8")
            self.assertNotIn("<repo-root>", text, relative)
            self.assertNotIn("<dsh-baton-app>", text, relative)
            self.assertIn("<plugin-root>/skills/workhorse", text, relative)
        routing = (PLUGIN / "skills" / "workhorse" / "references" / "parallel-headless.md").read_text(encoding="utf-8")
        self.assertIn("deepseek-flash", routing)
        self.assertIn("reasoning_effort", routing)
        self.assertIn("DSH_BATON_PATCHES", routing)
        self.assertIn("reasoningEffort", (PLUGIN / "skills" / "workhorse" / "scripts" / "workhorse.patch.yml").read_text(encoding="utf-8"))

    def test_setup_skill_documents_zero_task_prerequisites(self) -> None:
        text = (PLUGIN / "skills" / "dsh-baton-setup" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: dsh-baton-setup\ndescription:"))
        for needed in (
            "Node.js 22.15",
            "DSH_BATON_BACKEND",
            "DSH_BATON_ACCOUNT",
            "DSH_HOME",
            "DSH_BATON_HOME",
            "dsh_list_workspaces",
            "dsh sdk --help",
            "DSH_BATON_AUTO_REGISTER=0",
            "DSH_BATON_CODEX_HOOK=0",
            "Do not run `dsh_send`",
        ):
            self.assertIn(needed, text)
        self.assertNotRegex(text, r"(?m)^node .* send --cwd")
        self.assertNotIn("gemini", text.lower())


class RelocationAndStdioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = node_binary()

    def test_relocated_stdio_initialize_and_tools_list(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh-baton-relocation-") as temporary:
            root = Path(temporary)
            relocated = root / "plugin"
            shutil.copytree(PLUGIN, relocated)
            fake_bin = root / "bin"
            write_fake_dsh(fake_bin)
            env = isolated_env(root, fake_bin)

            mcp = load_json(relocated / "mcp.json")["mcpServers"]["dsh-baton"]
            command = mcp["command"]
            executable = shutil.which(command)
            self.assertIsNotNone(executable, command)
            args = [part.replace("${PLUGIN_ROOT}", str(relocated)) for part in mcp["args"]]
            cwd = mcp["cwd"].replace("${PLUGIN_ROOT}", str(relocated))
            self.assertTrue(Path(cwd).is_dir())

            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "codex-mcp-client", "version": "test"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
            ]
            completed = subprocess.run(
                [executable, *args],
                cwd=cwd,
                env=env,
                input="\n".join(json.dumps(request) for request in requests) + "\n",
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            replies = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
            by_id = {reply.get("id"): reply for reply in replies if "id" in reply}
            self.assertIn(1, by_id)
            self.assertEqual(by_id[1]["result"]["protocolVersion"], "2025-06-18")
            self.assertEqual(by_id[1]["result"]["serverInfo"], {"name": "dsh-baton", "version": "0.6.1"})
            self.assertIn("tools", by_id[1]["result"]["capabilities"])

            tools = by_id[2]["result"]["tools"]
            names = {tool["name"] for tool in tools}
            self.assertEqual(names, VISIBLE_TOOLS)
            schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
            self.assertIn("message", schemas["dsh_send"]["required"])
            self.assertIn("run_id", schemas["dsh_status"]["required"])
            self.assertIn("run_id", schemas["dsh_wait"]["required"])

            self.assertEqual(by_id[3]["result"], {})
            self.assertFalse(Path(env["DSH_TEST_MARKER"]).exists(), "the fake DSH launcher was invoked")
            self.assertEqual(list(Path(env["DSH_BATON_HOME"]).iterdir()), [], "MCP startup wrote runtime state")
            self.assertEqual(list(Path(env["DSH_HOME"]).iterdir()), [], "MCP startup wrote Harness state")

    def test_status_from_relocated_copy_uses_temporary_homes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh-baton-status-") as temporary:
            root = Path(temporary)
            relocated = root / "plugin"
            shutil.copytree(PLUGIN, relocated)
            fake_bin = root / "bin"
            write_fake_dsh(fake_bin)
            env = isolated_env(root, fake_bin)
            write_run(Path(env["DSH_BATON_HOME"]))

            completed = run_cli(self.node, relocated / "app", ["status", "run-20260101000000-test"], env)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["backend"], "headless")
            self.assertEqual(result["run_id"], "run-20260101000000-test")
            self.assertEqual(result["session_id"], "session-test")
            self.assertEqual(result["final_text"], "zero-task status reply")
            self.assertFalse(Path(env["DSH_TEST_MARKER"]).exists(), "status invoked dsh")

    def test_status_uses_home_default_when_baton_home_is_unset(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh-baton-default-home-") as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            write_fake_dsh(fake_bin)
            env = isolated_env(root, fake_bin)
            env.pop("DSH_BATON_HOME")
            write_run(Path(env["HOME"]) / ".dsh-baton")

            completed = run_cli(self.node, APP, ["status", "run-20260101000000-test"], env)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["final_text"], "zero-task status reply")
            self.assertFalse(Path(env["DSH_TEST_MARKER"]).exists(), "status invoked dsh")


if __name__ == "__main__":
    unittest.main(verbosity=2)
