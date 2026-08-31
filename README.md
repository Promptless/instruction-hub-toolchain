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
