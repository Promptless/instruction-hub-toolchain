---
name: update-instruction-hub-mid-session
description: Update the Promptless Instruction Hub marketplace and all of its installed plugins, then reload them during an active Claude Code session. Use when asked to update, refresh, or reload Instruction Hub without ending the session.
---

# Update Instruction Hub Mid-session

Update only `promptless-instruction-hub-marketplace`. Do not change any other marketplace or plugin.

1. Refresh the marketplace:

   ```sh
   claude plugin marketplace update promptless-instruction-hub-marketplace
   ```

2. Run `claude plugin list --json`. Select every entry whose `id` ends exactly with `@promptless-instruction-hub-marketplace`, including disabled entries.
3. Update each selected entry with `claude plugin update <id> --scope <scope>`. For `project` or `local` scope, run the command from that entry's `projectPath`. Do not update plugins from other marketplaces. Managed entries may be policy-controlled; report an update refusal without weakening the policy.
4. After every selected plugin has been attempted, reload the active session with:

   ```text
   /reload-plugins
   ```

   This is a Claude Code slash command, not a shell command. Use the harness-native command channel when it is available. If the agent cannot submit slash commands, ask the user to enter `/reload-plugins`; do not start a second Claude process and do not claim the current session was reloaded.
5. Report the updated plugin ids and versions, any failures, and the reload result.
