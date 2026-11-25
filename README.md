# shigo-plugins (`Shigo-45/codex-plugins`)

`Shigo-45/codex-plugins` is an independent Codex plugin marketplace (`shigo-plugins`) packaging three self-contained plugins for multi-route Gemini writing and consultation, local Fiji/ImageJ scientific image analysis, and DeepSeek Harness task delegation.

## Included Plugins

Each plugin under `plugins/<name>` is self-contained and includes both a compatibility manifest (`.codex-plugin/plugin.json`) and portable root manifests (`plugin.json` and `mcp.json`).

| Order | Plugin ID | Name | Purpose | Upstream / License |
| :--- | :--- | :--- | :--- | :--- |
| 1 | [`gemini-baton`](plugins/gemini-baton/README.md) | **Gemini Baton** | `write-to-me` skill and MCP bridge routing writing/review tasks across Gemini CLI, API, AI Studio, and Web channels. | Original code (MIT) + bundled Gemini Web by Andrew Li (MIT) |
| 2 | [`image-analysis`](plugins/image-analysis/README.md) | **Image Analysis** | Local Fiji/ImageJ MCP integration for scientific image inspection, Jython scripting, and owned-instance lifecycle control. | Adapted from [`neurogeom/copilotj`](https://github.com/neurogeom/copilotj) (Apache-2.0) |
| 3 | [`dsh-baton`](plugins/dsh-baton/README.md) | **DSH Baton** | `workhorse` skill and MCP bridge for delegating engineering work packages to DeepSeek Harness (`deepseek-flash` at `max` effort by default). | Vendored from [`JialaoLiu/dsh-baton`](https://github.com/JialaoLiu/dsh-baton) `0.6.1` (MIT) |

## Installation

### Option 1: Install via Codex Marketplace from GitHub

Register the repository as the `shigo-plugins` marketplace, then add the plugins you need:

```bash
codex plugin marketplace add Shigo-45/codex-plugins

codex plugin add gemini-baton@shigo-plugins
codex plugin add image-analysis@shigo-plugins
codex plugin add dsh-baton@shigo-plugins
```

### Option 2: Install from a Local Clone

If you prefer to audit or build the repository locally before installing:

```bash
git clone https://github.com/Shigo-45/codex-plugins.git
codex plugin marketplace add /absolute/path/to/codex-plugins

codex plugin add gemini-baton@shigo-plugins
codex plugin add image-analysis@shigo-plugins
codex plugin add dsh-baton@shigo-plugins
```

## Privacy & Data Boundaries

- **Gemini Baton:** Prompts, selected context, and attached files are transmitted to Google when a Gemini route is invoked. Local browser profiles and credentials remain in an external state directory outside the plugin tree.
- **Image Analysis:** Running Fiji operations processes images locally on your machine and does not upload image data externally. However, you are responsible for managing what exported images, crops, or measurements are subsequently passed to external LLM providers.
- **DSH Baton:** Ships clean of user state or credentials; requests dispatched via `dsh-baton` are sent to your configured DeepSeek Harness endpoint.

## License & Attribution

Original repository code and packaging are licensed under the **MIT License**. Third-party vendored and adapted components retain their original licenses (**MIT** and **Apache-2.0**). See [`NOTICE`](NOTICE) for full upstream attributions, local modification summaries, and non-endorsement disclosures.
