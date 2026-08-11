"""Promptless-owned skills injected into generated plugins."""

from __future__ import annotations

import shutil
from pathlib import Path

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.models import PIG_PACKAGE_ID, Harness, HubConfig, PackageDefinition
from promptless_instruction_hub.render.common import marketplace_name

UPDATE_INSTRUCTION_HUB_SKILL_ID = "update-instruction-hub-mid-session"
SUPPORTED_MANAGED_SKILL_TARGETS: tuple[Harness, ...] = ("claude", "codex")

_ASSET_ROOT = Path(__file__).parent / "managed_skill_assets"
_MARKETPLACE_NAME_TOKEN = "{{ instruction_hub_marketplace_name }}"


def render_managed_skills(
    target_root: Path,
    target: Harness,
    config: HubConfig,
    package: PackageDefinition,
) -> tuple[str, ...]:
    """Inject Promptless-managed skills into the canonical PIG package."""

    if package.id != PIG_PACKAGE_ID or target not in SUPPORTED_MANAGED_SKILL_TARGETS:
        return ()

    skill_id = UPDATE_INSTRUCTION_HUB_SKILL_ID
    source = _ASSET_ROOT / skill_id / target
    if not source.is_dir():
        msg = f"managed skill source is missing for {target}: {source}"
        raise InstructionHubError(msg)

    destination = target_root / "skills" / skill_id
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    _render_update_skill_template(destination / "SKILL.md", config)
    return (skill_id,)


def _render_update_skill_template(skill_path: Path, config: HubConfig) -> None:
    content = skill_path.read_text()
    replacements = {
        _MARKETPLACE_NAME_TOKEN: marketplace_name(config),
    }
    for token, value in replacements.items():
        if token not in content:
            msg = f"managed skill template is missing required token {token!r}: {skill_path}"
            raise InstructionHubError(msg)
        content = content.replace(token, value)
    skill_path.write_text(content)
