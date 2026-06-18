"""MCP config collection and per-target serialization."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, write_json
from promptless_instruction_hub.mcp_config import read_mcp_servers
from promptless_instruction_hub.models import Harness, LoadedAsset


def collect_mcp_servers(target: Harness, assets: list[LoadedAsset]) -> dict[str, JsonValue]:
    """Collect MCP server definitions supported by one target harness."""

    servers: dict[str, JsonValue] = {}
    server_origins: dict[str, tuple[int, str]] = {}
    mcp_assets = sorted(
        (asset for asset in assets if asset.type == "mcp"),
        key=lambda asset: (_mcp_asset_priority(asset, target), asset.id),
    )
    for asset in mcp_assets:
        support = asset.metadata.support[target]
        if support.mode == "unsupported":
            continue
        priority = _mcp_asset_priority(asset, target)
        for server_name, server_config in read_mcp_servers(asset.path, default_server_name=asset.id).items():
            previous_origin = server_origins.get(server_name)
            if previous_origin is not None:
                previous_priority, previous_ref = previous_origin
                if previous_priority == priority:
                    msg = f"duplicate MCP server {server_name!r} for {target}: {previous_ref} and {asset.ref}"
                    raise InstructionHubError(msg)
                if previous_priority > priority:
                    continue
            servers[server_name] = server_config
            server_origins[server_name] = (priority, asset.ref)
    return servers


def write_mcp_config(target_root: Path, target: Harness, mcp_servers: dict[str, JsonValue]) -> None:
    """Write the MCP config shape expected by one target harness."""

    if target == "codex":
        write_json(target_root / ".mcp.json", {"mcpServers": mcp_servers})
        return
    if target == "cursor":
        write_json(target_root / "mcp.json", {"mcpServers": mcp_servers})
        return
    if target == "gemini":
        return
    write_json(target_root / ".mcp.json", {"mcpServers": mcp_servers})


def _mcp_asset_priority(asset: LoadedAsset, target: Harness) -> int:
    supported_targets = [
        supported_target
        for supported_target, support in asset.metadata.support.items()
        if support.mode != "unsupported"
    ]
    if supported_targets == [target]:
        return 1
    return 0
