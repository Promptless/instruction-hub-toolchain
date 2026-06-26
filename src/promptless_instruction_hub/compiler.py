"""Instruction Hub initialization and build orchestration."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from promptless_instruction_hub.config import CONFIG_PATH, PACKAGE_DIR, RELEASE_MANIFEST_PATH, STABLE_CHANNEL_PATH
from promptless_instruction_hub.errors import BuildCheckFailedError
from promptless_instruction_hub.fs import JsonValue, replace_tree, trees_equal, write_yaml
from promptless_instruction_hub.models import HubConfig
from promptless_instruction_hub.release.manifests import build_release_manifest, write_release_files
from promptless_instruction_hub.render.plugins import embed_release_manifest, render_target_plugins
from promptless_instruction_hub.validate.hub import ValidationResult, validate_hub

GENERATED_PATHS = (
    Path("dist"),
    Path(".agents/plugins"),
    Path(".claude-plugin"),
    Path(".cursor-plugin"),
    RELEASE_MANIFEST_PATH,
    STABLE_CHANNEL_PATH,
)

__all__ = ["BuildResult", "ValidationResult", "build_hub", "init_hub", "validate_hub"]


@dataclass(frozen=True)
class BuildResult:
    """Summary of a generated Instruction Hub build."""

    release_id: str
    release_hash: str
    target_count: int
    asset_count: int
    checked: bool


def init_hub(
    hub_root: Path,
    *,
    org: str = "Promptless",
    plugin_id: str | None = None,
    plugin_name: str | None = None,
    plugin_version: str = "0.1.0",
) -> Path:
    """Initialize an empty customer-owned Instruction Hub repository."""

    root = hub_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    resolved_plugin_id = plugin_id or f"{_slugify(org)}-instruction-hub"
    resolved_plugin_name = plugin_name or f"{org} Instruction Hub"
    config = HubConfig(
        org=org,
        plugin_id=resolved_plugin_id,
        plugin_name=resolved_plugin_name,
        plugin_version=plugin_version,
    )
    _write_file_if_missing(root / CONFIG_PATH, config.model_dump())
    _write_file_if_missing(
        root / PACKAGE_DIR / "core.yaml",
        {"id": "core", "name": "Core", "owners": [], "includes": []},
    )
    for relative_dir in (
        "assets/skills",
        "assets/rules",
        "assets/agents",
        "assets/commands",
        "assets/hooks",
        "assets/mcps",
        "dist",
        ".agents/plugins",
        ".claude-plugin",
        ".cursor-plugin",
    ):
        (root / relative_dir).mkdir(parents=True, exist_ok=True)
    return root


def build_hub(hub_root: Path, *, check: bool = False, plugin_version: str | None = None) -> BuildResult:
    """Build generated target artifacts and manifests, or check that they are current."""

    root = hub_root.resolve()
    validation = validate_hub(root)
    if plugin_version is not None:
        validation = _with_plugin_version(validation, plugin_version)
    with tempfile.TemporaryDirectory(prefix="promptless-instruction-hub-") as temp_dir:
        output_root = Path(temp_dir)
        managed_runtimes = render_target_plugins(output_root, validation.config, validation.stable_packages)
        release_manifest = build_release_manifest(output_root, validation, managed_runtimes)
        write_release_files(output_root, release_manifest)
        embed_release_manifest(output_root, validation.config, validation.stable_packages, release_manifest)
        if check:
            _check_generated_output(root, output_root)
        else:
            _replace_generated_output(root, output_root)
    return BuildResult(
        release_id=str(release_manifest["release_id"]),
        release_hash=str(release_manifest["release_hash"]),
        target_count=len(validation.config.targets),
        asset_count=len(validation.stable_assets),
        checked=check,
    )


def _write_file_if_missing(path: Path, data: JsonValue) -> None:
    if path.exists():
        return
    write_yaml(path, data)


def _check_generated_output(hub_root: Path, output_root: Path) -> None:
    stale_paths = [str(path) for path in GENERATED_PATHS if not trees_equal(hub_root / path, output_root / path)]
    if stale_paths:
        msg = f"generated Instruction Hub output is stale for: {', '.join(stale_paths)}; run `pi build`"
        raise BuildCheckFailedError(msg)


def _replace_generated_output(hub_root: Path, output_root: Path) -> None:
    for relative_path in GENERATED_PATHS:
        replace_tree(output_root / relative_path, hub_root / relative_path)


def _with_plugin_version(validation: ValidationResult, plugin_version: str) -> ValidationResult:
    config = HubConfig.model_validate({**validation.config.model_dump(), "plugin_version": plugin_version})
    return ValidationResult(
        config=config,
        packages=validation.packages,
        assets=validation.assets,
        stable_packages=validation.stable_packages,
    )


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "instruction-hub"
