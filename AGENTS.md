# Personal Codex plugins workspace

## Purpose and ownership

This project is the dedicated development and maintenance workspace for the user's personal Codex plugins, skills, and their supporting bridges, including future additions. Resolve repository paths from this file's directory or from the relevant script location.

- `skills/write-to-me/` and `skills/workhorse/` are the maintained skill sources. The matching directories under `~/.codex/skills/` are installation symlinks to these sources. Edit the source here; preserve skill metadata, references, scripts, and existing invocation policies.
- `gemini-baton/` is the Python Gemini API bridge and belongs to the root Git repository.
- `gemini-web-bridge/` is an independent, nested Git repository with its existing upstream and local changes. It is ignored by the root repository. Run its Git operations from that directory and report its changes separately. Preserve its `.git` and upstream history. Any future change to this boundary requires an explicit migration plan.
- Root Git tracks this file, workspace tooling, maintained skills, and `gemini-baton/`. New plugins should establish their intended Git ownership before staging files. Inspect the owning repository's status before editing and preserve existing changes.

## Installed paths and relocation

Resolve this checkout from the repository root. Local MCP launch paths and skill installation symlinks belong in the user's external Codex configuration. Public plugin manifests use `${PLUGIN_ROOT}`. Keep developer-specific paths and credentials out of distributable plugin files.

The repo marketplace is `.agents/plugins/marketplace.json`; it exposes `plugins/gemini-baton`, `plugins/image-analysis`, and `plugins/dsh-baton`. `packaging/gemini-baton` is the packaging template; `scripts/build_write_to_me_plugin.py` snapshots the maintained Python bridge and skill into the generated plugin. `plugins/image-analysis/source` is a licensed vendored snapshot of the Fiji Java module; `plugins/dsh-baton/app` is a licensed vendored DSH bridge snapshot. Preserve upstream provenance and licenses. Nested developer checkouts keep their own Git ownership; updating a distributable snapshot does not migrate those repositories.

After moving this project, check MCP commands/args, installation symlinks, skill CLI examples, and Python editable installations. Rebuild the API virtual environment with `uv sync --locked`; virtual environment entrypoints and editable package paths embed their install location. Rebuild Web bundles after source edits. Preserve browser profiles, sessions, request state, and pending work in their existing external state directories. Use `python3 scripts/check_workspace.py` to inspect local integration without submitting model requests.

Keep credential files, runtime databases, browser profiles, cookies, model transcripts, local backups, virtual environments, dependency caches, and `node_modules` outside version control. Read selected configuration fields as needed; keep secret values out of logs and delegated prompts. Existing credentials are loaded by the bridges from their configured services or environment file.

## Delegation and writing

Read `skills/workhorse/SKILL.md` when assigning substantial implementation or investigation work to DeepSeek. Use dsh-baton directly, provide an explicit working directory and bounded editable area, and retain primary-agent ownership of acceptance and integration. Preserve the effective route/model evidence and verify completion. Parallel writers need separate workspaces or non-overlapping file ownership.

Follow `skills/write-to-me/SKILL.md` for human-facing prose, including README files, repo docs, PR text, and reusable written deliverables. Gemini drafts and revises that prose; Codex retrieves evidence, checks facts, verifies formatting, and writes the accepted result to disk. Materials selected for this route are sent to Google; disclose that transfer. Submit writing through `gemini_write` and retrieve active jobs through `gemini_write_result`; the plugin owns Antigravity CLI → API → AI Studio → Web routing, selected-file adaptation, and model/recovery evidence. Each channel tries 4 Argon (High), 4 Carbon (High), 4 Neon (xHigh), then 3.8 Flash (High). Preserve stable request IDs and inspect the returned route attempts. Use the same plugin's CLI if the current MCP process predates these tools. Codex directly maintains agent instructions, skills, internal plans, handoffs, memory, program logic, routine explanations, and task status. An explicit request for Codex to write takes precedence.

## Verification

- API bridge: from `gemini-baton/`, run `uv sync --locked`, then `.venv/bin/python -m pytest -q`. Tests use fake credentials/backends and include real MCP stdio client checks. Use the installed CLI's `status` for a local, zero-model-call check.
- Web bridge: from `gemini-web-bridge/`, run `npm test` and `npm run check`. The test pre-step builds the committed distribution bundles. Live smoke scripts may submit Gemini requests; inspect their behavior and match the user's authorized scope first. Keep pending-request recovery evidence and the shared concurrency state intact.
- Skills: run the installed Skill Creator's `scripts/quick_validate.py` against each changed skill, then verify installation symlinks and CLI examples.
- Workspace: run `python3 scripts/check_workspace.py`. Review root Git status and nested Web Git status separately. Keep paid/live integration claims tied to the commands actually run.

## Communication and figures

Use direct, affirmative explanations grounded in facts and next steps. Develop conditions and degrees naturally; keep defensive framing, binary rhetoric, repetitive parallel sentences, stock openings, and unsupported claims out of generated text. In mixed Chinese/English files, keep Chinese-English and Chinese-numeral boundaries free of added spaces; preserve normal spacing within English and numeric text.

For plots and figures, configure Arial explicitly, use a white background, and keep exactly one panel per output file. Add figure or panel titles only when requested or essential to interpretation. Use the user's custom palette only when explicitly requested; preserve category-to-color mappings across related figures and obtain the supplied palette values from the current task context.

When a task touches experimental records, protocols, data, analyses, or research deliverables, prioritize the LabForge plugin for retrieval and management. Distinguish measured evidence, proposals, and unfinished work. Report encountered plugin bugs or capability mismatches clearly.
