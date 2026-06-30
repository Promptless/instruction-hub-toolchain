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

## Modes

- `build`: validate the hub and run a build without committing generated files.
- `check`: validate the hub and fail if committed generated output is stale.
- `publish`: build generated output from `source-branch`, push it to
  `release/stable`, and update `source-branch` marketplace pointers for
  generated targets.

Customer hubs should usually use `build` for pull requests and `publish` after
changes merge to the default branch. Use `check` only for repositories that
intentionally commit generated artifacts on the same branch as source assets.

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

## Managed Runtime Bootstrap

The toolchain owns Promptless-managed runtime artifacts that must be injected
into generated customer plugins, including the host enrollment bootstrap used by
Codex and Claude startup hooks. During dogfood, generated hooks invoke the
bundled stdlib-only Python script with `python3`.

When local `PIGS_FLY` is set to a truthy value (`1` or `true`,
case-insensitive), the dogfood bootstrap uses
`PROMPTLESS_WORKER_BASE_URL` or the default production worker. It reads the
worker's public `/healthz` identity, opens the hosted Promptless dashboard start
URL, and listens on a loopback callback with a per-attempt state token for the
approved session proof. It then polls the hosted runtime for a one-time per-host
credential, caches that credential, and uses the host credential to fetch
`/v0/host-enrollment/policy?target=...` and post
`/v0/host-enrollment/check-ins`.

Host enrollment is per host, not per plugin. The credential and pending approval
are cached at a single host-global path (`~/.promptless/instruction-hub/`) and
keyed only on the worker deployment and agent host (claude/codex), so every
Promptless plugin a user installs from the hub shares one credential. A
non-blocking, per-credential enrollment-leader lock ensures that when multiple
plugins start at once, exactly one drives the single browser approval while the
others reuse the result or defer to a later session. The per-plugin
`CLAUDE_PLUGIN_DATA`/`PLUGIN_DATA` directories are intentionally not used for this
state.

For Claude Code, managed telemetry follows Claude's supported capture paths
instead of relying on OpenTelemetry SDK attribute-length variables to override
producer-side truncation. Inline tool content remains Claude-bounded OTel event
content. When policy enables raw API body capture, the bootstrap uses inline
`OTEL_LOG_RAW_API_BODIES=1` so request/response body events reach the configured
collector through the standard OTel logs pipeline. Claude's `file:<dir>` mode can
write untruncated local `body_ref` files, but the bootstrap must not enable or
report that mode as ingested until a local collector or uploader publishes those
files to Promptless.

Before the customer-grade release, replace that script with a static native
binary built and versioned by Promptless, then bundled into the toolchain
release. Customer Instruction Hub repositories should not need Python, uv, Go,
Rust, curl, jq, or other runtime/build dependencies installed for the bootstrap
hook to run. Customer builds should only consume the already-built Promptless
artifact that the toolchain copies into plugin `bin/`.

The dogfood bootstrap trusts the authenticated TLS worker response and validates
only the hosted policy shape. The customer-grade static binary must verify an
asymmetric hosted-policy signature with a pinned Promptless public key before it
writes local host telemetry config.
