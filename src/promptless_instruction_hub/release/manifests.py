"""Release and channel manifest generation."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.config import RELEASE_MANIFEST_PATH, STABLE_CHANNEL_PATH
from promptless_instruction_hub.fs import JsonValue, directory_hash, write_json
from promptless_instruction_hub.managed_runtime import ManagedRuntimeRecord
from promptless_instruction_hub.models import LoadedAsset, StablePackage
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
        "schema_version": 1,
        "org": validation.config.org,
        "plugin": {
            "id": validation.config.plugin_id,
            "name": validation.config.plugin_name,
            "version": validation.config.plugin_version,
        },
        "stable_packages": validation.config.stable_packages,
        "targets": validation.config.targets,
        "target_hashes": target_hashes,
        "managed_runtimes": [runtime.to_manifest() for runtime in managed_runtimes],
        "assets": [_asset_manifest(asset) for asset in validation.stable_assets],
        "version_basis": build_release_version_basis(output_root, validation, managed_runtimes),
    }
    content_hash = stable_hash(base_manifest)
    base_manifest["release_id"] = f"{validation.config.plugin_version}+{content_hash[:12]}"
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
        "plugin": {
            "id": validation.config.plugin_id,
            "name": validation.config.plugin_name,
            "version": validation.config.plugin_version,
        },
        "stable_packages": validation.config.stable_packages,
        "targets": validation.config.targets,
        "packages": [_package_version_basis(stable_package) for stable_package in validation.stable_packages],
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
            "schema_version": 1,
            "channel": "stable",
            "release_id": release_manifest["release_id"],
            "release_hash": release_manifest["release_hash"],
            "plugin_version": release_manifest["plugin"]["version"]
            if isinstance(release_manifest["plugin"], dict)
            else None,
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


def _package_version_basis(stable_package: StablePackage) -> dict[str, JsonValue]:
    package = stable_package.definition
    return {
        "id": package.id,
        "name": package.name,
        "includes": sorted(package.includes),
        "assets": [_asset_manifest(asset) for asset in stable_package.assets],
    }
