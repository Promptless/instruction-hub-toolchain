"""Validate portable hook declarations and render explicit native bindings."""

from __future__ import annotations

import shlex

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue
from promptless_instruction_hub.hook_runtime import EVENTS
from promptless_instruction_hub.models import Harness, LoadedAsset

RUNNER_NAME = "_instruction_hub_hook.py"
PLUGIN_ROOTS = {"claude": "CLAUDE_PLUGIN_ROOT", "codex": "PLUGIN_ROOT", "cursor": "CURSOR_PLUGIN_ROOT"}


def validate_hook_definition(asset: LoadedAsset) -> None:
    spec = asset.metadata.hook
    if spec is None:
        return
    if not asset.path.is_dir():
        raise InstructionHubError(f"{asset.ref}: hook declarations require a directory bundle")
    if not (asset.path / spec.entrypoint).is_file():
        raise InstructionHubError(f"{asset.ref}: hook entrypoint does not exist: {spec.entrypoint}")
    if any(
        (asset.path / name).exists()
        for name in ("hooks.json", *(f"hooks.{target}.json" for target in asset.metadata.support))
    ):
        raise InstructionHubError(f"{asset.ref}: choose a hook declaration or native JSON configuration, not both")
    supported = {target for target, support in asset.metadata.support.items() if support.mode == "native"}
    if set(spec.bindings) != supported:
        raise InstructionHubError(f"{asset.ref}: hook bindings must exactly match targets with native support")
    for target, bindings in spec.bindings.items():
        seen = set()
        for binding in bindings:
            if binding.event not in EVENTS.get(target, {}):
                raise InstructionHubError(
                    f"{asset.ref}: no portable adapter for {target} event {binding.event!r}; use native JSON"
                )
            key = (binding.event, binding.matcher)
            if key in seen:
                raise InstructionHubError(f"{asset.ref}: duplicate {target} hook binding: {binding.event}")
            seen.add(key)


def render_hook_definition(asset: LoadedAsset, target: Harness) -> dict[str, JsonValue]:
    spec = asset.metadata.hook
    assert spec is not None
    root = PLUGIN_ROOTS[target]
    runner = f'"${{{root}}}"/{shlex.quote("hooks/" + RUNNER_NAME)}'
    entrypoint = f'"${{{root}}}"/{shlex.quote(f"hooks/{asset.id}/{spec.entrypoint}")}'
    command = (
        "if command -v python3 >/dev/null 2>&1 "
        "&& python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then "
        f"exec python3 {runner} --host {target} --entrypoint {entrypoint}; "
        "else printf '%s\\n' 'Instruction Hub hooks require Python 3.9 or newer.' >&2; fi"
    )
    events: dict[str, JsonValue] = {}
    for binding in spec.bindings[target]:
        action: dict[str, JsonValue] = {"command": command, "timeout": spec.timeout}
        if target == "cursor":
            entry = action
        else:
            action["type"] = "command"
            if spec.status_message:
                action["statusMessage"] = spec.status_message
            entry = {"hooks": [action]}
        if binding.matcher:
            entry["matcher"] = binding.matcher
        handlers = events.setdefault(binding.event, [])
        assert isinstance(handlers, list)
        handlers.append(entry)
    return {"version": 1, "hooks": events} if target == "cursor" else {"hooks": events}
