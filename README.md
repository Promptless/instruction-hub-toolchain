# Promptless Instruction Hub Toolchain

This repository is the canonical public toolchain for Promptless Instruction
Hub repositories. It bundles the Python compiler in `python-core/` and exposes a
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
      - uses: Promptless/instruction-hub-toolchain@v0
        with:
          mode: publish
```

The action runs the bundled compiler directly:

```bash
uv run --project "$GITHUB_ACTION_PATH/python-core" promptless-instruction-hub <command>
```

## Modes

- `build`: validate the hub and run a build without committing generated files.
- `check`: validate the hub and fail if committed generated output is stale.
- `publish`: build generated output, push it to `release/stable`, and update the
  default-branch Claude marketplace pointer.

Customer hubs should usually use `build` for pull requests and `publish` after
changes merge to the default branch. Use `check` only for repositories that
intentionally commit generated artifacts on the same branch as source assets.

## Release Model

Action releases are tagged with immutable versions such as `v0.1.0` and a moving
major pointer such as `v0`. Customer workflows can use `@v0` for minor updates or
pin to an immutable tag for stricter reproducibility.
