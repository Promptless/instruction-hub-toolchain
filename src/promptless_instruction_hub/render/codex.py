"""Codex plugin rendering."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.fs import write_json
from promptless_instruction_hub.models import HubConfig
from promptless_instruction_hub.render.common import RenderedAssets, base_plugin_manifest


def write_manifest(
    target_root: Path,
    config: HubConfig,
    rendered: RenderedAssets,
    mcp_server_names: list[str],
) -> None:
    """Write the Codex plugin manifest."""

    manifest = base_plugin_manifest(config)
    if rendered.get("skills"):
        manifest["skills"] = "./skills/"
    if mcp_server_names:
        manifest["mcpServers"] = "./.mcp.json"
    manifest["interface"] = {
        "displayName": config.plugin_name,
        "shortDescription": f"Governed agent instructions for {config.org}.",
        "developerName": config.org,
        "category": "Productivity",
    }
    write_json(target_root / ".codex-plugin/plugin.json", manifest)


def write_marketplace(output_root: Path, config: HubConfig) -> None:
    """Write the Codex repository marketplace manifest."""

    marketplace = {
        "name": f"{config.plugin_id}-marketplace",
        "interface": {"displayName": f"{config.plugin_name} Marketplace"},
        "plugins": [
            {
                "name": config.plugin_id,
                "source": {"source": "local", "path": "./dist/codex"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
        ],
    }
    write_json(output_root / ".agents/plugins/marketplace.json", marketplace)
