"""Cursor plugin rendering."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.fs import write_json
from promptless_instruction_hub.models import HubConfig
from promptless_instruction_hub.render.common import RenderedAssets, base_plugin_manifest, plugin_description


def write_manifest(
    target_root: Path,
    config: HubConfig,
    rendered: RenderedAssets,
) -> None:
    """Write the Cursor plugin manifest."""

    manifest = base_plugin_manifest(config)
    manifest["displayName"] = config.plugin_name
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


def write_marketplace(output_root: Path, config: HubConfig) -> None:
    """Write the Cursor repository marketplace manifest."""

    marketplace = {
        "name": f"{config.plugin_id}-marketplace",
        "owner": {"name": config.org},
        "metadata": {"description": f"{config.plugin_name} marketplace."},
        "plugins": [
            {
                "name": config.plugin_id,
                "source": "dist/cursor",
                "description": plugin_description(config),
            }
        ],
    }
    write_json(output_root / ".cursor-plugin/marketplace.json", marketplace)
