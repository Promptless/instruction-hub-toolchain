"""Top-level target plugin rendering orchestration."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.fs import JsonValue, write_json
from promptless_instruction_hub.models import Harness, HubConfig, LoadedAsset
import promptless_instruction_hub.render.claude as claude
import promptless_instruction_hub.render.codex as codex
import promptless_instruction_hub.render.cursor as cursor
import promptless_instruction_hub.render.gemini as gemini
from promptless_instruction_hub.render.assets import render_assets_for_target
from promptless_instruction_hub.render.common import RenderedAssets
from promptless_instruction_hub.render.mcp import collect_mcp_servers, write_mcp_config


def render_target_plugins(output_root: Path, config: HubConfig, assets: list[LoadedAsset]) -> None:
    """Render one org-level plugin directory per configured target harness."""

    for marketplace_root in (".agents/plugins", ".claude-plugin", ".cursor-plugin"):
        (output_root / marketplace_root).mkdir(parents=True, exist_ok=True)

    for target in config.targets:
        target_root = output_root / "dist" / target
        target_root.mkdir(parents=True, exist_ok=True)
        rendered = render_assets_for_target(target_root, target, assets)
        mcp_servers = collect_mcp_servers(target, assets)
        if mcp_servers:
            write_mcp_config(target_root, target, mcp_servers)
        _write_manifest(target_root, target, config, rendered, mcp_servers)
    if "codex" in config.targets:
        codex.write_marketplace(output_root, config)
    if "claude" in config.targets:
        claude.write_marketplace(output_root, config)
    if "cursor" in config.targets:
        cursor.write_marketplace(output_root, config)


def embed_release_manifest(output_root: Path, config: HubConfig, release_manifest: dict[str, JsonValue]) -> None:
    """Copy the release manifest into each generated target plugin for local status tools."""

    for target in config.targets:
        write_json(output_root / "dist" / target / ".promptless/release.json", release_manifest)


def _write_manifest(
    target_root: Path,
    target: Harness,
    config: HubConfig,
    rendered: RenderedAssets,
    mcp_servers: dict[str, JsonValue],
) -> None:
    if target == "claude":
        claude.write_manifest(target_root, config, rendered, sorted(mcp_servers))
        return
    if target == "codex":
        codex.write_manifest(target_root, config, rendered, sorted(mcp_servers))
        return
    if target == "gemini":
        gemini.write_manifest(target_root, config, mcp_servers)
        return
    cursor.write_manifest(target_root, config, rendered)
