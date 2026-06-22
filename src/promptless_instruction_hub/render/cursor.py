"""Cursor plugin rendering."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from promptless_instruction_hub.fs import write_json
from promptless_instruction_hub.models import HubConfig, PackageDefinition, StablePackage
from promptless_instruction_hub.render.common import (
    RenderedAssets,
    base_plugin_manifest,
    package_plugin_id,
    plugin_description,
)


def write_manifest(
    target_root: Path,
    config: HubConfig,
    package: PackageDefinition,
    rendered: RenderedAssets,
) -> None:
    """Write the Cursor plugin manifest."""

    manifest = base_plugin_manifest(config, package)
    manifest["displayName"] = package.name
    manifest["author"] = {"name": config.org}
    manifest["category"] = "developer-tools"
    if rendered.get("skills"):
        manifest["skills"] = "./skills/"
    if rendered.get("rules"):
        manifest["rules"] = "./rules/"
    if rendered.get("agents"):
        manifest["agents"] = "./agents/"
    if rendered.get("commands"):
        manifest["commands"] = "./commands/"
    write_json(target_root / ".cursor-plugin/plugin.json", manifest)


def write_marketplace(output_root: Path, config: HubConfig, packages: Sequence[StablePackage]) -> None:
    """Write the Cursor repository marketplace manifest."""

    marketplace = {
        "name": f"{config.plugin_id}-marketplace",
        "owner": {"name": config.org},
        "metadata": {"description": f"{config.plugin_name} marketplace."},
        "plugins": [
            {
                "name": package_plugin_id(config, stable_package.definition),
                "source": f"dist/cursor/{stable_package.definition.id}",
                "description": plugin_description(config, stable_package.definition),
            }
            for stable_package in packages
        ],
    }
    write_json(output_root / ".cursor-plugin/marketplace.json", marketplace)
