"""Claude Code plugin rendering."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence

from promptless_instruction_hub.fs import write_json
from promptless_instruction_hub.models import ExternalPluginDefinition, HubConfig, PluginDefinition, StablePlugin
from promptless_instruction_hub.render.external import external_marketplace_entry
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
    mcp_server_names: list[str],
) -> None:
    """Write the Claude Code plugin manifest."""

    manifest = base_plugin_manifest(config, plugin)
    manifest["displayName"] = plugin.name
    manifest["author"] = {"name": config.org}
    if rendered.get("skills"):
        manifest["skills"] = "./skills/"
    if rendered.get("commands"):
        manifest["commands"] = "./commands/"
    if rendered.get("agents"):
        agent_files = sorted(path.name for path in (target_root / "agents").glob("*.md"))
        if agent_files:
            manifest["agents"] = [f"./agents/{name}" for name in agent_files]
    if mcp_server_names:
        manifest["mcpServers"] = "./.mcp.json"
    write_json(target_root / ".claude-plugin/plugin.json", manifest)


def write_marketplace(output_root: Path, config: HubConfig, plugins: Sequence[StablePlugin]) -> None:
    """Write the Claude Code repository marketplace manifest."""

    marketplace = {
        "name": config.marketplace.id,
        "owner": {"name": config.org},
        "description": f"{config.marketplace.name} marketplace.",
        "plugins": [
            external_marketplace_entry(stable_plugin.definition, "claude")
            if isinstance(stable_plugin.definition, ExternalPluginDefinition)
            else {
                "name": stable_plugin.definition.id,
                "source": f"./dist/claude/{stable_plugin.definition.id}",
                "displayName": stable_plugin.definition.name,
                "description": plugin_description(config, stable_plugin.definition),
                "version": config.version,
                "author": {"name": config.org},
                "category": "Productivity",
            }
            for stable_plugin in plugins
            if not isinstance(stable_plugin.definition, ExternalPluginDefinition)
            or "claude" in stable_plugin.definition.targets
        ],
    }
    write_json(output_root / ".claude-plugin/marketplace.json", marketplace)
