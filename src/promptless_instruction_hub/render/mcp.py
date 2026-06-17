"""MCP config collection and per-target serialization."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.fs import JsonValue, read_json_mapping, read_yaml_mapping, write_json
from promptless_instruction_hub.models import Harness, LoadedAsset

STATUS_MCP_SERVER_NAME = "promptless-instruction-hub-status"
MCP_SERVER_CONFIG_KEYS = {"command", "url", "type", "args", "env", "headers", "transport"}


def collect_mcp_servers(target: Harness, assets: list[LoadedAsset]) -> dict[str, JsonValue]:
    """Collect MCP server definitions supported by one target harness."""

    servers: dict[str, JsonValue] = {
        STATUS_MCP_SERVER_NAME: {
            "command": "promptless-instruction-hub",
            "args": ["mcp-status", "--manifest", ".promptless/release.json"],
            "env": {},
        }
    }
    mcp_assets = sorted(
        (asset for asset in assets if asset.type == "mcp"),
        key=lambda asset: (_mcp_asset_priority(asset, target), asset.id),
    )
    for asset in mcp_assets:
        support = asset.metadata.support[target]
        if support.mode == "unsupported":
            continue
        servers.update(_read_mcp_servers(asset))
    return servers


def write_mcp_config(target_root: Path, target: Harness, mcp_servers: dict[str, JsonValue]) -> None:
    """Write the MCP config shape expected by one target harness."""

    if target == "codex":
        write_json(target_root / ".mcp.json", mcp_servers)
        return
    if target == "cursor":
        write_json(target_root / "mcp.json", {"mcpServers": mcp_servers})
        return
    if target == "gemini":
        return
    write_json(target_root / ".mcp.json", {"mcpServers": mcp_servers})


def _read_mcp_servers(asset: LoadedAsset) -> dict[str, JsonValue]:
    raw_data = _read_asset_structured_mapping(asset)
    mcp_servers = raw_data.get("mcpServers")
    if isinstance(mcp_servers, dict):
        return {str(key): value for key, value in mcp_servers.items()}
    servers = raw_data.get("servers")
    if isinstance(servers, dict):
        return {str(key): value for key, value in servers.items()}
    if _looks_like_mcp_server_config(raw_data):
        return {asset.id: raw_data}
    return {str(key): value for key, value in raw_data.items()}


def _read_asset_structured_mapping(asset: LoadedAsset) -> dict[str, JsonValue]:
    source_path = asset.path
    if source_path.is_dir():
        for candidate_name in (".mcp.json", "mcp.json", "mcp.yaml", "mcp.yml"):
            candidate = source_path / candidate_name
            if candidate.exists():
                source_path = candidate
                break
    if source_path.suffix == ".json":
        return read_json_mapping(source_path)
    return read_yaml_mapping(source_path)


def _looks_like_mcp_server_config(value: dict[str, JsonValue]) -> bool:
    return any(key in value for key in MCP_SERVER_CONFIG_KEYS)


def _mcp_asset_priority(asset: LoadedAsset, target: Harness) -> int:
    supported_targets = [
        supported_target
        for supported_target, support in asset.metadata.support.items()
        if support.mode != "unsupported"
    ]
    if supported_targets == [target]:
        return 1
    return 0
