"""Codex plugin rendering."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from promptless_instruction_hub.fs import write_json
from promptless_instruction_hub.models import (
    PIG_PLUGIN_ID,
    ExternalPluginDefinition,
    HubConfig,
    PluginDefinition,
    StablePlugin,
)
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
    """Write the Codex plugin manifest."""

    manifest = base_plugin_manifest(config, plugin)
    description = plugin_description(config, plugin)
    if plugin.id == PIG_PLUGIN_ID:
        long_description = description
        default_prompt = "Use PIG instructions and lifecycle integration for this session."
        if not config.trace_ingestion.enabled:
            default_prompt = "Use Instruction Hub guidance for this session."
    else:
        long_description = f"{plugin.name} distributes governed agent instructions for {config.org}."
        default_prompt = f"Use {plugin.name} instructions for this task."
    manifest["author"] = {"name": config.org}
    if rendered.get("skills"):
        manifest["skills"] = "./skills/"
    if _has_hooks(target_root, rendered):
        manifest["hooks"] = "./hooks/hooks.json"
    if mcp_server_names:
        manifest["mcpServers"] = "./.mcp.json"
    manifest["interface"] = {
        "displayName": plugin.name,
        "shortDescription": description,
        "longDescription": long_description,
        "developerName": config.org,
        "category": "Productivity",
        "capabilities": _capabilities(target_root, rendered, mcp_server_names),
        "defaultPrompt": [default_prompt],
    }
    write_json(target_root / ".codex-plugin/plugin.json", manifest)


def write_marketplace(output_root: Path, config: HubConfig, plugins: Sequence[StablePlugin]) -> None:
    """Write the Codex repository marketplace manifest."""

    marketplace = {
        "name": config.marketplace.id,
        "interface": {"displayName": config.marketplace.name},
        "plugins": [
            external_marketplace_entry(stable_plugin.definition, "codex")
            if isinstance(stable_plugin.definition, ExternalPluginDefinition)
            else {
                "name": stable_plugin.definition.id,
                "source": {"source": "local", "path": f"./dist/codex/{stable_plugin.definition.id}"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
            for stable_plugin in plugins
            if not isinstance(stable_plugin.definition, ExternalPluginDefinition)
            or "codex" in stable_plugin.definition.targets
        ],
    }
    write_json(output_root / ".agents/plugins/marketplace.json", marketplace)


def _capabilities(target_root: Path, rendered: RenderedAssets, mcp_server_names: list[str]) -> list[str]:
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
    if _has_hooks(target_root, rendered):
        capabilities.append("Hooks")
    return capabilities or ["Instruction guidance"]


def _has_hooks(target_root: Path, rendered: RenderedAssets) -> bool:
    return bool(rendered.get("hooks")) or (target_root / "hooks/hooks.json").exists()
