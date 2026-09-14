"""Publish-time hub release version resolution."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from pydantic import ValidationError

from promptless_instruction_hub.config import RELEASE_MANIFEST_PATH
from promptless_instruction_hub.external_lock import load_external_resolutions
from promptless_instruction_hub.fs import JsonValue, read_json_mapping
from promptless_instruction_hub.models import (
    ASSET_KINDS,
    IDENTIFIER_RE,
    SEMVER_RE,
    SUPPORTED_HARNESSES,
    ExternalPluginDefinition,
    ExternalGitSource,
    HubConfig,
)
from promptless_instruction_hub.release.hashing import stable_hash
from promptless_instruction_hub.release.manifests import build_release_version_basis
from promptless_instruction_hub.render.plugins import render_target_plugins
from promptless_instruction_hub.validate.hub import ValidationResult, validate_hub

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "org",
        "marketplace",
        "version",
        "stable_plugins",
        "targets",
        "target_hashes",
        "managed_runtimes",
        "assets",
        "version_basis",
        "release_id",
        "release_hash",
    }
)
VERSION_BASIS_KEYS = frozenset(
    {
        "org",
        "marketplace",
        "version",
        "stable_plugins",
        "targets",
        "plugins",
        "target_hashes",
        "managed_runtimes",
    }
)
MARKETPLACE_KEYS = frozenset({"id", "name"})
PLUGIN_BASIS_KEYS = frozenset({"id", "name", "includes", "assets"})
ASSET_MANIFEST_KEYS = frozenset({"ref", "id", "type", "title", "source_path", "content_hash", "support"})
MANAGED_RUNTIME_KEYS = frozenset(
    {
        "id",
        "channel",
        "executable",
        "hook",
        "package_id",
        "path",
        "plugin_id",
        "plugin_name",
        "plugin_version",
        "sha256",
        "status",
        "target",
        "toolchain_version",
        "version",
    }
)
SUPPORT_KEYS = frozenset({"mode", "reason"})
SUPPORT_MODES = frozenset({"agent-skill", "native", "verbatim", "projected", "unsupported"})
HOST_RUNTIME_ID = "host-runtime"


def resolve_publish_version(
    hub_root: Path,
    *,
    previous_release_root: Path | None = None,
    hub_relative_path: str = "",
) -> str:
    """Resolve the hub release version from the configured version and published output."""

    validation = load_external_resolutions(hub_root, validate_hub(hub_root))
    return resolve_release_version(
        validation, previous_release_root=previous_release_root, hub_relative_path=hub_relative_path
    )


def resolve_release_version(
    validation: ValidationResult, *, previous_release_root: Path | None = None, hub_relative_path: str = ""
) -> str:
    """Compare resolved source state with the previous immutable release."""

    config_version = validation.config.version
    previous_hub_root = _previous_hub_root(previous_release_root, hub_relative_path)
    if previous_hub_root is None:
        return config_version

    previous_manifest_path = previous_hub_root / RELEASE_MANIFEST_PATH
    if not previous_manifest_path.is_file():
        raise ValueError(f"{previous_manifest_path}: previous release is missing its release manifest")

    previous_version, previous_basis = read_release_manifest(previous_manifest_path)
    current_basis = _build_current_version_basis(validation, version=previous_version)
    if previous_basis == current_basis:
        return _max_semver(config_version, previous_version)
    return _max_semver(config_version, _bump_patch(previous_version))


def _previous_hub_root(previous_release_root: Path | None, hub_relative_path: str) -> Path | None:
    if previous_release_root is None:
        return None
    if not previous_release_root.is_dir():
        raise ValueError(f"{previous_release_root}: previous release root must be a directory")
    relative_path = hub_relative_path.strip("/")
    if not relative_path:
        return previous_release_root
    previous_hub_root = previous_release_root / relative_path
    if previous_hub_root.exists():
        return previous_hub_root
    msg = f"{previous_release_root}: previous release is missing hub path: {relative_path}"
    raise ValueError(msg)


def read_release_manifest(manifest_path: Path) -> tuple[str, dict[str, JsonValue]]:
    """Read and validate an authoritative release before using its version or sources."""

    manifest = read_json_mapping(manifest_path)
    version = _read_manifest_version(manifest_path, manifest)
    version_basis = _read_manifest_version_basis(manifest_path, manifest)
    _validate_release_manifest(manifest_path, manifest, version, version_basis)
    return version, version_basis


def _read_manifest_version(manifest_path: Path, manifest: dict[str, JsonValue]) -> str:
    version = _require_string(manifest_path, manifest, "version")
    if SEMVER_RE.match(version) is None:
        msg = f"{manifest_path}: version must be SemVer, got: {version}"
        raise ValueError(msg)
    return version


def _read_manifest_version_basis(manifest_path: Path, manifest: dict[str, JsonValue]) -> dict[str, JsonValue]:
    basis = _require_mapping(manifest_path, manifest, "version_basis")
    _validate_manifest_version_basis(manifest_path, manifest, basis)
    return basis


def _validate_release_manifest(
    manifest_path: Path,
    manifest: dict[str, JsonValue],
    version: str,
    version_basis: dict[str, JsonValue],
) -> None:
    _require_exact_keys(manifest_path, manifest, "release manifest", RELEASE_MANIFEST_KEYS)
    _validate_schema_version(manifest_path, manifest)
    _validate_release_manifest_assets(manifest_path, manifest, version_basis)
    _validate_release_identity(manifest_path, manifest, version)


def _validate_schema_version(manifest_path: Path, manifest: dict[str, JsonValue]) -> None:
    schema_version = _lookup_path(manifest_path, manifest, "schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version not in {2, 3}:
        msg = f"{manifest_path}: schema_version must be 2 or 3"
        raise ValueError(msg)


def _validate_release_manifest_assets(
    manifest_path: Path,
    manifest: dict[str, JsonValue],
    version_basis: dict[str, JsonValue],
) -> None:
    assets = _require_list(manifest_path, manifest, "assets")
    asset_refs: list[str] = []
    assets_by_ref: dict[str, dict[str, JsonValue]] = {}
    for index, asset_value in enumerate(assets):
        asset = _require_mapping_value(manifest_path, asset_value, f"assets[{index}]")
        asset_ref = _validate_asset_manifest(manifest_path, asset, f"assets[{index}]")
        asset_refs.append(asset_ref)
        assets_by_ref[asset_ref] = asset
    _require_unique(manifest_path, asset_refs, "assets.ref")

    expected_assets_by_ref = _version_basis_assets_by_ref(manifest_path, version_basis)
    expected_asset_refs = sorted(expected_assets_by_ref)
    if asset_refs != expected_asset_refs:
        msg = f"{manifest_path}: assets refs must match version_basis plugin assets"
        raise ValueError(msg)
    for index, asset_ref in enumerate(asset_refs):
        if assets_by_ref[asset_ref] != expected_assets_by_ref[asset_ref]:
            msg = f"{manifest_path}: assets[{index}] must match version_basis plugin asset"
            raise ValueError(msg)


def _version_basis_assets_by_ref(
    manifest_path: Path,
    version_basis: dict[str, JsonValue],
) -> dict[str, dict[str, JsonValue]]:
    assets_by_ref: dict[str, dict[str, JsonValue]] = {}
    plugins = _require_list(
        manifest_path,
        version_basis,
        "plugins",
        display_path="version_basis.plugins",
    )
    for plugin_index, plugin_value in enumerate(plugins):
        package = _require_mapping_value(manifest_path, plugin_value, f"version_basis.plugins[{plugin_index}]")
        if package.get("kind") == "external":
            continue
        plugin_assets = _require_list(
            manifest_path,
            package,
            "assets",
            display_path=f"version_basis.plugins[{plugin_index}].assets",
        )
        for asset_index, asset_value in enumerate(plugin_assets):
            asset_path = f"version_basis.plugins[{plugin_index}].assets[{asset_index}]"
            asset = _require_mapping_value(manifest_path, asset_value, asset_path)
            asset_ref = _require_string(manifest_path, asset, "ref", display_path=f"{asset_path}.ref")
            assets_by_ref[asset_ref] = asset
    return assets_by_ref


def _validate_release_identity(
    manifest_path: Path,
    manifest: dict[str, JsonValue],
    version: str,
) -> None:
    release_id = _require_string(manifest_path, manifest, "release_id")
    if not release_id:
        msg = f"{manifest_path}: release_id must not be empty"
        raise ValueError(msg)
    release_hash = _require_string(manifest_path, manifest, "release_hash")
    _validate_sha256(manifest_path, release_hash, "release_hash")

    manifest_without_release_data = {
        key: value for key, value in manifest.items() if key not in {"release_id", "release_hash"}
    }
    expected_release_id = f"{version}+{stable_hash(manifest_without_release_data)[:12]}"
    if release_id != expected_release_id:
        msg = f"{manifest_path}: release_id must match manifest content"
        raise ValueError(msg)

    manifest_without_release_hash = {key: value for key, value in manifest.items() if key != "release_hash"}
    if release_hash != stable_hash(manifest_without_release_hash):
        msg = f"{manifest_path}: release_hash must match manifest content"
        raise ValueError(msg)


def _build_current_version_basis(validation: ValidationResult, *, version: str) -> dict[str, JsonValue]:
    versioned_validation = _with_version(validation, version)
    with tempfile.TemporaryDirectory(prefix="promptless-instruction-hub-version-") as temp_dir:
        output_root = Path(temp_dir)
        managed_runtimes = render_target_plugins(
            output_root,
            versioned_validation.config,
            versioned_validation.stable_plugins,
        )
        return build_release_version_basis(output_root, versioned_validation, managed_runtimes)


def _with_version(validation: ValidationResult, version: str) -> ValidationResult:
    config = HubConfig.model_validate({**validation.config.model_dump(), "version": version})
    return ValidationResult(
        config=config,
        plugins=validation.plugins,
        assets=validation.assets,
        stable_plugins=validation.stable_plugins,
    )


def _validate_manifest_version_basis(
    manifest_path: Path,
    manifest: dict[str, JsonValue],
    basis: dict[str, JsonValue],
) -> None:
    _require_exact_keys(manifest_path, basis, "version_basis", VERSION_BASIS_KEYS)
    _validate_marketplace_object(
        manifest_path,
        _require_mapping_value(manifest_path, basis["marketplace"], "version_basis.marketplace"),
        "version_basis.marketplace",
    )

    org = _require_string(manifest_path, basis, "org", display_path="version_basis.org")
    if not org:
        msg = f"{manifest_path}: version_basis.org must not be empty"
        raise ValueError(msg)

    stable_plugins = _require_string_list(
        manifest_path,
        basis,
        "stable_plugins",
        display_path="version_basis.stable_plugins",
    )
    if not stable_plugins:
        msg = f"{manifest_path}: version_basis.stable_plugins must not be empty"
        raise ValueError(msg)
    _require_unique(manifest_path, stable_plugins, "version_basis.stable_plugins")
    for index, plugin_id in enumerate(stable_plugins):
        _validate_identifier(manifest_path, plugin_id, f"version_basis.stable_plugins[{index}]")

    targets = _require_string_list(manifest_path, basis, "targets", display_path="version_basis.targets")
    if not targets:
        msg = f"{manifest_path}: version_basis.targets must not be empty"
        raise ValueError(msg)
    _require_unique(manifest_path, targets, "version_basis.targets")
    for index, target in enumerate(targets):
        if target not in SUPPORTED_HARNESSES:
            msg = f"{manifest_path}: version_basis.targets[{index}] must be a supported target"
            raise ValueError(msg)

    target_hashes = _require_mapping(
        manifest_path,
        basis,
        "target_hashes",
        display_path="version_basis.target_hashes",
    )
    _validate_target_hashes(manifest_path, target_hashes, targets, "version_basis.target_hashes")
    _validate_managed_runtimes(
        manifest_path,
        _require_list(manifest_path, basis, "managed_runtimes", display_path="version_basis.managed_runtimes"),
        "version_basis.managed_runtimes",
    )

    plugins = _require_list(manifest_path, basis, "plugins", display_path="version_basis.plugins")
    plugin_ids: list[str] = []
    for index, plugin_value in enumerate(plugins):
        package = _require_mapping_value(manifest_path, plugin_value, f"version_basis.plugins[{index}]")
        if package.get("kind") == "external":
            if manifest.get("schema_version") != 3:
                raise ValueError(f"{manifest_path}: external plugins require schema_version 3")
            if not set(_require_mapping(manifest_path, package, "targets")).intersection(targets):
                raise ValueError(f"{manifest_path}: external plugin has no enabled Hub target")
        plugin_ids.append(_validate_plugin_basis(manifest_path, package, f"version_basis.plugins[{index}]"))
    if plugin_ids != stable_plugins:
        msg = f"{manifest_path}: version_basis.plugins ids must match version_basis.stable_plugins"
        raise ValueError(msg)

    for key in ("org", "marketplace", "version", "stable_plugins", "targets", "target_hashes", "managed_runtimes"):
        top_level_value = _lookup_path(manifest_path, manifest, key)
        if top_level_value != basis[key]:
            msg = f"{manifest_path}: version_basis.{key} must match {key}"
            raise ValueError(msg)


def _validate_marketplace_object(manifest_path: Path, plugin: dict[str, JsonValue], key_path: str) -> None:
    _require_exact_keys(manifest_path, plugin, key_path, MARKETPLACE_KEYS)
    _validate_identifier(
        manifest_path,
        _require_string(manifest_path, plugin, "id", display_path=f"{key_path}.id"),
        f"{key_path}.id",
    )
    name = _require_string(manifest_path, plugin, "name", display_path=f"{key_path}.name")
    if not name:
        msg = f"{manifest_path}: {key_path}.name must not be empty"
        raise ValueError(msg)


def _validate_plugin_basis(manifest_path: Path, package: dict[str, JsonValue], key_path: str) -> str:
    if package.get("kind") == "external":
        _require_exact_keys(manifest_path, package, key_path, frozenset({"kind", "id", "name", "source", "targets"}))
        try:
            plugin = ExternalPluginDefinition.model_validate(package)
        except ValidationError as exc:
            raise ValueError(f"{manifest_path}: invalid {key_path}: {exc}") from exc
        if not isinstance(plugin.source, ExternalGitSource):
            raise ValueError(f"{manifest_path}: released external sources must be pinned to a commit SHA")
        return plugin.id
    _require_exact_keys(manifest_path, package, key_path, PLUGIN_BASIS_KEYS)
    plugin_id = _require_string(manifest_path, package, "id", display_path=f"{key_path}.id")
    _validate_identifier(manifest_path, plugin_id, f"{key_path}.id")
    name = _require_string(manifest_path, package, "name", display_path=f"{key_path}.name")
    if not name:
        msg = f"{manifest_path}: {key_path}.name must not be empty"
        raise ValueError(msg)

    includes = _require_string_list(manifest_path, package, "includes", display_path=f"{key_path}.includes")
    _require_unique(manifest_path, includes, f"{key_path}.includes")
    for index, asset_ref in enumerate(includes):
        _validate_asset_ref(manifest_path, asset_ref, f"{key_path}.includes[{index}]")

    assets = _require_list(manifest_path, package, "assets", display_path=f"{key_path}.assets")
    asset_refs: list[str] = []
    for index, asset_value in enumerate(assets):
        asset = _require_mapping_value(manifest_path, asset_value, f"{key_path}.assets[{index}]")
        asset_refs.append(_validate_asset_manifest(manifest_path, asset, f"{key_path}.assets[{index}]"))
    if asset_refs != includes:
        msg = f"{manifest_path}: {key_path}.assets refs must match {key_path}.includes"
        raise ValueError(msg)
    return plugin_id


def _validate_asset_manifest(manifest_path: Path, asset: dict[str, JsonValue], key_path: str) -> str:
    _require_exact_keys(manifest_path, asset, key_path, ASSET_MANIFEST_KEYS)
    asset_ref = _require_string(manifest_path, asset, "ref", display_path=f"{key_path}.ref")
    _validate_asset_ref(manifest_path, asset_ref, f"{key_path}.ref")
    asset_type, _, asset_id = asset_ref.partition(":")
    if _require_string(manifest_path, asset, "id", display_path=f"{key_path}.id") != asset_id:
        msg = f"{manifest_path}: {key_path}.id must match {key_path}.ref"
        raise ValueError(msg)
    if _require_string(manifest_path, asset, "type", display_path=f"{key_path}.type") != asset_type:
        msg = f"{manifest_path}: {key_path}.type must match {key_path}.ref"
        raise ValueError(msg)
    title = asset["title"]
    if title is not None and not isinstance(title, str):
        msg = f"{manifest_path}: {key_path}.title must be a string or null"
        raise ValueError(msg)
    source_path = asset["source_path"]
    if source_path is not None and not isinstance(source_path, str):
        msg = f"{manifest_path}: {key_path}.source_path must be a string or null"
        raise ValueError(msg)
    _validate_sha256(
        manifest_path,
        _require_string(manifest_path, asset, "content_hash", display_path=f"{key_path}.content_hash"),
        f"{key_path}.content_hash",
    )
    _validate_support_mapping(
        manifest_path,
        _require_mapping(manifest_path, asset, "support", display_path=f"{key_path}.support"),
        f"{key_path}.support",
    )
    return asset_ref


def _validate_support_mapping(manifest_path: Path, support: dict[str, JsonValue], key_path: str) -> None:
    for target, support_value in support.items():
        if target not in SUPPORTED_HARNESSES:
            msg = f"{manifest_path}: {key_path}.{target} must be a supported target"
            raise ValueError(msg)
        support_data = _require_mapping_value(manifest_path, support_value, f"{key_path}.{target}")
        if not set(support_data) <= SUPPORT_KEYS or "mode" not in support_data:
            msg = f"{manifest_path}: {key_path}.{target} must contain mode and optional reason"
            raise ValueError(msg)
        mode = support_data.get("mode")
        if not isinstance(mode, str) or mode not in SUPPORT_MODES:
            msg = f"{manifest_path}: {key_path}.{target}.mode must be a supported mode"
            raise ValueError(msg)
        reason = support_data.get("reason")
        if reason is not None and (not isinstance(reason, str) or not reason):
            msg = f"{manifest_path}: {key_path}.{target}.reason must be a non-empty string"
            raise ValueError(msg)
        if mode == "unsupported" and reason is None:
            msg = f"{manifest_path}: {key_path}.{target}.reason is required when mode is unsupported"
            raise ValueError(msg)


def _validate_target_hashes(
    manifest_path: Path,
    target_hashes: dict[str, JsonValue],
    targets: list[str],
    key_path: str,
) -> None:
    expected_targets = set(targets)
    if set(target_hashes) != expected_targets:
        msg = f"{manifest_path}: {key_path} keys must match version_basis.targets"
        raise ValueError(msg)
    for target in target_hashes:
        _validate_sha256(
            manifest_path,
            _require_string(manifest_path, target_hashes, target, display_path=f"{key_path}.{target}"),
            f"{key_path}.{target}",
        )


def _validate_managed_runtimes(manifest_path: Path, runtimes: list[JsonValue], key_path: str) -> None:
    for index, runtime_value in enumerate(runtimes):
        runtime = _require_mapping_value(manifest_path, runtime_value, f"{key_path}[{index}]")
        runtime_path = f"{key_path}[{index}]"
        if set(runtime) != MANAGED_RUNTIME_KEYS:
            expected = ", ".join(sorted(MANAGED_RUNTIME_KEYS))
            msg = f"{manifest_path}: {runtime_path} must contain exactly these keys: {expected}"
            raise ValueError(msg)
        runtime_id = runtime["id"]
        if runtime_id != HOST_RUNTIME_ID:
            msg = f"{manifest_path}: {runtime_path}.id must be host-runtime"
            raise ValueError(msg)
        if runtime["status"] != "included":
            msg = f"{manifest_path}: {runtime_path}.status must be included"
            raise ValueError(msg)
        target = runtime["target"]
        if target not in {"claude", "codex"}:
            msg = f"{manifest_path}: {runtime_path}.target must be claude or codex"
            raise ValueError(msg)
        for string_key in set(runtime) - {"id", "status", "target", "sha256"}:
            value = runtime[string_key]
            if not isinstance(value, str) or not value:
                msg = f"{manifest_path}: {runtime_path}.{string_key} must be a non-empty string"
                raise ValueError(msg)
        _validate_sha256(
            manifest_path,
            _require_string(manifest_path, runtime, "sha256", display_path=f"{runtime_path}.sha256"),
            f"{runtime_path}.sha256",
        )


def _require_mapping(
    manifest_path: Path,
    data: dict[str, JsonValue],
    key_path: str,
    *,
    display_path: str | None = None,
) -> dict[str, JsonValue]:
    value = _lookup_path(manifest_path, data, key_path, display_path=display_path)
    if isinstance(value, dict):
        return value
    msg = f"{manifest_path}: {display_path or key_path} must be a JSON object"
    raise ValueError(msg)


def _require_string(
    manifest_path: Path,
    data: dict[str, JsonValue],
    key_path: str,
    *,
    display_path: str | None = None,
) -> str:
    value = _lookup_path(manifest_path, data, key_path, display_path=display_path)
    if isinstance(value, str):
        return value
    msg = f"{manifest_path}: {display_path or key_path} must be a string"
    raise ValueError(msg)


def _require_list(
    manifest_path: Path,
    data: dict[str, JsonValue],
    key_path: str,
    *,
    display_path: str | None = None,
) -> list[JsonValue]:
    value = _lookup_path(manifest_path, data, key_path, display_path=display_path)
    if isinstance(value, list):
        return value
    msg = f"{manifest_path}: {display_path or key_path} must be a list"
    raise ValueError(msg)


def _require_string_list(
    manifest_path: Path,
    data: dict[str, JsonValue],
    key_path: str,
    *,
    display_path: str | None = None,
) -> list[str]:
    values = _require_list(manifest_path, data, key_path, display_path=display_path)
    item_path = display_path or key_path
    strings: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value:
            msg = f"{manifest_path}: {item_path}[{index}] must be a non-empty string"
            raise ValueError(msg)
        strings.append(value)
    return strings


def _require_mapping_value(manifest_path: Path, value: JsonValue, key_path: str) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return value
    msg = f"{manifest_path}: {key_path} must be a JSON object"
    raise ValueError(msg)


def _require_exact_keys(
    manifest_path: Path,
    data: dict[str, JsonValue],
    key_path: str,
    expected_keys: frozenset[str],
) -> None:
    if set(data) == expected_keys:
        return
    expected = ", ".join(sorted(expected_keys))
    msg = f"{manifest_path}: {key_path} must contain exactly these keys: {expected}"
    raise ValueError(msg)


def _require_unique(manifest_path: Path, values: list[str], key_path: str) -> None:
    if len(set(values)) == len(values):
        return
    msg = f"{manifest_path}: {key_path} must not contain duplicates"
    raise ValueError(msg)


def _lookup_path(
    manifest_path: Path,
    data: dict[str, JsonValue],
    key_path: str,
    *,
    display_path: str | None = None,
) -> JsonValue:
    value: JsonValue = data
    for key in key_path.split("."):
        if not isinstance(value, dict) or key not in value:
            msg = f"{manifest_path}: {display_path or key_path} is missing"
            raise ValueError(msg)
        value = value[key]
    return value


def _validate_identifier(manifest_path: Path, value: str, key_path: str) -> None:
    if IDENTIFIER_RE.match(value) is not None:
        return
    msg = f"{manifest_path}: {key_path} must be a kebab-case identifier"
    raise ValueError(msg)


def _validate_asset_ref(manifest_path: Path, value: str, key_path: str) -> None:
    asset_type, separator, asset_id = value.partition(":")
    if separator != ":" or asset_type not in ASSET_KINDS or IDENTIFIER_RE.match(asset_id) is None:
        msg = f"{manifest_path}: {key_path} must be a valid asset ref"
        raise ValueError(msg)


def _validate_sha256(manifest_path: Path, value: str, key_path: str) -> None:
    if SHA256_RE.fullmatch(value) is not None:
        return
    msg = f"{manifest_path}: {key_path} must be a sha256 hex digest"
    raise ValueError(msg)


def _max_semver(first: str, second: str) -> str:
    return first if _compare_semver(first, second) >= 0 else second


def _bump_patch(version: str) -> str:
    major, minor, patch = _core_tuple(version)
    return f"{major}.{minor}.{patch + 1}"


def _core_tuple(version: str) -> tuple[int, int, int]:
    match = SEMVER_RE.match(version)
    if match is None:
        msg = f"hub version must be SemVer, got: {version}"
        raise ValueError(msg)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _compare_semver(first: str, second: str) -> int:
    first_match = SEMVER_RE.match(first)
    second_match = SEMVER_RE.match(second)
    if first_match is None:
        msg = f"hub version must be SemVer, got: {first}"
        raise ValueError(msg)
    if second_match is None:
        msg = f"hub version must be SemVer, got: {second}"
        raise ValueError(msg)

    first_core = tuple(int(first_match.group(index)) for index in (1, 2, 3))
    second_core = tuple(int(second_match.group(index)) for index in (1, 2, 3))
    if first_core != second_core:
        return 1 if first_core > second_core else -1
    return _compare_prerelease(first_match.group(4), second_match.group(4))


def _compare_prerelease(first: str | None, second: str | None) -> int:
    if first is None and second is None:
        return 0
    if first is None:
        return 1
    if second is None:
        return -1
    first_parts = first.split(".")
    second_parts = second.split(".")
    for first_part, second_part in zip(first_parts, second_parts, strict=False):
        first_is_numeric = first_part.isdigit()
        second_is_numeric = second_part.isdigit()
        if first_is_numeric and second_is_numeric:
            first_number = int(first_part)
            second_number = int(second_part)
            if first_number != second_number:
                return 1 if first_number > second_number else -1
            continue
        if first_is_numeric != second_is_numeric:
            return -1 if first_is_numeric else 1
        if first_part != second_part:
            return 1 if first_part > second_part else -1
    if len(first_parts) == len(second_parts):
        return 0
    return 1 if len(first_parts) > len(second_parts) else -1
