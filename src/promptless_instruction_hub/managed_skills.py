"""Promptless-owned skills injected into generated plugins."""

from __future__ import annotations

import shutil
from pathlib import Path

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.models import (
    ADD_EXTERNAL_PLUGIN_SKILL_ID,
    PIG_PLUGIN_ID,
    UPDATE_INSTRUCTION_HUB_SKILL_ID,
    Harness,
    HubConfig,
    PluginDefinition,
)

MANAGED_SKILL_SOURCES: dict[str, dict[Harness, str]] = {
    UPDATE_INSTRUCTION_HUB_SKILL_ID: {"claude": "claude", "codex": "codex"},
    ADD_EXTERNAL_PLUGIN_SKILL_ID: {"claude": "shared", "codex": "shared", "cursor": "shared"},
}

_ASSET_ROOT = Path(__file__).parent / "managed_skill_assets"
_MARKETPLACE_NAME_TOKEN = "{{ instruction_hub_marketplace_name }}"


def render_managed_skills(
    target_root: Path,
    target: Harness,
    config: HubConfig,
    plugin: PluginDefinition,
) -> tuple[str, ...]:
    """Inject Promptless-managed skills into the canonical PIG plugin."""

    if plugin.id != PIG_PLUGIN_ID:
        return ()

    rendered = []
    for skill_id, sources in MANAGED_SKILL_SOURCES.items():
        if target not in sources:
            continue
        source = _ASSET_ROOT / skill_id / sources[target]
        if not source.is_dir():
            msg = f"managed skill source is missing for {target}: {source}"
            raise InstructionHubError(msg)

        destination = target_root / "skills" / skill_id
        if destination.exists():
            msg = f"managed skill {skill_id!r} conflicts with an authored skill in plugin {plugin.id!r}"
            raise InstructionHubError(msg)
        shutil.copytree(source, destination)
        _render_skill_template(destination / "SKILL.md", config)
        rendered.append(skill_id)
    return tuple(rendered)


def _render_skill_template(skill_path: Path, config: HubConfig) -> None:
    content = skill_path.read_text()
    replacements = {
        _MARKETPLACE_NAME_TOKEN: config.marketplace.id,
    }
    for token, value in replacements.items():
        if token not in content:
            msg = f"managed skill template is missing required token {token!r}: {skill_path}"
            raise InstructionHubError(msg)
        content = content.replace(token, value)
    skill_path.write_text(content)
