---
name: write-to-me-setup
description: Set up Write to me accounts and configure its writing fallback routes, or reopen its local settings page.
---

Open the plugin's local settings page with `gemini_settings` using the tool's supported open action. If the MCP server is unavailable, run `python3 <plugin-root>/scripts/run.py settings` from the installed plugin directory; inspect `--help` for the available flags. The launcher installs locked Python dependencies into the user's external cache on first use. Python 3.11+ is required; browser routes also require Node.js 22+ and Chrome supported by the bundled bridge.

Let the user choose routes and complete login in the dedicated browser. The local settings page provides AI Studio and Gemini Web login buttons. Antigravity uses the user's separately installed CLI account; API credentials are optional. Selected prompts and reference files are sent to Google when writing is invoked. Setup itself should use local status and settings only.

After configuration, read `gemini_status` and report the effective route order and available local prerequisites. Account login and model availability are separate evidence; a synthetic writing test requires an actual request. Preserve pending requests and existing browser profiles during setup. Settings apply to newly created jobs; continue existing jobs by their original job IDs.
