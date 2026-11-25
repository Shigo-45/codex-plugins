# DSH Baton (`dsh-baton`)

`dsh-baton` is a self-contained Codex plugin that bridges Codex to **DeepSeek Harness (DSH)** via MCP and provides the `workhorse` skill for delegating coherent implementation, refactoring, debugging, and test-and-fix work packages.

This plugin vendors the source of [`JialaoLiu/dsh-baton`](https://github.com/JialaoLiu/dsh-baton) version `0.6.1` (MIT License) from a clean local installation, stripped of all personal credentials and runtime state, with local adjustments for MCP host compatibility and headless operation.

## Prerequisites

- **Node.js 22.15+**
- A separately configured **DeepSeek Harness** account and accessible endpoint

## Installation

### From the `shigo-plugins` Marketplace
```bash
codex plugin marketplace add Shigo-45/codex-plugins
codex plugin add dsh-baton@shigo-plugins
```

Or from a local clone of `Shigo-45/codex-plugins`:
```bash
codex plugin marketplace add /absolute/path/to/codex-plugins
codex plugin add dsh-baton@shigo-plugins
```

### From GitHub Releases
Download the standalone `dsh-baton` ZIP archive from GitHub Releases. The archive contains the plugin root directly without marketplace directory nesting.

## Setup

After installing the plugin, follow the instructions in the bundled **`setup` skill** inside `plugins/dsh-baton` to configure your local DeepSeek Harness connection, endpoint settings, and credentials.

## Usage & Workflow

- **`workhorse` Skill:** Configured by default to dispatch coding, investigation, and verification work packages using **`deepseek-flash`** at **`max`** effort.
- **Division of Labor:** In a multi-plugin setup alongside `gemini-baton`, `dsh-baton` serves as the engineering workhorse while primary prose and long-form writing remain handled by Gemini.

## License & Attribution

Licensed under the **MIT License**. Based on [`JialaoLiu/dsh-baton`](https://github.com/JialaoLiu/dsh-baton) (`0.6.1`) by Jialao Liu, vendored with local MCP host and headless execution adjustments. See the root [`NOTICE`](../../NOTICE) file for complete attribution and non-endorsement notices.
