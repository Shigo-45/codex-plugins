---
name: workhorse
description: Hand substantial implementation, refactoring, investigation, and test-and-fix work to DeepSeek Harness while Codex coordinates and verifies. Use for coherent, long-running work packages that free Codex capacity or enable parallel progress, and explicit Workhorse/DeepSeek/DS requests.
---

# Workhorse

Use DeepSeek Harness (DSH) through dsh-baton for sustained execution. Default to **`deepseek-flash` with `max` reasoning effort**; apply the user's task-specific overrides.

## When to use

Delegate a substantial package with a clear outcome: a feature across several files, a larger refactor, a broad investigation, or a long implementation-and-test loop. Favor work that releases Codex capacity or runs independently alongside the primary agent. Keep quick edits and lookups local unless the user requests Workhorse or DeepSeek. Weigh handoff and review effort against the extra DeepSeek PAYG cost.

## How to use

1. **Define the package.** Supply the objective, selected context, absolute `cwd`, editable paths or read-only scope, acceptance criteria, existing checks, and expected result. Request changed files, checks actually run, and remaining limitations. Use isolated workspaces or disjoint file ownership for parallel writers. Enforce required permissions through the DSH profile or sandbox. Keep credentials in their configured services; retrieve experimental context through LabForge.

2. **Give it an adapter.** For independent work, spawn a thin native Codex subagent to dispatch and collect the DSH task. This skill requests that delegation. Create one Codex adapter layer; that adapter calls dsh-baton directly. Have it report `run_id`, `session_id`, and `backend` promptly; the primary agent continues useful work. DSH performs the package, while the adapter handles transport and questions. Inherit the configured Codex model. Direct dispatch remains suitable for a fully dependent handoff or environments without native subagents.

3. **Dispatch with the chosen defaults.** On the host backend, call `dsh_send` with `cwd`, the complete `message`, suitable `checks`, `model: "deepseek-flash"`, and `reasoning_effort: "max"`. Resolve an ambiguous model against `dsh_list_models`. For desktop-free execution, use the bundled headless patch and CLI as described in [routing and recovery](references/parallel-headless.md); bridge 0.6.1 accepts model parameters on its host path and headless settings through patches. Preserve the configured credential provider and verify the effective model/effort. Keep explicit user overrides across rework.

4. **Collect and finish.** The adapter waits for its matching run, then returns its terminal state, DSH reply, changed files, and check evidence. Unfinished waits continue the same run. Relay approval or missing-information questions to the primary agent for the user's decision when required. Review every result against the assignment, inspect relevant changes, and run checks where evidence has gaps. Send consolidated fixes through the same `session_id`; use separate sessions for unrelated packages. Accept only verified results.

Keep dispatch and waiting in the same adapter so Stop-hook ownership stays with that child. Follow the [routing and recovery reference](references/parallel-headless.md) for CLI fallback, hook metadata issues, host followup/steer, and cancellation. Cancel the recorded DSH `run_id` before retiring its adapter; inspect uncertain deliveries before sending again.

For deliberate fresh SDK batches when dsh-baton is unavailable, read [SDK fallback](references/sdk-fallback.md). Its shared runtime has different continuation and timeout behavior.
