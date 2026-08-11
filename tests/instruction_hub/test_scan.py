from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub, validate_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.scan.hub import scan_hub

from .helpers import (
    FIXTURES,
    _assert_no_promptless_directory,
)


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
    assert (hub_root / "packages/pig.yaml").exists()
    assert sorted(path.name for path in (hub_root / "packages").iterdir()) == ["pig.yaml"]
    assert validation.config.plugin_id == "acme-instruction-hub"
    assert validation.config.stable_packages == ["pig"]
    assert validation.stable_assets == ()


def test_scan_imports_skills_and_inventories_repo_context(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)

    result = scan_hub(hub_root, FIXTURES / "dogfood-source")

    assert result.imported_skills == ("review-docs",)
    assert result.imported_mcps == ("repo-mcp",)
    assert result.inventoried_context_files == ("AGENTS.md", "CLAUDE.md")
    assert (hub_root / "assets/skills/review-docs/SKILL.md").read_text().startswith("# Review Docs")
    pig_package = (hub_root / "packages/pig.yaml").read_text()
    assert "mcp:repo-mcp" in pig_package
    assert "skill:review-docs" in pig_package
    assert not (hub_root / "assets/skills/review-docs/asset.yaml").exists()
    assert (hub_root / "assets/mcps/repo-mcp.json").exists()
    assert not (hub_root / "assets/mcps/repo-mcp.asset.yaml").exists()
    assert not (hub_root / "assets/mcps/cursor-mcp.json").exists()
    inventory = json.loads((hub_root / "hub.repo-context.json").read_text())
    assert "source_root" not in inventory
    assert inventory["files"][0]["imported"] is False
    _assert_no_promptless_directory(hub_root)


def test_scan_rejects_legacy_core_hub_before_mutating(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    pig_package = hub_root / "packages/pig.yaml"
    core_package = hub_root / "packages/core.yaml"
    pig_package.rename(core_package)
    core_package.write_text(core_package.read_text().replace("id: pig\nname: PIG", "id: core\nname: Core"))
    (hub_root / "hub.yaml").write_text((hub_root / "hub.yaml").read_text().replace("- pig\n", "- core\n"))

    with pytest.raises(InstructionHubError, match="required 'pig' package"):
        scan_hub(hub_root, FIXTURES / "dogfood-source")

    assert not (hub_root / "assets/skills/review-docs").exists()
    assert not (hub_root / "assets/mcps/repo-mcp.json").exists()
    assert not pig_package.exists()
    assert not (hub_root / "hub.repo-context.json").exists()


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
    assert "mcp:cursor-mcp" in (hub_root / "packages/pig.yaml").read_text()
    mcp_metadata = (hub_root / "assets/mcps/cursor-mcp.asset.yaml").read_text()
    assert "id:" not in mcp_metadata
    assert "type:" not in mcp_metadata
    assert "source_path: .cursor/mcp.json" in mcp_metadata
    assert "claude:" in mcp_metadata
    assert "mode: unsupported" in mcp_metadata
    assert not (hub_root / "dist/codex/pig/.mcp.json").exists()
    cursor_mcp_config = json.loads((hub_root / "dist/cursor/pig/mcp.json").read_text())
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
    pig_package = (hub_root / "packages/pig.yaml").read_text()
    assert "mcp:repo-mcp" in pig_package
    assert "mcp:cursor-mcp" in pig_package
    codex_mcp_config = json.loads((hub_root / "dist/codex/pig/.mcp.json").read_text())
    assert codex_mcp_config["mcpServers"]["shared"]["command"] == "root-server"
    cursor_mcp_config = json.loads((hub_root / "dist/cursor/pig/mcp.json").read_text())
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
