# Image Analysis (`image-analysis`)

`image-analysis` is a self-contained Codex plugin that connects AI agents to a local **Fiji (ImageJ)** instance via an MCP HTTP server (`http://127.0.0.1:3001/mcp`) for inspecting scientific images, running image processing workflows, and validating reusable Jython scripts.

This plugin is adapted from [`neurogeom/copilotj`](https://github.com/neurogeom/copilotj) under the Apache License 2.0, incorporating local modifications for MCP-only lifecycle management, dedicated instance ownership tracking, and Jython execution workflows. It ships with both prebuilt runtime JARs (two JAR files) and the complete Java source tree (`plugins/image-analysis/source`).

## Prerequisites

- **Fiji (ImageJ)** installed locally
- **Java 21 (JDK 21)**
- **Python 3** (used by the lifecycle launcher and management scripts)

## Installation

### From the `shigo-plugins` Marketplace
```bash
codex plugin marketplace add Shigo-45/codex-plugins
codex plugin add image-analysis@shigo-plugins
```

Or from a local clone of `Shigo-45/codex-plugins`:
```bash
codex plugin marketplace add /absolute/path/to/codex-plugins
codex plugin add image-analysis@shigo-plugins
```

### From GitHub Releases
Download the standalone `image-analysis` ZIP archive from GitHub Releases. The archive contains the plugin files at its root without marketplace directory nesting.

## Setup & Starting Fiji

The plugin communicates with Fiji over the local MCP endpoint:

```text
http://127.0.0.1:3001/mcp
```

You can launch and manage the Fiji MCP server through the plugin's setup helpers or directly via `scripts/start_fiji.py`:

```bash
python3 <plugin>/scripts/start_fiji.py \
  --fiji /path/to/fiji \
  --java-home /path/to/jdk21
```

- On **macOS**, `start_fiji.py` includes default discovery paths for standard macOS Fiji and JDK 21 installations if `--fiji` or `--java-home` are omitted.
- On **other platforms** (or non-standard macOS locations), pass `--fiji` and `--java-home` explicitly.

## Instance Ownership & Lifecycle Safety

To avoid disrupting an already running Fiji session on your workstation:

1. When `start_fiji.py` launches a new Fiji process, it records dedicated ownership metadata consisting of the process **PID** and a unique **JVM token**.
2. Once analysis and result exports are complete, the stop routine checks this PID and JVM token and **only terminates Fiji instances that it spawned**, leaving any user-started Fiji sessions untouched.

## Privacy & Data Handling

- **Local Image Execution:** Invoking Fiji tools and Jython scripts through the local MCP server processes your images locally on `127.0.0.1`. Calling Fiji does not itself upload or transmit your images externally.
- **Downstream Model Review:** If an agent captures screenshots, crops, histograms, or exported summaries from Fiji and sends them to an LLM for interpretation, those derived outputs will be transmitted to your configured model provider. Users should manage and review which exported artifacts are passed to subsequent model analysis.

## Building from Source

The plugin includes two prebuilt runtime JARs for immediate use, plus the full Java source code and Maven `pom.xml` in `source/`.

To run tests or rebuild the JARs from source:

```bash
(cd plugins/image-analysis/source && mvn test)
(cd plugins/image-analysis/source && mvn package)
```

## License & Attribution

Licensed under the **Apache License 2.0**. Derived from [`neurogeom/copilotj`](https://github.com/neurogeom/copilotj) with local adaptations for MCP-only lifecycle control, PID/JVM token ownership safety, and Jython tooling. See the repository [`NOTICE`](../../NOTICE) file for full attribution details.
