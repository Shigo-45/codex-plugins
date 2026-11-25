# Legacy SDK dispatcher

Use this fallback when dsh-baton is unavailable and a fresh, bounded SDK batch fits the task. It starts one shared runtime per batch; sessions cannot be continued across later batches. A task timeout stops that runtime and interrupts every unfinished task in the batch. It does not provide dsh-baton host sessions, queues, or Stop-hook wakeups. Choose a timeout appropriate to the delegated work; the helper default is 300 seconds and is unsuitable for a long job without an explicit override.


Resolve this skill's directory from its installed path. The helper requires Python3.10+ and uses the official SDK JSON-RPC protocol with no Python package dependency. It starts one `dsh sdk` process per batch, initializes once, runs separate sessions concurrently, and shuts down the process tree. Later batches start a new process; no background service is installed.

Check the launcher when first using the skill in a new environment:

~~~sh
python3 <repo-root>/skills/workhorse/scripts/dispatch.py --check
~~~

Resolution order: `--dsh-bin`, `DSH_SUBAGENT_BIN`, `dsh` on PATH, standard macOS Desktop launcher. Desktop includes a CLI that works while the GUI is closed. A missing global npm installation does not establish that DSH is absent. Report concrete startup or compatibility failures; avoid changing installations or saved profiles to address a single task.

Write UTF-8 JSON in the task's scratch/work directory:

~~~json
{
  "tasks": [
    {"id": "parser", "prompt": "Inspect /absolute/repo/src/parser.py for empty-input handling. Read only. Report findings with line locations and evidence, or an empty findings list."},
    {"id": "export", "prompt": "Inspect /absolute/repo/src/export.py for newline handling. Read only. Report findings with line locations and evidence, or an empty findings list."}
  ]
}
~~~

~~~sh
python3 <repo-root>/skills/workhorse/scripts/dispatch.py \
  --cwd /absolute/repo --tasks /absolute/work/tasks.json --max-parallel 3 \
  --output /absolute/work/ds-results.json
~~~

For one task use `--task 'self-contained assignment'`. `--timeout` bounds each admitted task in seconds; `--startup-timeout` bounds initialization separately. Progress goes to stderr; the final JSON report goes to stdout or `--output`. Use asynchronous host execution to stay responsive.

The default route is `deepseek-official` / `deepseek-flash`, matching the verified local DSH0.2.1-rc.3 base profile. Override through `--provider` / `--model` or `DSH_SUBAGENT_PROVIDER` / `DSH_SUBAGENT_MODEL` for another configured route. Preserve a user-specified model; avoid silent substitution after a failure. The default reasoning effort is `max`; `--reasoning-effort` overrides it. `--max-tokens` passes an explicit output limit. Token-limited termination remains incomplete.

For authorized direct edits, pass `--permission-mode workspace-write --max-parallel 1` and name editable paths and verification requirements. The default read-only policy and workspace root are applied through a temporary per-launch patch. The helper never selects danger-full-access, changes saved profiles, or retries with broader permissions.


## Completion and failure

Read every result. `ok: true` requires successful SDK wait, completed termination, and non-empty committed assistant text. An enqueue receipt or idle notification alone does not establish completion. Session IDs are retained for diagnosis. Verify the substance and inspect proposed edits before integrating them. After a timeout, preserve collected results and re-plan only unfinished work.

## Interface references

Verified against Desktop DSH0.2.1-rc.3. On a protocol mismatch, inspect the installed version and relevant contract:

- [SDK protocol](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/protocol/README.md): initialization, prompt, wait, committed events, shutdown.
- [SDK server](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/server/README.md): session ownership and completion.
- [Desktop CLI](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md#bundled-command-runtime): packaged launcher.
