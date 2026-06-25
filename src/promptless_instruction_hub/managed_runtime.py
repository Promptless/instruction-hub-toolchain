"""Promptless-owned runtime artifacts injected into generated plugins."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from promptless_instruction_hub.fs import JsonValue, file_hash, read_json_mapping, write_json
from promptless_instruction_hub.models import Harness, HubConfig, PackageDefinition

RuntimeStatus = Literal["included", "unsupported"]

HOST_ENROLLMENT_BOOTSTRAP_ID = "host-enrollment-bootstrap"
HOST_ENROLLMENT_EXECUTABLE = "promptless-host-enrollment-bootstrap"
HOST_ENROLLMENT_CHANNEL = "stable"
HOST_ENROLLMENT_VERSION = "0.1.0"
MANAGED_RUNTIME_MANIFEST = Path(".promptless/managed-runtimes.json")
SUPPORTED_HOST_ENROLLMENT_TARGETS: tuple[Harness, ...] = ("claude", "codex")

_ASSET_ROOT = Path(__file__).parent / "managed_runtime_assets" / HOST_ENROLLMENT_BOOTSTRAP_ID
_EXECUTABLE_SOURCE = _ASSET_ROOT / HOST_ENROLLMENT_EXECUTABLE


@dataclass(frozen=True)
class ManagedRuntimeRecord:
    """Exact managed runtime metadata written into generated plugin output."""

    id: str
    status: RuntimeStatus
    target: Harness
    package_id: str
    plugin_id: str
    plugin_version: str
    toolchain_version: str
    channel: str | None = None
    version: str | None = None
    sha256: str | None = None
    executable: str | None = None
    path: str | None = None
    hook: str | None = None
    reason: str | None = None

    def to_manifest(self) -> dict[str, JsonValue]:
        """Return a deterministic JSON record for manifests and check-in context."""

        data: dict[str, JsonValue] = {
            "id": self.id,
            "package_id": self.package_id,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "status": self.status,
            "target": self.target,
            "toolchain_version": self.toolchain_version,
        }
        optional_fields: tuple[tuple[str, str | None], ...] = (
            ("channel", self.channel),
            ("version", self.version),
            ("sha256", self.sha256),
            ("executable", self.executable),
            ("path", self.path),
            ("hook", self.hook),
            ("reason", self.reason),
        )
        for key, value in optional_fields:
            if value is not None:
                data[key] = value
        return data


def render_managed_runtimes(
    target_root: Path,
    target: Harness,
    config: HubConfig,
    package: PackageDefinition,
) -> tuple[ManagedRuntimeRecord, ...]:
    """Inject Promptless-owned runtime artifacts for one generated plugin."""

    plugin_id = f"{config.plugin_id}-{package.id}"
    if target not in SUPPORTED_HOST_ENROLLMENT_TARGETS:
        record = ManagedRuntimeRecord(
            id=HOST_ENROLLMENT_BOOTSTRAP_ID,
            status="unsupported",
            target=target,
            package_id=package.id,
            plugin_id=plugin_id,
            plugin_version=config.plugin_version,
            toolchain_version=_toolchain_version(),
            reason="host enrollment bootstrap is only supported for Claude and Codex plugins",
        )
        _write_plugin_manifest(target_root, (record,))
        return (record,)

    _copy_bootstrap_executable(target_root)
    _write_host_enrollment_hook(target_root, target)
    record = ManagedRuntimeRecord(
        id=HOST_ENROLLMENT_BOOTSTRAP_ID,
        status="included",
        target=target,
        package_id=package.id,
        plugin_id=plugin_id,
        plugin_version=config.plugin_version,
        toolchain_version=_toolchain_version(),
        channel=HOST_ENROLLMENT_CHANNEL,
        version=HOST_ENROLLMENT_VERSION,
        sha256=file_hash(_EXECUTABLE_SOURCE),
        executable=HOST_ENROLLMENT_EXECUTABLE,
        path=f"bin/{HOST_ENROLLMENT_EXECUTABLE}",
        hook="hooks/hooks.json",
    )
    _write_plugin_manifest(target_root, (record,))
    return (record,)


def _copy_bootstrap_executable(target_root: Path) -> None:
    destination = target_root / "bin" / HOST_ENROLLMENT_EXECUTABLE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_EXECUTABLE_SOURCE, destination)
    destination.chmod(0o755)


def _write_host_enrollment_hook(target_root: Path, target: Harness) -> None:
    hook_path = target_root / "hooks/hooks.json"
    hook_config = _existing_hook_config(hook_path)
    hooks = hook_config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        hook_config["hooks"] = hooks
    session_start = hooks.setdefault("SessionStart", [])
    if not isinstance(session_start, list):
        session_start = []
        hooks["SessionStart"] = session_start
    session_start.append(_host_enrollment_hook_entry(target))
    write_json(hook_path, hook_config)


def _existing_hook_config(hook_path: Path) -> dict[str, JsonValue]:
    if not hook_path.exists():
        return {}
    try:
        return read_json_mapping(hook_path)
    except (OSError, ValueError):
        return {}


def _host_enrollment_hook_entry(target: Harness) -> dict[str, JsonValue]:
    if target == "claude":
        root_expr = "${CLAUDE_PLUGIN_ROOT:-$PLUGIN_ROOT}"
        host = "claude"
    else:
        root_expr = "${PLUGIN_ROOT}"
        host = "codex"

    # Codex and Claude both load plugin-root hooks from hooks/hooks.json and execute command hooks.
    # https://developers.openai.com/codex/plugins/build
    # https://docs.anthropic.com/en/docs/claude-code/hooks
    command = f'python3 "{root_expr}/bin/{HOST_ENROLLMENT_EXECUTABLE}" --host {host} --quiet'
    return {
        "matcher": "startup|resume",
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 20,
                "statusMessage": "Checking Promptless host enrollment",
            }
        ],
    }


def _write_plugin_manifest(target_root: Path, records: tuple[ManagedRuntimeRecord, ...]) -> None:
    write_json(
        target_root / MANAGED_RUNTIME_MANIFEST,
        {
            "schema_version": 1,
            "managed_runtimes": [record.to_manifest() for record in records],
        },
    )


def _toolchain_version() -> str:
    try:
        return version("promptless-instruction-hub")
    except PackageNotFoundError:
        return "0.0.0+local"
