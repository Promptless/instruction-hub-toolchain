"""Promptless-owned runtime artifacts injected into generated plugins."""

from __future__ import annotations

import json
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

HOST_RUNTIME_ID = "host-runtime"
HOST_RUNTIME_ASSET_DIR = "host-enrollment"
HOST_RUNTIME_EXECUTABLE = "promptless-host-runtime"
# Keep this above the browser callback deadline plus the follow-up poll,
# policy fetch, local config write, and check-in network calls. Otherwise the
# host can kill SessionStart before a resumable pending enrollment is persisted.
HOST_RUNTIME_HOOK_TIMEOUT_SECONDS = 390
HOST_RUNTIME_CHANNEL = "stable"
HOST_RUNTIME_VERSION = "0.2.1"
MANAGED_RUNTIME_MANIFEST = MANAGED_RUNTIME_MANIFEST_PATH
SUPPORTED_HOST_RUNTIME_TARGETS: tuple[Harness, ...] = ("claude", "codex")
MISSING_RUNTIME_ROOT_MESSAGE = (
    "Promptless Instruction Hub hook could not find its plugin root. "
    "Update the host CLI or reinstall the Promptless plugin."
)
MISSING_RUNTIME_FILE_MESSAGE = (
    "Promptless Instruction Hub hook could not find its managed runtime. Reinstall the Promptless plugin."
)
MISSING_PYTHON_MESSAGE = (
    "Promptless Instruction Hub hook could not find python3. Install Python 3 or reinstall the Promptless plugin."
)

_ASSET_ROOT = Path(__file__).parent / "managed_runtime_assets" / HOST_RUNTIME_ASSET_DIR
_EXECUTABLE_SOURCE = _ASSET_ROOT / HOST_RUNTIME_EXECUTABLE


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
    if target not in SUPPORTED_HOST_RUNTIME_TARGETS:
        return ()

    _copy_runtime_executable(target_root)
    _write_host_runtime_hook(target_root, target)
    record = ManagedRuntimeRecord(
        id=HOST_RUNTIME_ID,
        status="included",
        target=target,
        package_id=package.id,
        plugin_id=plugin_id,
        plugin_version=config.plugin_version,
        toolchain_version=_toolchain_version(),
        channel=HOST_RUNTIME_CHANNEL,
        version=HOST_RUNTIME_VERSION,
        sha256=file_hash(_EXECUTABLE_SOURCE),
        executable=HOST_RUNTIME_EXECUTABLE,
        path=f"bin/{HOST_RUNTIME_EXECUTABLE}",
        hook="hooks/hooks.json",
    )
    _write_plugin_manifest(target_root, (record,))
    return (record,)


def _copy_runtime_executable(target_root: Path) -> None:
    destination = target_root / "bin" / HOST_RUNTIME_EXECUTABLE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_EXECUTABLE_SOURCE, destination)
    destination.chmod(0o755)


def _write_host_runtime_hook(target_root: Path, target: Harness) -> None:
    hook_path = target_root / "hooks/hooks.json"
    hook_config = _existing_hook_config(hook_path)
    hooks = hook_config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        msg = f"{hook_path} field hooks must be a JSON object"
        raise InstructionHubError(msg)
    session_start = hooks.setdefault("SessionStart", [])
    if not isinstance(session_start, list):
        msg = f"{hook_path} field hooks.SessionStart must be a JSON array"
        raise InstructionHubError(msg)
    session_start.append(_host_enrollment_hook_entry(target))
    write_json(hook_path, hook_config)


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


def _host_enrollment_hook_entry(target: Harness) -> dict[str, JsonValue]:
    if target == "claude":
        hook_command = _claude_host_runtime_hook_command()
    else:
        root_expr = "${PLUGIN_ROOT:-}"
        host = "codex"
        hook_command = {
            "command": _posix_host_runtime_hook_command(root_expr=root_expr, host=host),
        }

    # Codex and Claude both load plugin-root hooks from hooks/hooks.json. Codex may require
    # the user to trust/review plugin hooks before running this startup command.
    # https://developers.openai.com/codex/plugins/build
    # https://docs.anthropic.com/en/docs/claude-code/hooks
    # The Python entrypoint is dogfood-only. Customer-grade releases should invoke a
    # Promptless-built static native binary so customer machines do not need Python or uv.
    # The hook deliberately omits --quiet so the runtime can surface its status. Both Claude and
    # Codex render a SessionStart `systemMessage`: the runtime emits one when the Instruction Hub
    # plugin version changes and for actionable enrollment outcomes (config written, pending
    # approval, blocked). The binary still accepts --quiet for manual runs.
    return {
        "matcher": "startup|resume",
        "hooks": [
            {
                "type": "command",
                "timeout": HOST_RUNTIME_HOOK_TIMEOUT_SECONDS,
                "statusMessage": "Checking Promptless host runtime",
                **hook_command,
            }
        ],
    }


def _hook_json_system_message(message: str) -> str:
    payload = json.dumps({"systemMessage": message}, separators=(",", ":"))
    return payload.replace('"', '\\"')


def _claude_host_runtime_hook_command() -> dict[str, JsonValue]:
    return {
        "command": "node",
        "args": [
            "-e",
            _node_host_runtime_hook_script(root_envs=("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT"), host="claude"),
            "${CLAUDE_PLUGIN_ROOT}",
        ],
    }


def _node_host_runtime_hook_script(*, root_envs: tuple[str, ...], host: Harness) -> str:
    root_env_names = json.dumps(list(root_envs), separators=(",", ":"))
    missing_root = json.dumps({"systemMessage": MISSING_RUNTIME_ROOT_MESSAGE}, separators=(",", ":"))
    missing_file = json.dumps({"systemMessage": MISSING_RUNTIME_FILE_MESSAGE}, separators=(",", ":"))
    missing_python = json.dumps({"systemMessage": MISSING_PYTHON_MESSAGE}, separators=(",", ":"))
    python_probe = "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"
    return (
        "const fs = require('fs');\n"
        "const path = require('path');\n"
        "const { spawnSync } = require('child_process');\n"
        f"const rootEnvNames = {root_env_names};\n"
        "let root = process.argv.slice(1).find((value) => value && !value.startsWith('${')) || '';\n"
        "for (const name of rootEnvNames) {\n  if (root) break;\n  root = process.env[name] || '';\n}\n"
        f"if (!root) {{ console.log({missing_root!r}); process.exit(0); }}\n"
        f"const runtime = path.join(root, 'bin', {HOST_RUNTIME_EXECUTABLE!r});\n"
        f"if (!fs.existsSync(runtime) || !fs.statSync(runtime).isFile()) {{ console.log({missing_file!r}); process.exit(0); }}\n"
        f"const pythonProbe = {python_probe!r};\n"
        f"const runtimeArgs = [runtime, 'ensure', '--host', {host!r}];\n"
        "const candidates = [\n"
        "  { command: 'python3', probeArgs: ['-c', pythonProbe], runArgs: runtimeArgs },\n"
        "  { command: 'python', probeArgs: ['-c', pythonProbe], runArgs: runtimeArgs },\n"
        "  { command: 'py', probeArgs: ['-3', '-c', pythonProbe], runArgs: ['-3', ...runtimeArgs] },\n"
        "];\n"
        "for (const candidate of candidates) {\n"
        "  const probe = spawnSync(candidate.command, candidate.probeArgs, { stdio: 'ignore' });\n"
        "  if (probe.error || probe.status !== 0) continue;\n"
        "  const result = spawnSync(candidate.command, candidate.runArgs, { stdio: 'inherit', env: process.env });\n"
        "  if (result.error) continue;\n"
        "  process.exit(result.status === null ? 1 : result.status);\n"
        "}\n"
        f"console.log({missing_python!r});\n"
        "process.exit(0);\n"
    )


def _posix_host_runtime_hook_command(*, root_expr: str, host: Harness) -> str:
    return (
        f"sh -c 'root={root_expr}; "
        f'if [ -z "$root" ]; then printf "%s\\n" "{_hook_json_system_message(MISSING_RUNTIME_ROOT_MESSAGE)}"; exit 0; fi; '
        f'runtime="$root/bin/{HOST_RUNTIME_EXECUTABLE}"; '
        f'if [ ! -x "$runtime" ]; then printf "%s\\n" "{_hook_json_system_message(MISSING_RUNTIME_FILE_MESSAGE)}"; exit 0; fi; '
        f'if ! command -v python3 >/dev/null 2>&1; then printf "%s\\n" "{_hook_json_system_message(MISSING_PYTHON_MESSAGE)}"; exit 0; fi; '
        f'exec python3 "$runtime" ensure --host {host}'
        "'"
    )


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
