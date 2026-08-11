from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path

import pytest

from promptless_instruction_hub.cli import main
from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.mcp_status import STATUS_TOOL_NAME, run_status_mcp
from promptless_instruction_hub.scan.hub import scan_hub

from .helpers import (
    FIXTURES,
    SCHEMAS,
    WORKFLOWS,
)


@pytest.mark.parametrize("workflow_name", ["pr-check.yml", "publish.yml"])
def test_reusable_workflows_run_caller_pinned_toolchain_ref(workflow_name: str) -> None:
    workflow_text = (WORKFLOWS / workflow_name).read_text()

    assert "Promptless/instruction-hub-toolchain@v0" not in workflow_text
    assert (
        f"EXPECTED_WORKFLOW_PREFIX: Promptless/instruction-hub-toolchain/.github/workflows/{workflow_name}@"
    ) in workflow_text
    assert "JOB_WORKFLOW_REF: ${{ job.workflow_ref }}" in workflow_text
    assert "repository: Promptless/instruction-hub-toolchain" in workflow_text
    assert "ref: ${{ steps.toolchain-ref.outputs.ref }}" in workflow_text
    assert "path: .promptless-instruction-hub-toolchain" in workflow_text
    assert "uses: ./.promptless-instruction-hub-toolchain" in workflow_text


def test_cli_init_scan_verify_build_validate_and_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hub_root = tmp_path / "hub"

    assert main(["init", "--hub", str(hub_root), "--org", "Acme"]) == 0
    assert main(["scan", "--hub", str(hub_root), "--source", str(FIXTURES / "dogfood-source")]) == 0
    assert main(["validate", "--hub", str(hub_root)]) == 0
    assert main(["verify", "--hub", str(hub_root)]) == 0
    assert main(["build", "--hub", str(hub_root)]) == 0
    assert main(["build", "--hub", str(hub_root), "--check"]) == 0
    assert main(["status", "--manifest", str(hub_root / "hub.release.json")]) == 0

    output = capsys.readouterr().out
    assert "valid Instruction Hub" in output
    assert "verified release" in output
    assert "release_hash" in output


def test_empty_hub_fixture_bootstraps(tmp_path: Path) -> None:
    hub_root = tmp_path / "empty-hub"
    shutil.copytree(FIXTURES / "empty-hub", hub_root)

    init_hub(hub_root)
    result = build_hub(hub_root)

    assert result.asset_count == 0
    assert (hub_root / "hub.release.json").exists()


def test_status_mcp_returns_invalid_request_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("[]\n"))

    run_status_mcp(tmp_path / "missing-release.json")

    response = json.loads(capsys.readouterr().out)
    assert response["error"]["code"] == -32600
    assert response["error"]["message"] == "JSON-RPC request must be an object"


def test_status_mcp_reports_release_metadata_without_git_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    scan_hub(hub_root, FIXTURES / "dogfood-source")
    build_hub(hub_root)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": STATUS_TOOL_NAME, "arguments": {}},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request) + "\n"))

    run_status_mcp(hub_root / "hub.release.json")

    response = json.loads(capsys.readouterr().out)
    status = json.loads(response["result"]["content"][0]["text"])
    assert status["release_hash"]
    assert status["plugin_version"] == "0.1.0"
    assert "git_commit" not in status


def test_release_manifest_schema_matches_generated_contract() -> None:
    schema = json.loads((SCHEMAS / "release-manifest.schema.json").read_text())

    assert schema["additionalProperties"] is False
    assert "target_hashes" in schema["required"]
    assert "version_basis" in schema["required"]
    assert "managed_runtimes" in schema["required"]
    assert "git_commit" not in schema["properties"]
    assert schema["properties"]["stable_packages"]["minItems"] == 1
    assert schema["properties"]["targets"]["minItems"] == 1
    assert schema["properties"]["target_hashes"]["minProperties"] == 1
    assert "default" not in schema["properties"]["managed_runtimes"]
    version_basis_schema = schema["properties"]["version_basis"]
    assert version_basis_schema["required"] == [
        "org",
        "plugin",
        "stable_packages",
        "targets",
        "packages",
        "target_hashes",
        "managed_runtimes",
    ]
    assert version_basis_schema["properties"]["stable_packages"]["minItems"] == 1
    assert version_basis_schema["properties"]["targets"]["minItems"] == 1
    assert version_basis_schema["properties"]["packages"]["minItems"] == 1
    assert version_basis_schema["properties"]["target_hashes"] == {"$ref": "#/properties/target_hashes"}
    assert version_basis_schema["properties"]["managed_runtimes"] == {"$ref": "#/properties/managed_runtimes"}
    managed_runtime_schema = schema["properties"]["managed_runtimes"]["items"]
    assert managed_runtime_schema["required"] == [
        "id",
        "channel",
        "executable",
        "hook",
        "package_id",
        "path",
        "plugin_id",
        "plugin_version",
        "sha256",
        "status",
        "target",
        "toolchain_version",
        "version",
    ]
    assert managed_runtime_schema["properties"]["id"] == {"const": "host-runtime"}
    assert managed_runtime_schema["properties"]["status"] == {"const": "included"}
    assert managed_runtime_schema["properties"]["target"] == {"enum": ["claude", "codex"]}
    assert "oneOf" not in managed_runtime_schema
    asset_schema = schema["properties"]["assets"]["items"]
    assert asset_schema["required"] == ["ref", "id", "type", "title", "source_path", "content_hash", "support"]
    assert "pattern" in schema["properties"]["plugin"]["properties"]["version"]
    target_support_schema = schema["$defs"]["target_support"]
    assert "source" not in target_support_schema["properties"]
    assert target_support_schema["properties"]["reason"] == {"type": "string", "minLength": 1}
    assert target_support_schema["allOf"][0]["then"]["required"] == ["reason"]


def test_instruction_hub_schema_requires_non_empty_lists() -> None:
    schema = json.loads((SCHEMAS / "instruction-hub.schema.json").read_text())

    assert schema["properties"]["stable_packages"]["minItems"] == 1
    assert schema["properties"]["stable_packages"]["contains"] == {"const": "pig"}
    assert schema["properties"]["stable_packages"]["default"] == ["pig"]
    assert schema["properties"]["targets"]["minItems"] == 1
