"""Release and channel manifest generation."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.config import RELEASE_MANIFEST_PATH, STABLE_CHANNEL_PATH
from promptless_instruction_hub.fs import JsonValue, directory_hash, write_json
from promptless_instruction_hub.managed_runtime import ManagedRuntimeRecord
from promptless_instruction_hub.models import ExternalPluginDefinition, LoadedAsset, StablePlugin
from promptless_instruction_hub.release.hashing import stable_hash
from promptless_instruction_hub.validate.hub import ValidationResult


def build_release_manifest(
    output_root: Path,
    validation: ValidationResult,
    managed_runtimes: tuple[ManagedRuntimeRecord, ...],
) -> dict[str, JsonValue]:
    """Build the deterministic release manifest for generated target output."""

    target_hashes = build_target_hashes(output_root, validation)
    base_manifest: dict[str, JsonValue] = {
        "schema_version": 3
        if any(isinstance(plugin.definition, ExternalPluginDefinition) for plugin in validation.stable_plugins)
        else 2,
        "org": validation.config.org,
        "version": validation.config.version,
        "marketplace": {
            "id": validation.config.marketplace.id,
            "name": validation.config.marketplace.name,
        },
        "stable_plugins": validation.config.stable_plugins,
        "targets": validation.config.targets,
        "target_hashes": target_hashes,
        "managed_runtimes": [runtime.to_manifest() for runtime in managed_runtimes],
        "assets": [_asset_manifest(asset) for asset in validation.stable_assets],
        "version_basis": build_release_version_basis(output_root, validation, managed_runtimes),
    }
    content_hash = stable_hash(base_manifest)
    base_manifest["release_id"] = f"{validation.config.version}+{content_hash[:12]}"
    base_manifest["release_hash"] = stable_hash(base_manifest)
    return base_manifest


def build_release_version_basis(
    output_root: Path,
    validation: ValidationResult,
    managed_runtimes: tuple[ManagedRuntimeRecord, ...],
) -> dict[str, JsonValue]:
    """Return the output-affecting state that should move plugin versions."""

    return {
        "org": validation.config.org,
        "version": validation.config.version,
        "marketplace": {
            "id": validation.config.marketplace.id,
            "name": validation.config.marketplace.name,
        },
        "stable_plugins": validation.config.stable_plugins,
        "targets": validation.config.targets,
        "plugins": [_plugin_version_basis(stable_plugin) for stable_plugin in validation.stable_plugins],
        "target_hashes": build_target_hashes(output_root, validation),
        "managed_runtimes": [runtime.to_manifest() for runtime in managed_runtimes],
    }


def build_target_hashes(output_root: Path, validation: ValidationResult) -> dict[str, JsonValue]:
    """Return deterministic hashes for generated target output."""

    return {
        target: directory_hash(output_root / "dist" / target, skip_names={RELEASE_MANIFEST_PATH.name})
        for target in validation.config.targets
    }


def write_release_files(output_root: Path, release_manifest: dict[str, JsonValue]) -> None:
    """Write generated release and stable-channel manifests."""

    write_json(output_root / RELEASE_MANIFEST_PATH, release_manifest)
    write_json(
        output_root / STABLE_CHANNEL_PATH,
        {
            "schema_version": release_manifest["schema_version"],
            "channel": "stable",
            "release_id": release_manifest["release_id"],
            "release_hash": release_manifest["release_hash"],
            "version": release_manifest["version"],
            "targets": release_manifest["targets"],
        },
    )


def _asset_manifest(asset: LoadedAsset) -> dict[str, JsonValue]:
    return {
        "ref": asset.ref,
        "id": asset.id,
        "type": asset.type,
        "title": asset.metadata.title,
        "source_path": asset.metadata.source_path,
        "content_hash": asset.content_hash,
        "support": {
            target: support.model_dump(exclude_none=True) for target, support in sorted(asset.metadata.support.items())
        },
    }


def _plugin_version_basis(stable_plugin: StablePlugin) -> dict[str, JsonValue]:
    plugin = stable_plugin.definition
    if isinstance(plugin, ExternalPluginDefinition):
        return plugin.model_dump(exclude={"owners"})
    return {
        "id": plugin.id,
        "name": plugin.name,
        "includes": sorted(plugin.includes),
        "assets": [_asset_manifest(asset) for asset in stable_plugin.assets],
    }
