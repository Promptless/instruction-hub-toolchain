---
name: update-instruction-hub
description: Update this Instruction Hub's marketplace and installed plugins, then reload them in the current Codex session. Use when asked to update, refresh, or reload this Instruction Hub.
---

# Update Instruction Hub

## Entry Criteria

- Use the copy of this skill supplied by the PIG plugin for the hub the user wants to update, especially when more than one Instruction Hub is installed.
- Use the generated marketplace name `{{ instruction_hub_marketplace_name }}`; do not derive it from a repository name or location.

## Workflow

1. Use Codex's current-session `marketplace/upgrade` action with `marketplaceName` set to `{{ instruction_hub_marketplace_name }}`. The action refreshes the marketplace and force-refreshes all installed plugins sourced from it.
2. If the host action is unavailable, report that the Instruction Hub could not be updated and stop. Do not substitute `codex plugin marketplace upgrade`: that CLI command refreshes only the configured Git marketplace snapshot, not installed plugins.
3. After the host action finishes, call `skills/list` once with `forceReload: true` to invalidate and reload skill discovery.
4. Verify the result with `codex plugin list --marketplace {{ instruction_hub_marketplace_name }} --json`.

## Output Specification

Report the installed plugin names, versions, enabled states, failures, and reload result.

## Scope

Update only `{{ instruction_hub_marketplace_name }}` and the installed plugins it supplies. Do not update other marketplaces.
