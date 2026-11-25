#!/usr/bin/env python3
"""Release contract checks against an extracted plugin, outside the checkout."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def test_relocated_release(self) -> None:
        version = json.loads((ROOT / "plugins/gemini-baton/plugin.json").read_text())["version"]
        archive = ROOT / f"dist/gemini-baton-{version}.zip"
        with tempfile.TemporaryDirectory(prefix="write-to-me-release-") as temp:
            base = Path(temp)
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                self.assertTrue(all(name.startswith("gemini-baton/") for name in names))
                self.assertFalse(any(part in name.split("/") for name in names
                                     for part in (".env", ".git", ".venv", "__pycache__", "node_modules")))
                bundle.extractall(base)
            plugin = base / "gemini-baton"
            provenance = json.loads((plugin / "BUILD.json").read_text())
            for name, digest in provenance["sha256"].items():
                self.assertEqual(hashlib.sha256((plugin / name).read_bytes()).hexdigest(), digest)
            self.assertTrue((plugin / "web/THIRD_PARTY_LICENSES.txt").is_file())
            portable = json.loads((plugin / "plugin.json").read_text())
            onboarding = portable["extensions"]["com.openai"]["onboardingSkill"]
            self.assertTrue((plugin / onboarding).is_file())
            config = json.loads((plugin / ".mcp.json").read_text())["mcpServers"]["gemini-baton"]
            self.assertTrue(Path(config["args"][0].replace("${PLUGIN_ROOT}", str(plugin))).is_file())
            env = dict(os.environ, GEMINI_BATON_STATE_DIR=str(base / "state"))
            for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BATON_ENV_FILE"):
                env.pop(key, None)
            argv = [sys.executable, str(plugin / "scripts/run.py")]
            status = subprocess.run(argv + ["status"], env=env, capture_output=True, text=True, check=True)
            result = json.loads(status.stdout)
            self.assertEqual(result["network_calls"], 0)
            self.assertTrue(result["writing"]["routes"])
            # Rename the extracted plugin after installation. Cached runtime uses a wheel,
            # so imports and MCP startup remain independent of its former location.
            moved = base / "moved plugin"
            plugin.rename(moved)
            asyncio.run(self.check_mcp(moved, env))

    async def check_mcp(self, plugin: Path, env: dict[str, str]) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=sys.executable,
                                      args=[str(plugin / "scripts/run.py"), "mcp"], env=env)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                self.assertTrue({"gemini_write", "gemini_write_result", "gemini_status", "gemini_settings"} <= names)
                status = await session.call_tool("gemini_status", {})
                self.assertFalse(status.isError)


if __name__ == "__main__":
    unittest.main()
