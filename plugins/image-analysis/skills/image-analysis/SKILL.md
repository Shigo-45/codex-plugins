---
name: image-analysis
description: Operate local Fiji/ImageJ through native MCP tools to inspect images, develop analysis workflows, and validate reusable Jython scripts.
---

# Image Analysis

Use this plugin's MCP tools to inspect and operate the running Fiji instance.
The agent supplies reasoning; Fiji supplies image operations, scripting, and
window state. No CopilotJ model service or Python agent backend is required.

## Connection

The endpoint is `http://127.0.0.1:3001/mcp`. Fiji must remain open while tools
are in use. Call `take_snapshot` to inspect existing windows, and use
`fiji_environment` when plugin availability or runtime versions matter.

If the endpoint is offline, run the installed plugin's `scripts/start_fiji.py`
with `python3`; resolve the path relative to this skill (`../../scripts/`).
The launcher checks an existing endpoint before starting Fiji, writes logs to
`~/.local/state/image-analysis-mcp/fiji.log`, and prints the connection result.
It accepts `--fiji` and `--java-home` if the local defaults have moved. Refresh
the conversation's MCP connection after launching if tools were unavailable
when the conversation started.

## Analysis and reusable scripts

- Use a small, complete, reusable assay as the script unit. A Jython script
  should accomplish a meaningful processing or measurement goal through a
  clear parameter interface and input/output contract. Keep basic ImageJ
  operations together when their combination forms that useful unit. A thin
  wrapper around one native ImageJ function usually adds little reuse value.
  Use internal functions to organize implementation details; create separate
  scripts when they serve independently useful assay goals.
- For example, keep rotation, flipping, cropping, resizing, calibration and
  format export in one configurable image-adjustment assay. Keep lane-profile
  extraction, local background subtraction and peak integration together in
  one gel-quantification assay. General peak decomposition has its own signal
  inputs and reference-shape constraints and can be a separate assay. Keep
  domain meanings and response-factor assumptions in the calling workflow.
  Let actual reuse guide
  the boundary; avoid splitting each native function or procedural step into
  its own script, and avoid collecting unrelated assays into one large tool.
- Before writing a new implementation, inspect available scripts for the same
  operation. Prefer one generic Jython implementation plus task-specific
  parameters. Keep dataset paths, ROI coordinates, lane names, thresholds and
  fit assumptions in configuration; avoid cloning a script for each sample.
  Declare relevant Fiji/plugin dependencies and coordinate/intensity units.
- Compose workflows from assay scripts and parameters. An orchestration script
  may connect assays, but should call their implementations rather than embed
  another copy. Intermediate artifacts or explicit returned objects should
  allow an assay to be inspected and rerun independently. Avoid depending on the
  currently selected window or hidden state from a previous call.
- Validate each assay's contract and the assembled workflow on representative
  inputs. Keep the tested script/configuration versions with the results.
  Reuse stable assays by changing parameters; create a new script when the
  analytical goal changes. Build shared infrastructure only when repeated
  use justifies it.
- Prefer `run_script` with `language: "jython"`. Here `python` is an alias for
  Jython 2.7 with access to ImageJ's Java APIs. Use the installed Fiji plugins
  and Java APIs for image processing.
- `run_script` accepts script text and a timeout. Read an existing `.py` file
  and send its contents through this tool; a separate file runner is unnecessary.
- Declare SciJava outputs such as `#@output String status` and assign them to
  return structured completion evidence. A null return value provides little
  evidence of success; inspect artifacts and Fiji state before deciding to retry.
- Preserve originals, identify images by title or ID, and write outputs into
  the task's chosen directory. Validate measurements and produced files against
  a representative input. Save the successful workflow as an ordinary Jython
  `.py` file with explicit inputs, outputs, parameters, and plugin requirements.
- LF may reuse that script through its Fiji plugin. Import into LF only when
  requested. This plugin and LabForge remain independent.
- Use `capture_image` for image previews. `capture_fiji_screen` captures the
  entire display containing Fiji, which can include other visible applications.

## Gel densitometry recipe

For rectangular gel lanes, inspect `../../scripts/gel_workflow_jython.py` with
`../../scripts/gel_workflow.example.json` as the configuration schema. Resolve
these paths relative to this skill. Copy the configuration into the analysis
directory and select coordinates from the current source. The example's values
are illustrative and its conversion assumptions start unconfirmed.
This file composes three independent assay scripts from the same directory:

- `adjust_and_export_image_jython.py`: apply an ordered operation list and
  export native images or explicit display derivatives. Operations use each
  intermediate image's coordinates. `resize` resamples pixels; `set_calibration`
  changes physical pixel spacing. Arbitrary-angle rotation uses a fixed canvas.
- `quantify_gel_peaks_jython.py`: extract lane profiles, subtract local linear
  backgrounds and integrate peaks. Optional profile-only lanes and background
  shifts export signed `profile_data.json` for downstream fitting.
- `decompose_peaks_jython.py`: fit one or more arbitrarily named components
  using measured reference shapes, shifts and explicit width sets. Input is
  `image-analysis.peak-curves.v1` JSON with a uniformly sampled `x`, signed
  baseline-corrected observations and references in matching units. Components
  may share a reference or use different references. Outputs are signal areas,
  signal fractions, component curves, residuals and configured sensitivity.
  Inspect `decompose_peaks.example.json` and its `.input.example.json` for the
  data contract. `plot.enabled: false` produces numeric artifacts without Fiji
  plotting. Keep shape applicability and residual inspection explicit.

The gel recipe adapts its quantified profile artifact to that generic curve
contract. It separately interprets `peak_A` and `peak_B` as product and substrate
under the user-confirmed molecular-response assumptions. Other assays supply
their own component meanings and response factors.

Each assay is self-contained and accepts `ASSAY_CONFIG` or `ASSAY_CONFIG_PATH`;
running it in Fiji opens a JSON chooser when these are absent. Inspect the
corresponding `.example.json` for its parameter schema. Set `ASSAY_SOURCE_PATH`
when submitting text so the result directory retains the executed source.
An assay can be reused by LF as an ordinary Jython file. The recipe locates its
companion scripts through `GEL_WORKFLOW_SOURCE_PATH` or `GEL_SCRIPT_DIR`.

All ROIs refer to the original image using zero-based, end-exclusive bounds;
crop uses `[x, y, width, height]`. Inspect raw pixels to set `signal_polarity`:
an inverted display LUT alone does not change the measured intensities. Use a
fresh output directory for each run. Confirm that the crop retains the intended
gel edges and faint bands. The script checks image bounds and preserves the
raw bit depth; ROI selection remains an image-specific decision.

Prepend `GEL_CONFIG_PATH = u'/absolute/path/to/config.json'` to the script text
and submit the combined text through `run_script` with `language: "jython"`.
Also set `GEL_WORKFLOW_SOURCE_PATH` to the original script's absolute path to
save its source snapshot and checksum with the results.
Alternatively set `GEL_CONFIG` to a dictionary. Running the unmodified file in
Fiji opens a configuration chooser. The same ordinary Jython file can be reused
by LF without importing any experimental object.

Remove `conversion` for lane integration alone. In this gel recipe, interpret
the signal fractions as conversion after confirming a pure substrate reference
and equivalent fluorescence per molecule. General peak decomposition reports
signal fractions independently of these gel-specific assumptions. Choose the
primary ROI/model before interpreting the fit; inspect
the fitted components, residual, template coverage, grid boundaries and the
configured sensitivity range. The range describes those particular model
settings; alternative peak families and biological replicates require separate
assessment. Reuse fitted scripts and parameters after validating each new gel.

Outputs include raw TIFF, display PNGs, source/prepared ROI archives, signed
background-corrected profiles and peak integrals, optional conversion fits and
sensitivity, run configuration, source hash and output checksums. No absolute
fragment-length assignment is inferred from the pixel coordinates.

## Task completion and Fiji lifetime

The launcher uses `--allow-multiple` to start a dedicated Fiji process and
records its PID and per-launch JVM marker outside the plugin. The default is
to exit that managed instance when the analysis task finishes. Finish artifact
export, verify saved results, and capture any needed previews first. Release
task-created images in reusable scripts with `try/finally`; keep originals
and explicitly requested unsaved work intact.

Run `python3 ../../scripts/stop_fiji.py` (resolve the path relative to this
skill) as the final Fiji operation before delivering the completed task.
It checks both the recorded process and the endpoint's JVM marker, requests
normal Fiji Quit through the existing script runner, and verifies process
exit. Report completion only after it confirms exit. A later task uses
`start_fiji.py` to launch a fresh instance.

When a script is active, a modal dialog is open, or an image has unsaved
changes, inspect and resolve the task state before finishing. Save required
outputs and close only identified task-owned temporary images; clear their
`changes` flag only once their contents are saved or intentionally disposable.
Keep the managed instance open on unresolved errors or timeouts, and report
the state. Honor a request to leave Fiji open for interactive inspection.

An existing endpoint can also belong to a manually started or older instance.
The stop script preserves these instances when ownership evidence is absent
or mismatched. Preserve the ownership record and existing windows rather than
adopting a process by application name or terminating arbitrary Java processes.

## Shared endpoint

All conversations connect to the same Fiji endpoint and see the same windows.
Inspect current state before changing it; serialize related mutations and avoid
changing images belonging to another active task. Defer instance shutdown while
another task is using it. If a script times out, inspect
state and wait for completion: timeout requests an interrupt, and some engines
continue processing. The runner blocks subsequent scripts while that execution
remains active. Closing Fiji also closes its MCP endpoint; use the launcher to
start it again.
