"""Promptless-owned runtime artifacts injected into generated plugins."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from promptless_instruction_hub.config import MANAGED_RUNTIME_MANIFEST_PATH
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, file_hash, read_json_mapping, write_json
from promptless_instruction_hub.models import Harness, HubConfig, PackageDefinition

RuntimeStatus = Literal["included"]

TRACE_COLLECTOR_RUNTIME_ID = "native-trace-collector"
TRACE_COLLECTOR_ASSET_DIR = "trace-collector"
TRACE_COLLECTOR_EXECUTABLE = "promptless-trace-collector"
TRACE_COLLECTOR_HOOK_TIMEOUT_SECONDS = 45
TRACE_COLLECTOR_CHANNEL = "stable"
TRACE_COLLECTOR_VERSION = "0.1.0"
MANAGED_RUNTIME_MANIFEST = MANAGED_RUNTIME_MANIFEST_PATH
SUPPORTED_TRACE_COLLECTOR_TARGETS: tuple[Harness, ...] = ("claude", "codex")

_ASSET_ROOT = Path(__file__).parent / "managed_runtime_assets" / TRACE_COLLECTOR_ASSET_DIR
_EXECUTABLE_SOURCE = _ASSET_ROOT / TRACE_COLLECTOR_EXECUTABLE


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
    """Write managed-runtime metadata and inject supported runtime artifacts for one generated plugin."""

    plugin_id = f"{config.plugin_id}-{package.id}"
    if target not in SUPPORTED_TRACE_COLLECTOR_TARGETS:
        return ()

    _copy_trace_collector_executable(target_root)
    _write_trace_collector_hooks(target_root, target)
    record = ManagedRuntimeRecord(
        id=TRACE_COLLECTOR_RUNTIME_ID,
        status="included",
        target=target,
        package_id=package.id,
        plugin_id=plugin_id,
        plugin_version=config.plugin_version,
        toolchain_version=_toolchain_version(),
        channel=TRACE_COLLECTOR_CHANNEL,
        version=TRACE_COLLECTOR_VERSION,
        sha256=file_hash(_EXECUTABLE_SOURCE),
        executable=TRACE_COLLECTOR_EXECUTABLE,
        path=f"bin/{TRACE_COLLECTOR_EXECUTABLE}",
        hook="hooks/hooks.json",
    )
    _write_plugin_manifest(target_root, (record,))
    return (record,)


def _copy_trace_collector_executable(target_root: Path) -> None:
    destination = target_root / "bin" / TRACE_COLLECTOR_EXECUTABLE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_EXECUTABLE_SOURCE, destination)
    destination.chmod(0o755)


def _write_trace_collector_hooks(target_root: Path, target: Harness) -> None:
    hook_path = target_root / "hooks/hooks.json"
    hook_config = _existing_hook_config(hook_path)
    hooks = hook_config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        msg = f"{hook_path} field hooks must be a JSON object"
        raise InstructionHubError(msg)

    for event_name in _trace_collector_hook_events(target):
        event_hooks = hooks.setdefault(event_name, [])
        if not isinstance(event_hooks, list):
            msg = f"{hook_path} field hooks.{event_name} must be a JSON array"
            raise InstructionHubError(msg)
        event_hooks.append(_trace_collector_hook_entry(target, event_name))

    write_json(hook_path, hook_config)


def _trace_collector_hook_events(target: Harness) -> tuple[str, ...]:
    if target == "codex":
        return ("SessionStart", "Stop")
    return ("SessionStart", "Stop", "SessionEnd")


def _existing_hook_config(hook_path: Path) -> dict[str, JsonValue]:
    if not hook_path.exists():
        return {}
    try:
        return read_json_mapping(hook_path)
    except OSError as exc:
        msg = f"failed to read existing hook config at {hook_path}: {exc}"
        raise InstructionHubError(msg) from exc
    except ValueError as exc:
        msg = f"existing hook config at {hook_path} is invalid: {exc}"
        raise InstructionHubError(msg) from exc


def _trace_collector_hook_entry(target: Harness, event_name: str) -> dict[str, JsonValue]:
    lifecycle = _trace_collector_lifecycle_arg(event_name)
    if target == "claude":
        hook_command: dict[str, JsonValue] = {
            "command": (
                f'python3 "${{CLAUDE_PLUGIN_ROOT}}/bin/{TRACE_COLLECTOR_EXECUTABLE}" '
                f"--host claude --lifecycle {lifecycle} --quiet"
            ),
        }
    else:
        hook_command = {
            "command": (
                f'python3 "${{PLUGIN_ROOT}}/bin/{TRACE_COLLECTOR_EXECUTABLE}" '
                f"--host codex --lifecycle {lifecycle} --quiet"
            ),
        }

    # Codex and Claude both load plugin-root hooks from hooks/hooks.json. Codex may require
    # the user to trust/review plugin hooks before running trace upload commands.
    # https://developers.openai.com/codex/plugins/build
    # https://docs.anthropic.com/en/docs/claude-code/hooks
    # The Python entrypoint is dogfood-only. Customer-grade releases should invoke a
    # Promptless-built static native binary so customer machines do not need Python or uv.
    entry: dict[str, JsonValue] = {
        "hooks": [
            {
                "type": "command",
                "timeout": TRACE_COLLECTOR_HOOK_TIMEOUT_SECONDS,
                "statusMessage": "Uploading Promptless traces",
                **hook_command,
            }
        ],
    }
    if event_name == "SessionStart":
        entry["matcher"] = "startup|resume"
    return entry


def _trace_collector_lifecycle_arg(event_name: str) -> str:
    if event_name == "SessionStart":
        return "session_start"
    if event_name == "Stop":
        return "stop"
    if event_name == "SessionEnd":
        return "session_end"
    msg = f"unsupported trace collector hook event: {event_name}"
    raise InstructionHubError(msg)


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
