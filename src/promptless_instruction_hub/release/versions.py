"""Publish-time plugin version resolution."""

from __future__ import annotations

import tempfile
from pathlib import Path

from promptless_instruction_hub.config import RELEASE_MANIFEST_PATH
from promptless_instruction_hub.fs import JsonValue, read_json_mapping
from promptless_instruction_hub.models import SEMVER_RE, HubConfig
from promptless_instruction_hub.release.manifests import build_release_version_basis
from promptless_instruction_hub.render.plugins import render_target_plugins
from promptless_instruction_hub.validate.hub import ValidationResult, validate_hub


def resolve_publish_plugin_version(
    hub_root: Path,
    *,
    previous_release_root: Path | None = None,
    hub_relative_path: str = "",
) -> str:
    """Return the generated plugin version to use for a publish build."""

    validation = validate_hub(hub_root)
    config_version = validation.config.plugin_version
    previous_hub_root = _previous_hub_root(previous_release_root, hub_relative_path)
    if previous_hub_root is None:
        return config_version

    previous_manifest_path = previous_hub_root / RELEASE_MANIFEST_PATH
    if previous_manifest_path.exists():
        previous_manifest = read_json_mapping(previous_manifest_path)
        previous_version = _read_manifest_plugin_version(previous_manifest_path, previous_manifest)
        previous_basis = _read_manifest_version_basis(previous_manifest_path, previous_manifest)
        current_basis = _build_current_version_basis(validation, plugin_version=previous_version)
        if previous_basis == current_basis:
            return _max_core_version(config_version, previous_version)
        return _max_core_version(config_version, _bump_patch(previous_version))

    previous_version = _read_legacy_plugin_manifest_version(previous_hub_root)
    if previous_version is None:
        return config_version

    # Legacy release branches have no root version basis, so the first flat-layout publish
    # must assume generated output may have changed.
    return _max_core_version(config_version, _bump_patch(previous_version))


def _previous_hub_root(previous_release_root: Path | None, hub_relative_path: str) -> Path | None:
    if previous_release_root is None or not previous_release_root.exists():
        return None
    relative_path = hub_relative_path.strip("/")
    previous_hub_root = previous_release_root / relative_path if relative_path else previous_release_root
    return previous_hub_root if previous_hub_root.exists() else None


def _read_manifest_plugin_version(manifest_path: Path, manifest: dict[str, JsonValue]) -> str:
    _require_mapping(manifest_path, manifest, "plugin")
    version = _require_string(manifest_path, manifest, "plugin.version")
    if SEMVER_RE.match(version) is None:
        msg = f"{manifest_path}: plugin.version must be SemVer, got: {version}"
        raise ValueError(msg)
    return version


def _read_manifest_version_basis(manifest_path: Path, manifest: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return _require_mapping(manifest_path, manifest, "version_basis")


def _read_legacy_plugin_manifest_version(previous_hub_root: Path) -> str | None:
    manifest_paths = sorted(
        [
            *previous_hub_root.glob("dist/*/*/.claude-plugin/plugin.json"),
            *previous_hub_root.glob("dist/*/*/.codex-plugin/plugin.json"),
            *previous_hub_root.glob("dist/*/*/.cursor-plugin/plugin.json"),
            *previous_hub_root.glob("dist/*/*/gemini-extension.json"),
        ]
    )
    versions: set[str] = set()
    for manifest_path in manifest_paths:
        manifest = read_json_mapping(manifest_path)
        version = manifest.get("version")
        if not isinstance(version, str) or SEMVER_RE.match(version) is None:
            msg = f"{manifest_path}: version must be SemVer"
            raise ValueError(msg)
        versions.add(version)
    if not versions:
        return None
    if len(versions) > 1:
        msg = f"{previous_hub_root}: legacy plugin manifests disagree on version: {', '.join(sorted(versions))}"
        raise ValueError(msg)
    return next(iter(versions))


def _build_current_version_basis(validation: ValidationResult, *, plugin_version: str) -> dict[str, JsonValue]:
    versioned_validation = _with_plugin_version(validation, plugin_version)
    with tempfile.TemporaryDirectory(prefix="promptless-instruction-hub-version-") as temp_dir:
        output_root = Path(temp_dir)
        managed_runtimes = render_target_plugins(
            output_root,
            versioned_validation.config,
            versioned_validation.stable_packages,
        )
        return build_release_version_basis(output_root, versioned_validation, managed_runtimes)


def _with_plugin_version(validation: ValidationResult, plugin_version: str) -> ValidationResult:
    config = HubConfig.model_validate({**validation.config.model_dump(), "plugin_version": plugin_version})
    return ValidationResult(
        config=config,
        packages=validation.packages,
        assets=validation.assets,
        stable_packages=validation.stable_packages,
    )


def _require_mapping(
    manifest_path: Path,
    data: dict[str, JsonValue],
    key_path: str,
) -> dict[str, JsonValue]:
    value = _lookup_path(manifest_path, data, key_path)
    if isinstance(value, dict):
        return value
    msg = f"{manifest_path}: {key_path} must be a JSON object"
    raise ValueError(msg)


def _require_string(manifest_path: Path, data: dict[str, JsonValue], key_path: str) -> str:
    value = _lookup_path(manifest_path, data, key_path)
    if isinstance(value, str):
        return value
    msg = f"{manifest_path}: {key_path} must be a string"
    raise ValueError(msg)


def _lookup_path(manifest_path: Path, data: dict[str, JsonValue], key_path: str) -> JsonValue:
    value: JsonValue = data
    for key in key_path.split("."):
        if not isinstance(value, dict) or key not in value:
            msg = f"{manifest_path}: {key_path} is missing"
            raise ValueError(msg)
        value = value[key]
    return value


def _max_core_version(first: str, second: str) -> str:
    first_core = _core_tuple(first)
    second_core = _core_tuple(second)
    return first if first_core > second_core else second


def _bump_patch(version: str) -> str:
    major, minor, patch = _core_tuple(version)
    return f"{major}.{minor}.{patch + 1}"


def _core_tuple(version: str) -> tuple[int, int, int]:
    match = SEMVER_RE.match(version)
    if match is None:
        msg = f"plugin version must be SemVer, got: {version}"
        raise ValueError(msg)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
