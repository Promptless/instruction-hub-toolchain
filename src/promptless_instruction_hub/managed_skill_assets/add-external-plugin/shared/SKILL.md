---
name: add-external-plugin
description: Add a third-party plugin to this Instruction Hub marketplace, follow its latest upstream commit at publication, or update or roll back a pin. Use when editing the Hub's external plugin catalog; refreshing already-installed plugins is handled by the host's update workflow.
---

# Add an external plugin

Edit the source repository for marketplace `{{ instruction_hub_marketplace_name }}`.
Locate its `hub.yaml` and `plugins/` directory; installed plugin caches and
generated marketplace JSON are build output, not the catalog's source.

## Declare the upstream plugin

Inspect the upstream repository at the intended commit. Use a credential-free
HTTPS Git URL ending in `.git`. Choose a full, 40-character `sha` for a fixed
commit, or `ref: latest` to follow the default branch at each Hub publication.
Each target needs its own native manifest beneath the chosen plugin directory:

| Target | Required manifest |
| --- | --- |
| `claude` | `.claude-plugin/plugin.json` |
| `codex` | `.codex-plugin/plugin.json` |
| `cursor` | `.cursor-plugin/plugin.json` |

Create `plugins/<id>.yaml` using the upstream manifest's exact `name` as `id`.
For example, after replacing the SHA placeholder:

```yaml
kind: external
id: doc-detective
name: Doc Detective
source:
  type: git
  url: https://github.com/doc-detective/agent-tools.git
  sha: "<full-40-character-commit-sha>"
targets:
  claude:
    path: plugins/doc-detective
  codex:
    path: plugins/doc-detective
  cursor:
    path: plugins/doc-detective
```

To follow latest, replace the `sha` line with `ref: latest`; never set both.
Latest means the upstream default-branch tip, not the newest tag or release.
Other branch and tag names must be resolved to a full SHA for a fixed pin.

Paths are relative to the upstream repository; use `.` for a repository-root
plugin. Declare only targets the upstream supports. Gemini external plugins are
unsupported. At least one declared target must also be enabled in `hub.yaml`.

Append the ID to `hub.yaml`'s `stable_plugins`, preserving the existing entries,
including the required authored `pig` plugin. External plugins cannot replace
`pig` or use `includes`. Their skills, hooks, MCP configuration, and versions
stay upstream-owned; there is no vendored `assets/` or `dist/` payload to edit.

## Verify and deliver

From the Hub source directory, run the toolchain CLI:

```bash
pig validate --hub .
pig resolve-external --hub .
pig verify --hub .
```

If `pig` is unavailable, use
`uvx --from git+https://github.com/Promptless/instruction-hub-toolchain.git pig`
in its place, using the Hub's configured toolchain revision when pinned.

`resolve-external` resolves latest once per repository, then checks enabled
targets' manifest names, optional SemVer versions, and declared component paths. It rejects
symlinks and submodules inside the plugin without running upstream code. CI and
each consuming host need their own access to private upstream repositories.
For latest sources it writes `hub.external-plugins.lock.json` only after all
checks pass; commit this generated lock with the catalog changes. `validate`,
`build`, and `verify` are offline. Builds and verification require a matching
lock when latest is selected. `pig verify-external --hub .` rechecks fixed or
locked commits without refreshing the lock or changing the worktree.

CI build and publish modes refresh latest, while check mode verifies the lock.
Each publication records the resolved SHA in both marketplaces and release
provenance, leaving `ref: latest` in source for the next publication. An upstream
change advances the Hub release. Latest does not schedule publication or force
desktop updates. For automatic refreshes, configure the Hub's publish workflow
to run on a schedule. Consumers use their host's update workflow to refresh
installed plugins.

For fixed updates or rollback, replace `source.ref` (if present) with `source.sha`
at the desired reviewed commit and repeat these checks. Resolution removes
unused lock entries. Publication compares the previous release and rejects
Claude source or path changes that keep the same explicit upstream version,
because Claude may retain its cached plugin. Select a commit with a different
upstream version; changing the Hub version does not override it. When a previous
release checkout is available, run `pig resolve-external --hub .
--previous-release-root /path/to/previous-release` to check this before CI.
For a nested Hub, also pass `--hub-relative-path <path-within-repository>`.

Follow the repository's PR and publishing workflow. The shared publisher writes
native Git source entries to both source and release marketplaces while
preserving the upstream URL, path, and SHA. Report structural verification
separately from desktop installation and refresh. Cursor pin installation,
updates, and rollback still need dogfood validation. A pinned repository also
does not freeze services behind remote MCP URLs.
