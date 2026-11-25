---
name: dsh-baton-setup
description: Verify DSH Baton runtime, DeepSeek Harness, account, and backend prerequisites without sending a model task or changing configuration.
---

# DSH Baton setup

Resolve `<plugin-root>` as the installed directory that contains this skill's parent `skills/` directory. The bundled server is `<plugin-root>/app`; its interface is the MCP entry in `<plugin-root>/mcp.json` (compatibility entry: `<plugin-root>/.mcp.json`). Runtime state stays outside the plugin, under the current user's home.

## Boundaries

Setup is read-only except for the commands the user chooses to run. Do not run `dsh_send`, `dsh_wait`, `_run`, `send`, `wait`, `setup`, `codex-stop-hook`, or any command or MCP tool that starts a Harness turn or writes Codex/Harness configuration. Do not install hooks, edit `config.toml`, or create credentials. The source server only starts a run when a send tool or `send` command is invoked; starting the MCP process and listing tools is zero-task.

## Prerequisites

- **Node.js 22.15 or newer.** The vendored package declares `"engines": {"node": ">=22.15"}` and the launcher refuses older runtimes. Check with `node --version`.
- **DeepSeek Harness.** The host backend needs the Harness desktop app or `dsh web` running with the `dsh-baton-host` plugin enabled (0.6.1 matches the bundled server) and reachable at `DSH_BATON_SOCKET` (default `~/.dsh-baton/host.sock`, owner-only). Install or enable that host plugin through Harness → Plugins if it is not already present; this portable bundle vendors the host source for reference but does not install it. The headless backend needs a Harness launcher: the macOS app path is detected automatically, otherwise `dsh` on `PATH`, or set `DSH_BIN` to the launcher.
- **Account or API credentials.** The default account route appends `<plugin-root>/app/patches/account.patch.yml`, which selects `provider: deepseek-account`, `model: deepseek-flash`, and enables the account, authorization, and local-credential plugins. It reuses the desktop account sign-in stored under `DSH_HOME` (default `~/.dsh`). Sign in through the DeepSeek Harness app; dsh-baton has no separate login command. Setting `DSH_BATON_ACCOUNT=0` skips that patch and leaves DSH on its API-key route (`deepseek-official`); configure credentials through DSH itself. dsh-baton does not store credentials.
- **External state.** `DSH_BATON_HOME` (default `~/.dsh-baton`) holds `runs/`, `evidence/`, and the host socket. If `~/.dsh-baton` does not exist but the legacy `~/.dsh-bridge` does, the upstream helper keeps using `~/.dsh-bridge`. `DSH_HOME` (default `~/.dsh`) holds Harness sessions and profiles. Both are outside the plugin; this bundle contains no user configuration, sessions, runs, credentials, or browser profiles.

## Zero-task checks

Run these from the installed plugin directory. They exercise local files and the host socket only.

```sh
node --version
node "<plugin-root>/app/src/cli.mjs" runs --limit 1
node "<plugin-root>/app/src/cli.mjs" status <known-run-id>
```

`runs` and `status` read local run records. `status` does not start or continue a run. To check host reachability without a task, use the MCP tool `dsh_list_workspaces` or:

```sh
node "<plugin-root>/app/src/cli.mjs" workspaces
```

That command fails clearly when the host plugin is unavailable; it never creates a session. For the headless route, verify the launcher separately with `dsh --version`, or `"$DSH_BIN" --version` when you set it.

The legacy Workhorse SDK fallback has a separate launcher check, but it invokes `dsh sdk --help`, which can initialize an SDK profile under `DSH_HOME`. Do not run it during setup unless the user authorizes that write. Do not treat a successful prerequisite check as proof of login, model availability, or a completed task. Those require separate evidence.

## Backend selection

- `DSH_BATON_BACKEND=auto` (default) uses the host plugin when its socket answers, otherwise headless.
- `DSH_BATON_BACKEND=host` requires the host plugin; it fails rather than silently running headless.
- `DSH_BATON_BACKEND=headless` uses detached `dsh headless` runs and does not need the host plugin. New headless sessions stay outside the desktop sidebar.

When the host plugin is enabled, it may auto-register its own MCP server and Codex Stop hook. To keep this portable plugin's `mcp.json` as the single registration and avoid hook/config writes, set `DSH_BATON_AUTO_REGISTER=0` in the Harness process environment before enabling it. If only the hook is unwanted, `DSH_BATON_CODEX_HOOK=0` disables that part; the source checks both variables.

## Models and effort

Host `dsh_send` accepts per-session `model` and `reasoning_effort`; Workhorse defaults are `deepseek-flash` with `max`. Headless dsh-baton 0.6.1 rejects those MCP arguments, so select the headless route through a patch instead. The bundled Workhorse patch (`skills/workhorse/scripts/workhorse.patch.yml`) sets `provider: deepseek-account`, `model: deepseek-flash`, and `reasoningEffort: max`; add its path to `DSH_BATON_PATCHES` on every related headless turn. The portable Workhorse routing reference documents the exact `send` and `wait` invocations; this setup skill does not run them.

For a different route, create a task-specific patch with `provider`, `model`, and `reasoningEffort` together. Verify the returned backend and effective route. For headless runs that omit model evidence, inspect only the returned session's metadata under `DSH_HOME/sessions`: `session.v4.jsonl.zstd` contains `request/header` events with `data.header.config.provider`, `.model`, and `.reasoningEffort`. Keep unrelated sessions and full headers private.

## Data and task boundary

The setup checks do not send a prompt and do not submit a model request. When the user later invokes `dsh_send`, the selected task text, working folder path, and any requested checks are sent to the local DeepSeek Harness and its configured model provider. Keep credentials in their configured services, and use the user's chosen model only when the task names it.
