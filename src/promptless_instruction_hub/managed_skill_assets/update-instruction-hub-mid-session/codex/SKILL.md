---
name: update-instruction-hub-mid-session
description: Update the Promptless Instruction Hub marketplace and its installed plugins, then reload them during an active Codex session. Use when asked to update, refresh, or reload Instruction Hub without ending the session.
---

# Update Instruction Hub Mid-session

Update only `promptless-instruction-hub-marketplace`. Do not change any other marketplace or plugin.

1. Prefer Codex's current-session plugin controls when they are available:
   - Call `marketplace/upgrade` with `marketplaceName` set to `promptless-instruction-hub-marketplace`. This refreshes the marketplace and force-refreshes every installed plugin sourced from it.
   - Then call `skills/list` with `forceReload: true` to invalidate and reload skill discovery.
2. If those host controls are not available to the agent, run:

   ```sh
   codex plugin marketplace upgrade promptless-instruction-hub-marketplace --json
   codex plugin list --marketplace promptless-instruction-hub-marketplace --json
   ```

   The upgrade command refreshes both the configured marketplace snapshot and the installed plugin cache. Treat a nonzero exit as a failure and report it without trying to reinstall unrelated plugins.
3. Verify the final list and report the installed Instruction Hub plugin names, versions, and enabled state.
4. Do not claim that the active Codex process was reloaded if only the CLI fallback was available. Explain that the on-disk marketplace and plugins are current, but the user must use the host's plugin refresh control or begin a new session to reload the active process.
