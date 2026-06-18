"""Asset file rendering for target plugin payloads."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from promptless_instruction_hub.assets import METADATA_FILE
from promptless_instruction_hub.fs import copy_tree
from promptless_instruction_hub.models import Harness, LoadedAsset
from promptless_instruction_hub.render.common import RenderedAssets, directory_for, manifest_key_for


def render_assets_for_target(target_root: Path, target: Harness, assets: list[LoadedAsset]) -> RenderedAssets:
    """Render source assets for one target and return manifest membership."""

    rendered: RenderedAssets = {"skills": [], "rules": [], "agents": [], "commands": [], "hooks": []}
    for asset in assets:
        support = asset.metadata.support[target]
        if support.mode == "unsupported" or asset.type == "mcp":
            continue
        if support.mode == "agent-skill":
            _render_agent_skill(target_root, target, asset)
            rendered["skills"].append(asset.id)
            continue
        if target == "cursor" and support.mode in {"projected", "native"} and asset.type in {"skill", "rule"}:
            _render_cursor_rule(target_root, asset)
            rendered["rules"].append(asset.id)
            continue
        if support.mode == "native":
            _render_native_asset(target_root, asset)
            rendered[manifest_key_for(asset.type)].append(asset.id)
            continue
        if support.mode == "projected":
            _render_projected_asset(target_root, target, asset)
            rendered[manifest_key_for(asset.type)].append(asset.id)
    return {key: sorted(values) for key, values in rendered.items() if values}


def _render_agent_skill(target_root: Path, target: Harness, asset: LoadedAsset) -> None:
    destination = target_root / "skills" / asset.id
    if asset.path.is_dir():
        copy_tree(asset.path, destination, skip_names={METADATA_FILE})
        if target == "codex":
            _normalize_codex_skill(destination, asset)
        return
    destination.mkdir(parents=True, exist_ok=True)
    if target == "codex":
        skill_path = destination / "SKILL.md"
        skill_path.write_text(_codex_skill_contents(asset.path.read_text(), asset))
        return
    shutil.copy2(asset.path, destination / "SKILL.md")


def _normalize_codex_skill(destination: Path, asset: LoadedAsset) -> None:
    source_skill_path = _find_markdown_file(destination, "skill.md")
    if source_skill_path.exists():
        contents = source_skill_path.read_text()
        if source_skill_path.name != "SKILL.md":
            source_skill_path.unlink()
    else:
        contents = _read_asset_markdown(asset)
    (destination / "SKILL.md").write_text(_codex_skill_contents(contents, asset))


def _codex_skill_contents(contents: str, asset: LoadedAsset) -> str:
    if _has_yaml_frontmatter(contents):
        return contents if contents.endswith("\n") else contents + "\n"
    title = asset.metadata.title or asset.id
    frontmatter = f"---\nname: {json.dumps(asset.id)}\ndescription: {json.dumps(title)}\n---"
    body = contents.rstrip()
    if not body:
        return frontmatter + "\n"
    return f"{frontmatter}\n\n{body}\n"


def _has_yaml_frontmatter(contents: str) -> bool:
    if not contents.startswith("---\n"):
        return False
    return any(line == "---" for line in contents.splitlines()[1:])


def _render_cursor_rule(target_root: Path, asset: LoadedAsset) -> None:
    rule_path = target_root / "rules" / f"{asset.id}.mdc"
    rule_path.parent.mkdir(parents=True, exist_ok=True)
    title = asset.metadata.title or asset.id
    content = _read_asset_markdown(asset)
    rule_path.write_text(f"---\ndescription: {json.dumps(title)}\nalwaysApply: false\n---\n\n{content.rstrip()}\n")


def _render_native_asset(target_root: Path, asset: LoadedAsset) -> None:
    destination = target_root / directory_for(asset.type) / asset.path.name
    if asset.path.is_dir():
        copy_tree(asset.path, destination, skip_names={METADATA_FILE})
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(asset.path, destination)


def _render_projected_asset(target_root: Path, target: Harness, asset: LoadedAsset) -> None:
    projected_path = target_root / "projected" / target / f"{asset.id}.md"
    projected_path.parent.mkdir(parents=True, exist_ok=True)
    projected_path.write_text(_read_asset_markdown(asset).rstrip() + "\n")


def _read_asset_markdown(asset: LoadedAsset) -> str:
    if asset.path.is_dir():
        skill_file = _find_markdown_file(asset.path, "skill.md")
        if skill_file.exists():
            return skill_file.read_text()
        markdown_files = sorted(asset.path.glob("*.md"))
        if markdown_files:
            return markdown_files[0].read_text()
        return f"# {asset.metadata.title or asset.id}\n"
    return asset.path.read_text()


def _find_markdown_file(directory: Path, file_name: str) -> Path:
    for child in sorted(directory.iterdir()):
        if child.is_file() and child.name.lower() == file_name:
            return child
    return directory / file_name
