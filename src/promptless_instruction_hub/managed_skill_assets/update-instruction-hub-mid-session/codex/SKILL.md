---
name: update-instruction-hub-mid-session
description: Use when asked to update, refresh, or reload this Instruction Hub and its installed plugins without ending an active Codex session.
---

# Update Instruction Hub Mid-session

## Entry Criteria

- Use the copy of this skill supplied by the PIG plugin for the hub the user wants to update, especially when more than one Instruction Hub is installed.
- Use the generated marketplace name `{{ instruction_hub_marketplace_name }}`; do not derive it from a repository name or location.

## Workflow

1. Prefer Codex's current-session `marketplace/upgrade` action with `marketplaceName` set to `{{ instruction_hub_marketplace_name }}`. The action refreshes the marketplace and force-refreshes all installed plugins sourced from it.
2. If the host action is unavailable, run:

   ```sh
   codex plugin marketplace upgrade {{ instruction_hub_marketplace_name }} --json
   ```

   Treat a nonzero exit as a failure and report it without trying to reinstall unrelated plugins.
3. After the upgrade finishes, call `skills/list` once with `forceReload: true` to invalidate and reload skill discovery.
4. Verify the result with `codex plugin list --marketplace {{ instruction_hub_marketplace_name }} --json`.
5. If only the CLI fallback was available, do not claim that the active Codex process reloaded. Explain that the on-disk marketplace and plugins are current, but the user must use the host's plugin refresh control or begin a new session to reload the active process.

## Output Specification

Report the installed plugin names, versions, enabled states, failures, and reload result.

## Scope

Update only `{{ instruction_hub_marketplace_name }}` and the installed plugins it supplies. Do not update other marketplaces.
