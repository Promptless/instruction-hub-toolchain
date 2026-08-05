"""Promptless-owned runtime artifacts injected into generated plugins."""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from promptless_instruction_hub.config import MANAGED_RUNTIME_MANIFEST_PATH
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, read_json_mapping, write_json
from promptless_instruction_hub.models import Harness, HubConfig, PackageDefinition

RuntimeStatus = Literal["included"]

HOST_RUNTIME_ID = "host-runtime"
HOST_RUNTIME_ASSET_DIR = "host_enrollment"
HOST_RUNTIME_EXECUTABLE = "promptless-host-runtime"
HOST_RUNTIME_PACKAGE = "promptless_host_runtime"
# Directory (relative to a plugin root) that the runtime bundle is copied into.
# Intentionally not "bin": claude.ai-hosted plugins must not ship a top-level bin/.
HOST_RUNTIME_OUTPUT_DIR = "runtime"
# Keep one startup pass above the browser callback deadline plus the follow-up poll,
# policy fetch, local config write, check-in network calls, and trace collection.
# Claude SessionStart can run two startup passes serially: Claude Code first, then a
# best-effort Claude Desktop pass when Desktop audit sources exist.
HOST_RUNTIME_STARTUP_PASS_TIMEOUT_SECONDS = 390
HOST_RUNTIME_CLAUDE_SESSION_START_TIMEOUT_SECONDS = HOST_RUNTIME_STARTUP_PASS_TIMEOUT_SECONDS * 2
HOST_RUNTIME_TERMINAL_HOOK_TIMEOUT_SECONDS = HOST_RUNTIME_STARTUP_PASS_TIMEOUT_SECONDS
HOST_RUNTIME_CHANNEL = "stable"
HOST_RUNTIME_VERSION = "0.2.5"
MANAGED_RUNTIME_MANIFEST = MANAGED_RUNTIME_MANIFEST_PATH
SUPPORTED_HOST_RUNTIME_TARGETS: tuple[Harness, ...] = ("claude", "codex")
MISSING_RUNTIME_ROOT_MESSAGE = (
    "Promptless Instruction Hub hook could not find its plugin root. "
    "Update the host CLI or reinstall the Promptless plugin."
)
MISSING_RUNTIME_FILE_MESSAGE = (
    "Promptless Instruction Hub hook could not find its managed runtime. Reinstall the Promptless plugin."
)
UNREADABLE_RUNTIME_FILE_MESSAGE = (
    "Promptless Instruction Hub hook found its managed runtime, but it is not readable. "
    "Reinstall the Promptless plugin."
)
MISSING_PYTHON_MESSAGE = (
    "Promptless Instruction Hub hook could not find Python 3.9 or newer. "
    "Install Python 3.9+ or reinstall the Promptless plugin."
)
UNSUPPORTED_PYTHON_MESSAGE = (
    "Promptless Instruction Hub hook found Python, but none are Python 3.9 or newer. "
    "Install Python 3.9+ or reinstall the Promptless plugin."
)
BROKEN_PYTHON_MESSAGE = (
    "Promptless Instruction Hub hook could not start a usable Python 3.9 or newer interpreter. "
    "Reinstall the Promptless plugin."
)
PYTHON_MIN_VERSION = (3, 9)
PYTHON_VERSION_PROBE = f"import sys; raise SystemExit(0 if sys.version_info >= {PYTHON_MIN_VERSION!r} else 2)"

_ASSET_ROOT = Path(__file__).parent / "managed_runtime_assets" / HOST_RUNTIME_ASSET_DIR
_EXECUTABLE_SOURCE = _ASSET_ROOT / HOST_RUNTIME_EXECUTABLE
_PACKAGE_SOURCE = _ASSET_ROOT / HOST_RUNTIME_PACKAGE


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

    _copy_runtime_bundle(target_root)
    _write_host_runtime_hooks(target_root, target)
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
        sha256=_runtime_bundle_sha256(_ASSET_ROOT),
        executable=HOST_RUNTIME_EXECUTABLE,
        path=f"{HOST_RUNTIME_OUTPUT_DIR}/{HOST_RUNTIME_EXECUTABLE}",
        hook="hooks/hooks.json",
    )
    _write_plugin_manifest(target_root, (record,))
    return (record,)


def _copy_runtime_bundle(target_root: Path) -> None:
    runtime_root = target_root / HOST_RUNTIME_OUTPUT_DIR
    runtime_root.mkdir(parents=True, exist_ok=True)

    executable_destination = runtime_root / HOST_RUNTIME_EXECUTABLE
    shutil.copy2(_EXECUTABLE_SOURCE, executable_destination)
    executable_destination.chmod(0o755)

    package_destination = runtime_root / HOST_RUNTIME_PACKAGE
    if package_destination.exists():
        shutil.rmtree(package_destination)
    shutil.copytree(
        _PACKAGE_SOURCE,
        package_destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def _runtime_bundle_sha256(bundle_root: Path) -> str:
    digest = hashlib.sha256()
    for path in _runtime_bundle_files(bundle_root):
        relative_path = path.relative_to(bundle_root).as_posix()
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _runtime_bundle_files(bundle_root: Path) -> tuple[Path, ...]:
    package_root = bundle_root / HOST_RUNTIME_PACKAGE
    package_files = (
        path
        for path in package_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(bundle_root).parts and path.suffix != ".pyc"
    )
    return tuple(
        sorted(
            (bundle_root / HOST_RUNTIME_EXECUTABLE, *package_files),
            key=lambda path: path.relative_to(bundle_root).as_posix(),
        )
    )


HOST_RUNTIME_BUNDLE_RELATIVE_PATHS = tuple(
    path.relative_to(_ASSET_ROOT).as_posix() for path in _runtime_bundle_files(_ASSET_ROOT)
)


def _write_host_runtime_hooks(target_root: Path, target: Harness) -> None:
    hook_path = target_root / "hooks/hooks.json"
    hook_config = _existing_hook_config(hook_path)
    hooks = hook_config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        msg = f"{hook_path} field hooks must be a JSON object"
        raise InstructionHubError(msg)
    for event_name in _host_runtime_hook_events():
        event_hooks = hooks.setdefault(event_name, [])
        if not isinstance(event_hooks, list):
            msg = f"{hook_path} field hooks.{event_name} must be a JSON array"
            raise InstructionHubError(msg)
        event_hooks.append(_host_runtime_hook_entry(target, event_name))
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


def _host_runtime_hook_events() -> tuple[str, ...]:
    return ("SessionStart", "Stop", "SessionEnd", "SubagentStop")


def _host_runtime_hook_entry(target: Harness, event_name: str) -> dict[str, JsonValue]:
    lifecycle = _host_runtime_lifecycle_arg(event_name)
    if event_name == "SessionStart":
        hook_command = _host_runtime_start_hook_command(target, lifecycle=lifecycle)
    else:
        hook_command = _host_runtime_terminal_hook_command(target, lifecycle=lifecycle)
    # Codex and Claude both load plugin-root hooks from hooks/hooks.json. Codex may require
    # the user to trust/review plugin hooks before running these commands.
    # https://developers.openai.com/codex/plugins/build
    # https://docs.anthropic.com/en/docs/claude-code/hooks
    # The Python entrypoint is dogfood-only. Customer-grade releases should invoke a
    # Promptless-built static native binary so customer machines do not need Python or uv.
    # SessionStart performs user-visible enrollment, writes config/check-in status, and then runs
    # a quiet forward-only JSONL baseline. Terminal lifecycle hooks only upload native JSONL
    # ranges; failures are non-blocking and stay out of the agent transcript.
    hook_entry: dict[str, JsonValue] = {
        "hooks": [
            {
                "type": "command",
                "timeout": _host_runtime_hook_timeout(target, event_name),
                "statusMessage": (
                    "Checking Promptless host runtime"
                    if event_name == "SessionStart"
                    else "Uploading Promptless traces"
                ),
                **hook_command,
            }
        ],
    }
    if event_name == "SessionStart":
        hook_entry["matcher"] = "startup|resume"
    return hook_entry


def _host_runtime_hook_timeout(target: Harness, event_name: str) -> int:
    if target == "claude" and event_name == "SessionStart":
        return HOST_RUNTIME_CLAUDE_SESSION_START_TIMEOUT_SECONDS
    if event_name == "SessionStart":
        return HOST_RUNTIME_STARTUP_PASS_TIMEOUT_SECONDS
    return HOST_RUNTIME_TERMINAL_HOOK_TIMEOUT_SECONDS


def _host_runtime_start_hook_command(target: Harness, *, lifecycle: str) -> dict[str, JsonValue]:
    if target == "claude":
        return _claude_host_runtime_hook_command(
            lifecycle=lifecycle,
            run_ensure=True,
            baseline=True,
            quiet_failure=False,
            allow_sibling_runtime=False,
            collect_claude_desktop=True,
        )
    return {
        "command": _posix_host_runtime_hook_command(
            root_expr="${PLUGIN_ROOT:-}",
            host="codex",
            lifecycle=lifecycle,
            run_ensure=True,
            baseline=True,
            quiet_failure=False,
            allow_sibling_runtime=False,
        ),
    }


def _host_runtime_terminal_hook_command(target: Harness, *, lifecycle: str) -> dict[str, JsonValue]:
    if target == "claude":
        return _claude_host_runtime_hook_command(
            lifecycle=lifecycle,
            run_ensure=False,
            baseline=False,
            quiet_failure=True,
            allow_sibling_runtime=True,
            collect_claude_desktop=False,
        )
    return {
        "command": _posix_host_runtime_hook_command(
            root_expr="${PLUGIN_ROOT:-}",
            host="codex",
            lifecycle=lifecycle,
            run_ensure=False,
            baseline=False,
            quiet_failure=True,
            allow_sibling_runtime=True,
        ),
    }


def _host_runtime_lifecycle_arg(event_name: str) -> str:
    match event_name:
        case "SessionStart":
            return "session_start"
        case "Stop":
            return "stop"
        case "SessionEnd":
            return "session_end"
        case "SubagentStop":
            return "subagent_stop"
        case _:
            msg = f"unsupported host runtime hook event: {event_name}"
            raise InstructionHubError(msg)


def _system_message_json(message: str) -> str:
    return json.dumps({"systemMessage": message}, separators=(",", ":"))


def _hook_json_system_message(message: str) -> str:
    return _system_message_json(message).replace('"', '\\"')


def _claude_host_runtime_hook_command(
    *,
    lifecycle: str,
    run_ensure: bool,
    baseline: bool,
    quiet_failure: bool,
    allow_sibling_runtime: bool,
    collect_claude_desktop: bool,
) -> dict[str, JsonValue]:
    return {
        "command": "node",
        "args": [
            "-e",
            _node_host_runtime_hook_script(
                root_envs=("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT"),
                host="claude",
                lifecycle=lifecycle,
                run_ensure=run_ensure,
                baseline=baseline,
                quiet_failure=quiet_failure,
                allow_sibling_runtime=allow_sibling_runtime,
                collect_claude_desktop=collect_claude_desktop,
            ),
            "${CLAUDE_PLUGIN_ROOT}",
        ],
    }


def _node_host_runtime_hook_script(
    *,
    root_envs: tuple[str, ...],
    host: Harness,
    lifecycle: str,
    run_ensure: bool,
    baseline: bool,
    quiet_failure: bool,
    allow_sibling_runtime: bool,
    collect_claude_desktop: bool,
) -> str:
    root_env_names = json.dumps(list(root_envs), separators=(",", ":"))
    bundle_relative_paths = json.dumps(HOST_RUNTIME_BUNDLE_RELATIVE_PATHS, separators=(",", ":"))
    missing_root = _system_message_json(MISSING_RUNTIME_ROOT_MESSAGE)
    missing_file = _system_message_json(MISSING_RUNTIME_FILE_MESSAGE)
    unreadable_file = _system_message_json(UNREADABLE_RUNTIME_FILE_MESSAGE)
    missing_python = _system_message_json(MISSING_PYTHON_MESSAGE)
    unsupported_python = _system_message_json(UNSUPPORTED_PYTHON_MESSAGE)
    broken_python = _system_message_json(BROKEN_PYTHON_MESSAGE)
    collect_args = [
        "runtime",
        "'collect'",
        "'--host'",
        repr(host),
        "'--lifecycle'",
        repr(lifecycle),
    ]
    if baseline:
        collect_args.append("'--baseline'")
    collect_args.append("'--quiet'")
    ensure_run_script = ""
    if run_ensure:
        ensure_run_script = (
            f"const ensureArgs = [runtime, 'ensure', '--host', {host!r}];\n"
            "  const ensure = spawnSync(candidate.command, [...candidate.runPrefix, ...ensureArgs], { stdio: 'inherit', env: process.env });\n"
            "  if (ensure.error) {\n"
            "    sawBrokenPython = true;\n"
            "    continue;\n"
            "  }\n"
            "  if (ensure.status !== 0) process.exit(ensure.status === null ? 1 : ensure.status);\n"
        )
    claude_desktop_collect_script = ""
    if collect_claude_desktop:
        # Desktop ensure is not quiet so pending approval is persisted, but stdout stays
        # suppressed so the best-effort sibling ensure cannot emit a second hook-control object.
        claude_desktop_collect_script = (
            "  const desktopEnsureArgs = [runtime, 'ensure', '--host', 'claude-desktop', '--if-sources'];\n"
            "  const desktopEnsure = spawnSync(candidate.command, [...candidate.runPrefix, ...desktopEnsureArgs], { stdio: ['ignore', 'ignore', 'inherit'], env: process.env });\n"
            "  if (!desktopEnsure.error && desktopEnsure.status === 0) {\n"
            "    const desktopCollectArgs = [runtime, 'collect', '--host', 'claude-desktop', '--lifecycle', 'session_start', '--baseline', '--quiet'];\n"
            "    spawnSync(candidate.command, [...candidate.runPrefix, ...desktopCollectArgs], { stdio: ['ignore', 'ignore', 'ignore'], env: process.env });\n"
            "  }\n"
        )
    return (
        "const fs = require('fs');\n"
        "const path = require('path');\n"
        "const { spawnSync } = require('child_process');\n"
        f"const rootEnvNames = {root_env_names};\n"
        f"const bundleRelativePaths = {bundle_relative_paths};\n"
        f"const emitDiagnostics = {json.dumps(not quiet_failure)};\n"
        f"const allowSiblingRuntime = {json.dumps(allow_sibling_runtime)};\n"
        "function finishWithDiagnostic(payload) {\n"
        "  if (emitDiagnostics) console.log(payload);\n"
        "  process.exit(0);\n"
        "}\n"
        "function runtimeState(candidate) {\n"
        "  const bundleRoot = path.dirname(candidate);\n"
        "  const requiredFiles = bundleRelativePaths.map((relativePath) => path.join(bundleRoot, ...relativePath.split('/')));\n"
        "  for (const requiredFile of requiredFiles) {\n"
        "    let stat;\n"
        "    try { stat = fs.statSync(requiredFile); } catch (error) { return 'missing'; }\n"
        "    if (!stat.isFile()) return 'missing';\n"
        "  }\n"
        "  for (const requiredFile of requiredFiles) {\n"
        "    try { fs.accessSync(requiredFile, fs.constants.R_OK); } catch (error) { return 'unreadable'; }\n"
        "  }\n"
        "  return 'ready';\n"
        "}\n"
        "function siblingRuntime(rootPath) {\n"
        "  const parent = path.dirname(rootPath);\n"
        "  let entries;\n"
        "  try { entries = fs.readdirSync(parent, { withFileTypes: true }); } catch (error) { return ''; }\n"
        "  entries.sort((left, right) => left.name.localeCompare(right.name));\n"
        "  let selected = '';\n"
        "  for (const entry of entries) {\n"
        "    if (!entry.isDirectory()) continue;\n"
        f"    const candidate = path.join(parent, entry.name, {HOST_RUNTIME_OUTPUT_DIR!r}, {HOST_RUNTIME_EXECUTABLE!r});\n"
        "    if (runtimeState(candidate) === 'ready') selected = candidate;\n"
        "  }\n"
        "  return selected;\n"
        "}\n"
        "let root = process.argv.slice(1).find((value) => value && !value.startsWith('${')) || '';\n"
        "for (const name of rootEnvNames) {\n  if (root) break;\n  root = process.env[name] || '';\n}\n"
        f"if (!root) finishWithDiagnostic({missing_root!r});\n"
        f"let runtime = path.join(root, {HOST_RUNTIME_OUTPUT_DIR!r}, {HOST_RUNTIME_EXECUTABLE!r});\n"
        "let runtimeStatus = runtimeState(runtime);\n"
        "if (runtimeStatus !== 'ready' && allowSiblingRuntime) {\n"
        "  const fallbackRuntime = siblingRuntime(root);\n"
        "  if (fallbackRuntime) {\n"
        "    runtime = fallbackRuntime;\n"
        "    runtimeStatus = 'ready';\n"
        "  }\n"
        "}\n"
        f"if (runtimeStatus === 'missing') finishWithDiagnostic({missing_file!r});\n"
        f"if (runtimeStatus === 'unreadable') finishWithDiagnostic({unreadable_file!r});\n"
        f"const pythonProbe = {PYTHON_VERSION_PROBE!r};\n"
        f"const collectArgs = [{', '.join(collect_args)}];\n"
        "const candidates = [\n"
        "  { command: 'python3', probeArgs: ['-c', pythonProbe], runPrefix: [] },\n"
        "  { command: 'python', probeArgs: ['-c', pythonProbe], runPrefix: [] },\n"
        "  { command: 'py', probeArgs: ['-3', '-c', pythonProbe], runPrefix: ['-3'] },\n"
        "];\n"
        "let sawUnsupportedPython = false;\n"
        "let sawBrokenPython = false;\n"
        "for (const candidate of candidates) {\n"
        "  const probe = spawnSync(candidate.command, candidate.probeArgs, { stdio: 'ignore' });\n"
        "  if (probe.error) {\n"
        "    if (probe.error.code !== 'ENOENT') sawBrokenPython = true;\n"
        "    continue;\n"
        "  }\n"
        "  if (probe.status !== 0) {\n"
        "    if (probe.status === 2) sawUnsupportedPython = true;\n"
        "    else sawBrokenPython = true;\n"
        "    continue;\n"
        "  }\n"
        f"{ensure_run_script}"
        "  const collectStdio = emitDiagnostics ? 'inherit' : ['inherit', 'ignore', 'ignore'];\n"
        "  const collect = spawnSync(candidate.command, [...candidate.runPrefix, ...collectArgs], { stdio: collectStdio, env: process.env });\n"
        "  if (!emitDiagnostics) process.exit(0);\n"
        "  if (collect.error) process.exit(1);\n"
        "  const collectStatus = collect.status === null ? 1 : collect.status;\n"
        "  if (collectStatus !== 0) process.exit(collectStatus);\n"
        f"{claude_desktop_collect_script}"
        "  process.exit(0);\n"
        "}\n"
        f"if (sawUnsupportedPython) finishWithDiagnostic({unsupported_python!r});\n"
        f"else if (sawBrokenPython) finishWithDiagnostic({broken_python!r});\n"
        f"else finishWithDiagnostic({missing_python!r});\n"
        "process.exit(0);\n"
    )


def _posix_host_runtime_hook_command(
    *,
    root_expr: str,
    host: Harness,
    lifecycle: str,
    run_ensure: bool,
    baseline: bool,
    quiet_failure: bool,
    allow_sibling_runtime: bool,
) -> str:
    collect_baseline_arg = " --baseline" if baseline else ""
    bundle_relative_paths = " ".join(shlex.quote(path) for path in HOST_RUNTIME_BUNDLE_RELATIVE_PATHS)
    runtime_state_function = (
        "runtime_state() { "
        "runtime_candidate=$1; runtime_bundle_dir=${runtime_candidate%/*}; "
        f"for relative_path in {bundle_relative_paths}; do "
        'required_path="$runtime_bundle_dir/$relative_path"; '
        'if [ ! -f "$required_path" ]; then return 1; fi; '
        "done; "
        f"for relative_path in {bundle_relative_paths}; do "
        'required_path="$runtime_bundle_dir/$relative_path"; '
        'if [ ! -r "$required_path" ]; then return 2; fi; '
        "done; "
        "return 0; "
        "}; "
    )
    missing_root_action = "exit 0"
    if not quiet_failure:
        missing_root_action = f"{_posix_emit_system_message(MISSING_RUNTIME_ROOT_MESSAGE)}; exit 0"
    if allow_sibling_runtime:
        runtime_check = (
            f'runtime="$root/{HOST_RUNTIME_OUTPUT_DIR}/{HOST_RUNTIME_EXECUTABLE}"; '
            'runtime_state "$runtime"; runtime_status=$?; '
            'if [ "$runtime_status" -ne 0 ]; then '
            "runtime=; root_parent=${root%/*}; "
            f'for candidate in "$root_parent"/*/{HOST_RUNTIME_OUTPUT_DIR}/{HOST_RUNTIME_EXECUTABLE}; do '
            'if runtime_state "$candidate"; then runtime="$candidate"; fi; '
            "done; "
            "fi; "
            'if [ -z "$runtime" ]; then exit 0; fi; '
        )
    else:
        runtime_check = (
            f'runtime="$root/{HOST_RUNTIME_OUTPUT_DIR}/{HOST_RUNTIME_EXECUTABLE}"; '
            'runtime_state "$runtime"; runtime_status=$?; '
            f'if [ "$runtime_status" -eq 1 ]; then {_posix_emit_system_message(MISSING_RUNTIME_FILE_MESSAGE)}; '
            "exit 0; fi; "
            f'if [ "$runtime_status" -ne 0 ]; then {_posix_emit_system_message(UNREADABLE_RUNTIME_FILE_MESSAGE)}; '
            "exit 0; fi; "
        )
    missing_python_action = "exit 0"
    if not quiet_failure:
        missing_python_action = (
            f'if [ "$unsupported_python" -eq 1 ]; then {_posix_emit_system_message(UNSUPPORTED_PYTHON_MESSAGE)}; '
            f'elif [ "$broken_python" -eq 1 ]; then {_posix_emit_system_message(BROKEN_PYTHON_MESSAGE)}; '
            f"else {_posix_emit_system_message(MISSING_PYTHON_MESSAGE)}; fi; "
            "exit 0"
        )
    ensure_command = ""
    if run_ensure:
        ensure_command = (
            f'if [ -n "$python_arg" ]; then "$python_cmd" "$python_arg" "$runtime" ensure --host {host}; '
            f'else "$python_cmd" "$runtime" ensure --host {host}; fi; '
            'status=$?; if [ "$status" -ne 0 ]; then exit "$status"; fi; '
        )
    if quiet_failure:
        collect_command = (
            f'if [ -n "$python_arg" ]; then "$python_cmd" "$python_arg" "$runtime" collect --host {host} --lifecycle {lifecycle}{collect_baseline_arg} --quiet >/dev/null 2>&1; '
            f'else "$python_cmd" "$runtime" collect --host {host} --lifecycle {lifecycle}{collect_baseline_arg} --quiet >/dev/null 2>&1; fi; '
            "exit 0"
        )
    else:
        collect_command = (
            f'if [ -n "$python_arg" ]; then exec "$python_cmd" "$python_arg" "$runtime" collect --host {host} --lifecycle {lifecycle}{collect_baseline_arg} --quiet; fi; '
            f'exec "$python_cmd" "$runtime" collect --host {host} --lifecycle {lifecycle}{collect_baseline_arg} --quiet'
        )
    script = (
        f"root={root_expr}; "
        f'if [ -z "$root" ]; then {missing_root_action}; fi; '
        f"{runtime_state_function}"
        f"{runtime_check}"
        f'probe="{PYTHON_VERSION_PROBE}"; '
        "python_cmd=; python_arg=; unsupported_python=0; broken_python=0; "
        "for candidate in python3 python; do "
        'if ! command -v "$candidate" >/dev/null 2>&1; then continue; fi; '
        '"$candidate" -c "$probe" >/dev/null 2>&1; status=$?; '
        'if [ "$status" -eq 0 ]; then python_cmd="$candidate"; break; fi; '
        'if [ "$status" -eq 2 ]; then unsupported_python=1; else broken_python=1; fi; '
        "done; "
        'if [ -z "$python_cmd" ] && command -v py >/dev/null 2>&1; then '
        'py -3 -c "$probe" >/dev/null 2>&1; status=$?; '
        'if [ "$status" -eq 0 ]; then python_cmd=py; python_arg=-3; '
        'elif [ "$status" -eq 2 ]; then unsupported_python=1; else broken_python=1; fi; '
        "fi; "
        'if [ -z "$python_cmd" ]; then '
        f"{missing_python_action}; "
        "fi; "
        f"{ensure_command}"
        f"{collect_command}"
    )
    return f"sh -c {shlex.quote(script)}"


def _posix_emit_system_message(message: str) -> str:
    return f'printf "%s\\n" "{_hook_json_system_message(message)}"'


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
