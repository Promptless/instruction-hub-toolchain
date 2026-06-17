"""Instruction Hub source validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from promptless_instruction_hub.assets import load_assets, validate_no_literal_secrets, validate_no_symlinks
from promptless_instruction_hub.config import load_hub_config, load_packages
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.models import HubConfig, LoadedAsset, PackageDefinition

SUPPORT_MODES_BY_ASSET_TYPE = {
    "skill": {"agent-skill", "native", "projected", "unsupported"},
    "rule": {"native", "projected", "unsupported"},
    "agent": {"native", "projected", "unsupported"},
    "command": {"native", "projected", "unsupported"},
    "hook": {"native", "projected", "unsupported"},
    "mcp": {"native", "unsupported"},
}


@dataclass(frozen=True)
class ValidationResult:
    """Loaded and validated Instruction Hub source state."""

    config: HubConfig
    packages: dict[str, PackageDefinition]
    assets: dict[str, LoadedAsset]
    stable_assets: tuple[LoadedAsset, ...]


def validate_hub(hub_root: Path) -> ValidationResult:
    """Validate config, packages, target support, secrets, and package refs."""

    root = hub_root.resolve()
    config = load_hub_config(root)
    packages = load_packages(root)
    validate_no_symlinks(root)
    assets = load_assets(root)
    validate_no_literal_secrets(root)
    _validate_target_support(config, assets)
    stable_assets = _resolve_stable_assets(config, packages, assets)
    return ValidationResult(config=config, packages=packages, assets=assets, stable_assets=stable_assets)


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
        if support.mode in allowed_modes:
            continue
        allowed = ", ".join(sorted(allowed_modes))
        msg = f"{asset.ref} declares unsupported mode {support.mode!r} for {target}; allowed modes: {allowed}"
        raise InstructionHubError(msg)


def _resolve_stable_assets(
    config: HubConfig,
    packages: dict[str, PackageDefinition],
    assets: dict[str, LoadedAsset],
) -> tuple[LoadedAsset, ...]:
    refs: set[str] = set()
    for package_id in config.stable_packages:
        package = packages.get(package_id)
        if package is None:
            msg = f"stable package not found: {package_id}"
            raise InstructionHubError(msg)
        refs.update(package.includes)
    missing_refs = sorted(ref for ref in refs if ref not in assets)
    if missing_refs:
        msg = f"package includes unknown asset refs: {', '.join(missing_refs)}"
        raise InstructionHubError(msg)
    return tuple(assets[ref] for ref in sorted(refs))
