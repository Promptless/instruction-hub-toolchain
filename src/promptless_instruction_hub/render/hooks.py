"""Assemble authored native hook configurations without translating host semantics."""

from __future__ import annotations

import shutil
from pathlib import Path

from promptless_instruction_hub.assets import METADATA_FILE
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, copy_tree, read_json_mapping, write_json
from promptless_instruction_hub.models import Harness, LoadedAsset


def render_native_hooks(target_root: Path, target: Harness, assets: list[LoadedAsset]) -> list[str]:
    """Copy supported bundles and merge their selected configs at the host discovery path."""

    selected = sorted(
        (asset for asset in assets if asset.type == "hook" and asset.metadata.support[target].mode == "native"),
        key=lambda asset: asset.ref,
    )
    if not selected:
        return []
    merged: dict[str, JsonValue] = {}
    events: dict[str, JsonValue] = {}
    descriptions: list[str] = []
    hook_root = target_root / "hooks"
    has_config = False
    for asset in selected:
        if asset.path.is_file() and asset.path.suffix != ".json":
            # Legacy non-JSON native files retain their original paths and contents.
            hook_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(asset.path, hook_root / asset.path.name)
            continue
        source = _config_path(asset, target)
        config = _read_config(source)
        has_config = True
        for key, value in config.items():
            if key == "hooks":
                assert isinstance(value, dict)
                for event, entries in value.items():
                    assert isinstance(entries, list)
                    combined = events.setdefault(event, [])
                    assert isinstance(combined, list)
                    combined.extend(entries)
            elif key == "description":
                assert isinstance(value, str)
                if value not in descriptions:
                    descriptions.append(value)
            elif key in merged and merged[key] != value:
                raise InstructionHubError(f"{source}: conflicting hook configuration field {key!r} for {target}")
            else:
                merged[key] = value
        if asset.path.is_dir():
            copy_tree(asset.path, hook_root / asset.id, skip_names={METADATA_FILE})
        else:
            hook_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, hook_root / source.name)
    if has_config:
        merged["hooks"] = events
        if descriptions:
            merged["description"] = "\n".join(descriptions)
        write_json(hook_root / "hooks.json", merged)
    return [asset.id for asset in selected]


def _config_path(asset: LoadedAsset, target: Harness) -> Path:
    if not asset.path.is_dir():
        return asset.path
    specific = asset.path / f"hooks.{target}.json"
    if specific.is_file():
        return specific
    shared = asset.path / "hooks.json"
    if shared.is_file():
        return shared
    raise InstructionHubError(f"{asset.ref}: native {target} bundle requires hooks.{target}.json or hooks.json")


def _read_config(path: Path) -> dict[str, JsonValue]:
    try:
        config = read_json_mapping(path)
    except (OSError, ValueError) as exc:
        raise InstructionHubError(f"{path}: invalid native hook configuration: {exc}") from exc
    events = config.get("hooks")
    if not isinstance(events, dict):
        raise InstructionHubError(f"{path} field hooks must be a JSON object")
    for event, entries in events.items():
        if not event or not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise InstructionHubError(f"{path}: hooks.{event} must be an array of handler objects")
    if "description" in config and not isinstance(config["description"], str):
        raise InstructionHubError(f"{path}: description must be a string")
    return config
