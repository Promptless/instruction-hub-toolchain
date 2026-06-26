"""Top-level target plugin rendering orchestration."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.config import RELEASE_MANIFEST_PATH
from promptless_instruction_hub.fs import JsonValue, write_json
from promptless_instruction_hub.managed_runtime import ManagedRuntimeRecord, render_managed_runtimes
from promptless_instruction_hub.models import Harness, HubConfig, PackageDefinition, StablePackage
import promptless_instruction_hub.render.claude as claude
import promptless_instruction_hub.render.codex as codex
import promptless_instruction_hub.render.cursor as cursor
import promptless_instruction_hub.render.gemini as gemini
from promptless_instruction_hub.render.assets import render_assets_for_target
from promptless_instruction_hub.render.common import RenderedAssets
from promptless_instruction_hub.render.mcp import collect_mcp_servers, write_mcp_config


def render_target_plugins(
    output_root: Path,
    config: HubConfig,
    packages: tuple[StablePackage, ...],
) -> tuple[ManagedRuntimeRecord, ...]:
    """Render per-package target plugin directories and target marketplace manifests."""

    managed_runtimes: list[ManagedRuntimeRecord] = []
    for marketplace_root in (".agents/plugins", ".claude-plugin", ".cursor-plugin"):
        (output_root / marketplace_root).mkdir(parents=True, exist_ok=True)

    for target in config.targets:
        for stable_package in packages:
            target_root = output_root / "dist" / target / stable_package.definition.id
            target_root.mkdir(parents=True, exist_ok=True)
            assets = list(stable_package.assets)
            rendered = render_assets_for_target(target_root, target, assets)
            mcp_servers = collect_mcp_servers(target, assets)
            if mcp_servers:
                write_mcp_config(target_root, target, mcp_servers)
            managed_runtimes.extend(render_managed_runtimes(target_root, target, config, stable_package.definition))
            _write_manifest(target_root, target, config, stable_package.definition, rendered, mcp_servers)
    if "codex" in config.targets:
        codex.write_marketplace(output_root, config, packages)
    if "claude" in config.targets:
        claude.write_marketplace(output_root, config, packages)
    if "cursor" in config.targets:
        cursor.write_marketplace(output_root, config, packages)
    return tuple(managed_runtimes)


def embed_release_manifest(
    output_root: Path,
    config: HubConfig,
    packages: tuple[StablePackage, ...],
    release_manifest: dict[str, JsonValue],
) -> None:
    """Copy the release manifest into each generated target plugin for local status tools."""

    for target in config.targets:
        for stable_package in packages:
            write_json(
                output_root / "dist" / target / stable_package.definition.id / RELEASE_MANIFEST_PATH,
                release_manifest,
            )


def _write_manifest(
    target_root: Path,
    target: Harness,
    config: HubConfig,
    package: PackageDefinition,
    rendered: RenderedAssets,
    mcp_servers: dict[str, JsonValue],
) -> None:
    if target == "claude":
        claude.write_manifest(target_root, config, package, rendered, sorted(mcp_servers))
        return
    if target == "codex":
        codex.write_manifest(target_root, config, package, rendered, sorted(mcp_servers))
        return
    if target == "gemini":
        gemini.write_manifest(target_root, config, package, mcp_servers)
        return
    cursor.write_manifest(target_root, config, package, rendered)
