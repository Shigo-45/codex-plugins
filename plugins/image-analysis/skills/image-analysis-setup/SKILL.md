---
name: image-analysis-setup
description: Set up the packaged Image Analysis runtime in a local Fiji installation and check its MCP endpoint.
---

Find the installed plugin root from this skill's location. Check Python 3, Fiji and Java 21. Run `python3 <plugin-root>/scripts/start_fiji.py --help`, then use the user's Fiji and Java paths with `--fiji` and `--java-home`. Defaults target macOS. Installation does not download Fiji or Java.

The launcher probes `http://127.0.0.1:3001/mcp` before starting its dedicated runtime. Preserve an existing endpoint and its windows. If this chat's Image Analysis tools were unavailable at startup, refresh the MCP connection after launch. Verify startup with `take_snapshot` and `fiji_environment`; keep this setup check separate from a dataset analysis. Use `stop_fiji.py` to stop only the launcher-owned instance after required exports are saved and no other active task uses it. Preserve uncertain state and unsaved images for inspection.
