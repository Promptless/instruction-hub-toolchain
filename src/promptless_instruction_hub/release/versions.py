"""Publish-time plugin version resolution."""

from __future__ import annotations

from pathlib import Path

from promptless_instruction_hub.config import RELEASE_MANIFEST_PATH
from promptless_instruction_hub.fs import JsonValue, read_json_mapping
from promptless_instruction_hub.models import SEMVER_RE
from promptless_instruction_hub.release.manifests import build_release_source_state
from promptless_instruction_hub.validate.hub import validate_hub


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

    previous_version = _read_previous_version(previous_hub_root)
    if previous_version is None:
        return config_version

    previous_state = _read_previous_source_state(previous_hub_root)
    current_state = build_release_source_state(validation)
    if previous_state == current_state:
        return _max_core_version(config_version, previous_version)
    return _max_core_version(config_version, _bump_patch(previous_version))


def _previous_hub_root(previous_release_root: Path | None, hub_relative_path: str) -> Path | None:
    if previous_release_root is None or not previous_release_root.exists():
        return None
    relative_path = hub_relative_path.strip("/")
    previous_hub_root = previous_release_root / relative_path if relative_path else previous_release_root
    return previous_hub_root if previous_hub_root.exists() else None


def _read_previous_source_state(previous_hub_root: Path) -> dict[str, JsonValue] | None:
    manifest = _read_previous_release_manifest(previous_hub_root)
    if manifest is None:
        return None
    plugin = manifest.get("plugin")
    if not isinstance(plugin, dict):
        return None
    return {
        "org": manifest.get("org"),
        "plugin": {
            "id": plugin.get("id"),
            "name": plugin.get("name"),
        },
        "stable_packages": manifest.get("stable_packages"),
        "targets": manifest.get("targets"),
        "assets": manifest.get("assets"),
    }


def _read_previous_version(previous_hub_root: Path) -> str | None:
    manifest = _read_previous_release_manifest(previous_hub_root)
    if manifest is not None:
        plugin = manifest.get("plugin")
        if isinstance(plugin, dict):
            version = plugin.get("version")
            if isinstance(version, str) and SEMVER_RE.match(version):
                return version
    return _read_first_plugin_manifest_version(previous_hub_root)


def _read_previous_release_manifest(previous_hub_root: Path) -> dict[str, JsonValue] | None:
    manifest_path = previous_hub_root / RELEASE_MANIFEST_PATH
    if not manifest_path.exists():
        return None
    return read_json_mapping(manifest_path)


def _read_first_plugin_manifest_version(previous_hub_root: Path) -> str | None:
    manifest_paths = sorted(
        [
            *previous_hub_root.glob("dist/*/*/.claude-plugin/plugin.json"),
            *previous_hub_root.glob("dist/*/*/.codex-plugin/plugin.json"),
            *previous_hub_root.glob("dist/*/*/.cursor-plugin/plugin.json"),
            *previous_hub_root.glob("dist/*/*/gemini-extension.json"),
        ]
    )
    for manifest_path in manifest_paths:
        manifest = read_json_mapping(manifest_path)
        version = manifest.get("version")
        if isinstance(version, str) and SEMVER_RE.match(version):
            return version
    return None


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
