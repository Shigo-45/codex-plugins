# Gemini Baton (`gemini-baton`)

`gemini-baton` is a self-contained Codex plugin that provides the `write-to-me` skill and an MCP bridge for routing writing, analysis, and consultation tasks to Google Gemini models across four channels: **CLI**, **API**, **Google AI Studio (`AIS`)**, and **Gemini Web (`Web`)**.

It combines an original Python MCP/API bridge with a self-contained bundle of Andrew Li's MIT-licensed Gemini Web automation client.

## Features

- **Multi-Channel & Multi-Model Fallback:** Automatically tries enabled routes in user-defined order.
- **Local Settings UI (`gemini_settings`):** Browser-based configuration panel on `127.0.0.1` protected by a session token, HTTP cookie, and CSRF validation.
- **External State & Profile Isolation:** Browser profiles, API keys, and task state are stored outside the plugin directory with strict directory (`700`) and file (`600`) permissions.
- **Pending Task Recovery:** Recovers and tracks in-flight or unfinished tasks across MCP server restarts.
- **File Attachments:** Supports attaching up to **10 files** per request (up to **100 MB** per file).
- **Dual Manifest Layout:** Includes both `.codex-plugin/plugin.json` and root `plugin.json` + `mcp.json`.

## Prerequisites

- **Python 3.11+ (Required):** On first start with internet access, the plugin automatically bootstraps its Python dependencies into an external virtual environment (`venv`).
- **Node.js 22+ and Google Chrome:** Required if you use the browser-backed **AI Studio (`AIS`)** or **Gemini Web (`Web`)** routes.
- **`agy` CLI (Optional):** Required only if you enable the **CLI** route.
- **Gemini API Key (Optional):** Required only if you enable the **API** route.

## Installation

### From the `shigo-plugins` Marketplace
```bash
codex plugin marketplace add Shigo-45/codex-plugins
codex plugin add gemini-baton@shigo-plugins
```

Or from a local clone of `Shigo-45/codex-plugins`:
```bash
codex plugin marketplace add /absolute/path/to/codex-plugins
codex plugin add gemini-baton@shigo-plugins
```

### From GitHub Releases
Download the standalone `gemini-baton` plugin ZIP from GitHub Releases. The archive contains the plugin root directly without marketplace folder nesting.

## Setup & Configuration

Invoke the `gemini_settings` MCP tool from Codex to launch the local configuration page on `127.0.0.1`:

1. **Configure CLI (`CLI`):** If using the CLI route, follow the prompt to authenticate in your terminal:
   ```bash
   agy acp --login
   ```
2. **Configure API Key (`API`):** Paste your Gemini API key into the settings page. It is saved to the external private state directory (`700` directory / `600` file permissions).
3. **Sign in to Google (`AIS` / `Web`):** Click the AI Studio or Gemini Web login buttons in the local UI and complete the interactive Google login directly in the launched Chrome window.

## Default Routing & Model Matrix

By default, `gemini-baton` evaluates channels in the following order:

1. **`CLI`** (`agy` CLI)
2. **`API`** (Google GenAI SDK)
3. **`AIS`** (Google AI Studio browser automation)
4. **`Web`** (Gemini Web browser automation)

Within each channel, models are attempted in the following order:

1. `4 Argon High`
2. `4 Carbon High`
3. `4 Neon xHigh`
4. `3.8 Flash High`

> **SDK Note for `API` (`4 Neon xHigh`):** The current Google GenAI Python SDK does not yet support the `xhigh` thinking level. When routing through the `API` channel, `4 Neon xHigh` is automatically skipped.

## Privacy & Security Considerations

- **External Transmission to Google:** All selected prompts, conversation context, and attached files (up to 10 files, 100 MB each) are sent to Google services when a Gemini route executes.
- **Local Loopback Protection:** The `gemini_settings` server binds only to `127.0.0.1` and enforces token, cookie, and CSRF checks before accepting configuration changes.
