#!/usr/bin/env python3
"""Validate and archive the three self-contained marketplace plugins."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {".env", ".git", ".venv", "node_modules", "__pycache__", ".DS_Store", "session.json"}


def validate() -> list[Path]:
    marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    assert [p["name"] for p in marketplace["plugins"]] == ["gemini-baton", "image-analysis", "dsh-baton"]
    plugins = []
    for entry in marketplace["plugins"]:
        plugin = ROOT / entry["source"]["path"]
        manifest = json.loads((plugin / "plugin.json").read_text())
        legacy = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
        assert plugin.name == manifest["name"] == legacy["name"] == entry["name"]
        assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        assert manifest["version"] == legacy["version"]
        mcp = json.loads((plugin / "mcp.json").read_text())
        assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
        for server in mcp["mcpServers"].values():
            assert server["type"] in {"stdio", "streamable-http"}
            if server["type"] == "stdio":
                for arg in server.get("args", []):
                    if "${PLUGIN_ROOT}" in arg:
                        assert Path(arg.replace("${PLUGIN_ROOT}", str(plugin))).is_file(), arg
        onboarding = manifest["extensions"]["com.openai"].get("onboardingSkill")
        if onboarding:
            assert (plugin / onboarding).is_file()
        assert list((plugin / "skills").glob("*/SKILL.md"))
        if plugin.name == "image-analysis":
            jars = list((plugin / "runtime").glob("ImageAnalysisMCP-*.jar"))
            assert len(jars) == 1
            with zipfile.ZipFile(jars[0]) as jar:
                assert b"copilotj.McpOnlyService" in jar.read("META-INF/json/org.scijava.plugin.Plugin"), "Fiji MCP service index is missing"
        for path in plugin.rglob("*"):
            assert not path.is_symlink(), path
            assert not FORBIDDEN.intersection(path.relative_to(plugin).parts), path
            assert not path.name.startswith(".env"), path
            if path.is_file() and path.suffix in {".md", ".json", ".yaml", ".toml", ".py", ".mjs", ".yml"}:
                assert str(Path.home()) + "/" not in path.read_text(), path
        plugins.append(plugin)
    return plugins


def main() -> None:
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    sums = []
    for plugin in validate():
        version = json.loads((plugin / "plugin.json").read_text())["version"]
        archive = output / f"{plugin.name}-{version}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(plugin.rglob("*")):
                if path.is_file():
                    bundle.write(path, str(path.relative_to(plugin.parent)))
        sums.append(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name)
        print(archive)
    (output / "SHA256SUMS").write_text("\n".join(sums) + "\n")


if __name__ == "__main__":
    main()
