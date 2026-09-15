"""Instruction Hub source validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from promptless_instruction_hub.agent_skills import AgentSkillWarning, read_agent_skill
from promptless_instruction_hub.assets import load_assets, validate_no_literal_secrets, validate_no_symlinks
from promptless_instruction_hub.config import load_hub_config, load_plugins
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.hook_definitions import validate_hook_definition
from promptless_instruction_hub.mcp_config import read_mcp_servers
from promptless_instruction_hub.models import (
    PIG_PLUGIN_ID,
    UPDATE_INSTRUCTION_HUB_SKILL_ID,
    HubConfig,
    LoadedAsset,
    PluginDefinition,
    StablePlugin,
)

SUPPORT_MODES_BY_ASSET_TYPE = {
    "skill": {"agent-skill", "native", "projected", "unsupported"},
    "rule": {"native", "projected", "unsupported"},
    "agent": {"agent-skill", "native", "projected", "unsupported"},
    "command": {"native", "projected", "unsupported"},
    "hook": {"native", "projected", "unsupported"},
    "mcp": {"native", "unsupported"},
}


@dataclass(frozen=True)
class ValidationResult:
    """Loaded and validated Instruction Hub source state."""

    config: HubConfig
    plugins: dict[str, PluginDefinition]
    assets: dict[str, LoadedAsset]
    stable_plugins: tuple[StablePlugin, ...]
    warnings: tuple[AgentSkillWarning, ...] = ()

    @property
    def stable_assets(self) -> tuple[LoadedAsset, ...]:
        """Return stable assets derived from the configured stable plugins."""

        return _resolve_stable_assets(self.stable_plugins)


def validate_hub(hub_root: Path) -> ValidationResult:
    """Validate config, plugins, target support, secrets, and asset references."""

    root = hub_root.resolve()
    config = load_hub_config(root)
    plugins = load_plugins(root)
    validate_no_symlinks(root)
    assets = load_assets(root)
    for asset in assets.values():
        validate_hook_definition(asset)
    validate_no_literal_secrets(root)
    _validate_target_support(config, assets)
    _validate_mcp_assets(assets)
    _validate_managed_skill_reservations(plugins)
    stable_plugins = _resolve_stable_plugins(config, plugins, assets)
    warnings = _validate_agent_skills(config, assets, stable_plugins)
    return ValidationResult(
        config=config,
        plugins=plugins,
        assets=assets,
        stable_plugins=stable_plugins,
        warnings=warnings,
    )


def _validate_target_support(config: HubConfig, assets: dict[str, LoadedAsset]) -> None:
    for asset in assets.values():
        missing_targets = sorted(target for target in config.targets if target not in asset.metadata.support)
        if missing_targets:
            msg = f"{asset.ref} is missing target support for: {', '.join(missing_targets)}"
            raise InstructionHubError(msg)
        _validate_support_modes(asset)


def _validate_support_modes(asset: LoadedAsset) -> None:
    allowed_modes = SUPPORT_MODES_BY_ASSET_TYPE[asset.type]
    for target, support in sorted(asset.metadata.support.items()):
        if asset.type == "agent" and support.mode == "agent-skill" and target != "codex":
            raise InstructionHubError(f"{asset.ref}: agent-skill conversion is supported only for codex, not {target}")
        if support.mode in allowed_modes:
            continue
        allowed = ", ".join(sorted(allowed_modes))
        msg = f"{asset.ref} declares unsupported mode {support.mode!r} for {target}; allowed modes: {allowed}"
        raise InstructionHubError(msg)


def _validate_agent_skills(
    config: HubConfig, assets: dict[str, LoadedAsset], stable_plugins: tuple[StablePlugin, ...]
) -> tuple[AgentSkillWarning, ...]:
    if "codex" not in config.targets:
        return ()
    stable_refs = {asset.ref for plugin in stable_plugins for asset in plugin.assets}
    warnings: list[AgentSkillWarning] = []
    for asset in sorted(assets.values(), key=lambda asset: asset.ref):
        if asset.type != "agent" or asset.metadata.support["codex"].mode != "agent-skill":
            continue
        skill = read_agent_skill(asset)
        if skill.warning is not None and asset.ref in stable_refs:
            warnings.append(skill.warning)
    for plugin in stable_plugins:
        _validate_codex_skill_destinations(plugin)
    return tuple(warnings)


def _validate_codex_skill_destinations(plugin: StablePlugin) -> None:
    owners: dict[str, str] = {}
    if plugin.definition.id == PIG_PLUGIN_ID:
        owners[UPDATE_INSTRUCTION_HUB_SKILL_ID] = "compiler-managed skill"
    for asset in plugin.assets:
        support = asset.metadata.support["codex"]
        if support.mode == "agent-skill":
            name = asset.id
        elif asset.type == "skill" and support.mode == "native":
            name = asset.path.name
        else:
            continue
        destination_key = name.casefold()
        if destination_key in owners:
            raise InstructionHubError(
                f"plugin {plugin.definition.id!r} (codex): skills/{name} conflicts between {owners[destination_key]} and {asset.ref}"
            )
        owners[destination_key] = asset.ref
        if asset.type == "agent" and len(f"{plugin.definition.id}:{name}") > 64:
            raise InstructionHubError(
                f"{asset.ref}: combined Codex plugin and skill name {plugin.definition.id}:{name} exceeds 64 characters"
            )


def _validate_mcp_assets(assets: dict[str, LoadedAsset]) -> None:
    for asset in assets.values():
        if asset.type == "mcp":
            read_mcp_servers(asset.path, default_server_name=asset.id)


def _validate_managed_skill_reservations(plugins: dict[str, PluginDefinition]) -> None:
    pig_plugin = plugins.get(PIG_PLUGIN_ID)
    reserved_ref = f"skill:{UPDATE_INSTRUCTION_HUB_SKILL_ID}"
    if pig_plugin is not None and reserved_ref in pig_plugin.includes:
        msg = f"plugin {PIG_PLUGIN_ID!r} cannot include reserved managed asset {reserved_ref!r}"
        raise InstructionHubError(msg)


def _resolve_stable_plugins(
    config: HubConfig,
    plugins: dict[str, PluginDefinition],
    assets: dict[str, LoadedAsset],
) -> tuple[StablePlugin, ...]:
    stable_plugins: list[StablePlugin] = []
    missing_refs: set[str] = set()
    for plugin_id in config.stable_plugins:
        plugin = plugins.get(plugin_id)
        if plugin is None:
            msg = f"stable plugin not found: {plugin_id}"
            raise InstructionHubError(msg)
        missing_refs.update(ref for ref in plugin.includes if ref not in assets)
        plugin_assets = tuple(assets[ref] for ref in sorted(plugin.includes) if ref in assets)
        stable_plugins.append(StablePlugin(definition=plugin, assets=plugin_assets))
    if missing_refs:
        msg = f"plugin includes unknown asset refs: {', '.join(sorted(missing_refs))}"
        raise InstructionHubError(msg)
    return tuple(stable_plugins)


def _resolve_stable_assets(stable_plugins: tuple[StablePlugin, ...]) -> tuple[LoadedAsset, ...]:
    assets_by_ref = {asset.ref: asset for stable_plugin in stable_plugins for asset in stable_plugin.assets}
    return tuple(assets_by_ref[ref] for ref in sorted(assets_by_ref))
