"""Instruction Hub source validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from promptless_instruction_hub.agent_skills import AgentSkillWarning, read_agent_skill
from promptless_instruction_hub.assets import load_assets, validate_no_literal_secrets, validate_no_symlinks
from promptless_instruction_hub.commands import read_command, validate_verbatim_command
from promptless_instruction_hub.config import load_hub_config, load_plugins
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.hook_definitions import validate_hook_definition
from promptless_instruction_hub.managed_skills import MANAGED_SKILL_SOURCES
from promptless_instruction_hub.mcp_config import read_mcp_servers
from promptless_instruction_hub.models import (
    PIG_PLUGIN_ID,
    UPDATE_INSTRUCTION_HUB_SKILL_ID,
    Harness,
    HubConfig,
    HubPluginDefinition,
    ExternalPluginDefinition,
    LoadedAsset,
    PluginDefinition,
    StablePlugin,
)

SUPPORT_MODES_BY_ASSET_TYPE = {
    "skill": {"agent-skill", "native", "projected", "unsupported"},
    "rule": {"native", "projected", "unsupported"},
    "agent": {"agent-skill", "native", "projected", "unsupported"},
    "command": {"native", "verbatim", "projected", "unsupported"},
    "hook": {"native", "projected", "unsupported"},
    "mcp": {"native", "unsupported"},
}


@dataclass(frozen=True)
class ValidationResult:
    """Loaded and validated Instruction Hub source state."""

    config: HubConfig
    plugins: dict[str, HubPluginDefinition]
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
    _validate_commands(config, assets)
    for target in config.targets:
        for plugin in stable_plugins:
            _validate_invocation_destinations(plugin, target)
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
    return tuple(warnings)


def _validate_commands(config: HubConfig, assets: dict[str, LoadedAsset]) -> None:
    for asset in sorted(assets.values(), key=lambda asset: asset.ref):
        if asset.type != "command":
            continue
        for target in config.targets:
            mode = asset.metadata.support[target].mode
            if mode == "native":
                read_command(asset, target)
            elif mode == "verbatim":
                validate_verbatim_command(asset, target)


def _validate_invocation_destinations(plugin: StablePlugin, target: Harness) -> None:
    """Commands and skills share invocation names even in different directories."""

    owners: dict[str, str] = {}
    invocation_owners: dict[str, str] = {}
    if plugin.definition.id == PIG_PLUGIN_ID:
        for skill_id, sources in MANAGED_SKILL_SOURCES.items():
            if skill_id == UPDATE_INSTRUCTION_HUB_SKILL_ID or target in sources:
                owners[f"skills/{skill_id}"] = "compiler-managed skill"
                invocation_owners[skill_id] = "compiler-managed skill"
    for asset in plugin.assets:
        support = asset.metadata.support[target]
        directory = "skills"
        if support.mode == "agent-skill":
            name = asset.id
        elif asset.type == "skill" and support.mode == "native" and target != "cursor":
            name = asset.path.name
        elif asset.type == "command" and support.mode == "native":
            name = asset.id
            if target == "gemini":
                directory = "commands"
        elif asset.type == "command" and support.mode == "verbatim":
            name = asset.path.stem
            directory = "commands"
        else:
            continue
        destination_key = f"{directory}/{name}".casefold()
        if destination_key in owners:
            raise InstructionHubError(
                f"plugin {plugin.definition.id!r} ({target}): {directory}/{name} conflicts between {owners[destination_key]} and {asset.ref}"
            )
        owners[destination_key] = asset.ref
        invocation_name = _authored_skill_name(asset, name) if asset.type == "skill" else name
        invocation_key = invocation_name.casefold()
        if invocation_key in invocation_owners:
            raise InstructionHubError(
                f"plugin {plugin.definition.id!r} ({target}): invocation {invocation_name!r} conflicts between "
                f"{invocation_owners[invocation_key]} and {asset.ref}"
            )
        invocation_owners[invocation_key] = asset.ref
        if target == "codex" and asset.type in {"agent", "command"} and len(f"{plugin.definition.id}:{name}") > 64:
            raise InstructionHubError(
                f"{asset.ref}: combined Codex plugin and skill name {plugin.definition.id}:{name} exceeds 64 characters"
            )


def _authored_skill_name(asset: LoadedAsset, fallback: str) -> str:
    """Skill frontmatter can alias a directory name in plugin invocation menus."""

    path = asset.path
    if path.is_dir():
        path = next(child for child in sorted(path.iterdir()) if child.is_file() and child.name.lower() == "skill.md")
    contents = path.read_text(encoding="utf-8")
    if not contents.startswith("---\n"):
        return fallback
    lines = contents.splitlines()
    closing = next((i for i, line in enumerate(lines[1:], 1) if line == "---"), None)
    if closing is None:
        return fallback
    try:
        metadata = yaml.safe_load("\n".join(lines[1:closing]))
    except yaml.YAMLError as exc:
        raise InstructionHubError(f"{asset.ref}: cannot read skill invocation name from malformed YAML") from exc
    name = metadata.get("name") if isinstance(metadata, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else fallback


def _validate_mcp_assets(assets: dict[str, LoadedAsset]) -> None:
    for asset in assets.values():
        if asset.type == "mcp":
            read_mcp_servers(asset.path, default_server_name=asset.id)


def _validate_managed_skill_reservations(plugins: dict[str, HubPluginDefinition]) -> None:
    pig_plugin = plugins.get(PIG_PLUGIN_ID)
    if isinstance(pig_plugin, PluginDefinition):
        for skill_id in MANAGED_SKILL_SOURCES:
            reserved_ref = f"skill:{skill_id}"
            if reserved_ref in pig_plugin.includes:
                msg = f"plugin {PIG_PLUGIN_ID!r} cannot include reserved managed asset {reserved_ref!r}"
                raise InstructionHubError(msg)


def _resolve_stable_plugins(
    config: HubConfig,
    plugins: dict[str, HubPluginDefinition],
    assets: dict[str, LoadedAsset],
) -> tuple[StablePlugin, ...]:
    stable_plugins: list[StablePlugin] = []
    missing_refs: set[str] = set()
    for plugin_id in config.stable_plugins:
        plugin = plugins.get(plugin_id)
        if plugin is None:
            msg = f"stable plugin not found: {plugin_id}"
            raise InstructionHubError(msg)
        if isinstance(plugin, ExternalPluginDefinition):
            if not set(plugin.targets).intersection(config.targets):
                raise InstructionHubError(f"external plugin {plugin.id!r} has no enabled Hub target")
            stable_plugins.append(StablePlugin(definition=plugin, assets=()))
            continue
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
