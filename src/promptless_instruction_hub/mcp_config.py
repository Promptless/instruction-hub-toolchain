"""Shared MCP configuration parsing."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, read_json_mapping, read_yaml_mapping

MCP_CONFIG_FILENAMES = (".mcp.json", "mcp.json", "mcp.yaml", "mcp.yml")
MCP_SERVER_CONFIG_KEYS = {"command", "url", "type", "args", "env", "headers", "transport"}


def read_mcp_servers(path: Path, *, default_server_name: str) -> dict[str, JsonValue]:
    """Read MCP server definitions from a file or asset directory."""

    source_path = resolve_mcp_config_path(path)
    raw_data = read_json_mapping(source_path) if source_path.suffix == ".json" else read_yaml_mapping(source_path)
    return parse_mcp_servers(raw_data, source_path=source_path, default_server_name=default_server_name)


def resolve_mcp_config_path(path: Path) -> Path:
    """Resolve an MCP asset path to the concrete config file that should be parsed."""

    if not path.is_dir():
        return path
    for candidate_name in MCP_CONFIG_FILENAMES:
        candidate = path / candidate_name
        if candidate.exists():
            return candidate
    expected = ", ".join(MCP_CONFIG_FILENAMES)
    msg = f"{path} must contain one of: {expected}"
    raise InstructionHubError(msg)


def parse_mcp_servers(
    raw_data: dict[str, JsonValue],
    *,
    source_path: Path,
    default_server_name: str,
) -> dict[str, JsonValue]:
    """Parse supported MCP config shapes into a server-name mapping."""

    if "mcpServers" in raw_data:
        return _parse_server_map(raw_data["mcpServers"], source_path, "mcpServers")
    if "servers" in raw_data:
        return _parse_server_map(raw_data["servers"], source_path, "servers")
    if _looks_like_mcp_server_config(raw_data):
        return {default_server_name: raw_data}
    return _parse_server_map(raw_data, source_path, "<root>")


def _parse_server_map(value: JsonValue, source_path: Path, field_name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        msg = f"{source_path} field {field_name} must be an object mapping of MCP server names to configs"
        raise InstructionHubError(msg)

    servers: dict[str, JsonValue] = {}
    for server_name, server_config in sorted(value.items()):
        if not isinstance(server_config, dict):
            msg = f"{source_path} field {field_name}.{server_name} must be an object MCP server config"
            raise InstructionHubError(msg)
        servers[server_name] = server_config
    return servers


def _looks_like_mcp_server_config(value: dict[str, JsonValue]) -> bool:
    return any(key in value for key in MCP_SERVER_CONFIG_KEYS)
