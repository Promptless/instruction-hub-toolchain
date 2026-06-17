from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from promptless_instruction_hub.cli import main
from promptless_instruction_hub.compiler import build_hub, init_hub, validate_hub
from promptless_instruction_hub.errors import BuildCheckFailedError, InstructionHubError
from promptless_instruction_hub.mcp_status import STATUS_TOOL_NAME, run_status_mcp
from promptless_instruction_hub.scan.hub import scan_hub

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests/fixtures"
SCHEMAS = REPO_ROOT / "schemas"


def test_init_creates_empty_hub_contract(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"

    init_hub(hub_root, org="Acme")
    validation = validate_hub(hub_root)

    assert (hub_root / ".promptless/instruction-hub.yaml").exists()
    assert (hub_root / ".agents/plugins").is_dir()
    assert (hub_root / ".claude-plugin").is_dir()
    assert (hub_root / ".cursor-plugin").is_dir()
    assert (hub_root / "assets/skills").is_dir()
    assert (hub_root / "packages/core.yaml").exists()
    assert validation.config.plugin_id == "acme-instruction-hub"
    assert validation.stable_assets == ()


def test_scan_imports_skills_and_inventories_repo_context(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)

    result = scan_hub(hub_root, FIXTURES / "dogfood-source")

    assert result.imported_skills == ("review-docs",)
    assert result.imported_mcps == ("repo-mcp",)
    assert result.inventoried_context_files == ("AGENTS.md", "CLAUDE.md")
    assert (hub_root / "assets/skills/review-docs/SKILL.md").read_text().startswith("# Review Docs")
    core_package = (hub_root / "packages/core.yaml").read_text()
    assert "mcp:repo-mcp" in core_package
    assert "skill:review-docs" in core_package
    assert not (hub_root / "assets/skills/review-docs/asset.yaml").exists()
    assert (hub_root / "assets/mcps/repo-mcp.json").exists()
    assert not (hub_root / "assets/mcps/repo-mcp.asset.yaml").exists()
    assert not (hub_root / "assets/mcps/cursor-mcp.json").exists()
    inventory = json.loads((hub_root / ".promptless/inventory/repo-context.json").read_text())
    assert "source_root" not in inventory
    assert inventory["files"][0]["imported"] is False


def test_scan_imports_cursor_only_mcp_config(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    source_root = tmp_path / "source"
    (source_root / ".cursor").mkdir(parents=True)
    (source_root / ".cursor/mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "cursor-debug": {
                        "command": "cursor-debug",
                        "args": ["--token", "${CURSOR_DEBUG_TOKEN}"],
                    }
                }
            }
        )
    )
    init_hub(hub_root)

    result = scan_hub(hub_root, source_root)
    build_hub(hub_root)

    assert result.imported_skills == ()
    assert result.imported_mcps == ("cursor-mcp",)
    assert "mcp:cursor-mcp" in (hub_root / "packages/core.yaml").read_text()
    mcp_metadata = (hub_root / "assets/mcps/cursor-mcp.asset.yaml").read_text()
    assert "id:" not in mcp_metadata
    assert "type:" not in mcp_metadata
    assert "source_path: .cursor/mcp.json" in mcp_metadata
    assert "claude:" in mcp_metadata
    assert "mode: unsupported" in mcp_metadata
    assert not (hub_root / "dist/codex/.mcp.json").exists()
    cursor_mcp_config = json.loads((hub_root / "dist/cursor/mcp.json").read_text())
    assert cursor_mcp_config["mcpServers"]["cursor-debug"]["args"] == ["--token", "${CURSOR_DEBUG_TOKEN}"]


def test_scan_imports_cursor_mcp_override_when_root_differs(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / ".cursor").mkdir()
    (source_root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared": {
                        "command": "root-server",
                    }
                }
            }
        )
    )
    (source_root / ".cursor/mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared": {
                        "command": "cursor-server",
                    }
                }
            }
        )
    )
    init_hub(hub_root)

    result = scan_hub(hub_root, source_root)
    build_hub(hub_root)

    assert result.imported_mcps == ("repo-mcp", "cursor-mcp")
    core_package = (hub_root / "packages/core.yaml").read_text()
    assert "mcp:repo-mcp" in core_package
    assert "mcp:cursor-mcp" in core_package
    codex_mcp_config = json.loads((hub_root / "dist/codex/.mcp.json").read_text())
    assert codex_mcp_config["shared"]["command"] == "root-server"
    cursor_mcp_config = json.loads((hub_root / "dist/cursor/mcp.json").read_text())
    assert cursor_mcp_config["mcpServers"]["shared"]["command"] == "cursor-server"


def test_scan_normalizes_lowercase_skill_file_to_canonical_name(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    source_root = tmp_path / "source"
    skill_root = source_root / ".agents/skills/lowercase"
    skill_root.mkdir(parents=True)
    (skill_root / "skill.md").write_text("# Lowercase\n")
    init_hub(hub_root)

    scan_hub(hub_root, source_root)

    imported_names = {path.name for path in (hub_root / "assets/skills/lowercase").iterdir()}
    assert "SKILL.md" in imported_names
    assert "skill.md" not in imported_names
    build_hub(hub_root)


def test_build_emits_target_outputs_and_deterministic_manifests(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    scan_hub(hub_root, FIXTURES / "dogfood-source")

    first = build_hub(hub_root)
    second = build_hub(hub_root, check=True)

    assert first.release_hash == second.release_hash
    assert (hub_root / "dist/claude/.claude-plugin/plugin.json").exists()
    assert (hub_root / "dist/codex/.codex-plugin/plugin.json").exists()
    assert (hub_root / "dist/gemini/gemini-extension.json").exists()
    assert (hub_root / "dist/cursor/.cursor-plugin/plugin.json").exists()
    assert (hub_root / "dist/cursor/skills/review-docs/SKILL.md").exists()
    assert not (hub_root / "dist/cursor/rules/review-docs.mdc").exists()
    codex_marketplace = json.loads((hub_root / ".agents/plugins/marketplace.json").read_text())
    assert codex_marketplace["plugins"][0]["source"]["path"] == "./dist/codex"
    assert codex_marketplace["plugins"][0]["policy"]["installation"] == "AVAILABLE"
    assert codex_marketplace["plugins"][0]["policy"]["authentication"] == "ON_INSTALL"
    assert codex_marketplace["plugins"][0]["category"] == "Productivity"
    claude_marketplace = json.loads((hub_root / ".claude-plugin/marketplace.json").read_text())
    assert claude_marketplace["owner"]["name"] == "Promptless"
    assert claude_marketplace["plugins"][0]["source"] == "./dist/claude"
    cursor_marketplace = json.loads((hub_root / ".cursor-plugin/marketplace.json").read_text())
    assert cursor_marketplace["owner"]["name"] == "Promptless"
    assert cursor_marketplace["plugins"][0]["source"] == "dist/cursor"
    claude_manifest = json.loads((hub_root / "dist/claude/.claude-plugin/plugin.json").read_text())
    assert claude_manifest["skills"] == "./skills/"
    assert claude_manifest["mcpServers"] == "./.mcp.json"
    codex_manifest = json.loads((hub_root / "dist/codex/.codex-plugin/plugin.json").read_text())
    assert codex_manifest["skills"] == "./skills/"
    assert codex_manifest["mcpServers"] == "./.mcp.json"
    assert codex_manifest["interface"]["displayName"] == "Promptless Instruction Hub"
    cursor_manifest = json.loads((hub_root / "dist/cursor/.cursor-plugin/plugin.json").read_text())
    assert cursor_manifest["skills"] == "./skills/"
    gemini_manifest = json.loads((hub_root / "dist/gemini/gemini-extension.json").read_text())
    assert "skills" not in gemini_manifest
    assert gemini_manifest["mcpServers"]["fixture-trace"]["env"]["PROMPTLESS_API_KEY"] == "${PROMPTLESS_API_KEY}"
    assert (hub_root / "dist/codex/.promptless/release.json").exists()
    mcp_config = json.loads((hub_root / "dist/codex/.mcp.json").read_text())
    assert mcp_config["fixture-trace"]["env"]["PROMPTLESS_API_KEY"] == "${PROMPTLESS_API_KEY}"
    assert mcp_config["fixture-docs"]["url"] == "https://example.invalid/mcp"
    assert "promptless-instruction-hub-status" not in mcp_config
    release_manifest = json.loads((hub_root / ".promptless/releases/current.json").read_text())
    assert "git_commit" not in release_manifest
    assert set(release_manifest["target_hashes"]) == {"claude", "codex", "cursor", "gemini"}
    assert {asset["title"] for asset in release_manifest["assets"]} == {"Repository MCP Servers", "Review Docs"}


def test_default_source_path_anchors_to_hub_assets_dir(tmp_path: Path) -> None:
    hub_root = tmp_path / "assets" / "customer" / "hub"
    init_hub(hub_root)
    (hub_root / "packages/core.yaml").write_text("id: core\nname: Core\nincludes:\n  - skill:review-docs\n")
    skill_root = hub_root / "assets/skills/review-docs"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Review Docs\n")

    validation = validate_hub(hub_root)

    assert validation.assets["skill:review-docs"].metadata.source_path == "assets/skills/review-docs"


def test_build_check_fails_when_generated_output_is_stale(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    scan_hub(hub_root, FIXTURES / "dogfood-source")
    build_hub(hub_root)
    (hub_root / "dist/codex/extra.txt").write_text("stale")

    with pytest.raises(BuildCheckFailedError, match="stale"):
        build_hub(hub_root, check=True)


@pytest.mark.parametrize(
    ("generated_path", "expected_stale_path"),
    [
        (Path(".agents/plugins/marketplace.json"), ".agents/plugins"),
        (Path(".claude-plugin/marketplace.json"), ".claude-plugin"),
        (Path(".cursor-plugin/marketplace.json"), ".cursor-plugin"),
    ],
)
def test_build_check_fails_when_root_marketplace_is_stale(
    tmp_path: Path,
    generated_path: Path,
    expected_stale_path: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    scan_hub(hub_root, FIXTURES / "dogfood-source")
    build_hub(hub_root)
    (hub_root / generated_path).write_text("{}\n")

    with pytest.raises(BuildCheckFailedError, match=re.escape(expected_stale_path)):
        build_hub(hub_root, check=True)


def test_build_check_passes_after_generated_output_is_committed(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    scan_hub(hub_root, FIXTURES / "dogfood-source")
    _git(hub_root, "init")
    _git(hub_root, "config", "user.email", "instruction-hub@example.com")
    _git(hub_root, "config", "user.name", "Instruction Hub Test")

    build_hub(hub_root)
    _git(hub_root, "add", ".")
    _git(hub_root, "commit", "-m", "generated instruction hub output")

    build_hub(hub_root, check=True)


def test_build_renders_projected_rules_native_cursor_rules_and_mcp_assets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    (hub_root / ".promptless/instruction-hub.yaml").write_text(
        "\n".join(
            [
                "org: Acme",
                "plugin_id: acme-instruction-hub",
                "plugin_name: Acme Instruction Hub",
                "plugin_version: 0.1.0",
                "stable_packages:",
                "  - core",
                "targets:",
                "  - codex",
                "  - cursor",
                "",
            ]
        )
    )
    (hub_root / "packages/core.yaml").write_text(
        "\n".join(
            [
                "id: core",
                "name: Core",
                "includes:",
                "  - rule:team-style",
                "  - mcp:trace-reporter",
                "",
            ]
        )
    )
    (hub_root / "assets/rules/team-style.md").write_text("# Team Style\n\nUse short, direct comments.\n")
    (hub_root / "assets/rules/team-style.asset.yaml").write_text(
        "\n".join(
            [
                "id: team-style",
                "type: rule",
                "title: Team Style",
                "support:",
                "  codex:",
                "    mode: projected",
                "  cursor:",
                "    mode: native",
                "",
            ]
        )
    )
    (hub_root / "assets/mcps/trace-reporter.json").write_text(
        json.dumps(
            {
                "trace-reporter": {
                    "command": "trace-reporter",
                    "args": ["--org", "${PROMPTLESS_ORG_ID}"],
                    "env": {"PROMPTLESS_API_KEY": "${PROMPTLESS_API_KEY}"},
                }
            }
        )
    )

    build_hub(hub_root)

    assert (hub_root / "dist/codex/projected/codex/team-style.md").read_text().startswith("# Team Style")
    assert "alwaysApply: false" in (hub_root / "dist/cursor/rules/team-style.mdc").read_text()
    codex_mcp_config = json.loads((hub_root / "dist/codex/.mcp.json").read_text())
    assert codex_mcp_config["trace-reporter"]["env"]["PROMPTLESS_API_KEY"] == "${PROMPTLESS_API_KEY}"
    cursor_mcp_config = json.loads((hub_root / "dist/cursor/mcp.json").read_text())
    assert "trace-reporter" in cursor_mcp_config["mcpServers"]


@pytest.mark.parametrize("generated_paths", ["/tmp/dist", "..", ".", "dist/../assets", "dist//codex"])
def test_action_script_rejects_generated_paths_outside_hub(generated_paths: str) -> None:
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/run.sh")],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "GITHUB_ACTION_PATH": str(REPO_ROOT),
            "INPUT_MODE": "check",
            "INPUT_HUB_ROOT": ".",
            "INPUT_GENERATED_PATHS": generated_paths,
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "generated-path" in result.stderr
    assert "inside hub-root" in result.stderr


def test_validate_rejects_unknown_package_refs(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "packages/core.yaml").write_text("id: core\nname: Core\nincludes:\n  - skill:missing\n")

    with pytest.raises(InstructionHubError, match="unknown asset refs"):
        validate_hub(hub_root)


def test_validate_merges_sparse_target_support_with_defaults(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "packages/core.yaml").write_text("id: core\nname: Core\nincludes:\n  - rule:partial\n")
    (hub_root / "assets/rules/partial.md").write_text("# Partial\n")
    (hub_root / "assets/rules/partial.asset.yaml").write_text(
        "\n".join(
            [
                "title: Partial",
                "support:",
                "  codex:",
                "    mode: projected",
                "",
            ]
        )
    )

    validation = validate_hub(hub_root)

    asset = validation.assets["rule:partial"]
    assert asset.metadata.support["codex"].mode == "projected"
    assert asset.metadata.support["cursor"].mode == "unsupported"


def test_validate_rejects_unsafe_asset_ids(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    skill_root = hub_root / "assets/skills/bad"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Bad\n")
    (skill_root / "asset.yaml").write_text("id: ../bad\ntype: skill\n")

    with pytest.raises(ValueError, match="asset id"):
        validate_hub(hub_root)


def test_validate_rejects_metadata_type_mismatch(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/rules/team-style.md").write_text("# Team Style\n")
    (hub_root / "assets/rules/team-style.asset.yaml").write_text("id: team-style\ntype: skill\n")

    with pytest.raises(InstructionHubError, match="declares type"):
        validate_hub(hub_root)


def test_validate_rejects_malformed_asset_candidates(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/skills/broken").mkdir(parents=True)

    with pytest.raises(InstructionHubError, match="must contain SKILL.md"):
        validate_hub(hub_root)


def test_validate_rejects_symlinked_assets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    outside_asset = tmp_path / "outside.json"
    init_hub(hub_root)
    outside_asset.write_text("{}\n")
    (hub_root / "assets/mcps/leak.json").symlink_to(outside_asset)

    with pytest.raises(InstructionHubError, match="symlink"):
        validate_hub(hub_root)


def test_validate_rejects_literal_mcp_secrets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    mcp_path = hub_root / "assets/mcps/bad.yaml"
    mcp_path.write_text("api_token: sk-live-secret\n")

    with pytest.raises(InstructionHubError, match="literal secret"):
        validate_hub(hub_root)


def test_validate_rejects_yaml_values_outside_json_manifest_contract(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/bad.yaml").write_text("1: one\n")

    with pytest.raises(ValueError, match="non-string mapping key"):
        validate_hub(hub_root)


def test_cli_init_scan_build_validate_and_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hub_root = tmp_path / "hub"

    assert main(["init", "--hub", str(hub_root), "--org", "Acme"]) == 0
    assert main(["scan", "--hub", str(hub_root), "--source", str(FIXTURES / "dogfood-source")]) == 0
    assert main(["validate", "--hub", str(hub_root)]) == 0
    assert main(["build", "--hub", str(hub_root)]) == 0
    assert main(["build", "--hub", str(hub_root), "--check"]) == 0
    assert main(["status", "--manifest", str(hub_root / ".promptless/releases/current.json")]) == 0

    output = capsys.readouterr().out
    assert "valid Instruction Hub" in output
    assert "release_hash" in output


def test_empty_hub_fixture_bootstraps(tmp_path: Path) -> None:
    hub_root = tmp_path / "empty-hub"
    shutil.copytree(FIXTURES / "empty-hub", hub_root)

    init_hub(hub_root)
    result = build_hub(hub_root)

    assert result.asset_count == 0
    assert (hub_root / ".promptless/releases/current.json").exists()


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

    run_status_mcp(hub_root / ".promptless/releases/current.json")

    response = json.loads(capsys.readouterr().out)
    status = json.loads(response["result"]["content"][0]["text"])
    assert status["release_hash"]
    assert status["plugin_version"] == "0.1.0"
    assert "git_commit" not in status


def test_release_manifest_schema_matches_generated_contract() -> None:
    schema = json.loads((SCHEMAS / "release-manifest.schema.json").read_text())

    assert schema["additionalProperties"] is False
    assert "target_hashes" in schema["required"]
    assert "git_commit" not in schema["properties"]
    asset_schema = schema["properties"]["assets"]["items"]
    assert asset_schema["required"] == ["ref", "id", "type", "title", "source_path", "content_hash", "support"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
