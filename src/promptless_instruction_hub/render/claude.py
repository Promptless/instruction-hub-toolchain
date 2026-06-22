"""Claude Code plugin rendering."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence

from promptless_instruction_hub.fs import write_json
from promptless_instruction_hub.models import HubConfig, PackageDefinition, StablePackage
from promptless_instruction_hub.render.common import RenderedAssets, base_plugin_manifest, plugin_description


def write_manifest(
    target_root: Path,
    config: HubConfig,
    package: PackageDefinition,
    rendered: RenderedAssets,
    mcp_server_names: list[str],
) -> None:
    """Write the Claude Code plugin manifest."""

    manifest = base_plugin_manifest(config, package)
    manifest["displayName"] = package.name
    manifest["author"] = {"name": config.org}
    if rendered.get("skills"):
        manifest["skills"] = "./skills/"
    if rendered.get("commands"):
        manifest["commands"] = "./commands/"
    if rendered.get("agents"):
        manifest["agents"] = "./agents/"
    if mcp_server_names:
        manifest["mcpServers"] = "./.mcp.json"
    write_json(target_root / ".claude-plugin/plugin.json", manifest)


def write_marketplace(output_root: Path, config: HubConfig, packages: Sequence[StablePackage]) -> None:
    """Write the Claude Code repository marketplace manifest."""

    marketplace = {
        "name": f"{config.plugin_id}-marketplace",
        "owner": {"name": config.org},
        "description": f"{config.plugin_name} marketplace.",
        "plugins": [
            {
                "name": stable_package.definition.id,
                "source": f"./dist/claude/{stable_package.definition.id}",
                "displayName": stable_package.definition.name,
                "description": plugin_description(config, stable_package.definition),
                "version": config.plugin_version,
                "author": {"name": config.org},
                "category": "Productivity",
            }
            for stable_package in packages
        ],
    }
    write_json(output_root / ".claude-plugin/marketplace.json", marketplace)
