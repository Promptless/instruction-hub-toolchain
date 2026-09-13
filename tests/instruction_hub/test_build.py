from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.config_helpers import enable_trace_ingestion

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
    enable_trace_ingestion(hub_root)
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
    assert codex_marketplace["plugins"][0]["name"] == "pig"
    assert codex_marketplace["plugins"][0]["source"]["path"] == "./dist/codex/pig"
    assert codex_marketplace["plugins"][0]["policy"]["installation"] == "AVAILABLE"
    assert codex_marketplace["plugins"][0]["policy"]["authentication"] == "ON_INSTALL"
    assert codex_marketplace["plugins"][0]["category"] == "Productivity"
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in codex_marketplace["plugins"]] == [
        ("pig", "./dist/codex/pig"),
    ]
    claude_marketplace = json.loads((hub_root / ".claude-plugin/marketplace.json").read_text())
    assert claude_marketplace["owner"]["name"] == "Promptless"
    assert claude_marketplace["plugins"][0]["name"] == "pig"
    assert claude_marketplace["plugins"][0]["displayName"] == "PIG"
    assert claude_marketplace["plugins"][0]["source"] == "./dist/claude/pig"
    assert [(plugin["name"], plugin["displayName"], plugin["source"]) for plugin in claude_marketplace["plugins"]] == [
        ("pig", "PIG", "./dist/claude/pig"),
    ]
    cursor_marketplace = json.loads((hub_root / ".cursor-plugin/marketplace.json").read_text())
    assert cursor_marketplace["owner"]["name"] == "Promptless"
    assert cursor_marketplace["plugins"][0]["name"] == "pig"
    assert cursor_marketplace["plugins"][0]["source"] == "dist/cursor/pig"
    claude_manifest = json.loads((hub_root / "dist/claude/pig/.claude-plugin/plugin.json").read_text())
    assert claude_manifest["name"] == "pig"
    assert claude_manifest["displayName"] == "PIG"
    assert claude_manifest["skills"] == "./skills/"
    assert claude_manifest["mcpServers"] == "./.mcp.json"
    codex_manifest = json.loads((hub_root / "dist/codex/pig/.codex-plugin/plugin.json").read_text())
    assert codex_manifest["name"] == "pig"
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
    codex_update_skill = (hub_root / "dist/codex/pig/skills/update-instruction-hub/SKILL.md").read_text()
    assert "name: update-instruction-hub\n" in codex_update_skill
    assert "# Update Instruction Hub\n" in codex_update_skill
    assert "generated marketplace name `promptless-instruction-hub`" in codex_update_skill
    assert "marketplaceName` set to `promptless-instruction-hub`" in codex_update_skill
    assert "refreshes only the configured Git marketplace snapshot" in codex_update_skill
    assert "skills/list" in codex_update_skill
    assert "{{ instruction_hub_" not in codex_update_skill
    claude_update_skill = (hub_root / "dist/claude/pig/skills/update-instruction-hub/SKILL.md").read_text()
    assert "name: update-instruction-hub\n" in claude_update_skill
    assert "# Update Instruction Hub\n" in claude_update_skill
    assert "generated marketplace name `promptless-instruction-hub`" in claude_update_skill
    assert "claude plugin marketplace update promptless-instruction-hub" in claude_update_skill
    assert "claude plugin update <id> --scope <scope>" in claude_update_skill
    assert "/reload-plugins" in claude_update_skill
    assert "{{ instruction_hub_" not in claude_update_skill
    for target in ("cursor", "gemini"):
        assert not (hub_root / "dist" / target / "pig/skills/update-instruction-hub").exists()
    cursor_manifest = json.loads((hub_root / "dist/cursor/pig/.cursor-plugin/plugin.json").read_text())
    assert cursor_manifest["name"] == "pig"
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
    assert {runtime["plugin_id"] for runtime in release_manifest["managed_runtimes"]} == {"pig"}
    assert {runtime["plugin_name"] for runtime in release_manifest["managed_runtimes"]} == {"PIG"}
    assert {asset["title"] for asset in release_manifest["assets"]} == {"Repository MCP Servers", "Review Docs"}


def test_build_renders_stable_plugins_as_separate_marketplace_plugins(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    (hub_root / "hub.yaml").write_text(
        "\n".join(
            [
                "org: Promptless",
                "marketplace:",
                "  id: promptless-instruction-hub",
                "  name: Promptless Instruction Hub",
                "version: 0.1.0",
                "stable_plugins:",
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
    (hub_root / "plugins/dev.yaml").write_text("id: dev\nname: Dev\nincludes:\n  - skill:authoring-tools\n")
    (hub_root / "plugins/ops.yaml").write_text("id: ops\nname: Ops\nincludes:\n  - skill:runbooks\n")
    (hub_root / "assets/skills/authoring-tools").mkdir(parents=True)
    (hub_root / "assets/skills/authoring-tools/SKILL.md").write_text("# Authoring Tools\n")
    (hub_root / "assets/skills/runbooks").mkdir(parents=True)
    (hub_root / "assets/skills/runbooks/SKILL.md").write_text("# Runbooks\n")

    enable_trace_ingestion(hub_root)
    validation = validate_hub(hub_root)
    build_hub(hub_root)

    assert [stable_package.definition.id for stable_package in validation.stable_plugins] == ["dev", "ops", "pig"]
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
        assert not (hub_root / "dist" / target / "dev/skills/update-instruction-hub").exists()
        assert not (hub_root / "dist" / target / "ops/skills/update-instruction-hub").exists()
        assert (hub_root / "dist" / target / "pig/skills/update-instruction-hub/SKILL.md").exists()

    codex_marketplace = json.loads((hub_root / ".agents/plugins/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in codex_marketplace["plugins"]] == [
        ("dev", "./dist/codex/dev"),
        ("ops", "./dist/codex/ops"),
        ("pig", "./dist/codex/pig"),
    ]
    claude_marketplace = json.loads((hub_root / ".claude-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["displayName"], plugin["source"]) for plugin in claude_marketplace["plugins"]] == [
        ("dev", "Dev", "./dist/claude/dev"),
        ("ops", "Ops", "./dist/claude/ops"),
        ("pig", "PIG", "./dist/claude/pig"),
    ]
    cursor_marketplace = json.loads((hub_root / ".cursor-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]) for plugin in cursor_marketplace["plugins"]] == [
        ("dev", "dist/cursor/dev"),
        ("ops", "dist/cursor/ops"),
        ("pig", "dist/cursor/pig"),
    ]
    release_manifest = json.loads((hub_root / "hub.release.json").read_text())
    assert release_manifest["stable_plugins"] == ["dev", "ops", "pig"]
    assert [(package["id"], package["name"]) for package in release_manifest["version_basis"]["plugins"]] == [
        ("dev", "Dev"),
        ("ops", "Ops"),
        ("pig", "PIG"),
    ]
    assert [asset["ref"] for asset in release_manifest["version_basis"]["plugins"][0]["assets"]] == [
        "skill:authoring-tools"
    ]
    assert [asset["ref"] for asset in release_manifest["version_basis"]["plugins"][1]["assets"]] == ["skill:runbooks"]
    assert release_manifest["version_basis"]["plugins"][2]["assets"] == []
    assert [asset["ref"] for asset in release_manifest["assets"]] == [
        "skill:authoring-tools",
        "skill:runbooks",
    ]


def test_default_source_path_anchors_to_hub_assets_dir(tmp_path: Path) -> None:
    hub_root = tmp_path / "assets" / "customer" / "hub"
    init_hub(hub_root)
    (hub_root / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes:\n  - skill:review-docs\n")
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
    (hub_root / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes:\n  - skill:missing\n")
    before = _snapshot_tree(hub_root)

    with pytest.raises(InstructionHubError, match="missing"):
        verify_hub(hub_root)

    assert _snapshot_tree(hub_root) == before


@pytest.mark.parametrize(
    "settings",
    ["alwaysApply: true", 'globs: ["**/*.mdx", "docs.json"]\nalwaysApply: false'],
)
def test_build_preserves_native_cursor_rule_frontmatter(tmp_path: Path, settings: str) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes:\n  - rule:docs-style\n")
    contents = (
        f"---\ndescription: Apply the documentation conventions\n{settings}\n---\n\n# Style\n\nUse clear prose.\n"
    )
    (hub_root / "assets/rules/docs-style.mdc").write_text(contents)
    (hub_root / "assets/rules/docs-style.asset.yaml").write_text(
        "id: docs-style\ntype: rule\ntitle: Documentation style\nsupport:\n  cursor:\n    mode: native\n"
    )

    build_hub(hub_root)

    assert (hub_root / "dist/cursor/pig/rules/docs-style.mdc").read_text() == contents
    manifest = json.loads((hub_root / "dist/cursor/pig/.cursor-plugin/plugin.json").read_text())
    assert manifest["rules"] == "./rules/"


def test_build_renders_projected_rules_native_cursor_rules_and_mcp_assets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    (hub_root / "hub.yaml").write_text(
        "\n".join(
            [
                "org: Acme",
                "marketplace:",
                "  id: acme-instruction-hub",
                "  name: Acme Instruction Hub",
                "version: 0.1.0",
                "stable_plugins:",
                "  - pig",
                "targets:",
                "  - claude",
                "  - codex",
                "  - cursor",
                "",
            ]
        )
    )
    (hub_root / "plugins/pig.yaml").write_text(
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
    update_skill = (hub_root / "dist/codex/pig/skills/update-instruction-hub/SKILL.md").read_text()
    assert "generated marketplace name `acme-instruction-hub`" in update_skill
    assert "marketplaceName` set to `acme-instruction-hub`" in update_skill
    assert "promptless-instruction-hub" not in update_skill
    claude_update_skill = (hub_root / "dist/claude/pig/skills/update-instruction-hub/SKILL.md").read_text()
    assert "generated marketplace name `acme-instruction-hub`" in claude_update_skill
    assert "claude plugin marketplace update acme-instruction-hub" in claude_update_skill
    assert "promptless-instruction-hub" not in claude_update_skill
    claude_manifest = json.loads((hub_root / "dist/claude/pig/.claude-plugin/plugin.json").read_text())
    assert claude_manifest["skills"] == "./skills/"
    assert "alwaysApply: false" in (hub_root / "dist/cursor/pig/rules/team-style.mdc").read_text()
    codex_mcp_config = json.loads((hub_root / "dist/codex/pig/.mcp.json").read_text())
    assert codex_mcp_config["mcpServers"]["trace-reporter"]["env"]["PROMPTLESS_API_KEY"] == "${PROMPTLESS_API_KEY}"
    cursor_mcp_config = json.loads((hub_root / "dist/cursor/pig/mcp.json").read_text())
    assert "trace-reporter" in cursor_mcp_config["mcpServers"]


def _write_claude_only_agent(hub_root: Path, asset_id: str, file_stem: str | None = None) -> None:
    stem = file_stem if file_stem is not None else asset_id
    agents_dir = hub_root / "assets/agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{stem}.md").write_text(
        f"---\nname: {asset_id}\ndescription: Plans things.\n---\n\n# {asset_id}\n",
    )
    (agents_dir / f"{stem}.asset.yaml").write_text(
        "\n".join(
            [
                f"id: {asset_id}",
                "support:",
                "  claude:",
                "    mode: native",
                "  codex:",
                "    mode: unsupported",
                "    reason: No plugin subagent equivalent.",
                "  gemini:",
                "    mode: unsupported",
                "    reason: No agents entry in the extension manifest.",
                "  cursor:",
                "    mode: unsupported",
                "    reason: Does not consume Claude subagent frontmatter.",
                "",
            ]
        )
    )
    (hub_root / "plugins/pig.yaml").write_text(
        f"id: pig\nname: PIG\nowners: []\nincludes:\n- agent:{asset_id}\n",
    )


def test_claude_manifest_lists_agent_files_rather_than_the_directory(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    _write_claude_only_agent(hub_root, "experiment-planner")

    build_hub(hub_root)

    plugin_root = hub_root / "dist/claude/pig"
    manifest = json.loads((plugin_root / ".claude-plugin/plugin.json").read_text())
    # Claude Code rejects a directory for `agents` (unlike `skills`), so the manifest
    # must enumerate each rendered agent file.
    assert manifest["agents"] == ["./agents/experiment-planner.md"]
    assert (plugin_root / "agents/experiment-planner.md").exists()
    for target in ("codex", "gemini", "cursor"):
        assert not (hub_root / f"dist/{target}/pig/agents").exists()


def test_claude_agent_manifest_uses_rendered_filename_not_asset_id(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    # A sidecar may set an `id` that differs from the source filename; the file is
    # rendered under its filename, so the manifest must point at that, not the id.
    _write_claude_only_agent(hub_root, "experiment-planner", file_stem="planner-source")

    build_hub(hub_root)

    plugin_root = hub_root / "dist/claude/pig"
    manifest = json.loads((plugin_root / ".claude-plugin/plugin.json").read_text())
    assert manifest["agents"] == ["./agents/planner-source.md"]
    assert (plugin_root / "agents/planner-source.md").exists()
