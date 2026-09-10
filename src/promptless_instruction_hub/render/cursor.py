"""Cursor plugin rendering."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from promptless_instruction_hub.fs import write_json
from promptless_instruction_hub.models import HubConfig, PluginDefinition, StablePlugin
from promptless_instruction_hub.render.common import (
    RenderedAssets,
    base_plugin_manifest,
    plugin_description,
)


def write_manifest(
    target_root: Path,
    config: HubConfig,
    plugin: PluginDefinition,
    rendered: RenderedAssets,
) -> None:
    """Write the Cursor plugin manifest."""

    manifest = base_plugin_manifest(config, plugin)
    manifest["displayName"] = plugin.name
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


def write_marketplace(output_root: Path, config: HubConfig, plugins: Sequence[StablePlugin]) -> None:
    """Write the Cursor repository marketplace manifest."""

    marketplace = {
        "name": config.marketplace.id,
        "owner": {"name": config.org},
        "metadata": {"description": f"{config.marketplace.name} marketplace."},
        "plugins": [
            {
                "name": stable_plugin.definition.id,
                "source": f"dist/cursor/{stable_plugin.definition.id}",
                "description": plugin_description(config, stable_plugin.definition),
            }
            for stable_plugin in plugins
            if isinstance(stable_plugin.definition, PluginDefinition)
        ],
    }
    write_json(output_root / ".cursor-plugin/marketplace.json", marketplace)
