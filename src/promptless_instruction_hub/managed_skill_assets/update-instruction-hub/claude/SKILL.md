---
name: update-instruction-hub
description: Update this Instruction Hub's marketplace and installed plugins, then reload them in the current Claude Code session. Use when asked to update, refresh, or reload this Instruction Hub.
---

# Update Instruction Hub

## Entry Criteria

- Use the copy of this skill supplied by the PIG plugin for the hub the user wants to update, especially when more than one Instruction Hub is installed.
- Use the generated marketplace name `{{ instruction_hub_marketplace_name }}`; do not derive it from a repository name or location.

## Workflow

1. Refresh the marketplace:

   ```sh
   claude plugin marketplace update {{ instruction_hub_marketplace_name }}
   ```

   If the refresh fails, report the failure and stop before updating plugins.
2. Run `claude plugin list --json`. Select every entry whose `id` ends exactly with `@{{ instruction_hub_marketplace_name }}`, including disabled entries.
3. Update each selected entry with `claude plugin update <id> --scope <scope>`. For `project` or `local` scope, run the command from that entry's `projectPath`. Do not update plugins from other marketplaces. Managed entries may be policy-controlled; report an update refusal without weakening the policy.
4. After every selected plugin has been attempted, reload the active session once with:

   ```text
   /reload-plugins
   ```

   This is a Claude Code slash command, not a shell command. Use the harness-native command channel when it is available. If the agent cannot submit slash commands, ask the user to enter `/reload-plugins`; do not start a second Claude process and do not claim the current session reloaded.

## Output Specification

Report the updated plugin ids and versions, any failures, and the reload result.

## Scope

Update only `{{ instruction_hub_marketplace_name }}` and the installed plugins it supplies. Do not update other marketplaces.
