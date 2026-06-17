"""Local release status helpers for generated Instruction Hub plugins."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.fs import JsonValue, read_json_mapping


def summarize_release_manifest(manifest_path: Path) -> dict[str, JsonValue]:
    """Return the stable local status fields from a release manifest."""

    manifest = read_json_mapping(manifest_path)
    plugin = manifest.get("plugin")
    plugin_version = None
    if isinstance(plugin, dict):
        plugin_version = plugin.get("version")
    return {
        "release_id": manifest.get("release_id"),
        "release_hash": manifest.get("release_hash"),
        "plugin_version": plugin_version,
        "targets": manifest.get("targets", []),
    }
