"""Runtime environment discovery and installed-bundle metadata."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .contracts import (
    DEFAULT_DASHBOARD_BASE_URL,
    DEFAULT_WORKER_BASE_URL,
    Host,
    MANAGED_RUNTIME_ID,
    MANAGED_RUNTIME_MANIFEST,
    RUNTIME_EXECUTABLE,
    RUNTIME_VERSION,
    RuntimeMetadata,
)
from .validation import _normalize_base_url, _string_value


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
            plugin_version=_string_value(runtime.get("plugin_version")) or "unknown",
            package_id=_string_value(runtime.get("package_id")) or "unknown",
            target=host,
        )
    return defaults


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
