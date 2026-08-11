from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub, validate_hub, verify_hub
from promptless_instruction_hub.errors import BuildCheckFailedError, InstructionHubError
from promptless_instruction_hub.scan.hub import scan_hub

from .helpers import (
    FIXTURES,
    _assert_codex_plugin_ingestion_contract,
    _assert_no_promptless_directory,
    _git,
    _snapshot_tree,
)


def test_build_emits_target_outputs_and_deterministic_manifests(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    scan_hub(hub_root, FIXTURES / "dogfood-source")

    first = build_hub(hub_root)
    second = build_hub(hub_root, check=True)

    assert first.release_hash == second.release_hash
    assert (hub_root / "dist/claude/pig/.claude-plugin/plugin.json").exists()
    assert (hub_root / "dist/codex/pig/.codex-plugin/plugin.json").exists()
    _assert_codex_plugin_ingestion_contract(hub_root / "dist/codex/pig")
    codex_skill = (hub_root / "dist/codex/pig/skills/review-docs/SKILL.md").read_text()
    assert codex_skill.startswith('---\nname: "review-docs"\ndescription: "Review Docs"\n---\n\n# Review Docs\n')
    assert (hub_root / "dist/gemini/pig/gemini-extension.json").exists()
    assert (hub_root / "dist/cursor/pig/.cursor-plugin/plugin.json").exists()
    assert (hub_root / "dist/cursor/pig/skills/review-docs/SKILL.md").exists()
    assert not (hub_root / "dist/cursor/pig/rules/review-docs.mdc").exists()
    codex_marketplace = json.loads((hub_root / ".agents/plugins/marketplace.json").read_text())
    assert codex_marketplace["plugins"][0]["name"] == "promptless-instruction-hub-pig"
    assert codex_marketplace["plugins"][0]["source"]["path"] == "./dist/codex/pig"
    assert codex_marketplace["plugins"][0]["policy"]["installation"] == "AVAILABLE"
    assert codex_marketplace["plugins"][0]["policy"]["authentication"] == "ON_INSTALL"
    assert codex_marketplace["plugins"][0]["category"] == "Productivity"
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in codex_marketplace["plugins"]] == [
        ("promptless-instruction-hub-pig", "./dist/codex/pig"),
    ]
    claude_marketplace = json.loads((hub_root / ".claude-plugin/marketplace.json").read_text())
    assert claude_marketplace["owner"]["name"] == "Promptless"
    assert claude_marketplace["plugins"][0]["name"] == "promptless-instruction-hub-pig"
    assert claude_marketplace["plugins"][0]["displayName"] == "PIG"
    assert claude_marketplace["plugins"][0]["source"] == "./dist/claude/pig"
    assert [(plugin["name"], plugin["displayName"], plugin["source"]) for plugin in claude_marketplace["plugins"]] == [
        ("promptless-instruction-hub-pig", "PIG", "./dist/claude/pig"),
    ]
    cursor_marketplace = json.loads((hub_root / ".cursor-plugin/marketplace.json").read_text())
    assert cursor_marketplace["owner"]["name"] == "Promptless"
    assert cursor_marketplace["plugins"][0]["name"] == "promptless-instruction-hub-pig"
    assert cursor_marketplace["plugins"][0]["source"] == "dist/cursor/pig"
    claude_manifest = json.loads((hub_root / "dist/claude/pig/.claude-plugin/plugin.json").read_text())
    assert claude_manifest["name"] == "promptless-instruction-hub-pig"
    assert claude_manifest["displayName"] == "PIG"
    assert claude_manifest["skills"] == "./skills/"
    assert claude_manifest["mcpServers"] == "./.mcp.json"
    codex_manifest = json.loads((hub_root / "dist/codex/pig/.codex-plugin/plugin.json").read_text())
    assert codex_manifest["name"] == "promptless-instruction-hub-pig"
    assert codex_manifest["skills"] == "./skills/"
    assert codex_manifest["hooks"] == "./hooks/hooks.json"
    assert codex_manifest["mcpServers"] == "./.mcp.json"
    assert codex_manifest["author"]["name"] == "Promptless"
    assert codex_manifest["interface"]["displayName"] == "PIG"
    assert (
        codex_manifest["description"]
        == "Promptless Instruction Governance instructions and lifecycle integration for Promptless."
    )
    assert (
        codex_manifest["interface"]["longDescription"]
        == "Promptless Instruction Governance instructions and lifecycle integration for Promptless."
    )
    assert codex_manifest["interface"]["capabilities"] == ["Skills", "MCP servers", "Hooks"]
    assert codex_manifest["interface"]["defaultPrompt"] == [
        "Use PIG instructions and lifecycle integration for this session."
    ]
    assert (hub_root / "dist/codex/pig/hooks/hooks.json").exists()
    assert (hub_root / "dist/codex/pig/runtime/promptless-host-runtime").exists()
    assert (hub_root / "dist/claude/pig/hooks/hooks.json").exists()
    assert (hub_root / "dist/claude/pig/runtime/promptless-host-runtime").exists()
    cursor_manifest = json.loads((hub_root / "dist/cursor/pig/.cursor-plugin/plugin.json").read_text())
    assert cursor_manifest["name"] == "promptless-instruction-hub-pig"
    assert cursor_manifest["displayName"] == "PIG"
    assert cursor_manifest["skills"] == "./skills/"
    gemini_manifest = json.loads((hub_root / "dist/gemini/pig/gemini-extension.json").read_text())
    assert "skills" not in gemini_manifest
    assert gemini_manifest["mcpServers"]["fixture-trace"]["env"]["PROMPTLESS_API_KEY"] == "${PROMPTLESS_API_KEY}"
    assert (hub_root / "dist/codex/pig/hub.release.json").exists()
    _assert_no_promptless_directory(hub_root)
    mcp_config = json.loads((hub_root / "dist/codex/pig/.mcp.json").read_text())
    assert mcp_config["mcpServers"]["fixture-trace"]["env"]["PROMPTLESS_API_KEY"] == "${PROMPTLESS_API_KEY}"
    assert mcp_config["mcpServers"]["fixture-docs"]["url"] == "https://example.invalid/mcp"
    assert "promptless-instruction-hub-status" not in mcp_config["mcpServers"]
    release_manifest = json.loads((hub_root / "hub.release.json").read_text())
    assert "git_commit" not in release_manifest
    assert set(release_manifest["target_hashes"]) == {"claude", "codex", "cursor", "gemini"}
    assert release_manifest["version_basis"]["target_hashes"] == release_manifest["target_hashes"]
    assert release_manifest["version_basis"]["managed_runtimes"] == release_manifest["managed_runtimes"]
    assert {runtime["package_id"] for runtime in release_manifest["managed_runtimes"]} == {"pig"}
    assert {runtime["plugin_id"] for runtime in release_manifest["managed_runtimes"]} == {
        "promptless-instruction-hub-pig"
    }
    assert {runtime["plugin_name"] for runtime in release_manifest["managed_runtimes"]} == {"PIG"}
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
                "  - pig",
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

    assert [stable_package.definition.id for stable_package in validation.stable_packages] == ["dev", "ops", "pig"]
    assert [asset.ref for asset in validation.stable_assets] == ["skill:authoring-tools", "skill:runbooks"]
    assert (hub_root / "dist/codex/dev/skills/authoring-tools/SKILL.md").exists()
    assert not (hub_root / "dist/codex/dev/skills/runbooks/SKILL.md").exists()
    assert (hub_root / "dist/codex/ops/skills/runbooks/SKILL.md").exists()
    assert not (hub_root / "dist/codex/ops/skills/authoring-tools/SKILL.md").exists()
    _assert_codex_plugin_ingestion_contract(hub_root / "dist/codex/dev")
    _assert_codex_plugin_ingestion_contract(hub_root / "dist/codex/ops")
    _assert_codex_plugin_ingestion_contract(hub_root / "dist/codex/pig")
    for target in ("claude", "codex"):
        assert not (hub_root / "dist" / target / "dev" / "hooks/hooks.json").exists()
        assert not (hub_root / "dist" / target / "ops" / "hooks/hooks.json").exists()
        assert (hub_root / "dist" / target / "pig" / "hooks/hooks.json").exists()

    codex_marketplace = json.loads((hub_root / ".agents/plugins/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in codex_marketplace["plugins"]] == [
        ("promptless-instruction-hub-dev", "./dist/codex/dev"),
        ("promptless-instruction-hub-ops", "./dist/codex/ops"),
        ("promptless-instruction-hub-pig", "./dist/codex/pig"),
    ]
    claude_marketplace = json.loads((hub_root / ".claude-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["displayName"], plugin["source"]) for plugin in claude_marketplace["plugins"]] == [
        ("promptless-instruction-hub-dev", "Dev", "./dist/claude/dev"),
        ("promptless-instruction-hub-ops", "Ops", "./dist/claude/ops"),
        ("promptless-instruction-hub-pig", "PIG", "./dist/claude/pig"),
    ]
    cursor_marketplace = json.loads((hub_root / ".cursor-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]) for plugin in cursor_marketplace["plugins"]] == [
        ("promptless-instruction-hub-dev", "dist/cursor/dev"),
        ("promptless-instruction-hub-ops", "dist/cursor/ops"),
        ("promptless-instruction-hub-pig", "dist/cursor/pig"),
    ]
    release_manifest = json.loads((hub_root / "hub.release.json").read_text())
    assert release_manifest["stable_packages"] == ["dev", "ops", "pig"]
    assert [(package["id"], package["name"]) for package in release_manifest["version_basis"]["packages"]] == [
        ("dev", "Dev"),
        ("ops", "Ops"),
        ("pig", "PIG"),
    ]
    assert [asset["ref"] for asset in release_manifest["version_basis"]["packages"][0]["assets"]] == [
        "skill:authoring-tools"
    ]
    assert [asset["ref"] for asset in release_manifest["version_basis"]["packages"][1]["assets"]] == ["skill:runbooks"]
    assert release_manifest["version_basis"]["packages"][2]["assets"] == []
    assert [asset["ref"] for asset in release_manifest["assets"]] == [
        "skill:authoring-tools",
        "skill:runbooks",
    ]


def test_default_source_path_anchors_to_hub_assets_dir(tmp_path: Path) -> None:
    hub_root = tmp_path / "assets" / "customer" / "hub"
    init_hub(hub_root)
    (hub_root / "packages/pig.yaml").write_text("id: pig\nname: PIG\nincludes:\n  - skill:review-docs\n")
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
    (hub_root / "dist/codex/pig/extra.txt").write_text("stale")

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


def test_verify_fully_compiles_without_changing_stale_worktree(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    scan_hub(hub_root, FIXTURES / "dogfood-source")
    (hub_root / "dist/stale.txt").write_text("verify must preserve this file\n")
    before = _snapshot_tree(hub_root)

    result = verify_hub(hub_root)

    assert result.target_count == 4
    assert result.asset_count == 2
    assert result.release_id
    assert result.release_hash
    assert _snapshot_tree(hub_root) == before


def test_verify_failure_does_not_change_worktree(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "packages/pig.yaml").write_text("id: pig\nname: PIG\nincludes:\n  - skill:missing\n")
    before = _snapshot_tree(hub_root)

    with pytest.raises(InstructionHubError, match="missing"):
        verify_hub(hub_root)

    assert _snapshot_tree(hub_root) == before


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
                "  - pig",
                "targets:",
                "  - codex",
                "  - cursor",
                "",
            ]
        )
    )
    (hub_root / "packages/pig.yaml").write_text(
        "\n".join(
            [
                "id: pig",
                "name: PIG",
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

    assert (hub_root / "dist/codex/pig/projected/codex/team-style.md").read_text().startswith("# Team Style")
    assert "alwaysApply: false" in (hub_root / "dist/cursor/pig/rules/team-style.mdc").read_text()
    codex_mcp_config = json.loads((hub_root / "dist/codex/pig/.mcp.json").read_text())
    assert codex_mcp_config["mcpServers"]["trace-reporter"]["env"]["PROMPTLESS_API_KEY"] == "${PROMPTLESS_API_KEY}"
    cursor_mcp_config = json.loads((hub_root / "dist/cursor/pig/mcp.json").read_text())
    assert "trace-reporter" in cursor_mcp_config["mcpServers"]
