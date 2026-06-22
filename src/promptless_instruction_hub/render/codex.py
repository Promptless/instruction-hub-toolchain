"""Codex plugin rendering."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

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
    """Write the Codex plugin manifest."""

    manifest = base_plugin_manifest(config, package)
    description = plugin_description(config, package)
    manifest["author"] = {"name": config.org}
    if rendered.get("skills"):
        manifest["skills"] = "./skills/"
    if mcp_server_names:
        manifest["mcpServers"] = "./.mcp.json"
    manifest["interface"] = {
        "displayName": package.name,
        "shortDescription": description,
        "longDescription": f"{package.name} distributes governed agent instructions for {config.org}.",
        "developerName": config.org,
        "category": "Productivity",
        "capabilities": _capabilities(rendered, mcp_server_names),
        "defaultPrompt": [f"Use {package.name} instructions for this task."],
    }
    write_json(target_root / ".codex-plugin/plugin.json", manifest)


def write_marketplace(output_root: Path, config: HubConfig, packages: Sequence[StablePackage]) -> None:
    """Write the Codex repository marketplace manifest."""

    marketplace = {
        "name": f"{config.plugin_id}-marketplace",
        "interface": {"displayName": f"{config.plugin_name} Marketplace"},
        "plugins": [
            {
                "name": stable_package.definition.id,
                "source": {"source": "local", "path": f"./dist/codex/{stable_package.definition.id}"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
            for stable_package in packages
        ],
    }
    write_json(output_root / ".agents/plugins/marketplace.json", marketplace)


def _capabilities(rendered: RenderedAssets, mcp_server_names: list[str]) -> list[str]:
    capabilities: list[str] = []
    if rendered.get("skills"):
        capabilities.append("Skills")
    if mcp_server_names:
        capabilities.append("MCP servers")
    if rendered.get("rules"):
        capabilities.append("Rules")
    if rendered.get("agents"):
        capabilities.append("Agents")
    if rendered.get("commands"):
        capabilities.append("Commands")
    return capabilities or ["Instruction guidance"]
