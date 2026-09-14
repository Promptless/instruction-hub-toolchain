```
             ,-,------,
              _ \(\(_,--'
         <`--'\>/(/(__
         /. .  `'` '  \
        (`')  ,        @
         `-._,        /
            )-)_/--( >
           ''''  ''''

pig.
```

# Promptless Instruction Hub Toolchain

This repository is the canonical public toolchain for Promptless Instruction
Hub repositories. It bundles the Python compiler and exposes reusable GitHub
workflows, a GitLab CI template, and a composite GitHub Action for validating,
building, and publishing generated hub artifacts.

## Usage

Use the [GitHub workflows](#github-actions) or [GitLab template](#gitlab-ci) to
keep toolchain setup in this repository. For custom GitHub jobs, see
[direct action usage](#direct-action-usage).

### GitHub Actions

Add these two caller files to your hub repository. They use GitHub's
[reusable workflow syntax](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows),
with `uses` on the job. The shared workflows handle checkout and toolchain setup.

`.github/workflows/instruction-hub-check.yml`:

```yaml
name: Check Instruction Hub

on:
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  instruction-hub:
    uses: Promptless/instruction-hub-toolchain/.github/workflows/pr-check.yml@main
```

`.github/workflows/instruction-hub-publish.yml`:

```yaml
name: Publish Instruction Hub

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: instruction-hub-release
  cancel-in-progress: false

jobs:
  instruction-hub:
    if: github.ref == 'refs/heads/main'
    uses: Promptless/instruction-hub-toolchain/.github/workflows/publish.yml@main
    with:
      source-branch: main
```

The [check workflow](.github/workflows/pr-check.yml) validates and builds the hub
without committing generated files. The [publish workflow](.github/workflows/publish.yml)
publishes generated artifacts to `release/stable` and updates marketplace
pointers on the source branch for the configured Claude, Codex, and Cursor targets.
The caller above runs publication on pushes to `main` or manual runs on `main`,
and its concurrency group prevents overlapping publish runs.

Publishing uses the caller repository's automatic `GITHUB_TOKEN`; no additional
secret is required. Grant `contents: write` as shown and ensure repository rules
allow that token to push to both the source and release branches.

All workflow inputs are optional. Set them under the calling job's `with`:

| Input | Workflow | Default | Purpose |
| --- | --- | --- | --- |
| `hub-root` | Both | `.` | Hub directory within the checked-out repository. |
| `mode` | Check | `build` | Use `check` only when generated artifacts are committed alongside source. |
| `source-branch` | Publish | `main` | Branch allowed to publish and receive marketplace pointer updates. |
| `release-branch` | Publish | `release/stable` | Branch that receives generated artifacts; must differ from `source-branch`. |

If your source branch is not `main`, update `push.branches`, the job's `if`, and
`source-branch` together. For a hub in a subdirectory, set `hub-root` in both
callers.

Use `@main` in both workflow references to follow the latest merged toolchain.
Each reusable workflow checks out the compiler using the ref in its
caller's `uses`; there is no separate `toolchain-ref` input on GitHub.

### GitLab CI

For a hub at the repository root, add this to `.gitlab-ci.yml`:

```yaml
include:
  - remote: https://raw.githubusercontent.com/Promptless/instruction-hub-toolchain/main/templates/gitlab/instruction-hub.yml
```

The [template](templates/gitlab/instruction-hub.yml) validates and builds hub
changes in merge requests. Default-branch pushes that change hub source or CI
configuration publish to `release/stable` and update the Claude, Codex, and Cursor
marketplace pointers. Manually started pipelines validate on every branch and
also publish on the default branch. Publishing is serialized, skips superseded
hub source, and includes pointer commits from earlier jobs before pushing.

Use a Linux runner that supports `image:`. Enable **Settings > CI/CD > Job token
permissions > Allow Git push requests to the repository** in the consuming
project. The user starting the pipeline must be allowed to push to the default
and release branches. Publishing uses `CI_JOB_TOKEN`; its pushes do not trigger
another pipeline. No GitHub token is needed.

The template scopes its image and variables to its own jobs. All inputs are
optional and go under the remote include's `inputs`:

| Input | Default | Purpose |
| --- | --- | --- |
| `toolchain-ref` | `main` | Leave as `main` to use the latest merged compiler. |
| `release-branch` | `release/stable` | Branch that receives generated artifacts; must differ from the default branch. |
| `check-stage` | `test` | Existing pipeline stage for validation. |
| `publish-stage` | `deploy` | Existing pipeline stage for publishing. |

For a pipeline with custom stages, use `include:inputs`:

```yaml
stages: [verify, publish]
include:
  - remote: https://raw.githubusercontent.com/Promptless/instruction-hub-toolchain/main/templates/gitlab/instruction-hub.yml
    inputs:
      check-stage: verify
      publish-stage: publish
      release-branch: release/stable
```

The compiler defaults to `main`: each job fetches the latest merged toolchain
when it starts and logs the resolved commit for diagnostics. Keep both the
remote template URL and `toolchain-ref` on `main`. The template runs the same `scripts/run.sh` entrypoint as the
GitHub Action, with GitLab workspace, repository, identity, and branch checks
supplied by the template.

### Direct action usage

Use the [composite action](action.yml) in a custom GitHub job when you need its
additional inputs, such as generated paths or marketplace pointer controls.
This publish job belongs in a workflow triggered from your source branch:

```yaml
jobs:
  instruction-hub:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: Promptless/instruction-hub-toolchain@main
        with:
          mode: publish
          source-branch: main
          github-token: ${{ github.token }}
```

Keep `fetch-depth: 0` for publication and pass the token as shown when checkout
credentials are not persisted. Use `@main` for the latest merged action and
compiler.

The action runs the bundled compiler directly:

```bash
uv run --project "$GITHUB_ACTION_PATH" promptless-instruction-hub <command>
```

### Local verification

Before publishing a source change, run the full non-mutating compilation:

```bash
pig verify --hub .
```

`pig verify` validates every stable asset and renders every stable plugin for
every configured target in an ephemeral directory. It does not create, update,
or compare generated files in the source worktree, whether verification
succeeds or fails. Use `pig build --check` instead when the repository
intentionally commits generated artifacts and must prove they are current.

## Development

Run the full test suite in parallel:

```bash
uv run pytest tests -n auto --dist worksteal
```

Omit the parallel flags when running a single test file or a `-k` selection.

## Marketplace and plugin identities

`hub.yaml` declares the marketplace's literal ID and display name:

```yaml
org: Promptless
marketplace:
  id: promptless-instruction-hub
  name: Promptless Instruction Hub
version: 0.1.0
stable_plugins: [pig, dev]
targets: [claude, codex, gemini, cursor]
```

Each file in `plugins/*.yaml` defines one plugin, using its own literal ID:

```yaml
# plugins/dev.yaml
id: dev
name: Promptless Dev
includes:
  - skill:review-docs
```

The skill in this example must exist in `assets/skills/review-docs/`. Keep the
required `plugins/pig.yaml` created by `pig init` in `stable_plugins`.

The compiler uses `marketplace.id` as the marketplace name and each plugin's
`id` as its native name. It adds no prefix or suffix. IDs use lowercase letters,
digits, and hyphens, with a letter or digit at each end. Plugin display names
come from `name` where the target supports them. Output goes to
`dist/{target}/{plugin.id}/`. Gemini receives extensions with the same plugin
IDs; it has no generated marketplace manifest.

`pig init --org Acme` defaults to marketplace ID `acme-instruction-hub` and
display name `Acme Instruction Hub`. Override those with `--marketplace-id` and
`--marketplace-name`. `version` is the hub release version shared by all compiled
plugins. Publication compares generated output at the previous release version:
changed output advances the patch version, while unchanged output keeps it.
Set a higher `version` in `hub.yaml` to request a specific release, such as a
minor or major bump. Publication never lowers the version.

The publisher writes the resolved version back to `hub.yaml` and commits it
with the source marketplace pointers. It pushes the source and release commits
in one atomic Git transaction with explicit leases on both branch revisions.
If either branch changes during the build, or either update is rejected, neither
publish update lands. Rerun from the latest source branch after a race. The Git
server must support atomic pushes, and the publisher needs write access to both
branches. Publish requires committed source files and a clean index.

An unchanged rerun creates no commits. If only one branch needs a content change,
the other receives an empty recording commit so Git checks both leases. Merely
writing the resolved version back does not cause another version bump.

### Migrating existing hubs

This is a breaking source configuration and installed plugin identity change.
Coordinate the toolchain upgrade with the hub migration. Validation rejects
legacy fields and the old `packages/` directory with migration guidance.

1. Replace root `plugin_id` and `plugin_name` with `marketplace.id` and
   `marketplace.name`. To keep an existing marketplace registration, set the new
   ID to its previous generated name: the old `plugin_id` plus `-marketplace`.
   The compiler now uses that value verbatim.
2. Move `packages/` to `plugins/` and rename `stable_packages` to
   `stable_plugins` in `hub.yaml`. Keep each definition's `id`, `name`, and
   `includes`. Update custom CI path filters and scripts that reference the old
   directory or `pig init --plugin-id` / `--plugin-name` flags.
3. Rename `plugin_version` to `version` in `hub.yaml` and use `--version`
   instead of `--plugin-version` in scripts. Version 2 release manifests use
   top-level `version`, `marketplace`, `stable_plugins`, and `version_basis.plugins`.
   Version 1 manifests are rejected. Coordinate a one-time rebuild of existing
   release artifacts with the source migration before resuming publication.
4. Run `pig verify --hub .`, then publish using the upgraded toolchain.
5. Refresh the marketplace and replace installed plugins using their new IDs.
   For example, `promptless-instruction-hub-dev` becomes `dev`. Remove the old
   installation so its skills and hooks are not loaded alongside the new one.
   An updater cannot infer that these different plugin IDs are replacements.

To distinguish plugins from multiple hubs in a host that uses plugin names as
skill namespaces, choose explicit IDs such as `acme-dev` for customer plugins.
The compiler never adds that prefix automatically. The managed PIG plugin
continues to require the ID `pig`.

`hub.release.json` and `hub.stable.json` both use `schema_version: 2` and the
same top-level `version`. Runtime enrollment metadata still uses `plugin_version`
for the installed plugin's version and `package_id` for the source plugin ID.
Runtime `plugin_id` matches the literal ID in the native plugin manifest.

## Modes

- `build`: validate the hub and run a build without committing generated files.
- `check`: validate the hub and fail if committed generated output is stale.
- `publish`: build generated output from `source-branch` and atomically update
  the release branch together with the source version and marketplace pointers.

Customer hubs should usually use `build` for pull requests and `publish` after
changes merge to the default branch. Use `check` only for repositories that
intentionally commit generated artifacts on the same branch as source assets.

Cursor source-branch pointers use the existing `type: github` descriptor for
`github.com`. Other hosts, including GitLab, use a `git-subdir` source with the
repository URL, plugin path, and release branch. This preserves GitLab subgroup
paths and supports hubs inside a repository subdirectory. Release-branch Cursor
marketplaces continue to use relative plugin paths.

GitLab CI adapters should map `GITHUB_SERVER_URL` to `CI_SERVER_URL` and
`GITHUB_REPOSITORY` to `CI_PROJECT_PATH`. `INPUT_UPDATE_CURSOR_POINTER` enables
the GitLab pointer writer. Keep `INPUT_GITHUB_TOKEN` unset and use GitLab checkout
credentials for publishing.

Cursor advertises GitLab imports through its
[team marketplaces](https://cursor.com/changelog/customize). Manifest generation
does not verify that import path: Cursor needs its own repository access, and
some desktop importers still reject GitLab URLs. Verify installation and refresh
through the team's GitLab-capable importer before relying on Cursor delivery.

Every hub must keep the canonical `pig` plugin in `stable_plugins`. `pig init`
scaffolds that plugin as the home for scanned shared instructions and the
optional Promptless-managed lifecycle integration. Other customer instruction plugins
do not receive managed hooks or runtime files.

## Hub File Layout

Instruction Hub source config lives at `hub.yaml` in the hub root. Build-generated
release metadata is also flat at the hub root:

- `hub.release.json`: current release manifest.
- `hub.stable.json`: stable channel pointer.

Scan-generated metadata is committed as a root file:

- `hub.repo-context.json`: scanned repository-context inventory.

Native Cursor rules can be authored as `assets/rules/<id>.mdc` with a matching
`<id>.asset.yaml` sidecar declaring `support.cursor.mode: native`. The compiler
preserves existing rule frontmatter, including `description`, `globs`, and
`alwaysApply`. Rules without frontmatter receive the generated description and
`alwaysApply: false` defaults.

Every generated plugin embeds local metadata as root files inside each plugin:

- `hub.release.json`: plugin-local release/status manifest.
- `hub.managed-runtimes.json`: Promptless-managed runtime metadata for plugins
  that include managed-runtime artifacts.

### Agent definitions as Codex skills

An agent can ship as a native Claude subagent and a Codex skill from the same
Markdown source. Set its adjacent `<id>.asset.yaml` file to:

```yaml
support:
  claude:
    mode: native
  codex:
    mode: agent-skill
```

Keep the `agent:<id>` reference in the plugin's `includes` list. The compiler
writes `skills/<id>/SKILL.md` into the Codex plugin and registers it as a skill.
Release metadata retains the source agent identity. Conversion is opt-in,
Codex-only, and accepts `.md` files; directory-based agents are not converted.

The generated skill contains the full authored procedure and instructions to
delegate it to one child. An assigned specialist executes the procedure directly
without delegating the same role again. The parent relays clarification questions
and answers, then returns the result. If subagent tools are unavailable, the
instructions require stopping. The model must follow these instructions; Codex has
no documented skill frontmatter that enforces subagent execution. See the
[Codex skill documentation](https://developers.openai.com/codex/skills).

Source frontmatter must have a nonempty `description` of at most 1,024 characters.
Use a shared portable description and put invocation examples in the body. The
generated skill name is the asset ID. Optional `name`, `model`, and `color` fields
are accepted; the generated skill omits model and color settings and tells the
parent to launch the child without model or reasoning overrides.

`tools` and `disallowedTools` accept comma-separated strings or YAML lists.
They become advisory instructions with their original tool names; the compiler
does not map names between hosts. An empty `tools` list means the specialist
must not use tools. `pig validate`, `pig verify`, and `pig build` print one warning
per included converted agent that declares either field. Codex does not enforce
these restrictions through skill metadata. Write required behavior in the shared
procedure without claiming that a host enforces it.

Conversion rejects other frontmatter fields, invalid descriptions, and skill
destination collisions, including names reserved for compiler-managed skills.
The compiler does not truncate descriptions or rewrite procedures. The
[implementation plan](docs/exec-plans/2026-09-13-codex-agent-skills.md) records the
design and verification scope.

The old `.promptless/instruction-hub.yaml` and generated `.promptless/...`
layout is not read or migrated by this toolchain. Existing hubs must rename
their config to `hub.yaml` and regenerate output with `pig build`.

## Release Model

Hubs follow the latest merged toolchain on `main`. GitHub callers use `@main`;
GitLab callers use the `/main/` template URL and the default `toolchain-ref: main`.
Resolved commit hashes in CI logs identify the compiler used for a build.

## Managed PIG Assets

The toolchain injects the harness-specific `update-instruction-hub`
skill into the canonical `pig` plugin for Codex and Claude. Each generated copy
is scoped to its hub's generated marketplace name, so customer-specific names,
repository URLs, and checkout locations are not hardcoded. Each hub's generated
PIG plugin receives its own scoped copy. Codex uses its marketplace upgrade and
skill refresh host operations, stopping if those current-session actions are
unavailable; Claude updates each installed plugin at its original scope and uses
`/reload-plugins` to apply the changes without restarting.

### Trace ingestion

Instruction Hubs can publish and install instructions without an ingestion worker.
New hubs created by `pig init` explicitly disable managed trace ingestion in `hub.yaml`:

```yaml
trace_ingestion:
  enabled: false
```

With ingestion disabled, the toolchain emits no managed enrollment hooks, runtime
bundle, or managed-runtime metadata. Authored skills, agents, rules, commands,
hooks, MCP configuration, and the Claude/Codex update skill remain available.
Verification and publishing do not require worker credentials or worker access.

Omitting `trace_ingestion` or `enabled` also disables ingestion. Existing hubs
that use ingestion must explicitly set `enabled: true`. Enabling ingestion
bundles the existing Claude/Codex host runtime; it does not
provision a worker. Cursor and Gemini do not receive that managed runtime.

After changing this setting, publish the hub and refresh its installed plugins.
Disabling it removes managed hooks from the new release; an older installed
plugin keeps its hooks until refreshed. It does not delete previously ingested
data or change a worker deployment.

### Managed Host Runtime

When `trace_ingestion.enabled` is true, the toolchain owns Promptless-managed runtime artifacts that are injected into
the canonical `pig` plugin, including the host runtime used by Codex and
Claude lifecycle hooks. Other generated plugins receive no toolchain-managed
runtime or lifecycle hooks. During dogfood, generated Codex hooks wrap the bundled
stdlib-only Python runtime with POSIX shell checks. The stable executable in
`runtime/` delegates to private sibling modules that separate CLI dispatch,
enrollment, trace collection, host configuration, persistence, and output.
Generated Claude hooks use Claude Code's exec-form hook so Windows installs do
not need a POSIX shell; Node must be available to start the inline launcher.
Every SessionStart launcher starts one detached supervisor that inherits the
hook input and redirects background output away from the agent transcript. The
Claude supervisor collects both Claude Code and any detected Claude Desktop
sources.
Startup launchers emit schema-safe diagnostics when the host cannot resolve the
plugin root, a readable managed runtime bundle (the launcher plus its sibling
package and CLI entry module), or Python 3.9+. Terminal lifecycle launchers stay
quiet: they resolve a complete runtime bundle under the plugin root, fall back
to a complete sibling installed version with the same runtime-bundle layout for
the same plugin id when the recorded root is stale or incomplete, and exit 0
with no output when no usable bundle exists.

```sh
sh -c 'root=${PLUGIN_ROOT:-}; ...; find python3/python/py; run promptless-host-runtime session-start --host codex --detach'
sh -c 'root=${PLUGIN_ROOT:-}; ...; find same-plugin sibling runtime if needed; run promptless-host-runtime collect --host codex --lifecycle stop --detach --quiet'
sh -c 'root=${PLUGIN_ROOT:-}; ...; find same-plugin sibling runtime if needed; run promptless-host-runtime collect --host codex --lifecycle session_end --detach --quiet'
node -e '... resolve ${CLAUDE_PLUGIN_ROOT}; find Python 3.9+; run promptless-host-runtime session-start --host claude --detach' '${CLAUDE_PLUGIN_ROOT}'
node -e '... resolve ${CLAUDE_PLUGIN_ROOT}; find same-plugin sibling runtime if needed; run promptless-host-runtime collect --host claude --lifecycle session_end --detach --quiet' '${CLAUDE_PLUGIN_ROOT}'
```

The dogfood host runtime uses `PROMPTLESS_WORKER_BASE_URL` or the default
production worker. It reads the worker's public `/healthz` identity, opens the
hosted Promptless dashboard start URL, and listens on a loopback callback with a
per-attempt state token for the approved session proof. It then polls the hosted
runtime for a one-time per-host credential, caches that credential, and uses the
host credential to fetch `/v0/host-enrollment/policy?target=...` and post
`/v0/host-enrollment/check-ins`.

SessionStart never waits for browser approval, worker requests, trace discovery,
or the upload ledger. It launches one detached supervisor, emits and claims any
already-pending plugin-update, first-enrollment, and internal-user notices using
local state only, and returns. The supervisor runs enrollment and reconciliation
before collecting Claude Code and Claude Desktop sequentially for Claude, or the
single native source family for other hosts. On Linux, enrollment does not invoke a browser
when `DISPLAY`, `WAYLAND_DISPLAY`, `MIR_SOCKET`, and `WSL_INTEROP` are all
absent; set `PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER=1` to force a browser
attempt or `0` to disable one explicitly. Detached enrollment outcomes remain
available in `~/.promptless/instruction-hub/last-bootstrap-status.json` and the
bounded `host-runtime-diagnostics.jsonl` log.

#### Native trace collection

The worker's per-source watermark is authoritative after an ambiguous upload.
When a committed response is lost and the local file grows before retry, the
worker returns its watermark with a digest for the committed source range. The
runtime verifies that digest against the current local bytes, advances only to
an interior worker watermark, and rebuilds the remaining upload in the same
hook run. The local ledger also records a digest for every acknowledged prefix
so replacement or rotation at the same path cannot silently mix two source
generations. It does not reconcile gaps, rewinds, changed source identities, or
conflicts for another range. Upload requests contain one source chunk because
the worker commits one chunk per transaction; this keeps the request-level
acknowledgement at the same atomic boundary.

The runtime uploads native host transcript JSONL ranges to
`/v0/traces/batches?target=...`. Claude Code, Codex, and Claude Desktop share one
uploader and forward-only ledger. The ledger lives at
`~/.promptless/instruction-hub/host-runtime-ledger.json` or
`PROMPTLESS_HOST_RUNTIME_LEDGER` when set. Uploads use the host credential and
are gated by the `enabled_hosts` policy. Codex idle discovery scans only
`CODEX_HOME/sessions/**/*.jsonl` and
`CODEX_HOME/archived_sessions/**/*.jsonl`. Hook-provided current transcript
paths remain eligible outside those roots.

SessionStart hooks launch one quiet `ensure`-then-collection supervisor. They
include active files so pre-existing history is uploaded from byte zero when a
source has no acknowledged offset. Terminal lifecycle hooks (`Stop`,
`SessionEnd`, and `SubagentStop`) run collection only. Hook input accepts
snake_case, camelCase, and nested
`session`/`transcript`/`agent` transcript references from Codex- and
Claude-style hooks. Claude Desktop has no hook-provided current transcript and
starts with idle catch-up.

Hook timeouts cover the launcher, while collection runs in a detached process.
Terminal hooks use a shared 3-second launcher budget on every host, which also
fits Codex's `SessionEnd` maximum. Startup hooks use 30 seconds.

A collection follows this order:

```text
upload at most one pending current-transcript request
    -> start a fresh 25-second catch-up deadline
    -> upload remaining current-transcript ranges
    -> scan and upload idle transcripts
```

The first pending current-transcript request receives its own fixed 25-second
deadline before the catch-up clock starts. Contention or exhausting that budget
reports `trace_upload_partial` for a later hook to resume. Remaining current-
transcript work, idle discovery, and idle uploads share the fresh catch-up
deadline, configurable with
`PROMPTLESS_HOST_RUNTIME_COLLECT_DEADLINE_SECONDS`.

Each request is one ledger transaction:

```text
lock -> reload ledger -> select request -> post -> validate acknowledgement
     -> persist acknowledged offsets -> unlock
```

Policy reads and transcript-root scans run without the ledger lock. The ledger
advances only after the worker acknowledges the exact source ranges and content
hashes. Releasing and reloading the ledger between requests preserves progress
from other collectors. When the catch-up deadline expires, collection
reports `trace_upload_partial` and resumes from the acknowledged offsets on a
later hook.

Source ranges target 4 MiB and end on complete-record boundaries. Serialized
requests target 6 MiB and never exceed 10 MiB; sizing includes chunks and request
metadata. Each batch carries the currently installed `plugin_version`, which is
treated as the version associated with every byte in that batch.

#### Collection safety

An unseen source starts at byte zero. A known source resumes at its last
worker-acknowledged offset, including offsets written by earlier runtime
versions. Plugin updates immediately use the new collection code, but do not
rewind those existing offsets; intentionally skipped prefixes therefore remain
grandfathered unless the ledger is reset. Obsolete baseline and release-marker
fields are discarded when an older ledger is next rewritten.

Collection runs detached from the hook process group. Quiet collection writes
no status JSON to hook stdout. A source that vanishes or loses read permission
mid-collect is recorded as drift and surfaced through `unreadable_source_count`;
it does not block later sources. Support diagnostics are bounded, redacted JSONL
at `~/.promptless/instruction-hub/host-runtime-diagnostics.jsonl` with `0600`
permissions and no transcript content, tool inputs, or credentials. Detached
launch and nonzero-exit failures are also recorded in the structured
`last-bootstrap-status.json` support status.

Host enrollment is per host, not per installed `pig` version. The credential
and pending approval are cached at a single host-global path
(`~/.promptless/instruction-hub/`) and keyed only on the worker deployment and
agent host (claude/codex). A non-blocking, per-credential enrollment-leader lock
ensures that overlapping host starts or plugin upgrades drive at most one browser
approval while the others reuse the result or defer to a later session. The
per-plugin `CLAUDE_PLUGIN_DATA`/`PLUGIN_DATA` directories are intentionally not
used for this state.

Native JSONL ledgers are the only telemetry source: the runtime writes no OTel
exporter config for either host. Hosts configured by earlier managed bootstraps
have that config removed on the next `ensure` run — the managed `[otel]` block
in Codex `config.toml` and the marker-owned `OTEL_*`/telemetry env keys in
Claude `settings.json` are deleted (with a timestamped backup), while unmanaged
user config is never touched. The hosted policy's legacy `collector` section is
ignored.

The host runtime has one executable entrypoint with subcommands. `session-start`
detaches one `ensure`-then-collection supervisor. `ensure` enrolls when needed,
removes legacy managed telemetry config, and posts a check-in. `collect` is the
native JSONL upload path; hooks pass `--detach` so the runtime supervises
collection outside the hook process group. Pass `--include-active` for a
user-initiated sweep that includes files still inside the idle grace period.
`enroll` acquires only
the host credential. `status` prints local JSON without network,
browser, config writes, or check-ins. `reset --yes` clears cached host
credentials and pending enrollments while preserving the stable host id,
last-seen plugin versions, and one internal welcome marker per installed
marketplace version. `version` reports runtime metadata.

Before the customer-grade release, replace the dogfood Python implementation
with a static native binary built and versioned by Promptless, then bundled into
the toolchain release. Customer Instruction Hub repositories should not need
Python, Node, uv, Go, Rust, curl, jq, or other runtime/build dependencies installed
for the hook to run. Customer builds should only consume the already-built
Promptless artifact bundle that the toolchain copies into plugin `runtime/`.

The dogfood runtime trusts the authenticated TLS worker response and validates
only the hosted policy shape. The customer-grade static binary must verify an
asymmetric hosted-policy signature with a pinned Promptless public key before it
edits local host config.
