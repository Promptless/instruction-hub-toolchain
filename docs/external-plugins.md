# External plugin guide

Start with the [README example](../README.md#external-plugins) to add an upstream
plugin to your Hub. The generated PIG plugin also includes an
`add-external-plugin` skill for Claude, Codex, and Cursor.

## Choose the source and targets

Set `source.ref` to a full 40-character commit SHA or `"latest"`. Latest means
the upstream default-branch tip. To use a branch, tag, or hosted release, resolve
it to a full commit SHA first.

Each declared target needs a native manifest beneath its plugin path:

| Target | Required manifest |
| --- | --- |
| `claude` | `.claude-plugin/plugin.json` |
| `codex` | `.codex-plugin/plugin.json` |
| `cursor` | `.cursor-plugin/plugin.json` |

The manifest's `name` must match the catalog `id`. The catalog `name` is display
metadata; it does not override the upstream manifest. Authors, versions, skills,
MCP configuration, and hooks remain upstream-owned.

At least one declared target must be enabled in `hub.yaml`. Only declared,
enabled targets receive marketplace entries. External plugins cannot include
local assets, replace `pig`, or declare Gemini support.

## Resolve and verify commits

Run `pig resolve-external --hub .` to refresh latest sources and verify all
selected external plugins. It resolves each upstream repository once and writes
verified commits to `hub.external-plugins.lock.json`. Commit this generated lock
with the catalog. The lock contains only selected latest plugins.

Catalog definitions retain `source.ref`. Locks, release provenance, and native
marketplaces use `source.sha` for the resolved commit. `pig validate`, `pig build`,
and `pig verify` stay offline; builds and verification require a matching lock
for latest sources. To recheck fixed or locked commits without refreshing the
lock, run `pig verify-external --hub .`.

Both external commands check manifest names, optional SemVer versions, and
declared component paths. They reject symlinks and submodules inside the plugin
and read Git objects without checking out files or running upstream code.
Verification reports each upstream version. It does not validate every native
setting or prove desktop installation. Cursor installation, pin updates, and
rollback still need dogfood validation.

## Publish, update, and roll back

CI `build` and `publish` modes refresh latest sources. `check` mode verifies the
existing lock. Publication uses the verified commit for versioning and every
target, then commits the lock on both source and release branches. Fetch or
verification failures preserve the previous lock and both publish branches.

An upstream change advances the next Hub release without a catalog edit. With
no other Hub changes, an unchanged upstream is a no-op. For a fixed update or
rollback, set `source.ref` to the desired commit SHA and repeat resolution and
verification. Resolution removes unused lock entries.

Latest refreshes when CI runs. Configure a scheduled publish workflow to refresh
automatically. Consumers still use their host's update workflow for installed
plugins.

## Handle Claude version changes

Publication rejects Claude source or path changes that retain the same explicit
upstream version, because Claude may keep its cached plugin. This also applies
when an external plugin replaces an authored plugin of the same name and
version. Choose an upstream commit with a different version. Changing the Hub
version cannot override the upstream manifest; manifests without a version use
the host's commit-based behavior.

When an authored plugin replaces an external Claude plugin, the resolved Hub
release version must differ from the previous upstream version. If the automatic
bump collides, choose a higher version with
`pig set-version --hub . --version <new-version>` before publishing.

Both external commands accept `--previous-release-root /path/to/previous-release`
to check version transitions before publication. For a nested Hub, also pass
`--hub-relative-path <path-within-repository>`.

## Understand release records

The compiler emits native `git-subdir` sources, or `url` for repository-root
plugins, preserving the upstream URL, path, and SHA on both publish branches.
External plugins have no Hub-owned `dist` payload or managed runtime. Consumers
install them from the Hub marketplace without adding the upstream marketplace.

Source and target declarations participate in release versioning. Releases
containing external plugins use manifest schema 3 and record their provenance
in `version_basis.plugins`; authored-only releases use schema 2. The publisher
accepts both. Older toolchains cannot consume schema 3 releases.

The publisher stores verified upstream versions in release-side
`hub.external.json`, bound to the release hash and exact source declarations.
Version comparisons use this record even if the old repository is unavailable.
Releases without this record require fetching the old pin.

## Access private repositories

Use credential-free HTTPS Git URLs. CI and each consuming host need their own
access to private upstream repositories; Hub checkout credentials do not grant
that access.

A commit pins repository content, including declared MCP configuration. Remote
MCP services and dependencies downloaded by upstream code can still change.
