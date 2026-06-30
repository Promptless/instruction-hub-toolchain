from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from promptless_instruction_hub.cli import main
from promptless_instruction_hub.compiler import build_hub, init_hub, validate_hub
from promptless_instruction_hub.errors import BuildCheckFailedError, InstructionHubError
from promptless_instruction_hub.mcp_status import STATUS_TOOL_NAME, run_status_mcp
from promptless_instruction_hub.release.hashing import stable_hash
from promptless_instruction_hub.release.versions import resolve_publish_plugin_version
from promptless_instruction_hub.scan.hub import scan_hub

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests/fixtures"
SCHEMAS = REPO_ROOT / "schemas"
WORKFLOWS = REPO_ROOT / ".github/workflows"


def _write_release_manifest_with_fresh_identity(manifest_path: Path, manifest: dict[str, Any]) -> None:
    plugin = manifest.get("plugin")
    assert isinstance(plugin, dict)
    plugin_version = plugin.get("version")
    assert isinstance(plugin_version, str)
    manifest.pop("release_id", None)
    manifest.pop("release_hash", None)
    content_hash = stable_hash(manifest)
    manifest["release_id"] = f"{plugin_version}+{content_hash[:12]}"
    manifest["release_hash"] = stable_hash(manifest)
    manifest_path.write_text(json.dumps(manifest))


def _assert_no_promptless_directory(root: Path) -> None:
    assert list(root.rglob(".promptless")) == []


def _assert_codex_plugin_ingestion_contract(plugin_root: Path) -> None:
    manifest_path = plugin_root / ".codex-plugin/plugin.json"
    manifest_data: object = json.loads(manifest_path.read_text())
    assert isinstance(manifest_data, dict)

    for field in ("name", "version", "description"):
        _assert_non_empty_string(manifest_data.get(field), f"plugin.json {field}")
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", manifest_data["version"])

    author_data = manifest_data.get("author")
    assert isinstance(author_data, dict)
    _assert_non_empty_string(author_data.get("name"), "plugin.json author.name")

    interface_data = manifest_data.get("interface")
    assert isinstance(interface_data, dict)
    for field in ("displayName", "shortDescription", "longDescription", "developerName", "category"):
        _assert_non_empty_string(interface_data.get(field), f"plugin.json interface.{field}")

    capabilities_data = interface_data.get("capabilities")
    assert isinstance(capabilities_data, list)
    assert all(isinstance(capability, str) and capability for capability in capabilities_data)
    assert "defaultPrompt" in interface_data or "default_prompt" in interface_data

    if manifest_data.get("skills") is not None:
        assert manifest_data["skills"] == "./skills/"
        skill_files = sorted((plugin_root / "skills").glob("*/SKILL.md"))
        assert skill_files
        for skill_file in skill_files:
            skill_contents = skill_file.read_text()
            assert skill_contents.startswith("---\n")
            assert any(line == "---" for line in skill_contents.splitlines()[1:])

    if "mcpServers" not in manifest_data:
        return
    assert manifest_data["mcpServers"] == "./.mcp.json"
    mcp_data: object = json.loads((plugin_root / ".mcp.json").read_text())
    assert isinstance(mcp_data, dict)
    assert set(mcp_data) == {"mcpServers"}
    servers_data = mcp_data["mcpServers"]
    assert isinstance(servers_data, dict)
    assert all(isinstance(server_name, str) and server_name for server_name in servers_data)
    assert all(isinstance(server_config, dict) for server_config in servers_data.values())


def _assert_non_empty_string(value: object, field_path: str) -> None:
    assert isinstance(value, str), f"{field_path} must be a string"
    assert value, f"{field_path} must not be empty"


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


def test_init_creates_empty_hub_contract(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"

    init_hub(hub_root, org="Acme")
    validation = validate_hub(hub_root)

    assert (hub_root / "hub.yaml").exists()
    assert not (hub_root / ".promptless").exists()
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
    inventory = json.loads((hub_root / "hub.repo-context.json").read_text())
    assert "source_root" not in inventory
    assert inventory["files"][0]["imported"] is False
    _assert_no_promptless_directory(hub_root)


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
    assert not (hub_root / "dist/codex/core/.mcp.json").exists()
    cursor_mcp_config = json.loads((hub_root / "dist/cursor/core/mcp.json").read_text())
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
    codex_mcp_config = json.loads((hub_root / "dist/codex/core/.mcp.json").read_text())
    assert codex_mcp_config["mcpServers"]["shared"]["command"] == "root-server"
    cursor_mcp_config = json.loads((hub_root / "dist/cursor/core/mcp.json").read_text())
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


def test_scan_rejects_skill_slug_collisions(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    source_root = tmp_path / "source"
    first_skill = source_root / ".agents/skills/Review Docs"
    second_skill = source_root / ".agents/skills/review-docs"
    first_skill.mkdir(parents=True)
    second_skill.mkdir(parents=True)
    (first_skill / "SKILL.md").write_text("# First\n")
    (second_skill / "SKILL.md").write_text("# Second\n")
    init_hub(hub_root)

    with pytest.raises(InstructionHubError, match="both map to asset id"):
        scan_hub(hub_root, source_root)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable on this platform")
def test_scan_rejects_symlinked_source_skill_directories(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    source_root = tmp_path / "source"
    outside_skill = tmp_path / "outside-skill"
    outside_skill.mkdir()
    (outside_skill / "SKILL.md").write_text("# Outside\n")
    (source_root / ".agents/skills").mkdir(parents=True)
    os.symlink(outside_skill, source_root / ".agents/skills/outside")
    init_hub(hub_root)

    with pytest.raises(InstructionHubError, match="symlink"):
        scan_hub(hub_root, source_root)

    assert not (hub_root / "assets/skills/outside").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable on this platform")
@pytest.mark.parametrize(
    ("mcp_path", "asset_path"),
    [
        (Path(".mcp.json"), Path("assets/mcps/repo-mcp.json")),
        (Path(".cursor/mcp.json"), Path("assets/mcps/cursor-mcp.json")),
    ],
)
def test_scan_rejects_symlinked_source_mcp_configs(
    tmp_path: Path,
    mcp_path: Path,
    asset_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    source_root = tmp_path / "source"
    outside_mcp = tmp_path / "outside-mcp.json"
    outside_mcp.write_text(json.dumps({"mcpServers": {"leak": {"command": "leak"}}}))
    (source_root / mcp_path.parent).mkdir(parents=True, exist_ok=True)
    os.symlink(outside_mcp, source_root / mcp_path)
    init_hub(hub_root)

    with pytest.raises(InstructionHubError, match="symlink"):
        scan_hub(hub_root, source_root)

    assert not (hub_root / asset_path).exists()


def test_build_emits_target_outputs_and_deterministic_manifests(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    scan_hub(hub_root, FIXTURES / "dogfood-source")

    first = build_hub(hub_root)
    second = build_hub(hub_root, check=True)

    assert first.release_hash == second.release_hash
    assert (hub_root / "dist/claude/core/.claude-plugin/plugin.json").exists()
    assert (hub_root / "dist/codex/core/.codex-plugin/plugin.json").exists()
    _assert_codex_plugin_ingestion_contract(hub_root / "dist/codex/core")
    codex_skill = (hub_root / "dist/codex/core/skills/review-docs/SKILL.md").read_text()
    assert codex_skill.startswith('---\nname: "review-docs"\ndescription: "Review Docs"\n---\n\n# Review Docs\n')
    assert (hub_root / "dist/gemini/core/gemini-extension.json").exists()
    assert (hub_root / "dist/cursor/core/.cursor-plugin/plugin.json").exists()
    assert (hub_root / "dist/cursor/core/skills/review-docs/SKILL.md").exists()
    assert not (hub_root / "dist/cursor/core/rules/review-docs.mdc").exists()
    codex_marketplace = json.loads((hub_root / ".agents/plugins/marketplace.json").read_text())
    assert codex_marketplace["plugins"][0]["name"] == "promptless-instruction-hub-core"
    assert codex_marketplace["plugins"][0]["source"]["path"] == "./dist/codex/core"
    assert codex_marketplace["plugins"][0]["policy"]["installation"] == "AVAILABLE"
    assert codex_marketplace["plugins"][0]["policy"]["authentication"] == "ON_INSTALL"
    assert codex_marketplace["plugins"][0]["category"] == "Productivity"
    claude_marketplace = json.loads((hub_root / ".claude-plugin/marketplace.json").read_text())
    assert claude_marketplace["owner"]["name"] == "Promptless"
    assert claude_marketplace["plugins"][0]["name"] == "promptless-instruction-hub-core"
    assert claude_marketplace["plugins"][0]["displayName"] == "Core"
    assert claude_marketplace["plugins"][0]["source"] == "./dist/claude/core"
    cursor_marketplace = json.loads((hub_root / ".cursor-plugin/marketplace.json").read_text())
    assert cursor_marketplace["owner"]["name"] == "Promptless"
    assert cursor_marketplace["plugins"][0]["name"] == "promptless-instruction-hub-core"
    assert cursor_marketplace["plugins"][0]["source"] == "dist/cursor/core"
    claude_manifest = json.loads((hub_root / "dist/claude/core/.claude-plugin/plugin.json").read_text())
    assert claude_manifest["name"] == "promptless-instruction-hub-core"
    assert claude_manifest["displayName"] == "Core"
    assert claude_manifest["skills"] == "./skills/"
    assert claude_manifest["mcpServers"] == "./.mcp.json"
    codex_manifest = json.loads((hub_root / "dist/codex/core/.codex-plugin/plugin.json").read_text())
    assert codex_manifest["name"] == "promptless-instruction-hub-core"
    assert codex_manifest["skills"] == "./skills/"
    assert codex_manifest["hooks"] == "./hooks/hooks.json"
    assert codex_manifest["mcpServers"] == "./.mcp.json"
    assert codex_manifest["author"]["name"] == "Promptless"
    assert codex_manifest["interface"]["displayName"] == "Core"
    assert (
        codex_manifest["interface"]["longDescription"] == "Core distributes governed agent instructions for Promptless."
    )
    assert codex_manifest["interface"]["capabilities"] == ["Skills", "MCP servers", "Hooks"]
    assert codex_manifest["interface"]["defaultPrompt"] == ["Use Core instructions for this task."]
    cursor_manifest = json.loads((hub_root / "dist/cursor/core/.cursor-plugin/plugin.json").read_text())
    assert cursor_manifest["name"] == "promptless-instruction-hub-core"
    assert cursor_manifest["displayName"] == "Core"
    assert cursor_manifest["skills"] == "./skills/"
    gemini_manifest = json.loads((hub_root / "dist/gemini/core/gemini-extension.json").read_text())
    assert "skills" not in gemini_manifest
    assert gemini_manifest["mcpServers"]["fixture-trace"]["env"]["PROMPTLESS_API_KEY"] == "${PROMPTLESS_API_KEY}"
    assert (hub_root / "dist/codex/core/hub.release.json").exists()
    _assert_no_promptless_directory(hub_root)
    mcp_config = json.loads((hub_root / "dist/codex/core/.mcp.json").read_text())
    assert mcp_config["mcpServers"]["fixture-trace"]["env"]["PROMPTLESS_API_KEY"] == "${PROMPTLESS_API_KEY}"
    assert mcp_config["mcpServers"]["fixture-docs"]["url"] == "https://example.invalid/mcp"
    assert "promptless-instruction-hub-status" not in mcp_config["mcpServers"]
    release_manifest = json.loads((hub_root / "hub.release.json").read_text())
    assert "git_commit" not in release_manifest
    assert set(release_manifest["target_hashes"]) == {"claude", "codex", "cursor", "gemini"}
    assert release_manifest["version_basis"]["target_hashes"] == release_manifest["target_hashes"]
    assert release_manifest["version_basis"]["managed_runtimes"] == release_manifest["managed_runtimes"]
    assert {asset["title"] for asset in release_manifest["assets"]} == {"Repository MCP Servers", "Review Docs"}


def test_build_renders_stable_packages_as_separate_marketplace_plugins(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    (hub_root / "hub.yaml").write_text(
        "\n".join(
            [
                "org: Promptless",
                "plugin_id: promptless-instruction-hub",
                "plugin_name: Promptless Instruction Hub",
                "plugin_version: 0.1.0",
                "stable_packages:",
                "  - dev",
                "  - ops",
                "targets:",
                "  - claude",
                "  - codex",
                "  - gemini",
                "  - cursor",
                "",
            ]
        )
    )
    (hub_root / "packages/dev.yaml").write_text("id: dev\nname: Dev\nincludes:\n  - skill:authoring-tools\n")
    (hub_root / "packages/ops.yaml").write_text("id: ops\nname: Ops\nincludes:\n  - skill:runbooks\n")
    (hub_root / "assets/skills/authoring-tools").mkdir(parents=True)
    (hub_root / "assets/skills/authoring-tools/SKILL.md").write_text("# Authoring Tools\n")
    (hub_root / "assets/skills/runbooks").mkdir(parents=True)
    (hub_root / "assets/skills/runbooks/SKILL.md").write_text("# Runbooks\n")

    validation = validate_hub(hub_root)
    build_hub(hub_root)

    assert [stable_package.definition.id for stable_package in validation.stable_packages] == ["dev", "ops"]
    assert [asset.ref for asset in validation.stable_assets] == ["skill:authoring-tools", "skill:runbooks"]
    assert (hub_root / "dist/codex/dev/skills/authoring-tools/SKILL.md").exists()
    assert not (hub_root / "dist/codex/dev/skills/runbooks/SKILL.md").exists()
    assert (hub_root / "dist/codex/ops/skills/runbooks/SKILL.md").exists()
    assert not (hub_root / "dist/codex/ops/skills/authoring-tools/SKILL.md").exists()
    _assert_codex_plugin_ingestion_contract(hub_root / "dist/codex/dev")
    _assert_codex_plugin_ingestion_contract(hub_root / "dist/codex/ops")

    codex_marketplace = json.loads((hub_root / ".agents/plugins/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in codex_marketplace["plugins"]] == [
        ("promptless-instruction-hub-dev", "./dist/codex/dev"),
        ("promptless-instruction-hub-ops", "./dist/codex/ops"),
    ]
    claude_marketplace = json.loads((hub_root / ".claude-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["displayName"], plugin["source"]) for plugin in claude_marketplace["plugins"]] == [
        ("promptless-instruction-hub-dev", "Dev", "./dist/claude/dev"),
        ("promptless-instruction-hub-ops", "Ops", "./dist/claude/ops"),
    ]
    cursor_marketplace = json.loads((hub_root / ".cursor-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]) for plugin in cursor_marketplace["plugins"]] == [
        ("promptless-instruction-hub-dev", "dist/cursor/dev"),
        ("promptless-instruction-hub-ops", "dist/cursor/ops"),
    ]
    release_manifest = json.loads((hub_root / "hub.release.json").read_text())
    assert release_manifest["stable_packages"] == ["dev", "ops"]
    assert [(package["id"], package["name"]) for package in release_manifest["version_basis"]["packages"]] == [
        ("dev", "Dev"),
        ("ops", "Ops"),
    ]
    assert [asset["ref"] for asset in release_manifest["version_basis"]["packages"][0]["assets"]] == [
        "skill:authoring-tools"
    ]
    assert [asset["ref"] for asset in release_manifest["version_basis"]["packages"][1]["assets"]] == ["skill:runbooks"]
    assert [asset["ref"] for asset in release_manifest["assets"]] == [
        "skill:authoring-tools",
        "skill:runbooks",
    ]


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
    (hub_root / "dist/codex/core/extra.txt").write_text("stale")

    with pytest.raises(BuildCheckFailedError, match="stale"):
        build_hub(hub_root, check=True)


@pytest.mark.parametrize(
    ("generated_path", "expected_stale_path"),
    [
        (Path("hub.release.json"), "hub.release.json"),
        (Path("hub.stable.json"), "hub.stable.json"),
        (Path(".agents/plugins/marketplace.json"), ".agents/plugins"),
        (Path(".claude-plugin/marketplace.json"), ".claude-plugin"),
        (Path(".cursor-plugin/marketplace.json"), ".cursor-plugin"),
    ],
)
def test_build_check_fails_when_root_generated_output_is_stale(
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
    (hub_root / "hub.yaml").write_text(
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

    assert (hub_root / "dist/codex/core/projected/codex/team-style.md").read_text().startswith("# Team Style")
    assert "alwaysApply: false" in (hub_root / "dist/cursor/core/rules/team-style.mdc").read_text()
    codex_mcp_config = json.loads((hub_root / "dist/codex/core/.mcp.json").read_text())
    assert codex_mcp_config["mcpServers"]["trace-reporter"]["env"]["PROMPTLESS_API_KEY"] == "${PROMPTLESS_API_KEY}"
    cursor_mcp_config = json.loads((hub_root / "dist/cursor/core/mcp.json").read_text())
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


def test_action_script_rejects_invalid_release_branch() -> None:
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/run.sh")],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "GITHUB_ACTION_PATH": str(REPO_ROOT),
            "INPUT_MODE": "check",
            "INPUT_HUB_ROOT": ".",
            "INPUT_RELEASE_BRANCH": "bad\nbranch",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Invalid release-branch" in result.stderr


@pytest.mark.parametrize(
    ("env_name", "label"),
    [
        ("INPUT_UPDATE_CLAUDE_POINTER", "update-claude-pointer"),
        ("INPUT_UPDATE_CODEX_POINTER", "update-codex-pointer"),
        ("INPUT_UPDATE_CURSOR_POINTER", "update-cursor-pointer"),
    ],
)
def test_action_script_rejects_invalid_update_marketplace_pointer(env_name: str, label: str) -> None:
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/run.sh")],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "GITHUB_ACTION_PATH": str(REPO_ROOT),
            "INPUT_MODE": "check",
            "INPUT_HUB_ROOT": ".",
            env_name: "tru",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert f"Invalid {label}" in result.stderr


def test_action_check_rejects_hub_root_outside_checkout(tmp_path: Path) -> None:
    outside_hub = tmp_path / "outside-hub"
    outside_hub.mkdir()

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/run.sh")],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "GITHUB_ACTION_PATH": str(REPO_ROOT),
            "INPUT_MODE": "check",
            "INPUT_HUB_ROOT": str(outside_hub),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "hub-root must be inside the git checkout" in result.stderr


def test_action_build_cleans_untracked_files_under_tracked_generated_paths(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "build-clean-tracked-generated-path", targets=("claude", "codex"))
    build_hub(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "track generated instruction hub output")
    stale_generated_file = repo / "dist/claude/core/obsolete/dead.json"
    stale_generated_file.parent.mkdir(parents=True)
    stale_generated_file.write_text("{}\n")

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/run.sh")],
        cwd=repo,
        env={
            **os.environ,
            "GITHUB_ACTION_PATH": str(REPO_ROOT),
            "GITHUB_WORKSPACE": str(repo),
            "INPUT_MODE": "build",
            "INPUT_HUB_ROOT": ".",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not stale_generated_file.exists()
    assert _git_output(repo, "status", "--short") == ""


def test_action_publish_rejects_non_branch_ref(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-tag-ref", targets=("claude",))

    result = _run_action(
        repo,
        tmp_path / "github-output.txt",
        extra_env={
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF_TYPE": "tag",
            "GITHUB_REF_NAME": "v1.0.0",
        },
    )

    assert result.returncode == 2
    assert "Publish mode must run from branch ref 'main'" in result.stderr


def test_action_publish_rejects_unexpected_source_branch(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-wrong-branch", targets=("claude",))

    result = _run_action(
        repo,
        tmp_path / "github-output.txt",
        extra_env={
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF_TYPE": "branch",
            "GITHUB_REF_NAME": "feature/instructions",
        },
    )

    assert result.returncode == 2
    assert "Publish mode must run from source branch 'main'" in result.stderr


def test_action_publish_rejects_release_branch_equal_to_source_branch(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-same-branch", targets=("claude",))

    result = _run_action(repo, tmp_path / "github-output.txt", release_branch="main")

    assert result.returncode == 2
    assert "release-branch must differ from source-branch" in result.stderr


def test_action_publish_writes_release_branch_and_marketplace_pointers_for_stable_packages(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish", targets=("claude", "codex", "cursor"))
    output_path = tmp_path / "github-output.txt"
    _configure_split_package_hub(repo, ("claude", "codex", "cursor"))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "split stable packages")

    result = _run_action(repo, output_path)

    assert result.returncode == 0, result.stdout + result.stderr
    _git(repo, "fetch", "origin", "release/stable")
    for target in ("claude", "codex", "cursor"):
        for package in ("dev", "ops"):
            manifest_name = {
                "claude": ".claude-plugin/plugin.json",
                "codex": ".codex-plugin/plugin.json",
                "cursor": ".cursor-plugin/plugin.json",
            }[target]
            assert json.loads(
                _git_output(repo, "show", f"origin/release/stable:dist/{target}/{package}/{manifest_name}")
            )

    claude_pointer = json.loads((repo / ".claude-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in claude_pointer["plugins"]] == [
        ("acme-instruction-hub-dev", "dist/claude/dev"),
        ("acme-instruction-hub-ops", "dist/claude/ops"),
    ]
    assert all(plugin["source"]["source"] == "git-subdir" for plugin in claude_pointer["plugins"])
    assert all(
        plugin["source"]["url"] == "https://github.com/Promptless/instruction-hub-test.git"
        for plugin in claude_pointer["plugins"]
    )
    assert all(plugin["source"]["ref"] == "release/stable" for plugin in claude_pointer["plugins"])
    assert all("version" not in plugin for plugin in claude_pointer["plugins"])

    codex_pointer = json.loads((repo / ".agents/plugins/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in codex_pointer["plugins"]] == [
        ("acme-instruction-hub-dev", "dist/codex/dev"),
        ("acme-instruction-hub-ops", "dist/codex/ops"),
    ]
    assert all(plugin["source"]["source"] == "git-subdir" for plugin in codex_pointer["plugins"])
    assert all(
        plugin["source"]["url"] == "https://github.com/Promptless/instruction-hub-test.git"
        for plugin in codex_pointer["plugins"]
    )
    assert all(plugin["source"]["ref"] == "release/stable" for plugin in codex_pointer["plugins"])
    assert all("version" not in plugin for plugin in codex_pointer["plugins"])

    cursor_pointer = json.loads((repo / ".cursor-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in cursor_pointer["plugins"]] == [
        ("acme-instruction-hub-dev", "dist/cursor/dev"),
        ("acme-instruction-hub-ops", "dist/cursor/ops"),
    ]
    assert all(plugin["source"]["owner"] == "Promptless" for plugin in cursor_pointer["plugins"])
    assert all(plugin["source"]["repo"] == "instruction-hub-test" for plugin in cursor_pointer["plugins"])
    assert all(plugin["source"]["ref"] == "release/stable" for plugin in cursor_pointer["plugins"])
    assert all(plugin["source"]["type"] == "github" for plugin in cursor_pointer["plugins"])
    assert all("version" not in plugin for plugin in cursor_pointer["plugins"])
    assert output_path.read_text() == "release-branch=release/stable\n"


def test_action_publish_bumps_and_rewrites_outputs_when_package_id_changes(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-package-id-rename", targets=("claude", "codex", "cursor"))
    _configure_split_package_hub(repo, ("claude", "codex", "cursor"))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "split stable packages")

    first = _run_action(repo, tmp_path / "github-output-first.txt")
    assert first.returncode == 0, first.stdout + first.stderr
    _git(repo, "fetch", "origin", "release/stable")
    assert _release_branch_plugin_versions(repo, packages=("dev", "ops"), targets=("claude", "codex", "cursor")) == {
        "0.1.0"
    }

    (repo / "hub.yaml").write_text((repo / "hub.yaml").read_text().replace("  - dev\n", "  - developer\n"))
    (repo / "packages/dev.yaml").rename(repo / "packages/developer.yaml")
    (repo / "packages/developer.yaml").write_text(
        "id: developer\nname: Developer\nincludes:\n  - skill:authoring-tools\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "rename dev package id")

    second = _run_action(repo, tmp_path / "github-output-second.txt")
    assert second.returncode == 0, second.stdout + second.stderr
    _git(repo, "fetch", "origin", "release/stable")

    assert _release_branch_plugin_versions(
        repo,
        packages=("developer", "ops"),
        targets=("claude", "codex", "cursor"),
    ) == {"0.1.1"}
    assert _release_branch_path_exists(repo, "dist/claude/developer/.claude-plugin/plugin.json")
    assert not _release_branch_path_exists(repo, "dist/claude/dev/.claude-plugin/plugin.json")
    claude_pointer = json.loads((repo / ".claude-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in claude_pointer["plugins"]] == [
        ("acme-instruction-hub-developer", "dist/claude/developer"),
        ("acme-instruction-hub-ops", "dist/claude/ops"),
    ]
    codex_pointer = json.loads((repo / ".agents/plugins/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in codex_pointer["plugins"]] == [
        ("acme-instruction-hub-developer", "dist/codex/developer"),
        ("acme-instruction-hub-ops", "dist/codex/ops"),
    ]
    cursor_pointer = json.loads((repo / ".cursor-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in cursor_pointer["plugins"]] == [
        ("acme-instruction-hub-developer", "dist/cursor/developer"),
        ("acme-instruction-hub-ops", "dist/cursor/ops"),
    ]


@pytest.mark.parametrize(
    ("env_name", "disabled_pointer", "enabled_pointers"),
    [
        (
            "INPUT_UPDATE_CLAUDE_POINTER",
            Path(".claude-plugin/marketplace.json"),
            (Path(".agents/plugins/marketplace.json"), Path(".cursor-plugin/marketplace.json")),
        ),
        (
            "INPUT_UPDATE_CODEX_POINTER",
            Path(".agents/plugins/marketplace.json"),
            (Path(".claude-plugin/marketplace.json"), Path(".cursor-plugin/marketplace.json")),
        ),
        (
            "INPUT_UPDATE_CURSOR_POINTER",
            Path(".cursor-plugin/marketplace.json"),
            (Path(".claude-plugin/marketplace.json"), Path(".agents/plugins/marketplace.json")),
        ),
    ],
)
def test_action_publish_respects_disabled_marketplace_pointer(
    tmp_path: Path,
    env_name: str,
    disabled_pointer: Path,
    enabled_pointers: tuple[Path, Path],
) -> None:
    repo = _init_action_repo(tmp_path / f"publish-disable-{env_name.lower()}", targets=("claude", "codex", "cursor"))

    result = _run_action(repo, tmp_path / "github-output.txt", extra_env={env_name: "false"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (repo / disabled_pointer).exists()
    for pointer_path in enabled_pointers:
        assert (repo / pointer_path).exists()


def test_action_publish_fails_cursor_pointer_when_repository_is_not_owner_repo(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-bad-cursor-repository", targets=("cursor",))

    result = _run_action(repo, tmp_path / "github-output.txt", extra_env={"GITHUB_REPOSITORY": "bad"})

    assert result.returncode == 2
    assert "GITHUB_REPOSITORY must be owner/repo" in result.stderr
    assert not _remote_branch_exists(repo, "release/stable")
    assert not (repo / ".cursor-plugin/marketplace.json").exists()


def test_action_publish_uses_github_server_url_for_push_credentials(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-enterprise", targets=("claude",))
    fake_git_bin = tmp_path / "fake-git-bin"
    fake_git_bin.mkdir()
    fake_git_log = tmp_path / "fake-git.log"
    real_git = shutil.which("git")
    assert real_git is not None
    fake_git = fake_git_bin / "git"
    fake_git.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
args=("$@")
command_index=0
while [[ "$command_index" -lt "${#args[@]}" ]]; do
  case "${args[$command_index]}" in
    -C | -c | --git-dir | --work-tree)
      command_index=$((command_index + 2))
      ;;
    --*)
      command_index=$((command_index + 1))
      ;;
    *)
      break
      ;;
  esac
done
command="${args[$command_index]:-}"
subcommand="${args[$((command_index + 1))]:-}"
if [[ "$command" == "remote" && "$subcommand" == "set-url" ]]; then
  printf '%s\\n' "$*" >> "$FAKE_GIT_LOG"
  exit 0
fi
if [[ "$command" == "push" ]]; then
  printf '%s\\n' "$*" >> "$FAKE_GIT_LOG"
  exit 0
fi
exec "$REAL_GIT" "$@"
"""
    )
    fake_git.chmod(0o755)

    result = _run_action(
        repo,
        tmp_path / "github-output.txt",
        extra_env={
            "FAKE_GIT_LOG": str(fake_git_log),
            "GITHUB_SERVER_URL": "https://github.enterprise.example",
            "INPUT_GITHUB_TOKEN": "enterprise-token",
            "PATH": f"{fake_git_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "REAL_GIT": real_git,
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    expected_remote = (
        "https://x-access-token:enterprise-token@github.enterprise.example/Promptless/instruction-hub-test.git"
    )
    log_text = fake_git_log.read_text()
    assert expected_remote in log_text
    assert "https://x-access-token:enterprise-token@github.com/" not in log_text
    pointer = json.loads((repo / ".claude-plugin/marketplace.json").read_text())
    assert (
        pointer["plugins"][0]["source"]["url"]
        == "https://github.enterprise.example/Promptless/instruction-hub-test.git"
    )


def test_action_publish_authenticates_before_release_branch_inspection(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-private-existing-branch", targets=("claude", "codex"))
    first = _run_action(repo, tmp_path / "github-output-first.txt")
    assert first.returncode == 0, first.stdout + first.stderr

    fake_git_bin = tmp_path / "private-fake-git-bin"
    fake_git_bin.mkdir()
    fake_git_log = tmp_path / "private-fake-git.log"
    fake_auth_marker = tmp_path / "private-fake-git-authenticated"
    real_git = shutil.which("git")
    assert real_git is not None
    fake_git = fake_git_bin / "git"
    fake_git.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
args=("$@")
command_index=0
while [[ "$command_index" -lt "${#args[@]}" ]]; do
  case "${args[$command_index]}" in
    -C | -c | --git-dir | --work-tree)
      command_index=$((command_index + 2))
      ;;
    --*)
      command_index=$((command_index + 1))
      ;;
    *)
      break
      ;;
  esac
done
command="${args[$command_index]:-}"
subcommand="${args[$((command_index + 1))]:-}"
if [[ "$command" == "remote" && "$subcommand" == "set-url" ]]; then
  remote_url="${args[$((command_index + 3))]:-}"
  printf 'set-url %s\\n' "$remote_url" >> "$FAKE_GIT_LOG"
  if [[ "$remote_url" == https://x-access-token:private-token@* ]]; then
    printf 'authenticated\\n' > "$FAKE_GIT_AUTH_MARKER"
  else
    rm -f "$FAKE_GIT_AUTH_MARKER"
  fi
  exit 0
fi
if [[ "$command" == "ls-remote" || "$command" == "fetch" ]]; then
  printf '%s %s\\n' "$command" "$*" >> "$FAKE_GIT_LOG"
  if [[ ! -f "$FAKE_GIT_AUTH_MARKER" ]]; then
    echo "authentication required" >&2
    exit 128
  fi
fi
exec "$REAL_GIT" "$@"
"""
    )
    fake_git.chmod(0o755)

    second = _run_action(
        repo,
        tmp_path / "github-output-second.txt",
        extra_env={
            "FAKE_GIT_AUTH_MARKER": str(fake_auth_marker),
            "FAKE_GIT_LOG": str(fake_git_log),
            "INPUT_GITHUB_TOKEN": "private-token",
            "PATH": f"{fake_git_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "REAL_GIT": real_git,
        },
    )

    assert second.returncode == 0, second.stdout + second.stderr
    log_lines = fake_git_log.read_text().splitlines()
    for remote_command in ("ls-remote", "fetch"):
        command_index = next(index for index, line in enumerate(log_lines) if line.startswith(remote_command))
        assert command_index > 0
        assert "x-access-token:private-token" in log_lines[command_index - 1]


def test_action_publish_second_run_is_noop(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-noop", targets=("claude", "codex"))

    first = _run_action(repo, tmp_path / "github-output-first.txt")
    second = _run_action(repo, tmp_path / "github-output-second.txt")

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert "No release branch changes to publish." in second.stdout
    assert "No marketplace pointer changes to publish." in second.stdout


def test_action_publish_bumps_generated_plugin_version_when_assets_change(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-version-bump", targets=("claude", "codex", "cursor", "gemini"))
    (repo / "packages/core.yaml").write_text("id: core\nname: Core\nincludes:\n  - skill:review-docs\n")
    skill_root = repo / "assets/skills/review-docs"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Review Docs\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add review docs")

    first = _run_action(repo, tmp_path / "github-output-first.txt")
    assert first.returncode == 0, first.stdout + first.stderr
    _git(repo, "fetch", "origin", "release/stable")
    assert _release_branch_plugin_versions(repo) == {"0.1.0"}

    (skill_root / "SKILL.md").write_text("# Review Docs\n\nPrefer concise summaries.\n")
    _git(repo, "add", "assets/skills/review-docs/SKILL.md")
    _git(repo, "commit", "-m", "update review docs")

    second = _run_action(repo, tmp_path / "github-output-second.txt")
    assert second.returncode == 0, second.stdout + second.stderr
    _git(repo, "fetch", "origin", "release/stable")

    assert _release_branch_plugin_versions(repo) == {"0.1.1"}
    release_manifest = json.loads(_git_output(repo, "show", "origin/release/stable:hub.release.json"))
    stable_channel = json.loads(_git_output(repo, "show", "origin/release/stable:hub.stable.json"))
    assert release_manifest["plugin"]["version"] == "0.1.1"
    assert stable_channel["plugin_version"] == "0.1.1"
    assert "plugin_version: 0.1.0" in (repo / "hub.yaml").read_text()

    third = _run_action(repo, tmp_path / "github-output-third.txt")
    assert third.returncode == 0, third.stdout + third.stderr
    assert "No release branch changes to publish." in third.stdout


def test_action_publish_removes_legacy_promptless_release_metadata(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-cleans-legacy-release-metadata", targets=("claude",))
    _git(repo, "switch", "--orphan", "release/stable")
    for path in repo.iterdir():
        if path.name == ".git":
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    (repo / ".promptless/releases").mkdir(parents=True)
    (repo / ".promptless/channels").mkdir(parents=True)
    (repo / ".promptless/releases/current.json").write_text("{}\n")
    (repo / ".promptless/channels/stable.json").write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed legacy release metadata")
    _git(repo, "push", "-u", "origin", "release/stable")
    _git(repo, "switch", "main")

    result = _run_action(repo, tmp_path / "github-output.txt")

    assert result.returncode == 0, result.stdout + result.stderr
    _git(repo, "fetch", "origin", "release/stable")
    assert _release_branch_path_exists(repo, "hub.release.json")
    assert not _release_branch_path_exists(repo, ".promptless/releases/current.json")
    assert not _release_branch_path_exists(repo, ".promptless/channels/stable.json")


def test_publish_version_bumps_when_package_name_changes(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    _configure_split_package_hub(hub_root, targets=("claude", "codex"))
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)

    (hub_root / "packages/dev.yaml").write_text(
        "id: dev\nname: Developer Tools\nincludes:\n  - skill:authoring-tools\n"
    )

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.1.1"


def test_publish_version_bumps_when_package_membership_changes(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    _configure_split_package_hub(hub_root, targets=("claude", "codex"))
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)

    (hub_root / "packages/dev.yaml").write_text("id: dev\nname: Dev\nincludes:\n  - skill:runbooks\n")
    (hub_root / "packages/ops.yaml").write_text("id: ops\nname: Ops\nincludes:\n  - skill:authoring-tools\n")

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.1.1"


def test_publish_version_prefers_manual_semver_promotion(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, plugin_version="1.0.0-alpha.1")
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    (hub_root / "hub.yaml").write_text(
        (hub_root / "hub.yaml").read_text().replace("plugin_version: 1.0.0-alpha.1", "plugin_version: 1.0.0")
    )

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "1.0.0"


def test_publish_version_prefers_higher_configured_version_floor(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, plugin_version="0.1.1")
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    (hub_root / "hub.yaml").write_text(
        (hub_root / "hub.yaml").read_text().replace("plugin_version: 0.1.1", "plugin_version: 0.2.0")
    )

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.2.0"


def test_publish_version_accepts_legacy_managed_runtime_id_in_previous_release(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    for runtime in manifest["managed_runtimes"]:
        runtime["id"] = "host-enrollment-bootstrap"
    for runtime in manifest["version_basis"]["managed_runtimes"]:
        runtime["id"] = "host-enrollment-bootstrap"
    _write_release_manifest_with_fresh_identity(manifest_path, manifest)

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.1.1"


def test_publish_version_rejects_invalid_authoritative_release_manifest(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_text(
        json.dumps({"plugin": {"id": "acme", "name": "Acme", "version": "not-semver"}, "version_basis": {}})
    )

    with pytest.raises(ValueError, match=r"hub\.release\.json: plugin\.version must be SemVer"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_rejects_malformed_authoritative_version_basis(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_text(
        json.dumps({"plugin": {"id": "acme", "name": "Acme", "version": "0.1.0"}, "version_basis": {}})
    )

    with pytest.raises(ValueError, match=r"hub\.release\.json: version_basis must contain exactly"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


@pytest.mark.parametrize("field", ["stable_packages", "targets"])
def test_publish_version_rejects_empty_authoritative_version_basis_required_lists(
    tmp_path: Path,
    field: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version_basis"][field] = []
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=rf"hub\.release\.json: version_basis\.{field} must not be empty"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_reports_nested_authoritative_version_basis_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version_basis"]["plugin"]["id"] = None
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=r"hub\.release\.json: version_basis\.plugin\.id must be a string"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("schema_version", r"hub\.release\.json: schema_version must be 1"),
        ("assets_object", r"hub\.release\.json: assets must be a list"),
        ("assets_empty", r"hub\.release\.json: assets refs must match version_basis package assets"),
        (
            "target_hash_missing",
            r"hub\.release\.json: version_basis\.target_hashes keys must match version_basis\.targets",
        ),
        (
            "managed_runtime_bad_sha",
            r"hub\.release\.json: version_basis\.managed_runtimes\[0\]\.sha256 must be a sha256 hex digest",
        ),
        ("release_id_empty", r"hub\.release\.json: release_id must not be empty"),
        ("release_hash_mismatch", r"hub\.release\.json: release_hash must match manifest content"),
    ],
)
def test_publish_version_rejects_authoritative_release_manifest_tampering(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    _configure_split_package_hub(hub_root, targets=("claude", "codex"))
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "schema_version":
        manifest["schema_version"] = 999
    elif mutation == "assets_object":
        manifest["assets"] = {}
    elif mutation == "assets_empty":
        manifest["assets"] = []
    elif mutation == "target_hash_missing":
        del manifest["version_basis"]["target_hashes"]["claude"]
    elif mutation == "managed_runtime_bad_sha":
        manifest["version_basis"]["managed_runtimes"][0]["sha256"] = "bad"
    elif mutation == "release_id_empty":
        manifest["release_id"] = ""
    elif mutation == "release_hash_mismatch":
        manifest["release_hash"] = "0" * 64
    else:
        raise AssertionError(f"unhandled mutation: {mutation}")
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=message):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_rejects_authoritative_release_manifest_unexpected_root_key(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["unexpected"] = True
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=r"hub\.release\.json: release manifest must contain exactly"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_uses_config_when_previous_release_has_no_version_metadata(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, plugin_version="0.2.0")
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "README.md").write_text("# Previous release\n")

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.2.0"


def test_publish_version_ignores_repo_context_inventory(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    _configure_split_package_hub(hub_root, targets=("claude", "codex"))
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    (hub_root / "hub.repo-context.json").write_text(
        json.dumps({"schema_version": 1, "files": [{"path": "AGENTS.md", "imported": False}]})
    )

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.1.0"


def test_publish_version_rejects_release_manifest_without_version_basis(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_text(
        json.dumps({"plugin": {"id": "acme", "name": "Acme", "version": "0.1.0"}})
    )

    with pytest.raises(ValueError, match=r"hub\.release\.json: version_basis is missing"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_reports_malformed_previous_release_json_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_text("{")

    with pytest.raises(ValueError, match=r"hub\.release\.json contains malformed JSON"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_reports_malformed_previous_release_json_encoding_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_bytes(b"\xff")

    with pytest.raises(ValueError, match=r"hub\.release\.json contains malformed JSON"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_rejects_missing_previous_hub_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "repo/hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()

    with pytest.raises(ValueError, match="previous release is missing hub path: hub"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root, hub_relative_path="hub")


def test_publish_version_uses_config_floor_when_previous_release_has_no_manifest(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, plugin_version="0.3.0")
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "dist").mkdir()

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.3.0"


def test_action_publish_supports_subdirectory_hub_root_and_custom_release_branch(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-subdir", targets=("claude", "codex", "cursor"), hub_root_name="hub")

    result = _run_action(repo, tmp_path / "github-output.txt", hub_root="hub", release_branch="release/custom")

    assert result.returncode == 0, result.stdout + result.stderr
    _git(repo, "fetch", "origin", "release/custom")
    assert json.loads(
        _git_output(repo, "show", "origin/release/custom:hub/dist/claude/core/.claude-plugin/plugin.json")
    )
    assert json.loads(_git_output(repo, "show", "origin/release/custom:hub/dist/codex/core/.codex-plugin/plugin.json"))
    assert json.loads(
        _git_output(repo, "show", "origin/release/custom:hub/dist/cursor/core/.cursor-plugin/plugin.json")
    )
    claude_pointer = json.loads((repo / "hub/.claude-plugin/marketplace.json").read_text())
    assert claude_pointer["plugins"][0]["source"]["path"] == "hub/dist/claude/core"
    assert claude_pointer["plugins"][0]["source"]["ref"] == "release/custom"
    codex_pointer = json.loads((repo / "hub/.agents/plugins/marketplace.json").read_text())
    assert codex_pointer["plugins"][0]["source"]["path"] == "hub/dist/codex/core"
    assert codex_pointer["plugins"][0]["source"]["ref"] == "release/custom"
    cursor_pointer = json.loads((repo / "hub/.cursor-plugin/marketplace.json").read_text())
    assert cursor_pointer["plugins"][0]["source"]["path"] == "hub/dist/cursor/core"
    assert cursor_pointer["plugins"][0]["source"]["ref"] == "release/custom"


def test_action_publish_skips_claude_pointer_when_claude_target_is_absent(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-codex-only", targets=("codex",))

    result = _run_action(repo, tmp_path / "github-output.txt")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No Claude marketplace was generated" in result.stdout
    assert not (repo / ".claude-plugin/marketplace.json").exists()
    pointer = json.loads((repo / ".agents/plugins/marketplace.json").read_text())
    assert pointer["plugins"][0]["source"]["path"] == "dist/codex/core"


def test_action_publish_removes_stale_pointer_when_target_is_removed(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-removed-target", targets=("claude", "codex"))
    first = _run_action(repo, tmp_path / "github-output-first.txt")
    assert first.returncode == 0, first.stdout + first.stderr
    assert (repo / ".claude-plugin/marketplace.json").exists()

    _write_hub_config(repo, ("codex",))
    _git(repo, "add", "hub.yaml")
    _git(repo, "commit", "-m", "remove claude target")

    second = _run_action(repo, tmp_path / "github-output-second.txt")

    assert second.returncode == 0, second.stdout + second.stderr
    assert "removing stale source-branch Claude pointer" in second.stdout
    assert not (repo / ".claude-plugin/marketplace.json").exists()
    assert ".claude-plugin/marketplace.json" not in _git_output(repo, "ls-files").splitlines()
    _git(repo, "fetch", "origin", "release/stable")
    release_files = _git_output(repo, "ls-tree", "-r", "--name-only", "origin/release/stable").splitlines()
    assert ".claude-plugin/marketplace.json" not in release_files


def test_validate_rejects_empty_stable_packages(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "hub.yaml").write_text(
        "\n".join(
            [
                "org: Acme",
                "plugin_id: acme-instruction-hub",
                "plugin_name: Acme Instruction Hub",
                "plugin_version: 0.1.0",
                "stable_packages: []",
                "",
            ]
        )
    )

    with pytest.raises(InstructionHubError, match="stable_packages"):
        validate_hub(hub_root)


def test_validate_rejects_empty_package_name(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "packages/core.yaml").write_text("id: core\nname: ''\nincludes: []\n")

    with pytest.raises(InstructionHubError, match="name"):
        validate_hub(hub_root)


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


@pytest.mark.parametrize(
    "config_text",
    [
        "org: ''\nplugin_id: acme-instruction-hub\nplugin_name: Acme Instruction Hub\nplugin_version: 0.1.0\n",
        "org: Acme\nplugin_id: acme-instruction-hub\nplugin_name: ''\nplugin_version: 0.1.0\n",
    ],
)
def test_validate_rejects_empty_required_config_strings(tmp_path: Path, config_text: str) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "hub.yaml").write_text(config_text)

    with pytest.raises(InstructionHubError, match="String should have at least 1 character"):
        validate_hub(hub_root)


def test_validate_rejects_empty_target_list(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "hub.yaml").write_text(
        "\n".join(
            [
                "org: Acme",
                "plugin_id: acme-instruction-hub",
                "plugin_name: Acme Instruction Hub",
                "plugin_version: 0.1.0",
                "targets: []",
                "",
            ]
        )
    )

    with pytest.raises(InstructionHubError, match="at least 1 item"):
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


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable on this platform")
def test_validate_rejects_symlinked_skill_files(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    secret_path = tmp_path / "outside-secret.md"
    skill_root = hub_root / "assets/skills/leak"
    init_hub(hub_root)
    skill_root.mkdir(parents=True)
    secret_path.write_text("# Leaked\n\nexternal content\n")
    os.symlink(secret_path, skill_root / "SKILL.md")

    with pytest.raises(InstructionHubError, match="symlink"):
        validate_hub(hub_root)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable on this platform")
def test_validate_rejects_symlinked_mcp_files(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    outside_asset = tmp_path / "outside.json"
    init_hub(hub_root)
    outside_asset.write_text("{}\n")
    os.symlink(outside_asset, hub_root / "assets/mcps/leak.json")

    with pytest.raises(InstructionHubError, match="symlink"):
        validate_hub(hub_root)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable on this platform")
def test_validate_rejects_symlinked_assets_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    outside_assets = tmp_path / "outside-assets"
    init_hub(hub_root)
    shutil.rmtree(hub_root / "assets")
    outside_assets.mkdir()
    os.symlink(outside_assets, hub_root / "assets")

    with pytest.raises(InstructionHubError, match="assets.*symlink"):
        validate_hub(hub_root)


def test_validate_allows_json_array_files_inside_skill_assets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    skill_root = hub_root / "assets/skills/json-fixture"
    init_hub(hub_root)
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# JSON Fixture\n")
    (skill_root / "examples").mkdir()
    (skill_root / "examples/data.json").write_text(json.dumps([{"name": "safe fixture"}]))

    validate_hub(hub_root)


def test_validate_rejects_literal_mcp_secrets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    mcp_path = hub_root / "assets/mcps/bad.yaml"
    mcp_path.write_text("api_token: sk-live-secret\n")

    with pytest.raises(InstructionHubError, match="literal secret"):
        validate_hub(hub_root)


def test_validate_rejects_literal_mcp_authorization_headers(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/bad.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bad": {
                        "url": "https://example.invalid/mcp",
                        "headers": {"Authorization": "Bearer literal-secret"},
                    }
                }
            }
        )
    )

    with pytest.raises(InstructionHubError, match="literal secret"):
        validate_hub(hub_root)


def test_validate_rejects_literal_secret_mcp_arg_values(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/bad.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bad": {
                        "command": "bad",
                        "args": ["--token", "literal-secret"],
                    }
                }
            }
        )
    )

    with pytest.raises(InstructionHubError, match=r"args\.1"):
        validate_hub(hub_root)


def test_validate_rejects_literal_secret_mcp_inline_arg_values(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/bad.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bad": {
                        "command": "bad",
                        "args": ["--api-key=literal-secret"],
                    }
                }
            }
        )
    )

    with pytest.raises(InstructionHubError, match=r"args\.0"):
        validate_hub(hub_root)


def test_validate_accepts_env_placeholder_mcp_arg_values(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/good.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "good": {
                        "command": "good",
                        "args": ["--token", "${MCP_TOKEN}"],
                    }
                }
            }
        )
    )

    validate_hub(hub_root)


@pytest.mark.parametrize(
    "payload",
    [
        {"mcpServers": []},
        {"servers": "bad"},
        {"bad-server": "bad"},
    ],
)
def test_validate_rejects_malformed_mcp_server_shapes(tmp_path: Path, payload: object) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/bad.json").write_text(json.dumps(payload))

    with pytest.raises(InstructionHubError, match="MCP server"):
        validate_hub(hub_root)


def test_build_rejects_same_priority_duplicate_mcp_servers(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "packages/core.yaml").write_text(
        "\n".join(
            [
                "id: core",
                "name: Core",
                "includes:",
                "  - mcp:first",
                "  - mcp:second",
                "",
            ]
        )
    )
    (hub_root / "assets/mcps/first.json").write_text(json.dumps({"shared": {"command": "first"}}))
    (hub_root / "assets/mcps/second.json").write_text(json.dumps({"shared": {"command": "second"}}))

    with pytest.raises(InstructionHubError, match="duplicate MCP server 'shared'"):
        build_hub(hub_root)


def test_validate_wraps_malformed_yaml_with_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    config_path = hub_root / "hub.yaml"
    config_path.write_text("org: [\n")

    with pytest.raises(InstructionHubError, match=re.escape(str(config_path))):
        validate_hub(hub_root)


def test_validate_rejects_unimplemented_target_support_source(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/rules/source-mode.md").write_text("# Source Mode\n")
    (hub_root / "assets/rules/source-mode.asset.yaml").write_text(
        "\n".join(
            [
                "support:",
                "  codex:",
                "    mode: projected",
                "    source: native",
                "",
            ]
        )
    )

    with pytest.raises(ValueError, match="source"):
        validate_hub(hub_root)


def test_validate_rejects_mcp_support_modes_that_cannot_render(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/trace.json").write_text(json.dumps({"trace": {"command": "trace-agent"}}))
    (hub_root / "assets/mcps/trace.asset.yaml").write_text(
        "\n".join(
            [
                "support:",
                "  codex:",
                "    mode: projected",
                "",
            ]
        )
    )

    with pytest.raises(InstructionHubError, match="mcp:trace declares unsupported mode"):
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
    assert main(["status", "--manifest", str(hub_root / "hub.release.json")]) == 0

    output = capsys.readouterr().out
    assert "valid Instruction Hub" in output
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
    assert schema["properties"]["targets"]["minItems"] == 1


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _git_output(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout


def _remote_branch_exists(cwd: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise AssertionError(result.stdout + result.stderr)


def _release_branch_path_exists(repo: Path, path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"origin/release/stable:{path}"],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _release_branch_plugin_versions(
    repo: Path,
    *,
    packages: tuple[str, ...] = ("core",),
    targets: tuple[str, ...] = ("claude", "codex", "cursor", "gemini"),
) -> set[str]:
    manifest_names = {
        "claude": ".claude-plugin/plugin.json",
        "codex": ".codex-plugin/plugin.json",
        "cursor": ".cursor-plugin/plugin.json",
        "gemini": "gemini-extension.json",
    }
    manifest_paths = [f"dist/{target}/{package}/{manifest_names[target]}" for target in targets for package in packages]
    return {
        json.loads(_git_output(repo, "show", f"origin/release/stable:{manifest_path}"))["version"]
        for manifest_path in manifest_paths
    }


def _init_action_repo(root: Path, *, targets: tuple[str, ...], hub_root_name: str = ".") -> Path:
    remote = root / "remote.git"
    repo = root / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "--bare", str(remote))
    repo.mkdir()
    hub_root = repo if hub_root_name == "." else repo / hub_root_name
    init_hub(hub_root, org="Acme")
    _write_hub_config(hub_root, targets)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "instruction-hub@example.com")
    _git(repo, "config", "user.name", "Instruction Hub Test")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial hub")
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _write_hub_config(hub_root: Path, targets: tuple[str, ...]) -> None:
    target_lines = "\n".join(f"  - {target}" for target in targets)
    (hub_root / "hub.yaml").write_text(
        "\n".join(
            [
                "org: Acme",
                "plugin_id: acme-instruction-hub",
                "plugin_name: Acme Instruction Hub",
                "plugin_version: 0.1.0",
                "stable_packages:",
                "  - core",
                "targets:",
                target_lines,
                "",
            ]
        )
    )


def _configure_split_package_hub(hub_root: Path, targets: tuple[str, ...]) -> None:
    target_lines = "\n".join(f"  - {target}" for target in targets)
    (hub_root / "hub.yaml").write_text(
        "\n".join(
            [
                "org: Acme",
                "plugin_id: acme-instruction-hub",
                "plugin_name: Acme Instruction Hub",
                "plugin_version: 0.1.0",
                "stable_packages:",
                "  - dev",
                "  - ops",
                "targets:",
                target_lines,
                "",
            ]
        )
    )
    (hub_root / "packages/dev.yaml").write_text("id: dev\nname: Dev\nincludes:\n  - skill:authoring-tools\n")
    (hub_root / "packages/ops.yaml").write_text("id: ops\nname: Ops\nincludes:\n  - skill:runbooks\n")
    (hub_root / "assets/skills/authoring-tools").mkdir(parents=True, exist_ok=True)
    (hub_root / "assets/skills/authoring-tools/SKILL.md").write_text("# Authoring Tools\n")
    (hub_root / "assets/skills/runbooks").mkdir(parents=True, exist_ok=True)
    (hub_root / "assets/skills/runbooks/SKILL.md").write_text("# Runbooks\n")


def _run_action(
    repo: Path,
    output_path: Path,
    *,
    hub_root: str = ".",
    release_branch: str = "release/stable",
    source_branch: str = "main",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GITHUB_ACTION_PATH": str(REPO_ROOT),
        "GITHUB_WORKSPACE": str(repo),
        "GITHUB_REPOSITORY": "Promptless/instruction-hub-test",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REF_NAME": source_branch,
        "GITHUB_REF_TYPE": "branch",
        "GITHUB_OUTPUT": str(output_path),
        "INPUT_MODE": "publish",
        "INPUT_HUB_ROOT": hub_root,
        "INPUT_RELEASE_BRANCH": release_branch,
        "INPUT_SOURCE_BRANCH": source_branch,
        "INPUT_UPDATE_CLAUDE_POINTER": "true",
    }
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/run.sh")],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
