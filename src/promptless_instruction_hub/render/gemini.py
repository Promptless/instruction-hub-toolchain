"""Gemini extension rendering."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.fs import JsonValue, write_json
from promptless_instruction_hub.models import HubConfig, PackageDefinition
from promptless_instruction_hub.render.common import base_plugin_manifest


def write_manifest(
    target_root: Path,
    config: HubConfig,
    package: PackageDefinition,
    mcp_servers: dict[str, JsonValue],
) -> None:
    """Write the Gemini extension manifest."""

    manifest = base_plugin_manifest(config, package)
    if mcp_servers:
        manifest["mcpServers"] = mcp_servers
    write_json(target_root / "gemini-extension.json", manifest)
