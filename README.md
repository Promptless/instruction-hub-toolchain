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
Hub repositories. It bundles the Python compiler and exposes a
composite GitHub Action for validating, building, and publishing generated hub
artifacts.

## Usage

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
      - uses: Promptless/instruction-hub-toolchain@v0
        with:
          mode: publish
          source-branch: main
          github-token: ${{ github.token }}
```

The action runs the bundled compiler directly:

```bash
uv run --project "$GITHUB_ACTION_PATH" promptless-instruction-hub <command>
```

Before publishing a source change, run the full non-mutating compilation:

```bash
pig verify --hub .
```

`pig verify` validates every stable asset and renders every stable package for
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

## Modes

- `build`: validate the hub and run a build without committing generated files.
- `check`: validate the hub and fail if committed generated output is stale.
- `publish`: build generated output from `source-branch`, push it to
  `release/stable`, and update `source-branch` marketplace pointers for
  generated targets.

Customer hubs should usually use `build` for pull requests and `publish` after
changes merge to the default branch. Use `check` only for repositories that
intentionally commit generated artifacts on the same branch as source assets.

Every hub must keep the canonical `pig` package in `stable_packages`. `pig init`
scaffolds that package as the home for scanned shared instructions and the
Promptless-managed lifecycle integration. Other customer instruction packages
do not receive managed hooks or runtime files.

## Hub File Layout

Instruction Hub source config lives at `hub.yaml` in the hub root. Build-generated
release metadata is also flat at the hub root:

- `hub.release.json`: current release manifest.
- `hub.stable.json`: stable channel pointer.

Scan-generated metadata is committed as a root file:

- `hub.repo-context.json`: scanned repository-context inventory.

Every generated plugin embeds local metadata as root files inside each plugin:

- `hub.release.json`: plugin-local release/status manifest.
- `hub.managed-runtimes.json`: Promptless-managed runtime metadata for plugins
  that include managed-runtime artifacts.

The old `.promptless/instruction-hub.yaml` and generated `.promptless/...`
layout is not read or migrated by this toolchain. Existing hubs must rename
their config to `hub.yaml` and regenerate output with `pig build`.

## Release Model

Action releases are tagged with immutable versions such as `v0.1.0` and a moving
major pointer such as `v0`. Customer workflows can use `@v0` for minor updates or
pin to an immutable tag for stricter reproducibility.

## Managed PIG Assets

The toolchain injects the harness-specific `update-instruction-hub`
skill into the canonical `pig` plugin for Codex and Claude. Each generated copy
is scoped to its hub's generated marketplace name, so customer-specific names,
repository URLs, and checkout locations are not hardcoded. Each hub's generated
PIG plugin receives its own scoped copy. Codex uses its marketplace upgrade and
skill refresh host operations, stopping if those current-session actions are
unavailable; Claude updates each installed plugin at its original scope and uses
`/reload-plugins` to apply the changes without restarting.

### Managed Host Runtime

The toolchain owns Promptless-managed runtime artifacts that are injected into
the canonical `pig` plugin, including the host runtime used by Codex and
Claude lifecycle hooks. Other generated plugins receive no toolchain-managed
runtime or lifecycle hooks. During dogfood, generated Codex hooks wrap the bundled
stdlib-only Python runtime with POSIX shell checks. The stable executable in
`runtime/` delegates to private sibling modules that separate CLI dispatch,
enrollment, trace collection, host configuration, persistence, and output.
Generated Claude hooks use Claude Code's exec-form hook so Windows installs do
not need a POSIX shell; Node must be available to start the inline launcher.
Every launcher starts its primary host collection in a detached process that
inherits the hook input and redirects collection output away from the agent
transcript. The sibling Claude Desktop baseline starts without hook input.
Startup launchers emit schema-safe diagnostics when the host cannot resolve the
plugin root, a readable managed runtime bundle (the launcher plus its sibling
package and CLI entry module), or Python 3.9+. Terminal lifecycle launchers stay
quiet: they resolve a complete runtime bundle under the plugin root, fall back
to a complete sibling installed version with the same runtime-bundle layout for
the same plugin id when the recorded root is stale or incomplete, and exit 0
with no output when no usable bundle exists.

```sh
sh -c 'root=${PLUGIN_ROOT:-}; ...; find python3/python/py; run promptless-host-runtime ensure --host codex --prepare-baseline; run promptless-host-runtime collect --host codex --lifecycle session_start --baseline --detach --quiet'
sh -c 'root=${PLUGIN_ROOT:-}; ...; find same-plugin sibling runtime if needed; run promptless-host-runtime collect --host codex --lifecycle stop --detach --quiet'
sh -c 'root=${PLUGIN_ROOT:-}; ...; find same-plugin sibling runtime if needed; run promptless-host-runtime collect --host codex --lifecycle session_end --detach --quiet'
node -e '... resolve ${CLAUDE_PLUGIN_ROOT}; find Python 3.9+; run promptless-host-runtime ensure --host claude --prepare-baseline; run promptless-host-runtime collect --host claude --lifecycle session_start --baseline --detach --quiet; best-effort run promptless-host-runtime ensure --host claude-desktop --if-sources --prepare-baseline; then collect --host claude-desktop only if ensure succeeds' '${CLAUDE_PLUGIN_ROOT}'
node -e '... resolve ${CLAUDE_PLUGIN_ROOT}; find same-plugin sibling runtime if needed; run promptless-host-runtime collect --host claude --lifecycle session_end --detach --quiet' '${CLAUDE_PLUGIN_ROOT}'
```

The dogfood host runtime uses `PROMPTLESS_WORKER_BASE_URL` or the default
production worker. It reads the worker's public `/healthz` identity, opens the
hosted Promptless dashboard start URL, and listens on a loopback callback with a
per-attempt state token for the approved session proof. It then polls the hosted
runtime for a one-time per-host credential, caches that credential, and uses the
host credential to fetch `/v0/host-enrollment/policy?target=...` and post
`/v0/host-enrollment/check-ins`.

#### Native trace collection

The runtime uploads native host transcript JSONL ranges to
`/v0/traces/batches?target=...`. Claude Code, Codex, and Claude Desktop share one
uploader and forward-only ledger. The ledger lives at
`~/.promptless/instruction-hub/host-runtime-ledger.json` or
`PROMPTLESS_HOST_RUNTIME_LEDGER` when set. Uploads use the host credential and
are gated by the `enabled_hosts` policy.

SessionStart hooks run `ensure` and then a quiet first baseline for each host.
Terminal lifecycle hooks (`Stop`, `SessionEnd`, and `SubagentStop`) run
collection only. Hook input accepts snake_case, camelCase, and nested
`session`/`transcript`/`agent` transcript references from Codex- and
Claude-style hooks. Claude Desktop has no hook-provided current transcript and
starts with idle catch-up.

A collection follows this order:

```text
persist the SessionStart release marker, when applicable
    -> enforce the baseline gate; a first baseline records offsets and stops
    -> upload at most one pending current-transcript request
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

Policy reads and transcript-root scans run without the ledger lock. Releasing
the lock between requests lets SessionStart persist a release marker during
catch-up; the next request reloads that marker instead of overwriting it from
stale state. The ledger advances only after the worker acknowledges the exact
source ranges and content hashes. When the catch-up deadline expires, collection
reports `trace_upload_partial` and resumes from the acknowledged offsets on a
later hook.

Source ranges target 4 MiB and end on complete-record boundaries. Serialized
requests target 6 MiB and never exceed 10 MiB; sizing includes chunks, release
snapshots, and request metadata.

#### Release provenance

When a SessionStart hook identifies an exact transcript path and session, the
collector validates the installed plugin's `hub.release.json` and durably marks
the source offset where that release begins to govern new bytes. Uploads retain
their original raw chunks and add customer-local analysis-context snapshots for
the marker intersections. Each snapshot carries the package-scoped `plugin_id`
and display `plugin_name` of the plugin whose embedded runtime is executing,
plus the hub-wide `plugin_version` and content-derived `release_id`. The runtime
identity must match the content-validated release manifest. A release may list
other package plugins, but the collector does not claim those siblings are
installed. The marker is written before upload so retries preserve the same
boundary and capture timestamp. Idle catch-up sources and ambiguous sessions
receive no release assertion; missing provenance means unknown, not that no
Instruction Hub release was installed.

Snapshot-heavy uploads page at existing chunk boundaries to stay within the
worker's 200-snapshot request limit. If one complete-record chunk alone crosses
more than 200 release boundaries, collection fails before upload instead of
acknowledging bytes whose provenance was omitted.

Roll out the fully compatible worker first, with empty release fields omitted
when it calls Hosted Runtime. Deploy Hosted Runtime next, then this collector
version last; older native upload models reject unknown fields.

#### Collection safety

SessionStart creates a durable pending guard before it waits for enrollment and
check-in. Its first baseline performs a complete, unmetered source inventory;
truncating that scan could miss a file and cause its history to replay from
offset zero later. Baseline collection waits for the shared ledger lock until
the collection deadline. If the guard cannot be created, collection stops before
policy lookup or upload. A timed-out baseline leaves the guard in place so
terminal hooks cannot upload pre-enrollment history before a later SessionStart
completes the baseline.

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

The host runtime has one executable entrypoint with subcommands. `ensure` is the hook-safe
path that enrolls when needed, removes legacy managed telemetry config, and
posts a check-in. SessionStart adds `--prepare-baseline` so `ensure` persists the
guard required before it can detach baseline collection. `collect` is the
native JSONL upload path; hooks pass `--detach` so the runtime supervises
collection outside the hook process group. Pass `--include-active` for a
user-initiated sweep that includes files still inside the idle grace period
after SessionStart has established the upload baseline. `enroll` acquires only
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
