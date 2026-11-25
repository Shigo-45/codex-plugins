# Routing and recovery

Use this for desktop-free execution, adapter setup, and run recovery. Bundled interfaces: dsh-baton 0.6.1 (in `<plugin-root>/app`), DSH 0.2.1-rc.3. CLI: `node <plugin-root>/app/src/cli.mjs`.

## Models and desktop-free execution

The host MCP interface supports per-session `model` and `reasoning_effort`. Workhorse supplies `deepseek-flash` and `max`; user overrides take precedence. Headless 0.6.1 rejects those MCP arguments and uses DSH startup settings instead. Its bundled patch sets the same defaults with `deepseek-account`, matching the default desktop account route.

Send an assignment file on stdin:

```sh
DSH_BATON_BACKEND=headless \
DSH_BATON_PATCHES="${DSH_BATON_PATCHES:+$DSH_BATON_PATCHES:}<plugin-root>/skills/workhorse/scripts/workhorse.patch.yml" \
node <plugin-root>/app/src/cli.mjs send --cwd /absolute/workspace - < /absolute/work/assignment.txt
```

For a task-specific model/effort override, create an equivalent temporary patch with the requested values and use its path. DSH replaces the entire plugin `config`, so include `provider`, `model`, and `reasoningEffort` together. Preserve the patch until all related turns end and apply it on every headless continuation. The bundled patch reuses stored account sign-in. When an API-key or custom provider is selected, use a temporary patch naming that configured provider and the bridge's corresponding credential settings; preserve the selected route.

Headless starts detached runs and continues its own sessions; new sessions stay outside the desktop sidebar. Busy-session `followup`/`steer`, model catalog lookup and host approval controls use the host backend. Use separate headless sessions for parallel packages, and finish the active turn before continuing a session.

Verify returned backend and effective provider/model/effort. For headless receipts that omit model evidence, inspect only the returned session's selected metadata under `$DSH_HOME/sessions` (default `~/.dsh/sessions`). In DSH 0.2.1-rc.3, `session.v4.jsonl.zstd` contains `request/header` events with `data.header.config.provider`, `.model`, and `.reasoningEffort`. Cross-check `assistant/message` fields under `data.message.source`. Extract only these route fields; keep full headers and unrelated sessions private.

## Adapter handoff

Give the native Codex adapter the complete package plus these transport instructions:

```text
Forward this package to DSH through dsh-baton. Apply Workhorse's
deepseek-flash/max defaults unless the package specifies overrides.
Use explicit cwd and assigned file ownership. Report run_id, session_id,
backend and start time promptly. DSH performs the work.
Wait for the matching run; return terminal state, reply, changed files,
checks and effective route evidence. Relay questions to the primary agent.
Continue this DSH session for related rework. On cancellation, cancel
the recorded DSH run and verify it stopped.
```

Each adapter owns one coherent session. Its task entry provides a native Codex coordination surface; DSH's detailed execution evidence comes through the bridge.

## Waiting, hooks, and cancellation

MCP adapters use `dsh_wait`; headless CLI adapters use:

```sh
node <plugin-root>/app/src/cli.mjs wait run-RETURNED-ID --timeout 45
```

Parse JSON `state`, `timed_out`, `needs_input`, and `error`. CLI exit codes are 0 for completion, 3 for required input, and 1 for timeout or another outcome. Repeat unfinished waits for the same run within the environment's limits. Use `send --session session-RETURNED-ID -` for related rework, retaining the selected headless patch.

MCP sends register Stop-hook delivery under the calling Codex thread; terminal `dsh_wait` clears it. Let the adapter own send and wait. A hook end-turn hint applies to that dispatching agent while the primary continues independent work. Use a child Stop hook only where child delivery has been verified. If child calls carry parent metadata, send and wait through CLI from the start; CLI sends bypass hook registration. Diagnose ownership by reading only the known run's `thread` in `$DSH_BATON_HOME/codex-hook/pending.json` (default `~/.dsh-baton/codex-hook/pending.json`) and comparing its adapter and parent thread IDs. Preserve hook state and trust settings.

The primary can cancel a known run while its adapter waits: use `dsh_cancel` with `run_id`, or CLI `cancel run-RETURNED-ID`, then verify terminal cancellation and stopped work. Headless 0.6.1 routes cancellation by `run_id`. Adapter interruption and DSH cancellation are separate operations. For uncertain send delivery, inspect the recorded run before retrying.
