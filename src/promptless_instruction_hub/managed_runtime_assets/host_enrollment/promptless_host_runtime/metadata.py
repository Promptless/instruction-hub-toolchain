"""Runtime environment discovery and installed-bundle metadata."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

from .contracts import (
    DEFAULT_DASHBOARD_BASE_URL,
    DEFAULT_WORKER_BASE_URL,
    Host,
    InstalledInstructionHubRelease,
    MANAGED_RUNTIME_ID,
    MANAGED_RUNTIME_MANIFEST,
    RELEASE_MANIFEST,
    RUNTIME_EXECUTABLE,
    RUNTIME_VERSION,
    RuntimeMetadata,
)
from .validation import _is_kebab_case_identifier, _non_empty, _normalize_base_url, _string_value


def _resolve_host(host_arg: str) -> Host:
    if host_arg == "codex" or host_arg == "claude" or host_arg == "claude-desktop":
        return host_arg
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "claude"
    return "codex"


def _plugin_root() -> Path | None:
    raw_root = os.environ.get("PLUGIN_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT")
    if raw_root is None or raw_root.strip() == "":
        return None
    return Path(raw_root).expanduser()


def _worker_base_url() -> str:
    return _normalize_base_url(
        os.environ.get("PROMPTLESS_WORKER_BASE_URL") or DEFAULT_WORKER_BASE_URL,
        label="worker base URL",
    )


def _dashboard_base_url() -> str:
    return _normalize_base_url(
        os.environ.get("PROMPTLESS_DASHBOARD_BASE_URL") or DEFAULT_DASHBOARD_BASE_URL,
        label="dashboard base URL",
    )


def _load_runtime_metadata(plugin_root: Path | None, host: Host) -> RuntimeMetadata:
    defaults = RuntimeMetadata(
        bootstrap_version=RUNTIME_VERSION,
        toolchain_version="unknown",
        plugin_id="unknown",
        plugin_name="unknown",
        plugin_version="unknown",
        package_id="unknown",
        target=host,
    )
    if plugin_root is None:
        return defaults
    manifest_path = plugin_root / MANAGED_RUNTIME_MANIFEST
    if not manifest_path.exists():
        return defaults
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(manifest, dict):
        return defaults
    runtimes = manifest.get("managed_runtimes")
    if not isinstance(runtimes, list):
        return defaults
    for runtime in runtimes:
        if not isinstance(runtime, dict):
            continue
        if runtime.get("id") != MANAGED_RUNTIME_ID or runtime.get("status") != "included":
            continue
        return RuntimeMetadata(
            bootstrap_version=RUNTIME_VERSION,
            toolchain_version=_string_value(runtime.get("toolchain_version")) or "unknown",
            plugin_id=_string_value(runtime.get("plugin_id")) or "unknown",
            plugin_name=_string_value(runtime.get("plugin_name")) or "unknown",
            plugin_version=_string_value(runtime.get("plugin_version")) or "unknown",
            package_id=_string_value(runtime.get("package_id")) or "unknown",
            target=host,
        )
    return defaults


def _load_installed_instruction_hub_release(
    plugin_root: Path | None,
    metadata: RuntimeMetadata,
) -> InstalledInstructionHubRelease | None:
    """Return the content-validated release embedded at the installed plugin root."""

    if (
        plugin_root is None
        or metadata.plugin_id == "unknown"
        or metadata.plugin_name == "unknown"
        or metadata.plugin_version == "unknown"
        or metadata.package_id == "unknown"
    ):
        return None
    manifest_path = plugin_root / RELEASE_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        return None
    plugin = manifest.get("plugin")
    if not isinstance(plugin, dict):
        return None
    hub_plugin_id = _non_empty(_string_value(plugin.get("id")))
    plugin_version = _non_empty(_string_value(plugin.get("version")))
    release_id = _non_empty(_string_value(manifest.get("release_id")))
    release_hash = _string_value(manifest.get("release_hash"))
    if (
        hub_plugin_id is None
        or not _is_kebab_case_identifier(hub_plugin_id)
        or metadata.plugin_id != f"{hub_plugin_id}-{metadata.package_id}"
        or len(metadata.plugin_id) > 120
        or not _is_kebab_case_identifier(metadata.plugin_id)
        or not metadata.plugin_name.strip()
        or len(metadata.plugin_name) > 200
        or plugin_version is None
        or plugin_version != metadata.plugin_version
        or release_id is None
        or release_hash is None
        or len(plugin_version) > 80
        or len(release_id) > 120
    ):
        return None

    manifest_without_release_data = {
        key: value for key, value in manifest.items() if key not in {"release_id", "release_hash"}
    }
    expected_release_id = f"{plugin_version}+{_stable_json_hash(manifest_without_release_data)[:12]}"
    if release_id != expected_release_id:
        return None
    manifest_without_release_hash = {key: value for key, value in manifest.items() if key != "release_hash"}
    if release_hash != _stable_json_hash(manifest_without_release_hash):
        return None
    if not _release_manifest_contains_runtime_identity(manifest, metadata):
        return None
    return InstalledInstructionHubRelease(
        plugin_id=metadata.plugin_id,
        plugin_name=metadata.plugin_name,
        plugin_version=plugin_version,
        release_id=release_id,
    )


def _release_manifest_contains_runtime_identity(manifest: dict[object, object], metadata: RuntimeMetadata) -> bool:
    runtimes = manifest.get("managed_runtimes")
    if not isinstance(runtimes, list):
        return False
    target = "claude" if metadata.target == "claude-desktop" else metadata.target
    for runtime in runtimes:
        if not isinstance(runtime, dict):
            continue
        runtime_value = cast(dict[str, object], runtime)
        if (
            runtime_value.get("id") == MANAGED_RUNTIME_ID
            and runtime_value.get("status") == "included"
            and runtime_value.get("target") == target
            and runtime_value.get("package_id") == metadata.package_id
            and runtime_value.get("plugin_id") == metadata.plugin_id
            and runtime_value.get("plugin_name") == metadata.plugin_name
            and runtime_value.get("plugin_version") == metadata.plugin_version
        ):
            return True
    return False


def _stable_json_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _self_sha256() -> str:
    package_root = Path(__file__).resolve().parent
    bundle_root = package_root.parent
    files = [bundle_root / RUNTIME_EXECUTABLE]
    files.extend(
        path
        for path in package_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(bundle_root).parts and path.suffix != ".pyc"
    )

    digest = hashlib.sha256()
    for path in sorted(files, key=lambda candidate: candidate.relative_to(bundle_root).as_posix()):
        relative_path = path.relative_to(bundle_root).as_posix()
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
