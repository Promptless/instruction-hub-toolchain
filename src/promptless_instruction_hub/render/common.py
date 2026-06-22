"""Shared helpers for target plugin renderers."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.fs import JsonValue
from promptless_instruction_hub.models import HubConfig, PackageDefinition

RenderedAssets = dict[str, list[str]]


def base_plugin_manifest(config: HubConfig, package: PackageDefinition) -> dict[str, JsonValue]:
    """Return manifest fields shared by all generated target plugins."""

    return {
        "name": package_plugin_id(config, package),
        "version": config.plugin_version,
        "description": plugin_description(config, package),
    }


def package_plugin_id(config: HubConfig, package: PackageDefinition) -> str:
    """Return the hub-scoped plugin identifier for a stable package."""

    return f"{config.plugin_id}-{package.id}"


def plugin_description(config: HubConfig, package: PackageDefinition) -> str:
    """Return the stable user-facing plugin description."""

    return f"Governed agent instructions for {config.org}: {package.name}."


def manifest_key_for(asset_type: str) -> str:
    """Return the generated manifest collection key for an asset type."""

    if asset_type == "rule":
        return "rules"
    return f"{asset_type}s"


def directory_for(asset_type: str) -> Path:
    """Return the generated native asset directory for an asset type."""

    return Path(manifest_key_for(asset_type))
