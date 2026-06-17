"""Tiny stdio MCP server for local Instruction Hub release status."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from promptless_instruction_hub.fs import JsonValue, validate_json_value
from promptless_instruction_hub.status import summarize_release_manifest

STATUS_TOOL_NAME = "promptless_instruction_hub_status"


@dataclass(frozen=True)
class InvalidJsonRpcRequest:
    """A JSON-RPC request that could not be handled as a request object."""

    message: str


def run_status_mcp(manifest_path: Path) -> None:
    """Serve release metadata over stdio without network access."""

    for line in sys.stdin:
        request = _parse_request(line)
        if request is None:
            continue
        if isinstance(request, InvalidJsonRpcRequest):
            response = _error(None, -32600, request.message)
        else:
            response = _handle_request(request, manifest_path)
        if response is not None:
            sys.stdout.write(json.dumps(response, sort_keys=True) + "\n")
            sys.stdout.flush()


def _parse_request(line: str) -> dict[str, JsonValue] | InvalidJsonRpcRequest | None:
    if not line.strip():
        return None
    try:
        raw_request = json.loads(line)
    except json.JSONDecodeError as exc:
        return InvalidJsonRpcRequest(f"invalid JSON: {exc.msg}")
    try:
        validated_request = validate_json_value(raw_request, "json-rpc request")
    except ValueError as exc:
        return InvalidJsonRpcRequest(str(exc))
    if not isinstance(validated_request, dict):
        return InvalidJsonRpcRequest("JSON-RPC request must be an object")
    return validated_request


def _handle_request(request: dict[str, JsonValue], manifest_path: Path) -> dict[str, JsonValue] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return _ok(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "promptless-instruction-hub-status", "version": "0.1.0"},
            },
        )
    if method == "tools/list":
        return _ok(
            request_id,
            {
                "tools": [
                    {
                        "name": STATUS_TOOL_NAME,
                        "description": "Return local Instruction Hub release/version/hash metadata.",
                        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
                    }
                ]
            },
        )
    if method == "tools/call":
        params = request.get("params")
        if not isinstance(params, dict) or params.get("name") != STATUS_TOOL_NAME:
            return _error(request_id, -32602, f"unknown tool: {params}")
        status = summarize_release_manifest(manifest_path)
        return _ok(request_id, {"content": [{"type": "text", "text": json.dumps(status, sort_keys=True)}]})
    return _error(request_id, -32601, f"unsupported method: {method}")


def _ok(request_id: JsonValue, result: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: JsonValue, code: int, message: str) -> dict[str, JsonValue]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
