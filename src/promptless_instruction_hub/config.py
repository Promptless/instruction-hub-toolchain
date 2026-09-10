"""Load and validate Instruction Hub configuration."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import read_yaml_mapping
from promptless_instruction_hub.models import ExternalPluginDefinition, HubConfig, HubPluginDefinition, PluginDefinition

CONFIG_PATH = Path("hub.yaml")
PLUGIN_DIR = Path("plugins")
RELEASE_MANIFEST_PATH = Path("hub.release.json")
STABLE_CHANNEL_PATH = Path("hub.stable.json")
REPO_CONTEXT_PATH = Path("hub.repo-context.json")
MANAGED_RUNTIME_MANIFEST_PATH = Path("hub.managed-runtimes.json")
MIGRATION_GUIDE_URL = (
    "https://github.com/Promptless/instruction-hub-toolchain/blob/main/README.md#migrating-existing-hubs"
)


def load_hub_config(hub_root: Path) -> HubConfig:
    """Load the root hub configuration from a hub repository."""

    config_path = hub_root / CONFIG_PATH
    if not config_path.exists():
        msg = f"missing Instruction Hub config: {config_path}"
        raise FileNotFoundError(msg)
    raw_config = read_yaml_mapping(config_path)
    if {"plugin_id", "plugin_name", "stable_packages"} & raw_config.keys():
        msg = (
            f"{config_path}: legacy hub configuration; replace plugin_id/plugin_name with marketplace.id/name, "
            "rename stable_packages to stable_plugins, and move packages/*.yaml to plugins/. "
            f"IDs are now literal; see {MIGRATION_GUIDE_URL} for installation changes."
        )
        raise InstructionHubError(msg)
    try:
        return HubConfig.model_validate(raw_config)
    except ValidationError as exc:
        msg = f"invalid Instruction Hub config {config_path}: {exc}"
        raise InstructionHubError(msg) from exc


def load_plugins(hub_root: Path) -> dict[str, HubPluginDefinition]:
    """Load plugin definitions from `plugins/*.yaml`."""

    plugins: dict[str, HubPluginDefinition] = {}
    if (hub_root / "packages").exists():
        msg = (
            f"{hub_root / 'packages'}: legacy plugin directory; move packages/*.yaml to plugins/ and remove packages/. "
            f"See {MIGRATION_GUIDE_URL} for installation changes."
        )
        raise InstructionHubError(msg)
    plugins_dir = hub_root / PLUGIN_DIR
    if not plugins_dir.exists():
        return plugins
    for plugin_path in sorted(plugins_dir.glob("*.yaml")):
        try:
            raw_plugin = read_yaml_mapping(plugin_path)
            model = ExternalPluginDefinition if raw_plugin.get("kind") == "external" else PluginDefinition
            plugin_definition = model.model_validate(raw_plugin)
        except ValidationError as exc:
            msg = f"invalid plugin definition {plugin_path}: {exc}"
            raise InstructionHubError(msg) from exc
        if plugin_definition.id in plugins:
            msg = f"duplicate plugin id: {plugin_definition.id}"
            raise InstructionHubError(msg)
        plugins[plugin_definition.id] = plugin_definition
    return plugins


def write_hub_version(hub_root: Path, version: str) -> None:
    """Update the release version while preserving source formatting and comments."""

    config = load_hub_config(hub_root)
    HubConfig.model_validate({**config.model_dump(), "version": version})
    if config.version == version:
        return
    config_path = hub_root / CONFIG_PATH
    source = config_path.read_text()
    document = yaml.compose(source)
    if not isinstance(document, yaml.MappingNode):
        raise ValueError(f"{config_path}: expected a YAML mapping")
    nodes = [value for key, value in document.value if key.value == "version"]
    if len(nodes) != 1 or not isinstance(nodes[0], yaml.ScalarNode):
        raise ValueError(f"{config_path}: expected exactly one scalar version")
    node = nodes[0]
    start, end = node.start_mark.index, node.end_mark.index
    replacement = version
    if node.style in {"|", ">"}:
        # Keep the block header, comments, indentation, and separating newline.
        # A valid SemVer block contains exactly one non-whitespace value.
        start = source.index("\n", start) + 1
        replacement = source[start:end].replace(config.version, version, 1)
    updated = source[:start] + replacement + source[end:]
    try:
        updated_config = yaml.safe_load(updated)
    except yaml.YAMLError as exc:
        raise ValueError(f"{config_path}: version must be an independent YAML scalar") from exc
    if updated_config != {**yaml.safe_load(source), "version": version}:
        raise ValueError(f"{config_path}: version must be an independent YAML scalar")
    config_path.write_text(updated)
