"""Load and validate Instruction Hub configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import read_yaml_mapping
from promptless_instruction_hub.models import HubConfig, PackageDefinition

CONFIG_PATH = Path("hub.yaml")
PACKAGE_DIR = Path("packages")
RELEASE_MANIFEST_PATH = Path("hub.release.json")
STABLE_CHANNEL_PATH = Path("hub.stable.json")
REPO_CONTEXT_PATH = Path("hub.repo-context.json")
MANAGED_RUNTIME_MANIFEST_PATH = Path("hub.managed-runtimes.json")


def load_hub_config(hub_root: Path) -> HubConfig:
    """Load the root hub configuration from a hub repository."""

    config_path = hub_root / CONFIG_PATH
    if not config_path.exists():
        msg = f"missing Instruction Hub config: {config_path}"
        raise FileNotFoundError(msg)
    try:
        return HubConfig.model_validate(read_yaml_mapping(config_path))
    except ValidationError as exc:
        msg = f"invalid Instruction Hub config {config_path}: {exc}"
        raise InstructionHubError(msg) from exc


def load_packages(hub_root: Path) -> dict[str, PackageDefinition]:
    """Load package definitions from `packages/*.yaml`."""

    packages: dict[str, PackageDefinition] = {}
    packages_dir = hub_root / PACKAGE_DIR
    if not packages_dir.exists():
        return packages
    for package_path in sorted(packages_dir.glob("*.yaml")):
        try:
            package_definition = PackageDefinition.model_validate(read_yaml_mapping(package_path))
        except ValidationError as exc:
            msg = f"invalid package definition {package_path}: {exc}"
            raise InstructionHubError(msg) from exc
        if package_definition.id in packages:
            msg = f"duplicate package id: {package_definition.id}"
            raise InstructionHubError(msg)
        packages[package_definition.id] = package_definition
    return packages
