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
their config to `hub.yaml` and regenerate output with `pi build`.

## Release Model

Action releases are tagged with immutable versions such as `v0.1.0` and a moving
major pointer such as `v0`. Customer workflows can use `@v0` for minor updates or
pin to an immutable tag for stricter reproducibility.

## Managed Runtime Trace Collector

The toolchain owns Promptless-managed runtime artifacts that are injected into
generated customer plugins. The current managed runtime is the native trace
collector used by Codex and Claude lifecycle hooks. During dogfood, generated
hooks invoke the bundled stdlib-only Python script with `python3`.

The collector reads the private enrollment seed, fetches the signed hosted
policy through the customer worker, validates the signed-policy envelope and
native trace upload policy, then uploads new complete JSONL ranges from the
configured native trace roots. It maintains a per-user ledger in plugin data so
successful uploads advance monotonically and failed uploads are retried. First
install defaults to forward-only baselining so historical local traces are not
uploaded unless policy explicitly opts into backfill.

Codex hooks run on `SessionStart` and `Stop`; Claude hooks run on
`SessionStart`, `Stop`, and `SessionEnd`. When policy disables in-progress
trace uploads, Codex `Stop` and Claude `SessionEnd` are treated as terminal
events. Upload responses must echo the batch and policy version and confirm all
raw chunks before the ledger advances.

Before the customer-grade release, replace the Python dogfood script with a
static native binary built and versioned by Promptless, then bundled into the
toolchain release. Customer Instruction Hub repositories should not need Python,
uv, Go, Rust, curl, jq, or other runtime/build dependencies installed for the
hook to run. Customer builds should only consume the already-built Promptless
artifact that the toolchain copies into plugin `bin/`. The customer-grade
binary must verify the asymmetric hosted-policy signature with a pinned
Promptless public key before trusting the policy body.
