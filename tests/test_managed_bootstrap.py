from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, validate_json_value
from promptless_instruction_hub.managed_runtime import (
    HOST_RUNTIME_BUNDLE_RELATIVE_PATHS,
    HOST_RUNTIME_CLAUDE_SESSION_START_TIMEOUT_SECONDS,
    HOST_RUNTIME_STARTUP_PASS_TIMEOUT_SECONDS,
    MISSING_PYTHON_MESSAGE,
    MISSING_RUNTIME_FILE_MESSAGE,
    MISSING_RUNTIME_ROOT_MESSAGE,
    UNSUPPORTED_PYTHON_MESSAGE,
)

HOST_RUNTIME_BIN = "promptless-host-runtime"
HOST_RUNTIME_PACKAGE = "promptless_host_runtime"
HOST_STATE_REL_PATH = Path(".promptless/instruction-hub/host-enrollment-state.json")
LAST_STATUS_REL_PATH = Path(".promptless/instruction-hub/last-bootstrap-status.json")
DIAGNOSTIC_LOG_REL_PATH = Path(".promptless/instruction-hub/host-runtime-diagnostics.jsonl")
INTERNAL_WELCOME_SHOWN_AT_KEY = "internal_promptless_welcome_shown_at"
INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY = "internal_promptless_welcome_shown_at_by_version"
FIRST_SUCCESS_SHOWN_KEY = "first_enrollment_success_shown_at_by_target"
FIRST_SUCCESS_ACTIVE_FRAGMENT = "telemetry is now active for"
FIRST_SUCCESS_NO_RESTART_FRAGMENT = "No restart or plugin reload is needed."
BROWSER_ENROLLMENT_MESSAGE = (
    "Promptless Instruction Governance telemetry is starting browser-based enrollment. "
    "Approve the Promptless browser tab to continue."
)
BUNDLE_LOAD_ERROR = (
    "Promptless Instruction Hub could not load its managed runtime bundle. Reinstall the Promptless plugin.\n"
)


def _host_state_path(home: Path) -> Path:
    """Return the host-global enrollment state file shared by every plugin for one user/home."""
    return home / HOST_STATE_REL_PATH


def _last_status_path(home: Path) -> Path:
    """Return the last host-global bootstrap status file for debugging failed hook runs."""
    return home / LAST_STATUS_REL_PATH


def _diagnostic_log_path(home: Path) -> Path:
    """Return the redacted host-global hook diagnostic log."""
    return home / DIAGNOSTIC_LOG_REL_PATH


def _runtime_bundle_sha256(bin_root: Path) -> str:
    package_root = bin_root / HOST_RUNTIME_PACKAGE
    files = [bin_root / HOST_RUNTIME_BIN]
    files.extend(
        path
        for path in package_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(bin_root).parts and path.suffix != ".pyc"
    )

    digest = hashlib.sha256()
    for path in sorted(files, key=lambda candidate: candidate.relative_to(bin_root).as_posix()):
        relative_path = path.relative_to(bin_root).as_posix()
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _assert_no_promptless_directory(root: Path) -> None:
    assert list(root.rglob(".promptless")) == []


def _assert_hook_output(result: subprocess.CompletedProcess[str], expected: object) -> None:
    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == expected


def _assert_hook_system_message(result: subprocess.CompletedProcess[str], message: str) -> None:
    _assert_hook_output(result, {"systemMessage": message})


def _assert_hook_argv(result: subprocess.CompletedProcess[str], target: str) -> None:
    _assert_hook_output(result, {"argv": ["ensure", "--host", target]})


def test_build_injects_managed_bootstrap_runtime(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")

    build_hub(hub_root)

    for target in ("codex", "claude"):
        plugin_root = hub_root / "dist" / target / "core"
        bootstrap_path = plugin_root / "runtime" / HOST_RUNTIME_BIN
        runtime_package_path = plugin_root / "runtime" / HOST_RUNTIME_PACKAGE
        assert bootstrap_path.exists()
        assert os.access(bootstrap_path, os.X_OK)
        assert runtime_package_path.is_dir()
        hooks = json.loads((plugin_root / "hooks/hooks.json").read_text())
        hook_events = hooks["hooks"]
        expected_events = ("SessionStart", "Stop", "SessionEnd", "SubagentStop")
        assert set(hook_events) == set(expected_events)
        session_start_hook = hook_events["SessionStart"][0]["hooks"][0]
        callback_deadline_match = re.search(
            r"^ENROLLMENT_CALLBACK_DEADLINE_SECONDS = (?P<value>\d+)$",
            (runtime_package_path / "contracts.py").read_text(),
            re.MULTILINE,
        )
        assert callback_deadline_match is not None
        expected_startup_timeout = HOST_RUNTIME_STARTUP_PASS_TIMEOUT_SECONDS
        if target == "claude":
            expected_startup_timeout = HOST_RUNTIME_CLAUDE_SESSION_START_TIMEOUT_SECONDS
        assert session_start_hook["timeout"] == expected_startup_timeout
        assert session_start_hook["timeout"] > int(callback_deadline_match.group("value"))
        assert hook_events["SessionStart"][0]["matcher"] == "startup|resume"
        terminal_events = tuple(
            (event_name, lifecycle)
            for event_name, lifecycle in (
                ("Stop", "stop"),
                ("SessionEnd", "session_end"),
                ("SubagentStop", "subagent_stop"),
            )
            if event_name in hook_events
        )
        for event_name, _lifecycle in terminal_events:
            hook = hook_events[event_name][0]["hooks"][0]
            assert hook["timeout"] == HOST_RUNTIME_STARTUP_PASS_TIMEOUT_SECONDS
            assert hook["statusMessage"] == "Uploading Promptless traces"

        for event_name, lifecycle in terminal_events:
            hook = hook_events[event_name][0]["hooks"][0]
            if target == "claude":
                assert hook["command"] == "node"
                assert hook["args"][0] == "-e"
                assert len(hook["args"]) == 3
                hook_script = hook["args"][1]
                assert "const allowSiblingRuntime = true;" in hook_script
                assert "'ensure'" not in hook_script
                assert "'--baseline'" not in hook_script
                assert (
                    f"const collectArgs = [runtime, 'collect', '--host', 'claude', '--lifecycle', {lifecycle!r}, '--quiet'];"
                    in hook_script
                )
                assert "'claude-desktop'" not in hook_script
            else:
                hook_command = hook["command"]
                assert hook_command.startswith("sh -c '")
                assert "root=${PLUGIN_ROOT:-}" in hook_command
                assert "root_parent=${root%/*}" in hook_command
                assert f'"$runtime" collect --host codex --lifecycle {lifecycle} --quiet' in hook_command
                assert "ensure --host" not in hook_command
                assert "--baseline" not in hook_command

        stub_root = tmp_path / f"{target}-stub-plugin"
        stub_runtime = stub_root / "runtime" / HOST_RUNTIME_BIN
        stub_runtime_package = stub_root / "runtime" / HOST_RUNTIME_PACKAGE
        stub_call_log = tmp_path / f"{target}-stub-calls.jsonl"
        stub_stdin_log = tmp_path / f"{target}-stub-stdin.jsonl"
        stub_runtime.parent.mkdir(parents=True)
        assert f"{HOST_RUNTIME_PACKAGE}/contracts.py" in HOST_RUNTIME_BUNDLE_RELATIVE_PATHS
        for relative_path in HOST_RUNTIME_BUNDLE_RELATIVE_PATHS:
            if relative_path == HOST_RUNTIME_BIN:
                continue
            stub_path = stub_root / "runtime" / relative_path
            stub_path.parent.mkdir(parents=True, exist_ok=True)
            stub_path.write_text("")
        stub_runtime.write_text(
            "import json, os, sys\n"
            "with open(os.environ['PROMPTLESS_STUB_CALL_LOG'], 'a') as call_log:\n"
            "    call_log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:2] == ['ensure']:\n"
            "    print(json.dumps({'argv': sys.argv[1:]}))\n"
            "elif sys.argv[1:2] == ['collect'] and '--baseline' not in sys.argv:\n"
            "    with open(os.environ['PROMPTLESS_STUB_STDIN_LOG'], 'a') as stdin_log:\n"
            "        stdin_log.write(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}) + '\\n')\n"
        )
        stub_runtime.chmod(0o644)

        missing_runtime_root = tmp_path / f"{target}-missing-runtime-plugin"
        missing_runtime_root.mkdir()
        incomplete_runtime_root = tmp_path / f"{target}-incomplete-runtime-plugin"
        incomplete_runtime = incomplete_runtime_root / "runtime" / HOST_RUNTIME_BIN
        incomplete_runtime_package = incomplete_runtime_root / "runtime" / HOST_RUNTIME_PACKAGE
        incomplete_runtime_package.mkdir(parents=True)
        shutil.copy2(stub_runtime, incomplete_runtime)
        (incomplete_runtime_package / "__init__.py").write_text("")
        missing_import_runtime_root = tmp_path / f"{target}-missing-import-runtime-plugin"
        shutil.copytree(stub_root / "runtime", missing_import_runtime_root / "runtime")
        (missing_import_runtime_root / "runtime" / HOST_RUNTIME_PACKAGE / "contracts.py").unlink()

        def reset_stub_calls() -> None:
            stub_call_log.unlink(missing_ok=True)
            stub_stdin_log.unlink(missing_ok=True)

        def stub_calls() -> list[list[str]]:
            if not stub_call_log.exists():
                return []
            return [json.loads(line) for line in stub_call_log.read_text().splitlines()]

        def stub_stdin_entries() -> list[dict[str, JsonValue]]:
            if not stub_stdin_log.exists():
                return []
            return [json.loads(line) for line in stub_stdin_log.read_text().splitlines()]

        def assert_startup_calls() -> None:
            expected_calls = [
                ["ensure", "--host", target],
                ["collect", "--host", target, "--lifecycle", "session_start", "--baseline", "--quiet"],
            ]
            if target == "claude":
                expected_calls.extend(
                    [
                        [
                            "collect",
                            "--host",
                            "claude-desktop",
                            "--lifecycle",
                            "session_start",
                            "--baseline",
                            "--if-sources",
                            "--quiet",
                        ],
                    ]
                )
            assert stub_calls() == expected_calls

        def assert_terminal_calls(lifecycle: str) -> None:
            assert stub_calls() == [
                ["collect", "--host", target, "--lifecycle", lifecycle, "--quiet"],
            ]

        def assert_quiet_success(result: subprocess.CompletedProcess[str]) -> None:
            assert result.returncode == 0
            assert result.stdout == ""
            assert result.stderr == ""

        def terminal_hook_result(
            event_name: str,
            *,
            root: Path | None,
            home: Path,
            input_text: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            hook = hook_events[event_name][0]["hooks"][0]
            env_vars = {
                "HOME": str(home),
                "PROMPTLESS_STUB_CALL_LOG": str(stub_call_log),
                "PROMPTLESS_STUB_STDIN_LOG": str(stub_stdin_log),
            }
            if target == "claude":
                node_path = shutil.which("node")
                assert node_path is not None
                if root is not None:
                    env_vars["CLAUDE_PLUGIN_ROOT"] = str(root)
                return subprocess.run(
                    [node_path, *hook["args"]],
                    env=_clean_env(**env_vars),
                    text=True,
                    input=input_text,
                    capture_output=True,
                    check=False,
                )
            if root is not None:
                env_vars["PLUGIN_ROOT"] = str(root)
            return subprocess.run(
                hook["command"],
                shell=True,
                env=_clean_env(**env_vars),
                text=True,
                input=input_text,
                capture_output=True,
                check=False,
            )

        def make_stale_root_with_sibling_runtime(lifecycle: str) -> Path:
            plugin_id_root = tmp_path / f"{target}-{lifecycle}-managed-cache" / "promptless-instruction-hub-dev"
            stale_root = plugin_id_root / "0.3.1"
            stale_runtime = stale_root / "runtime" / HOST_RUNTIME_BIN
            sibling_bin = plugin_id_root / "0.3.2" / "runtime"
            sibling_runtime = sibling_bin / HOST_RUNTIME_BIN
            stale_runtime.parent.mkdir(parents=True)
            shutil.copy2(stub_runtime, stale_runtime)
            shutil.copytree(stub_runtime_package, stale_runtime.parent / HOST_RUNTIME_PACKAGE)
            (stale_runtime.parent / HOST_RUNTIME_PACKAGE / "contracts.py").unlink()
            stale_runtime.write_text("raise SystemExit(97)\n")
            sibling_runtime.parent.mkdir(parents=True)
            shutil.copy2(stub_runtime, sibling_runtime)
            shutil.copytree(stub_runtime_package, sibling_bin / HOST_RUNTIME_PACKAGE)
            sibling_runtime.chmod(0o644)
            return stale_root

        def make_stale_root_without_runtime(lifecycle: str) -> Path:
            plugin_id_root = tmp_path / f"{target}-{lifecycle}-missing-runtime-cache" / "promptless-instruction-hub-dev"
            stale_root = plugin_id_root / "0.3.1"
            incomplete_sibling_runtime = plugin_id_root / "0.3.2" / "runtime" / HOST_RUNTIME_BIN
            stale_root.mkdir(parents=True)
            incomplete_sibling_runtime.parent.mkdir(parents=True)
            shutil.copy2(stub_runtime, incomplete_sibling_runtime)
            return stale_root

        if target == "claude":
            hook_args = session_start_hook["args"]
            assert session_start_hook["command"] == "node"
            assert hook_args[0] == "-e"
            assert len(hook_args) == 3
            hook_script = hook_args[1]
            assert hook_args[2] == "${CLAUDE_PLUGIN_ROOT}"
            assert "CLAUDE_PLUGIN_ROOT" in hook_script
            assert "PLUGIN_ROOT" in hook_script
            assert f"path.join(root, 'runtime', {HOST_RUNTIME_BIN!r})" in hook_script
            assert "const bundleRelativePaths =" in hook_script
            assert f'"{HOST_RUNTIME_PACKAGE}/contracts.py"' in hook_script
            assert "relativePath.split('/')" in hook_script
            assert "spawnSync" in hook_script
            assert "sys.version_info >= (3, 9)" in hook_script
            assert MISSING_PYTHON_MESSAGE in hook_script
            assert UNSUPPORTED_PYTHON_MESSAGE in hook_script
            assert "'collect'" in hook_script
            assert "'--baseline'" in hook_script
            assert "'--quiet'" in hook_script
            assert "'claude-desktop'" in hook_script
            assert "'--if-sources'" in hook_script
            assert "desktopEnsure" not in hook_script
            assert "desktopCollectArgs" in hook_script
            assert "timeout: 5000" not in hook_script

            node_path = shutil.which("node")
            assert node_path is not None

            missing_root = subprocess.run(
                [session_start_hook["command"], *hook_args],
                env=_clean_env(HOME=str(tmp_path / f"{target}-home")),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_system_message(missing_root, MISSING_RUNTIME_ROOT_MESSAGE)

            missing_runtime = subprocess.run(
                [node_path, hook_args[0], hook_script, str(missing_runtime_root)],
                env=_clean_env(HOME=str(tmp_path / f"{target}-missing-runtime-home")),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_system_message(missing_runtime, MISSING_RUNTIME_FILE_MESSAGE)

            incomplete_runtime_result = subprocess.run(
                [node_path, hook_args[0], hook_script, str(incomplete_runtime_root)],
                env=_clean_env(HOME=str(tmp_path / f"{target}-incomplete-runtime-home")),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_system_message(incomplete_runtime_result, MISSING_RUNTIME_FILE_MESSAGE)

            missing_import_runtime_result = subprocess.run(
                [node_path, hook_args[0], hook_script, str(missing_import_runtime_root)],
                env=_clean_env(HOME=str(tmp_path / f"{target}-missing-import-runtime-home")),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_system_message(missing_import_runtime_result, MISSING_RUNTIME_FILE_MESSAGE)

            missing_python = subprocess.run(
                [node_path, hook_args[0], hook_script, str(stub_root)],
                env=_clean_env(HOME=str(tmp_path / f"{target}-missing-python-home"), PATH=""),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_system_message(missing_python, MISSING_PYTHON_MESSAGE)

            unsupported_python_bin = tmp_path / f"{target}-unsupported-python-bin"
            unsupported_python_bin.mkdir()
            _write_shell_script(unsupported_python_bin / "python3", "exit 2")
            _write_shell_script(unsupported_python_bin / "python", "exit 2")
            unsupported_python = subprocess.run(
                [node_path, hook_args[0], hook_script, str(stub_root)],
                env=_clean_env(
                    HOME=str(tmp_path / f"{target}-unsupported-python-home"),
                    PATH=str(unsupported_python_bin),
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_system_message(unsupported_python, UNSUPPORTED_PYTHON_MESSAGE)

            fallback_python_bin = tmp_path / f"{target}-fallback-python-bin"
            fallback_python_bin.mkdir()
            _write_shell_script(fallback_python_bin / "python3", "exit 2")
            _write_python_forwarder(fallback_python_bin / "python")
            reset_stub_calls()
            fallback_python = subprocess.run(
                [node_path, hook_args[0], hook_script, str(stub_root)],
                env=_clean_env(
                    HOME=str(tmp_path / f"{target}-fallback-python-home"),
                    PATH=str(fallback_python_bin),
                    PROMPTLESS_STUB_CALL_LOG=str(stub_call_log),
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_argv(fallback_python, target)
            assert_startup_calls()

            reset_stub_calls()
            env_rooted = subprocess.run(
                [node_path, *hook_args],
                env=_clean_env(
                    HOME=str(tmp_path / f"{target}-env-rooted-home"),
                    CLAUDE_PLUGIN_ROOT=str(stub_root),
                    PROMPTLESS_STUB_CALL_LOG=str(stub_call_log),
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_argv(env_rooted, target)
            assert_startup_calls()

            reset_stub_calls()
            rooted = subprocess.run(
                [session_start_hook["command"], hook_args[0], hook_script, str(stub_root)],
                env=_clean_env(
                    HOME=str(tmp_path / f"{target}-rooted-home"),
                    PROMPTLESS_STUB_CALL_LOG=str(stub_call_log),
                ),
                text=True,
                capture_output=True,
                check=False,
            )
        else:
            hook_command = session_start_hook["command"]
            assert "root=${PLUGIN_ROOT:-}" in hook_command
            assert '"$runtime" ensure --host codex' in hook_command
            assert '"$runtime" collect --host codex --lifecycle session_start --baseline --quiet' in hook_command
            assert hook_command.startswith("sh -c '")
            assert f'runtime="$root/runtime/{HOST_RUNTIME_BIN}"' in hook_command
            assert "runtime_state" in hook_command
            assert "runtime_bundle_dir=${runtime_candidate%/*}" in hook_command
            assert f"{HOST_RUNTIME_PACKAGE}/contracts.py" in hook_command
            assert 'required_path="$runtime_bundle_dir/$relative_path"' in hook_command

            missing_root = subprocess.run(
                hook_command,
                shell=True,
                env=_clean_env(HOME=str(tmp_path / f"{target}-home")),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_system_message(missing_root, MISSING_RUNTIME_ROOT_MESSAGE)

            missing_runtime = subprocess.run(
                hook_command,
                shell=True,
                env=_clean_env(
                    HOME=str(tmp_path / f"{target}-missing-runtime-home"),
                    PLUGIN_ROOT=str(missing_runtime_root),
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_system_message(missing_runtime, MISSING_RUNTIME_FILE_MESSAGE)

            incomplete_runtime_result = subprocess.run(
                hook_command,
                shell=True,
                env=_clean_env(
                    HOME=str(tmp_path / f"{target}-incomplete-runtime-home"),
                    PLUGIN_ROOT=str(incomplete_runtime_root),
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_system_message(incomplete_runtime_result, MISSING_RUNTIME_FILE_MESSAGE)

            missing_import_runtime_result = subprocess.run(
                hook_command,
                shell=True,
                env=_clean_env(
                    HOME=str(tmp_path / f"{target}-missing-import-runtime-home"),
                    PLUGIN_ROOT=str(missing_import_runtime_root),
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_system_message(missing_import_runtime_result, MISSING_RUNTIME_FILE_MESSAGE)

            fallback_python_bin = tmp_path / f"{target}-fallback-python-bin"
            fallback_python_bin.mkdir()
            sh_path = shutil.which("sh") or "/bin/sh"
            _write_shell_script(fallback_python_bin / "sh", f'exec {shlex.quote(sh_path)} "$@"')
            _write_shell_script(fallback_python_bin / "python3", "exit 2")
            _write_python_forwarder(fallback_python_bin / "python")
            reset_stub_calls()
            fallback_python = subprocess.run(
                shlex.split(hook_command),
                env=_clean_env(
                    HOME=str(tmp_path / f"{target}-fallback-python-home"),
                    PATH=str(fallback_python_bin),
                    PLUGIN_ROOT=str(stub_root),
                    PROMPTLESS_STUB_CALL_LOG=str(stub_call_log),
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            _assert_hook_argv(fallback_python, target)
            assert_startup_calls()

            reset_stub_calls()
            rooted = subprocess.run(
                hook_command,
                shell=True,
                env=_clean_env(
                    HOME=str(tmp_path / f"{target}-rooted-home"),
                    PLUGIN_ROOT=str(stub_root),
                    PROMPTLESS_STUB_CALL_LOG=str(stub_call_log),
                ),
                text=True,
                capture_output=True,
                check=False,
            )
        _assert_hook_argv(rooted, target)
        assert_startup_calls()

        for event_name, lifecycle in terminal_events:
            reset_stub_calls()
            primary_runtime = terminal_hook_result(
                event_name,
                root=stub_root,
                home=tmp_path / f"{target}-{lifecycle}-terminal-primary-home",
            )
            assert_quiet_success(primary_runtime)
            assert_terminal_calls(lifecycle)

            reset_stub_calls()
            stale_root = make_stale_root_with_sibling_runtime(lifecycle)
            sibling_runtime = terminal_hook_result(
                event_name,
                root=stale_root,
                home=tmp_path / f"{target}-{lifecycle}-terminal-sibling-home",
            )
            assert_quiet_success(sibling_runtime)
            assert_terminal_calls(lifecycle)

            reset_stub_calls()
            missing_root = terminal_hook_result(
                event_name,
                root=make_stale_root_without_runtime(lifecycle),
                home=tmp_path / f"{target}-{lifecycle}-terminal-missing-home",
            )
            assert_quiet_success(missing_root)
            assert stub_calls() == []

            reset_stub_calls()
            empty_root = terminal_hook_result(
                event_name,
                root=None,
                home=tmp_path / f"{target}-{lifecycle}-terminal-empty-root-home",
            )
            assert_quiet_success(empty_root)
            assert stub_calls() == []

        if target == "claude":
            reset_stub_calls()
            stdin_payload = json.dumps(
                {
                    "session_id": "claude_session_1",
                    "transcript_path": str(tmp_path / "claude-session.jsonl"),
                }
            )
            stop_result = terminal_hook_result(
                "Stop",
                root=stub_root,
                home=tmp_path / "claude-stop-stdin-home",
                input_text=stdin_payload,
            )
            assert_quiet_success(stop_result)
            assert_terminal_calls("stop")
            assert stub_stdin_entries() == [
                {
                    "argv": ["collect", "--host", "claude", "--lifecycle", "stop", "--quiet"],
                    "stdin": stdin_payload,
                }
            ]

        metadata = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())
        assert not (plugin_root / ".promptless").exists()
        runtime = metadata["managed_runtimes"][0]
        assert runtime["id"] == "host-runtime"
        assert runtime["status"] == "included"
        assert runtime["target"] == target
        assert runtime["version"] == "0.2.5"
        assert runtime["channel"] == "stable"
        assert runtime["path"] == f"runtime/{HOST_RUNTIME_BIN}"
        assert runtime["sha256"] == _runtime_bundle_sha256(plugin_root / "runtime")
        assert list(runtime_package_path.rglob("__pycache__")) == []
        assert list(runtime_package_path.rglob("*.pyc")) == []

    codex_manifest = json.loads((hub_root / "dist/codex/core/.codex-plugin/plugin.json").read_text())
    assert codex_manifest["hooks"] == "./hooks/hooks.json"

    for target in ("cursor", "gemini"):
        plugin_root = hub_root / "dist" / target / "core"
        assert not (plugin_root / "runtime" / HOST_RUNTIME_BIN).exists()
        assert not (plugin_root / "runtime" / HOST_RUNTIME_PACKAGE).exists()
        assert not (plugin_root / "hub.managed-runtimes.json").exists()

    release_manifest = json.loads((hub_root / "hub.release.json").read_text())
    assert {runtime["target"] for runtime in release_manifest["managed_runtimes"]} == {"codex", "claude"}
    _assert_no_promptless_directory(hub_root)


def test_host_runtime_requires_subcommand_and_reports_version(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    runtime_path = plugin_root / "runtime" / HOST_RUNTIME_BIN
    home = tmp_path / "home"

    missing_command = subprocess.run(
        [str(runtime_path)],
        env=_clean_env(HOME=str(home), PLUGIN_ROOT=str(plugin_root)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing_command.returncode == 2
    assert "usage:" in missing_command.stderr

    payload, _ = _run_runtime_json(
        plugin_root,
        ["version", "--json"],
        {"HOME": str(home), "PLUGIN_ROOT": str(plugin_root)},
    )
    assert payload["id"] == "host-runtime"
    assert payload["name"] == HOST_RUNTIME_BIN
    assert payload["version"] == "0.2.5"
    assert payload["channel"] == "stable"
    manifest = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())
    bundle_sha256 = _runtime_bundle_sha256(plugin_root / "runtime")
    assert payload["sha256"] == manifest["managed_runtimes"][0]["sha256"] == bundle_sha256

    isolated = subprocess.run(
        [sys.executable, "-S", str(runtime_path), "version", "--json"],
        cwd=tmp_path,
        env=_clean_env(
            HOME=str(home),
            PLUGIN_ROOT=str(plugin_root),
            PYTHONPATH="",
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert isolated.returncode == 0
    assert json.loads(isolated.stdout)["sha256"] == bundle_sha256
    assert isolated.stderr == ""
    assert list((plugin_root / "runtime" / HOST_RUNTIME_PACKAGE).rglob("__pycache__")) == []
    assert list((plugin_root / "runtime" / HOST_RUNTIME_PACKAGE).rglob("*.pyc")) == []

    text_version = subprocess.run(
        [str(runtime_path), "version"],
        env=_clean_env(HOME=str(home), PLUGIN_ROOT=str(plugin_root)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert text_version.returncode == 0
    assert text_version.stdout == f"{HOST_RUNTIME_BIN} 0.2.5\n"
    assert text_version.stderr == ""

    poison_root = tmp_path / "poison-pythonpath"
    poison_package = poison_root / HOST_RUNTIME_PACKAGE
    poison_marker = tmp_path / "poison-loaded"
    poison_package.mkdir(parents=True)
    (poison_package / "__init__.py").write_text("")
    (poison_package / "cli.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['PROMPTLESS_POISON_MARKER']).write_text('loaded')\n"
        "def main():\n"
        "    return 99\n"
    )
    poisoned_pythonpath = subprocess.run(
        [str(runtime_path), "version", "--json"],
        env=_clean_env(
            HOME=str(home),
            PLUGIN_ROOT=str(plugin_root),
            PYTHONPATH=str(poison_root),
            PROMPTLESS_POISON_MARKER=str(poison_marker),
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert poisoned_pythonpath.returncode == 0
    assert json.loads(poisoned_pythonpath.stdout)["version"] == "0.2.5"
    assert poisoned_pythonpath.stderr == ""
    assert not poison_marker.exists()

    incomplete_bin = tmp_path / "incomplete-bin"
    incomplete_bin.mkdir()
    incomplete_runtime = incomplete_bin / HOST_RUNTIME_BIN
    shutil.copy2(runtime_path, incomplete_runtime)
    incomplete_bundle = subprocess.run(
        [str(incomplete_runtime), "version"],
        env=_clean_env(
            HOME=str(home),
            PLUGIN_ROOT=str(plugin_root),
            PYTHONPATH=str(poison_root),
            PROMPTLESS_POISON_MARKER=str(poison_marker),
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert incomplete_bundle.returncode == 1
    assert incomplete_bundle.stdout == ""
    assert incomplete_bundle.stderr == BUNDLE_LOAD_ERROR
    assert not poison_marker.exists()

    missing_module_bin = tmp_path / "missing-module-bin"
    shutil.copytree(plugin_root / "runtime", missing_module_bin)
    (missing_module_bin / HOST_RUNTIME_PACKAGE / "contracts.py").unlink()
    missing_module = subprocess.run(
        [str(missing_module_bin / HOST_RUNTIME_BIN), "version"],
        env=_clean_env(
            HOME=str(home),
            PLUGIN_ROOT=str(plugin_root),
            PYTHONPATH=str(poison_root),
            PROMPTLESS_POISON_MARKER=str(poison_marker),
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing_module.returncode == 1
    assert missing_module.stdout == ""
    assert missing_module.stderr == BUNDLE_LOAD_ERROR
    assert not poison_marker.exists()

    internal_import_bin = tmp_path / "internal-import-bin"
    shutil.copytree(plugin_root / "runtime", internal_import_bin)
    internal_cli = internal_import_bin / HOST_RUNTIME_PACKAGE / "cli.py"
    internal_cli.write_text(
        internal_cli.read_text().replace(
            "from __future__ import annotations\n",
            "from __future__ import annotations\n\nraise ImportError('sentinel internal import')\n",
            1,
        )
    )
    internal_import = subprocess.run(
        [str(internal_import_bin / HOST_RUNTIME_BIN), "version"],
        env=_clean_env(HOME=str(home), PLUGIN_ROOT=str(plugin_root)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert internal_import.returncode == 1
    assert internal_import.stdout == ""
    assert "ImportError: sentinel internal import" in internal_import.stderr
    assert BUNDLE_LOAD_ERROR.strip() not in internal_import.stderr


def test_host_runtime_bundle_digest_tracks_runtime_files_only(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    bin_root = plugin_root / "runtime"
    runtime_path = bin_root / HOST_RUNTIME_BIN
    manifest = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())
    manifest_sha256 = manifest["managed_runtimes"][0]["sha256"]
    env = {"HOME": str(tmp_path / "home"), "PLUGIN_ROOT": str(plugin_root)}

    original_launcher = runtime_path.read_bytes()
    runtime_path.write_bytes(original_launcher + b"\n# digest mutation\n")
    launcher_payload, _ = _run_runtime_json(plugin_root, ["version", "--json"], env)
    assert launcher_payload["sha256"] != manifest_sha256
    runtime_path.write_bytes(original_launcher)

    module_path = bin_root / HOST_RUNTIME_PACKAGE / "contracts.py"
    original_module = module_path.read_bytes()
    module_path.write_bytes(original_module + b"\n# digest mutation\n")
    module_payload, _ = _run_runtime_json(plugin_root, ["version", "--json"], env)
    assert module_payload["sha256"] != manifest_sha256
    module_path.write_bytes(original_module)

    cache_root = bin_root / HOST_RUNTIME_PACKAGE / "__pycache__"
    cache_root.mkdir()
    (cache_root / "contracts.cpython-311.pyc").write_bytes(b"ignored bytecode")
    cache_payload, _ = _run_runtime_json(plugin_root, ["version", "--json"], env)
    assert cache_payload["sha256"] == manifest_sha256


def test_host_runtime_enroll_status_and_reset_commands(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        enroll_payload, _ = _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        assert enroll_payload["status"] == "enrolled"
        assert enroll_payload["host"] == "codex"
        assert enroll_payload["credential_id"] == "22222222-2222-4222-8222-222222222222"
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert len(server.poll_requests) == 1
        assert server.policy_requests == []
        assert server.check_ins == []

        status_payload, _ = _run_runtime_json(
            plugin_root,
            ["status", "--host", "codex"],
            env,
        )
        assert status_payload["status"] == "ok"
        status_state = _json_mapping(status_payload["state"], "status.state")
        status_config = _json_mapping(status_payload["config"], "status.config")
        assert status_state["credential_count"] == 1
        assert status_state["pending_enrollment_count"] == 0
        assert status_config["managed_config_detected"] is False
        assert len(server.session_requests) == 1
        assert len(server.poll_requests) == 1
        assert server.policy_requests == []
        assert server.check_ins == []

        _run_bootstrap(plugin_root, "codex", env)
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert server.policy_requests == ["/v0/host-enrollment/policy?target=codex"]
        assert len(server.check_ins) == 1

        configured_status, _ = _run_runtime_json(
            plugin_root,
            ["status", "--host", "codex"],
            env,
        )
        configured_state = _json_mapping(configured_status["state"], "configured.state")
        configured_config = _json_mapping(configured_status["config"], "configured.config")
        host_instance_id = _json_string(configured_state["host_instance_id"], "host_instance_id")
        assert configured_state["credential_count"] == 1
        assert configured_state["last_seen_plugin_version"] == "0.1.0"
        assert configured_config["managed_config_detected"] is False
        assert len(server.session_requests) == 1
        assert len(server.check_ins) == 1

        reset_payload, _ = _run_runtime_json(
            plugin_root,
            ["reset", "--host", "codex", "--yes"],
            env,
        )
        assert reset_payload == {
            "credentials_removed": 1,
            "host": "codex",
            "pending_enrollments_removed": 0,
            "status": "reset",
        }
        state_after_reset = json.loads(_host_state_path(home).read_text())
        assert state_after_reset["host_instance_id"] == host_instance_id
        assert state_after_reset["last_seen_plugin_versions"] == {"codex": "0.1.0"}
        assert state_after_reset["credentials"] == {}
        assert state_after_reset["pending_enrollments"] == {}
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert len(server.check_ins) == 1

        reset_status, _ = _run_runtime_json(
            plugin_root,
            ["status", "--host", "codex"],
            env,
        )
        reset_state = _json_mapping(reset_status["state"], "reset.state")
        reset_config = _json_mapping(reset_status["config"], "reset.config")
        assert reset_state["credential_count"] == 0
        assert reset_state["last_seen_plugin_version"] == "0.1.0"
        assert reset_config["managed_config_detected"] is False
    finally:
        server.stop()


def test_bootstrap_unreachable_worker_exits_zero_without_config_write(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"

    result = subprocess.run(
        [str(hub_root / "dist/codex/core/runtime" / HOST_RUNTIME_BIN), "ensure", "--host", "codex"],
        env=_clean_env(
            HOME=str(home),
            CODEX_HOME=str(home / ".codex"),
            PLUGIN_ROOT=str(hub_root / "dist/codex/core"),
            PROMPTLESS_WORKER_BASE_URL="http://127.0.0.1:9",
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = _assert_session_start_streams(result.stdout, result.stderr, "error")
    message = _json_string(payload["systemMessage"], "systemMessage")
    assert "Promptless host enrollment failed for Codex" in message
    last_status = _json_mapping(
        validate_json_value(json.loads(_last_status_path(home).read_text()), "last bootstrap status"),
        "last bootstrap status",
    )
    assert last_status["status"] == "error"
    assert last_status["host"] == "codex"
    assert "emitted_at" in last_status
    assert not (home / ".codex/config.toml").exists()

    quiet_result = subprocess.run(
        [str(hub_root / "dist/codex/core/runtime" / HOST_RUNTIME_BIN), "ensure", "--host", "codex", "--quiet"],
        env=_clean_env(
            HOME=str(home),
            CODEX_HOME=str(home / ".codex"),
            PLUGIN_ROOT=str(hub_root / "dist/codex/core"),
            PROMPTLESS_WORKER_BASE_URL="http://127.0.0.1:9",
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert quiet_result.returncode == 0
    assert quiet_result.stdout == ""
    assert quiet_result.stderr == ""


def test_bootstrap_runs_without_local_dogfood_gate(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        payload, result = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )

        assert any(
            diagnostic.get("status") == "browser_enrollment_starting"
            and diagnostic.get("systemMessage") == BROWSER_ENROLLMENT_MESSAGE
            for diagnostic in _bootstrap_diagnostics(result.stderr)
        )
        assert payload["status"] == "configured"
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert server.policy_requests == ["/v0/host-enrollment/policy?target=codex"]
        assert len(server.check_ins) == 1
    finally:
        server.stop()


@pytest.mark.parametrize(
    "identity_location",
    ["envelope", "policy"],
    ids=["identity-envelope", "identity-policy"],
)
def test_bootstrap_welcomes_is_internal_promptless_user_once_per_plugin_version(
    tmp_path: Path,
    identity_location: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root, plugin_version="0.1.0")
    internal_policy = _policy_with()
    if identity_location == "envelope":
        internal_policy["user_email"] = "Adit@GoPromptless.AI"
    else:
        policy_body = _json_mapping(internal_policy["policy"], "policy")
        policy_body["user_email"] = "Adit@GoPromptless.AI"
    server = _FakeWorkerServer(policy=internal_policy)
    server.start()
    try:
        home = tmp_path / "home"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        first_payload, first_result = _run_bootstrap(hub_root / "dist/codex/core", "codex", env)
        first_message = _json_string(first_payload["systemMessage"], "systemMessage")
        assert "welcome promptless pigfooder." in first_message
        assert "version: v0.1.0" in first_message
        assert "Promptless marketplace version installed" not in first_message
        assert ",-,------," in first_message
        first_stdout = _json_mapping(
            validate_json_value(json.loads(first_result.stdout), "bootstrap stdout"),
            "bootstrap stdout",
        )
        assert first_stdout == {"systemMessage": first_message}

        state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "host state"),
            "host state",
        )
        shown_at = _json_string(state[INTERNAL_WELCOME_SHOWN_AT_KEY], "welcome shown at")
        assert shown_at != ""
        shown_by_version = _json_mapping(
            state[INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY],
            "welcome shown by version",
        )
        assert shown_by_version == {"0.1.0": shown_at}
        credentials = _json_mapping(state["credentials"], "credentials")
        assert len(credentials) == 1
        credential = _json_mapping(next(iter(credentials.values())), "credential")
        assert credential["is_internal_promptless_user"] is True
        assert "user_email" not in credential
        assert "email" not in credential

        second_payload, second_result = _run_bootstrap(
            hub_root / "dist/codex/core", "codex", env, expected_status="configured"
        )
        assert "systemMessage" not in second_payload
        assert second_result.stdout == ""
        second_state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "host state"),
            "host state",
        )
        assert second_state[INTERNAL_WELCOME_SHOWN_AT_KEY] == shown_at
        assert second_state[INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY] == {"0.1.0": shown_at}

        build_hub(hub_root, plugin_version="0.2.0")
        upgraded_payload, upgraded_result = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            env,
            expected_status="configured",
        )
        upgraded_message = _json_string(upgraded_payload["systemMessage"], "systemMessage")
        assert "Promptless Instruction Hub updated to v0.2.0 (was v0.1.0)." in upgraded_message
        assert "welcome promptless pigfooder." in upgraded_message
        assert "version updated: v0.2.0" in upgraded_message
        assert "Promptless marketplace version installed" not in upgraded_message
        upgraded_stdout = _json_mapping(
            validate_json_value(json.loads(upgraded_result.stdout), "upgraded stdout"),
            "upgraded stdout",
        )
        assert upgraded_stdout == {"systemMessage": upgraded_message}
        upgraded_state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "upgraded host state"),
            "upgraded host state",
        )
        upgraded_shown_at = _json_string(upgraded_state[INTERNAL_WELCOME_SHOWN_AT_KEY], "upgraded welcome shown at")
        upgraded_shown_by_version = _json_mapping(
            upgraded_state[INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY],
            "upgraded welcome shown by version",
        )
        assert upgraded_shown_by_version == {"0.1.0": shown_at, "0.2.0": upgraded_shown_at}

        steady_payload, steady_result = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            env,
            expected_status="configured",
        )
        assert "systemMessage" not in steady_payload
        assert steady_result.stdout == ""
    finally:
        server.stop()


@pytest.mark.parametrize(
    ("identity_location", "email"),
    [
        ("envelope", "customer@example.com"),
        ("policy", "customer@example.com"),
        ("envelope", "adit @gopromptless.ai"),
    ],
    ids=["external-envelope", "external-policy", "malformed-envelope"],
)
def test_bootstrap_ignores_non_internal_worker_identity(
    tmp_path: Path,
    identity_location: str,
    email: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    policy = _policy_with()
    if identity_location == "envelope":
        policy["user_email"] = email
    else:
        policy_body = _json_mapping(policy["policy"], "policy")
        policy_body["user_email"] = email
    server = _FakeWorkerServer(policy=policy)
    server.start()
    try:
        home = tmp_path / "home"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        payload, _ = _run_bootstrap(hub_root / "dist/codex/core", "codex", env)
        # A non-internal identity gets the generic first-success confirmation, never the internal welcome.
        message = _json_string(payload["systemMessage"], "systemMessage")
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT in message
        assert "welcome promptless pigfooder." not in message

        state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "host state"),
            "host state",
        )
        assert INTERNAL_WELCOME_SHOWN_AT_KEY not in state
        assert INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY not in state
        assert list(_json_mapping(state[FIRST_SUCCESS_SHOWN_KEY], "first-success shown")) == ["codex"]
        credentials = _json_mapping(state["credentials"], "credentials")
        credential = _json_mapping(next(iter(credentials.values())), "credential")
        assert "is_internal_promptless_user" not in credential
    finally:
        server.stop()


def test_cached_credential_trusts_only_persisted_internal_flag(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        plugin_root = hub_root / "dist/codex/core"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        state_path = _host_state_path(home)
        state = _json_mapping(validate_json_value(json.loads(state_path.read_text()), "host state"), "host state")
        credentials = _json_mapping(state["credentials"], "credentials")
        credential_key = _credential_cache_key(worker_base_url=server.base_url, target="codex")
        credential = _json_mapping(credentials[credential_key], "credential")
        credential["user_email"] = "adit@gopromptless.ai"
        credential.pop("is_internal_promptless_user", None)
        state_path.write_text(json.dumps(state))

        payload, _ = _run_bootstrap(plugin_root, "codex", env)
        # A cached credential lacking the persisted internal flag still gets the generic
        # first-success confirmation, but never the internal welcome.
        message = _json_string(payload["systemMessage"], "systemMessage")
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT in message
        assert "welcome promptless pigfooder." not in message

        updated_state = _json_mapping(
            validate_json_value(json.loads(state_path.read_text()), "updated host state"),
            "updated host state",
        )
        assert INTERNAL_WELCOME_SHOWN_AT_KEY not in updated_state
        assert INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY not in updated_state
        updated_credentials = _json_mapping(updated_state["credentials"], "updated credentials")
        updated_credential = _json_mapping(updated_credentials[credential_key], "updated credential")
        assert "is_internal_promptless_user" not in updated_credential
    finally:
        server.stop()


def test_bootstrap_welcomes_is_internal_promptless_user_from_poll_response(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(poll_response=_approved_poll_response(user_email="Adit@GoPromptless.AI"))
    server.start()
    try:
        home = tmp_path / "home"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        payload, _ = _run_bootstrap(hub_root / "dist/codex/core", "codex", env)
        message = _json_string(payload["systemMessage"], "systemMessage")
        assert "welcome promptless pigfooder." in message
        assert "version: v0.1.0" in message
        assert "Promptless marketplace version installed" not in message

        state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "host state"),
            "host state",
        )
        shown_at = _json_string(state[INTERNAL_WELCOME_SHOWN_AT_KEY], "welcome shown at")
        assert state[INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY] == {"0.1.0": shown_at}
        credentials = _json_mapping(state["credentials"], "credentials")
        credential = _json_mapping(next(iter(credentials.values())), "credential")
        assert credential["is_internal_promptless_user"] is True
    finally:
        server.stop()


def test_bootstrap_confirms_first_successful_enrollment_once_per_host(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        codex_env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        # The first healthy enrollment confirms once and reassures the user no restart is needed.
        first_payload, first_result = _run_bootstrap(hub_root / "dist/codex/core", "codex", codex_env)
        first_message = _json_string(first_payload["systemMessage"], "systemMessage")
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT in first_message
        assert FIRST_SUCCESS_NO_RESTART_FRAGMENT in first_message
        assert "welcome promptless pigfooder." not in first_message
        first_stdout = _json_mapping(
            validate_json_value(json.loads(first_result.stdout), "bootstrap stdout"),
            "bootstrap stdout",
        )
        assert first_stdout == {"systemMessage": first_message}

        state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "host state"),
            "host state",
        )
        shown_targets = _json_mapping(state[FIRST_SUCCESS_SHOWN_KEY], "first-success shown")
        codex_shown_at = _json_string(shown_targets["codex"], "codex shown at")
        assert codex_shown_at != ""
        assert "claude" not in shown_targets

        # A later session for the same host has nothing to say.
        second_payload, second_result = _run_bootstrap(
            hub_root / "dist/codex/core", "codex", codex_env, expected_status="configured"
        )
        assert "systemMessage" not in second_payload
        assert second_result.stdout == ""
        second_state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "host state"),
            "host state",
        )
        assert second_state[FIRST_SUCCESS_SHOWN_KEY] == {"codex": codex_shown_at}

        # A different host sharing the same home is confirmed independently (the latch is per target).
        claude_env = {
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
            "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        claude_payload, _ = _run_bootstrap(hub_root / "dist/claude/core", "claude", claude_env)
        claude_message = _json_string(claude_payload["systemMessage"], "systemMessage")
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT in claude_message
        assert "Claude Code" in claude_message
        third_state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "host state"),
            "host state",
        )
        third_shown = _json_mapping(third_state[FIRST_SUCCESS_SHOWN_KEY], "first-success shown")
        assert set(third_shown) == {"codex", "claude"}
        assert third_shown["codex"] == codex_shown_at
    finally:
        server.stop()


def test_reset_clears_first_successful_enrollment_latch(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        plugin_root = hub_root / "dist/codex/core"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        first_payload, _ = _run_bootstrap(plugin_root, "codex", env)
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT in _json_string(first_payload["systemMessage"], "systemMessage")

        # Steady state is silent; the latch keeps the confirmation from repeating every session.
        steady_payload, _ = _run_bootstrap(plugin_root, "codex", env, expected_status="configured")
        assert "systemMessage" not in steady_payload

        # A reset re-arms the confirmation by dropping the host from the latch.
        _run_runtime_json(plugin_root, ["reset", "--host", "codex", "--yes"], env)
        reset_state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "host state"),
            "host state",
        )
        assert "codex" not in _json_mapping(reset_state[FIRST_SUCCESS_SHOWN_KEY], "first-success shown")
    finally:
        server.stop()


def test_bootstrap_surfaces_browser_open_failure(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        payload, _ = _run_bootstrap(
            hub_root / "dist/claude/core",
            "claude",
            {
                "HOME": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
                "PROMPTLESS_DASHBOARD_BASE_URL": "https://app.gopromptless.ai",
            },
            expected_status="setup_pending",
        )

        assert payload["reason"] == "browser_launch_failed"
        message = _json_string(payload["systemMessage"], "systemMessage")
        assert "Promptless host enrollment could not open a browser for Claude Code" in message
        state = json.loads(_host_state_path(home).read_text())
        assert _json_string(state["host_instance_id"], "host_instance_id").startswith("host-")
        assert "credentials" not in state
        assert "pending_enrollments" not in state
        seen_versions = _json_mapping(
            validate_json_value(state["last_seen_plugin_versions"], "last seen plugin versions"),
            "last seen plugin versions",
        )
        assert seen_versions["claude"] == "0.1.0"
        assert server.session_requests == []
        assert server.policy_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


def test_bootstrap_persists_host_global_state_file(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        # A per-plugin data dir must NOT relocate the state: host enrollment is host-global so the
        # credential lands at the shared ~/.promptless path regardless of CLAUDE_PLUGIN_DATA.
        _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(tmp_path / "plugin-data"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )

        assert len(server.check_ins) == 1
        assert server.check_ins[0]["host"] == "codex"
        assert len(server.session_requests) == 1
        assert not (tmp_path / "plugin-data/host-enrollment-state.json").exists()
        state = json.loads(_host_state_path(home).read_text())
        credentials = _json_mapping(validate_json_value(state["credentials"], "credentials"), "credentials")
        stored_credential = _json_mapping(next(iter(credentials.values())), "stored credential")
        assert stored_credential["deployment_instance_id"] == "worker-local-1"
    finally:
        server.stop()


def test_bootstrap_concurrent_hosts_preserve_shared_state_file(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer(session_barrier_count=2)
    server.start()
    codex_process: subprocess.Popen[str] | None = None
    claude_process: subprocess.Popen[str] | None = None
    try:
        # codex and claude are distinct agent hosts (distinct credential cache keys), so they
        # enroll in parallel even while writing to the one shared host-global state file.
        home = tmp_path / "home"
        codex_process = _start_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )
        claude_process = _start_bootstrap(
            hub_root / "dist/claude/core",
            "claude",
            {
                "HOME": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )

        _read_bootstrap_process(codex_process)
        _read_bootstrap_process(claude_process)

        state = json.loads(_host_state_path(home).read_text())
        credentials = _json_mapping(validate_json_value(state["credentials"], "credentials"), "credentials")
        stored_credentials = [_json_mapping(value, "stored credential") for value in credentials.values()]
        assert {
            _json_string(credential["target"], "stored credential target") for credential in stored_credentials
        } == {
            "codex",
            "claude",
        }
        assert {
            _json_string(credential["deployment_instance_id"], "stored credential deployment_instance_id")
            for credential in stored_credentials
        } == {"worker-local-1"}
        assert _json_mapping(validate_json_value(state["pending_enrollments"], "pending_enrollments"), "pending") == {}
        assert len(server.session_requests) == 2
        assert len(server.check_ins) == 2
    finally:
        for process in (codex_process, claude_process):
            if process is not None and process.poll() is None:
                process.kill()
        server.stop()


def test_bootstrap_concurrent_same_host_plugins_enroll_once(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    dev_process: subprocess.Popen[str] | None = None
    ops_process: subprocess.Popen[str] | None = None
    try:
        # Two claude plugins from the same hub (distinct plugin/package ids) share one host
        # credential. Starting both at once must open exactly one browser approval, not one per
        # plugin -- the regression that previously surfaced two browser windows on session start.
        home = tmp_path / "home"
        dev_plugin = _clone_plugin_with_identity(
            hub_root / "dist/claude/core", tmp_path / "plugin-dev", plugin_id="hub-dev", package_id="dev"
        )
        ops_plugin = _clone_plugin_with_identity(
            hub_root / "dist/claude/core", tmp_path / "plugin-ops", plugin_id="hub-ops", package_id="ops"
        )

        def claude_plugin_env(plugin_root: Path) -> dict[str, str]:
            return {
                "HOME": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "PLUGIN_ROOT": str(plugin_root),
                "CLAUDE_PLUGIN_ROOT": str(plugin_root),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            }

        dev_process = _start_bootstrap(dev_plugin, "claude", claude_plugin_env(dev_plugin))
        ops_process = _start_bootstrap(ops_plugin, "claude", claude_plugin_env(ops_plugin))

        dev_payload = _read_any_bootstrap_status(dev_process)
        ops_payload = _read_any_bootstrap_status(ops_process)

        # Exactly one browser approval (one /start) and one shared host credential, no matter
        # which plugin won the enrollment-leader lock.
        assert len(server.session_requests) == 1
        state = json.loads(_host_state_path(home).read_text())
        credentials = _json_mapping(validate_json_value(state["credentials"], "credentials"), "credentials")
        assert len(credentials) == 1
        stored_credential = _json_mapping(next(iter(credentials.values())), "stored credential")
        assert stored_credential["target"] == "claude"
        assert _json_mapping(validate_json_value(state["pending_enrollments"], "pending_enrollments"), "pending") == {}
        # The leader configured the shared host telemetry once; the follower never opened a
        # browser (it either reused the credential or deferred to a later session).
        leader_statuses = {"needs_restart", "configured"}
        statuses = {_json_string(dev_payload["status"], "status"), _json_string(ops_payload["status"], "status")}
        assert statuses & leader_statuses
        assert statuses <= leader_statuses | {"setup_pending"}
    finally:
        for process in (dev_process, ops_process):
            if process is not None and process.poll() is None:
                process.kill()
        server.stop()


def test_bootstrap_rejects_plaintext_non_loopback_worker_base_url(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"

    payload, result = _run_bootstrap(
        hub_root / "dist/codex/core",
        "codex",
        {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": "http://example.com",
            "PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES": "0",
        },
        expected_status="error",
    )

    assert "worker base URL must use HTTPS unless" in str(payload["message"])
    message = _json_string(payload["systemMessage"], "systemMessage")
    assert "Promptless host enrollment failed for Codex" in message
    assert result.stdout != ""


def test_bootstrap_reports_browser_launch_failure_without_claiming_browser_opened(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        payload, result = _run_bootstrap(
            hub_root / "dist/claude/core",
            "claude",
            {
                "HOME": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
                "PROMPTLESS_DASHBOARD_BASE_URL": "https://app.gopromptless.ai",
            },
            expected_status="setup_pending",
        )

        assert payload["reason"] == "browser_launch_failed"
        message = _json_string(payload["systemMessage"], "systemMessage")
        assert "could not open a browser" in message
        assert "browser tab that opened" not in message
        assert _json_string(payload["terminalSequence"], "terminalSequence").startswith("\x1b]777;notify;Promptless;")
        stdout_payload = _json_mapping(validate_json_value(json.loads(result.stdout), "bootstrap stdout"), "stdout")
        assert set(stdout_payload) == {"systemMessage", "terminalSequence"}
        assert stdout_payload["systemMessage"] == message
        assert stdout_payload["terminalSequence"] == payload["terminalSequence"]
        last_status = _json_mapping(
            validate_json_value(json.loads(_last_status_path(home).read_text()), "last bootstrap status"),
            "last bootstrap status",
        )
        assert last_status["status"] == "setup_pending"
        assert last_status["reason"] == "browser_launch_failed"
        assert last_status["systemMessage"] == payload["systemMessage"]
        assert last_status["terminalSequence"] == payload["terminalSequence"]
        assert "emitted_at" in last_status
        assert not (home / ".claude/settings.json").exists()
        assert server.session_requests == []
        assert server.policy_requests == []
        assert server.poll_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


def test_bootstrap_configures_codex_and_claude_and_reports_metadata(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        codex_home = tmp_path / "codex-home"
        _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(codex_home),
                "CODEX_HOME": str(codex_home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )
        assert not (codex_home / ".codex/config.toml").exists()

        claude_home = tmp_path / "claude-home"
        _run_bootstrap(
            hub_root / "dist/claude/core",
            "claude",
            {
                "HOME": str(claude_home),
                "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )
        assert not (claude_home / ".claude/settings.json").exists()

        assert len(server.session_requests) == 2
        codex_callback_state = _callback_state(server.session_requests[0]["callback_url"], "codex callback_url")
        claude_callback_state = _callback_state(server.session_requests[1]["callback_url"], "claude callback_url")
        assert codex_callback_state != claude_callback_state
        assert server.session_requests[0]["deployment_instance_id"] == "worker-local-1"
        assert server.session_requests[0]["target"] == "codex"
        assert server.session_requests[0]["plugin_id"] == "promptless-instruction-hub-core"
        assert server.session_requests[0]["plugin_version"] == "0.1.0"
        assert server.session_requests[0]["package_id"] == "core"
        assert server.session_requests[0]["bootstrap_version"] == "0.2.5"
        assert server.session_requests[0]["toolchain_version"] != "unknown"
        assert server.session_requests[0]["pending_callback"] == "1"
        assert server.session_requests[1]["target"] == "claude"
        assert server.session_requests[1]["pending_callback"] == "1"
        assert server.policy_requests == [
            "/v0/host-enrollment/policy?target=codex",
            "/v0/host-enrollment/policy?target=claude",
        ]
        assert len(server.check_ins) == 2
        for check_in in server.check_ins:
            assert set(check_in) == {
                "bootstrap_version",
                "checked_at",
                "drift_reports",
                "effective_config",
                "host",
                "needs_restart",
                "plugin_version",
                "policy_version",
                "status",
            }
            assert check_in["bootstrap_version"] == "0.2.5"
            assert check_in["plugin_version"] == "0.1.0"
            assert check_in["status"] == "configured"
            assert check_in["needs_restart"] is False
            assert check_in["drift_reports"] == []
            effective_config = _json_mapping(check_in["effective_config"], "effective_config")
            assert set(effective_config) == {
                "host",
                "configured",
                "trace_upload_endpoint",
                "native_root_count",
                "source_ledger_path",
                "managed_config_detected",
                "config_hash",
            }
            assert effective_config["configured"] is True
            assert effective_config["managed_config_detected"] is False
            assert effective_config["trace_upload_endpoint"] == f"{server.base_url}/v0/traces/batches"
            assert effective_config["native_root_count"] == 1
            assert _json_string(effective_config["source_ledger_path"], "source_ledger_path").endswith(
                "host-runtime-ledger.json"
            )
        codex_effective_config = _json_mapping(server.check_ins[0]["effective_config"], "codex effective_config")
        claude_effective_config = _json_mapping(server.check_ins[1]["effective_config"], "claude effective_config")
        assert codex_effective_config["host"] == "codex"
        assert claude_effective_config["host"] == "claude"
    finally:
        server.stop()


def test_bootstrap_rejects_loopback_callback_with_wrong_state(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer(callback_state_override="attacker-state")
    server.start()
    try:
        home = tmp_path / "home"
        payload, _result = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "hosted enrollment start request failed with HTTP 403" in str(payload["message"])
        assert not (home / ".codex/config.toml").exists()
        assert server.poll_requests == []
        assert server.policy_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


@pytest.mark.parametrize(
    ("pending_approval_url_override", "pending_approval_path"),
    [
        ("https://attacker.example/instruction-hub/enroll", "/instruction-hub/enroll"),
        (None, "/attacker/enroll"),
    ],
    ids=["wrong-origin", "wrong-path"],
)
def test_bootstrap_rejects_pending_callback_approval_url_outside_dashboard_route(
    tmp_path: Path,
    pending_approval_url_override: str | None,
    pending_approval_path: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer(
        pending_approval_url_override=pending_approval_url_override,
        pending_approval_path=pending_approval_path,
    )
    server.start()
    try:
        home = tmp_path / "home"
        payload, _result = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "hosted enrollment start request failed with HTTP 400" in str(payload["message"])
        assert not (home / ".codex/config.toml").exists()
        assert server.poll_requests == []
        assert server.policy_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


def test_bootstrap_fails_fast_when_browser_pending_callback_rejects_approval_url(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer(pending_approval_url_override="https://attacker.example/instruction-hub/enroll")
    server.start()
    try:
        home = tmp_path / "home"
        result = subprocess.run(
            [str(hub_root / "dist/codex/core/runtime" / HOST_RUNTIME_BIN), "ensure", "--host", "codex"],
            env=_clean_env(
                HOME=str(home),
                CODEX_HOME=str(home / ".codex"),
                PLUGIN_ROOT=str(hub_root / "dist/codex/core"),
                PROMPTLESS_WORKER_BASE_URL=server.base_url,
                PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER="1",
                BROWSER=_async_urlopen_browser_command(tmp_path / "fake-browser.py"),
            ),
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

        assert result.returncode == 0
        payload = _assert_session_start_streams(result.stdout, result.stderr, "error")
        assert payload["message"] == "host enrollment approval URL did not match dashboard enrollment route"
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert server.poll_requests == []
        assert server.policy_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


def test_bootstrap_requires_callback_deployment_instance_id(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer(
        session_response={
            "session_id": "11111111-1111-4111-8111-111111111111",
            "device_code": "plihenroll_devicecode",
            "poll_url": "https://api.gopromptless.ai/v1/instruction-hub/host-enrollments/sessions/11111111-1111-4111-8111-111111111111/poll",
            "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
            "poll_interval_seconds": 1,
        }
    )
    server.start()
    try:
        home = tmp_path / "home"
        payload, _result = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "host enrollment callback missing required fields" in str(payload["message"])
        assert not (home / ".codex/config.toml").exists()
        assert server.policy_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


def test_bootstrap_missing_managed_runtime_manifest_uses_default_metadata(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    (plugin_root / "hub.managed-runtimes.json").unlink()
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        _run_bootstrap(
            plugin_root,
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(plugin_root),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )

        assert not (home / ".codex/config.toml").exists()
        assert server.check_ins[0]["plugin_version"] == "unknown"
        assert "plugin_id" not in server.check_ins[0]
        assert "package_id" not in server.check_ins[0]
    finally:
        server.stop()


def test_bootstrap_preserves_unrelated_config_and_writes_backups(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        codex_home = tmp_path / "codex-home"
        codex_config = codex_home / ".codex/config.toml"
        codex_config.parent.mkdir(parents=True)
        managed_block = (
            '# BEGIN PROMPTLESS MANAGED HOST ENROLLMENT\n[otel]\nenvironment = "prod"\n'
            "# END PROMPTLESS MANAGED HOST ENROLLMENT\n"
        )
        original_codex_config = f'model = "gpt-5"\n[profiles.local]\nmodel = "gpt-5-codex"\n\n{managed_block}'
        codex_config.write_text(original_codex_config)

        _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(codex_home),
                "CODEX_HOME": str(codex_home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="needs_restart",
        )

        updated_codex_config = codex_config.read_text()
        assert 'model = "gpt-5"' in updated_codex_config
        assert "[profiles.local]" in updated_codex_config
        assert "PROMPTLESS MANAGED HOST ENROLLMENT" not in updated_codex_config
        codex_backups = list(codex_config.parent.glob("config.toml.*.bak"))
        assert len(codex_backups) == 1
        assert codex_backups[0].read_text() == original_codex_config
        assert list(codex_config.parent.glob(".config.toml.*.tmp")) == []

        claude_home = tmp_path / "claude-home"
        claude_settings = claude_home / ".claude/settings.json"
        claude_settings.parent.mkdir(parents=True)
        original_claude_settings = {
            "env": {"CUSTOM_ENV": "1", "PROMPTLESS_MANAGED_HOST_ENROLLMENT": "1", "OTEL_LOGS_EXPORTER": "otlp"},
            "theme": "dark",
        }
        claude_settings.write_text(json.dumps(original_claude_settings))

        _run_bootstrap(
            hub_root / "dist/claude/core",
            "claude",
            {
                "HOME": str(claude_home),
                "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="needs_restart",
        )

        updated_claude_settings = json.loads(claude_settings.read_text())
        assert updated_claude_settings["theme"] == "dark"
        assert updated_claude_settings["env"] == {"CUSTOM_ENV": "1"}
        claude_backups = list(claude_settings.parent.glob("settings.json.*.bak"))
        assert len(claude_backups) == 1
        assert json.loads(claude_backups[0].read_text()) == original_claude_settings
        assert list(claude_settings.parent.glob(".settings.json.*.tmp")) == []
    finally:
        server.stop()


def test_bootstrap_removes_managed_host_otel_config(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        managed_begin = "# BEGIN PROMPTLESS MANAGED HOST ENROLLMENT"
        managed_end = "# END PROMPTLESS MANAGED HOST ENROLLMENT"
        stale_codex_block = "\n".join(
            [
                managed_begin,
                "[otel]",
                'environment = "stale"',
                "log_user_prompt = false",
                "",
                "[otel.exporter.otlp-http]",
                'endpoint = "http://stale.local:4318/v1/logs"',
                'protocol = "json"',
                'headers = { Authorization = "Bearer stale-token" }',
                managed_end,
                "",
            ]
        )
        original_codex_config = (
            f'model = "gpt-5"\n\n{stale_codex_block}\n[profiles.local]\nmodel = "gpt-5-codex"\n\n{stale_codex_block}'
        )
        codex_home = tmp_path / "codex-home"
        codex_config = codex_home / ".codex/config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(original_codex_config)

        codex_payload, _ = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(codex_home),
                "CODEX_HOME": str(codex_home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="needs_restart",
        )

        updated_codex_config = codex_config.read_text()
        assert managed_begin not in updated_codex_config
        assert managed_end not in updated_codex_config
        assert "otel" not in updated_codex_config
        assert "stale.local" not in updated_codex_config
        assert 'model = "gpt-5"' in updated_codex_config
        assert "[profiles.local]" in updated_codex_config
        codex_backups = list(codex_config.parent.glob("config.toml.*.bak"))
        assert len(codex_backups) == 1
        assert codex_backups[0].read_text() == original_codex_config
        assert codex_payload["status"] == "needs_restart"
        assert "removed" in _json_string(codex_payload["systemMessage"], "systemMessage").lower()
        codex_check_in = server.check_ins[-1]
        codex_effective_config = _json_mapping(codex_check_in["effective_config"], "codex effective_config")
        assert codex_effective_config["managed_config_detected"] is True
        codex_drift_reports = _json_list(codex_check_in["drift_reports"], "codex drift_reports")
        codex_report = _json_mapping(codex_drift_reports[0], "codex drift_reports[0]")
        assert codex_report["kind"] == "removed_managed_config"
        assert codex_report["repaired"] is True

        original_claude_settings = {
            "env": {
                "CUSTOM_ENV": "1",
                "PROMPTLESS_MANAGED_HOST_ENROLLMENT": "1",
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
                "ENABLE_BETA_TRACING_DETAILED": "1",
                "BETA_TRACING_ENDPOINT": "http://stale.local:4318/v1/traces",
                "OTEL_LOGS_EXPORTER": "otlp",
                "OTEL_METRICS_EXPORTER": ["bad"],
                "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer stale-token",
                "OTEL_LOG_RAW_API_BODIES": "1",
            },
            "theme": "dark",
        }
        claude_home = tmp_path / "claude-home"
        claude_settings = claude_home / ".claude/settings.json"
        claude_settings.parent.mkdir(parents=True)
        claude_settings.write_text(json.dumps(original_claude_settings))

        claude_payload, _ = _run_bootstrap(
            hub_root / "dist/claude/core",
            "claude",
            {
                "HOME": str(claude_home),
                "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="needs_restart",
        )

        updated_claude_settings = json.loads(claude_settings.read_text())
        assert updated_claude_settings["theme"] == "dark"
        assert updated_claude_settings["env"] == {"CUSTOM_ENV": "1"}
        claude_backups = list(claude_settings.parent.glob("settings.json.*.bak"))
        assert len(claude_backups) == 1
        assert json.loads(claude_backups[0].read_text()) == original_claude_settings
        assert claude_payload["status"] == "needs_restart"
        claude_check_in = server.check_ins[-1]
        claude_drift_reports = _json_list(claude_check_in["drift_reports"], "claude drift_reports")
        claude_report = _json_mapping(claude_drift_reports[0], "claude drift_reports[0]")
        assert claude_report["kind"] == "removed_managed_config"
        assert claude_report["repaired"] is True
    finally:
        server.stop()


def test_bootstrap_blocks_malformed_managed_codex_config(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        codex_home = tmp_path / "codex-home"
        codex_config = codex_home / ".codex/config.toml"
        codex_config.parent.mkdir(parents=True)
        original_codex_config = (
            'model = "gpt-5"\n# BEGIN PROMPTLESS MANAGED HOST ENROLLMENT\n[otel]\nenvironment = "prod"\n'
        )
        codex_config.write_text(original_codex_config)

        codex_env = {
            "HOME": str(codex_home),
            "CODEX_HOME": str(codex_home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        codex_payload, _ = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            codex_env,
            expected_status="blocked",
        )

        assert codex_config.read_text() == original_codex_config
        assert list(codex_config.parent.glob("config.toml.*.bak")) == []
        assert codex_payload["status"] == "blocked"
        drift_reports = _json_list(server.check_ins[-1]["drift_reports"], "drift_reports")
        first_drift_report = _json_mapping(drift_reports[0], "drift_reports[0]")
        assert first_drift_report["kind"] == "manual_config_required"
        assert "malformed" in _json_string(first_drift_report["message"], "drift_reports[0].message")

        # A blocked result is not a success: no first-success confirmation is mixed into the
        # blocked message, and the latch stays unclaimed so the confirmation can fire later.
        blocked_message = _json_string(codex_payload["systemMessage"], "systemMessage")
        assert "blocked" in blocked_message
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT not in blocked_message
        assert FIRST_SUCCESS_NO_RESTART_FRAGMENT not in blocked_message
        blocked_state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(codex_home).read_text()), "host state"),
            "host state",
        )
        assert FIRST_SUCCESS_SHOWN_KEY not in blocked_state

        # After the user repairs the config by hand, the next session is the real first success
        # and confirms once.
        codex_config.write_text('model = "gpt-5"\n')
        repaired_payload, _ = _run_bootstrap(hub_root / "dist/codex/core", "codex", codex_env)
        repaired_message = _json_string(repaired_payload["systemMessage"], "systemMessage")
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT in repaired_message
        repaired_state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(codex_home).read_text()), "host state"),
            "host state",
        )
        assert "codex" in _json_mapping(repaired_state[FIRST_SUCCESS_SHOWN_KEY], "first-success shown")
    finally:
        server.stop()


def test_build_appends_bootstrap_hook_to_existing_hook_asset(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    _write_native_hook_asset(
        hub_root,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [{"type": "command", "command": "existing-hook"}],
                    }
                ]
            }
        },
    )

    build_hub(hub_root)

    hooks = json.loads((hub_root / "dist/codex/core/hooks/hooks.json").read_text())
    session_start = hooks["hooks"]["SessionStart"]
    assert session_start[0]["hooks"][0]["command"] == "existing-hook"
    assert f"runtime/{HOST_RUNTIME_BIN}" in session_start[1]["hooks"][0]["command"]


def test_build_rejects_malformed_existing_hook_asset(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    _write_native_hook_asset(hub_root, {"hooks": []})

    with pytest.raises(InstructionHubError, match="field hooks must be a JSON object"):
        build_hub(hub_root)


def test_bootstrap_leaves_unmanaged_host_config_untouched(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        codex_home = tmp_path / "codex-home"
        codex_config = codex_home / ".codex/config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text('[otel]\nenvironment = "local"\n')

        _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(codex_home),
                "CODEX_HOME": str(codex_home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )

        assert codex_config.read_text() == '[otel]\nenvironment = "local"\n'
        assert list(codex_config.parent.glob("config.toml.*.bak")) == []
        assert server.check_ins[-1]["status"] == "configured"

        claude_home = tmp_path / "claude-home"
        claude_settings = claude_home / ".claude/settings.json"
        claude_settings.parent.mkdir(parents=True)
        claude_settings.write_text('{"env":{"OTEL_EXPORTER_OTLP_HEADERS":"Authorization=Bearer customer-token"}}\n')

        _run_bootstrap(
            hub_root / "dist/claude/core",
            "claude",
            {
                "HOME": str(claude_home),
                "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )

        assert (
            claude_settings.read_text()
            == '{"env":{"OTEL_EXPORTER_OTLP_HEADERS":"Authorization=Bearer customer-token"}}\n'
        )
        assert list(claude_settings.parent.glob("settings.json.*.bak")) == []
        assert server.check_ins[-1]["status"] == "configured"
    finally:
        server.stop()


def test_bootstrap_surfaces_enrollment_message_only_on_change(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        claude_home = tmp_path / "claude-home"
        claude_settings = claude_home / ".claude/settings.json"
        claude_settings.parent.mkdir(parents=True)
        claude_settings.write_text(json.dumps({"env": {"PROMPTLESS_MANAGED_HOST_ENROLLMENT": "1"}}))
        claude_env = {
            "HOME": str(claude_home),
            "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
            "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
            "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        # Removing legacy managed config surfaces a restart prompt naming the host; the
        # steady state is silent.
        first_claude, _ = _run_bootstrap(
            hub_root / "dist/claude/core", "claude", claude_env, expected_status="needs_restart"
        )
        claude_message = _json_string(first_claude["systemMessage"], "systemMessage")
        assert "Claude Code" in claude_message
        assert "removed" in claude_message.lower()

        steady_claude, _ = _run_bootstrap(hub_root / "dist/claude/core", "claude", claude_env)
        assert "systemMessage" not in steady_claude

        codex_home = tmp_path / "codex-home"
        codex_config = codex_home / ".codex/config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "# BEGIN PROMPTLESS MANAGED HOST ENROLLMENT\n[otel]\n# END PROMPTLESS MANAGED HOST ENROLLMENT\n"
        )
        codex_env = {
            "HOME": str(codex_home),
            "CODEX_HOME": str(codex_home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        first_codex, _ = _run_bootstrap(
            hub_root / "dist/codex/core", "codex", codex_env, expected_status="needs_restart"
        )
        codex_message = _json_string(first_codex["systemMessage"], "systemMessage")
        assert "Codex" in codex_message
        assert "removed" in codex_message.lower()

        steady_codex, _ = _run_bootstrap(hub_root / "dist/codex/core", "codex", codex_env)
        assert "systemMessage" not in steady_codex
    finally:
        server.stop()


def test_bootstrap_writes_no_host_config_on_fresh_hosts(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        claude_home = tmp_path / "claude-home"
        _run_bootstrap(
            hub_root / "dist/claude/core",
            "claude",
            {
                "HOME": str(claude_home),
                "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )

        assert not (claude_home / ".claude").exists()
        assert not (claude_home / ".promptless/instruction-hub/claude-raw-api-bodies").exists()
    finally:
        server.stop()


def test_bootstrap_stdout_stays_codex_schema_safe(tmp_path: Path) -> None:
    # Regression: Codex rejects SessionStart hook stdout that carries keys outside its schema
    # (serde deny_unknown_fields) with "hook returned invalid session start JSON output". The
    # bootstrap's diagnostic fields (status/host/needs_restart/reason) must never reach stdout —
    # only the user-facing systemMessage may, and stdout stays empty when there is no message.
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        codex_config = tmp_path / "codex-home/.codex/config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "# BEGIN PROMPTLESS MANAGED HOST ENROLLMENT\n[otel]\n# END PROMPTLESS MANAGED HOST ENROLLMENT\n"
        )
        codex_env = {
            "HOME": str(tmp_path / "codex-home"),
            "CODEX_HOME": str(tmp_path / "codex-home/.codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        # Fresh browser enrollment records the start banner in diagnostics but leaves stdout for
        # the final actionable restart message emitted by the managed-config cleanup.
        configured_payload, configured_result = _run_bootstrap(
            hub_root / "dist/codex/core", "codex", codex_env, expected_status="needs_restart"
        )
        configured_stdout = _json_mapping(
            validate_json_value(json.loads(configured_result.stdout), "codex stdout"), "codex stdout"
        )
        assert set(configured_stdout) == {"systemMessage"}
        assert configured_stdout["systemMessage"] == configured_payload["systemMessage"]
        assert "Restart Codex" in _json_string(configured_stdout["systemMessage"], "systemMessage")
        assert "removed" in _json_string(configured_stdout["systemMessage"], "systemMessage").lower()
        assert any(
            diagnostic.get("status") == "browser_enrollment_starting"
            and diagnostic.get("systemMessage") == BROWSER_ENROLLMENT_MESSAGE
            for diagnostic in _bootstrap_diagnostics(configured_result.stderr)
        )
        for forbidden_key in ("status", "host", "needs_restart", "reason"):
            assert forbidden_key not in configured_stdout

        # Steady state has nothing to say: stdout is empty so Codex treats it as success.
        _, steady_result = _run_bootstrap(
            hub_root / "dist/codex/core", "codex", codex_env, expected_status="configured"
        )
        assert steady_result.stdout == ""
    finally:
        server.stop()


def test_bootstrap_announces_plugin_update_per_host(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root, plugin_version="0.1.0")
    server = _FakeWorkerServer()
    server.start()
    try:
        claude_env = {
            "HOME": str(tmp_path / "claude-home"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home/.claude"),
            "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
            "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        codex_env = {
            "HOME": str(tmp_path / "codex-home"),
            "CODEX_HOME": str(tmp_path / "codex-home/.codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        # First install on each host records the version but is not an update, so it shows the
        # one-time first-success confirmation without any version-change notice.
        first_claude, _ = _run_bootstrap(hub_root / "dist/claude/core", "claude", claude_env)
        first_claude_message = _json_string(first_claude["systemMessage"], "systemMessage")
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT in first_claude_message
        assert "updated to" not in first_claude_message
        first_codex, _ = _run_bootstrap(hub_root / "dist/codex/core", "codex", codex_env)
        first_codex_message = _json_string(first_codex["systemMessage"], "systemMessage")
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT in first_codex_message
        assert "updated to" not in first_codex_message

        # Rebuild the same hub at a newer version, then re-run: each host announces the change once.
        build_hub(hub_root, plugin_version="0.2.0")
        upgraded_claude, _ = _run_bootstrap(
            hub_root / "dist/claude/core", "claude", claude_env, expected_status="configured"
        )
        claude_message = _json_string(upgraded_claude["systemMessage"], "systemMessage")
        assert "0.2.0" in claude_message and "0.1.0" in claude_message

        upgraded_codex, _ = _run_bootstrap(
            hub_root / "dist/codex/core", "codex", codex_env, expected_status="configured"
        )
        codex_message = _json_string(upgraded_codex["systemMessage"], "systemMessage")
        assert "0.2.0" in codex_message and "0.1.0" in codex_message

        # A subsequent run at the same version is silent again.
        steady_claude, _ = _run_bootstrap(
            hub_root / "dist/claude/core", "claude", claude_env, expected_status="configured"
        )
        assert "systemMessage" not in steady_claude
    finally:
        server.stop()


def test_bootstrap_update_notice_tolerates_unreadable_state(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        # Without the local dogfood gate, a corrupt host-global state file must surface as a
        # diagnosable bootstrap error before enrollment proceeds.
        state_path = _host_state_path(home)
        state_path.parent.mkdir(parents=True)
        state_path.write_text("{ not valid json")

        payload, result = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "invalid JSON" in _json_string(payload["message"], "message")
        assert "Promptless host enrollment failed for Codex" in _json_string(payload["systemMessage"], "systemMessage")
        assert result.stdout != ""
    finally:
        server.stop()


def test_bootstrap_defers_recording_update_until_notice_surfaces(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root, plugin_version="0.1.0")
    server = _FakeWorkerServer()
    server.start()
    try:
        state_path = _host_state_path(tmp_path / "claude-home")

        def claude_env(worker_base_url: str) -> dict[str, str]:
            return {
                "HOME": str(tmp_path / "claude-home"),
                "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home/.claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_WORKER_BASE_URL": worker_base_url,
            }

        def seen_claude_version() -> str:
            state = json.loads(state_path.read_text())
            versions = _json_mapping(
                validate_json_value(state["last_seen_plugin_versions"], "last_seen_plugin_versions"),
                "last_seen_plugin_versions",
            )
            return _json_string(versions["claude"], "last_seen_plugin_versions.claude")

        # A first healthy session records v0.1.0 as seen.
        _run_bootstrap(hub_root / "dist/claude/core", "claude", claude_env(server.base_url))
        assert seen_claude_version() == "0.1.0"

        # Upgrade, then hit a failing session (unreachable worker): the new version must NOT be
        # marked seen, because its update notice was never surfaced.
        build_hub(hub_root, plugin_version="0.2.0")
        _run_bootstrap(
            hub_root / "dist/claude/core",
            "claude",
            claude_env("http://127.0.0.1:9"),
            expected_status="error",
        )
        assert seen_claude_version() == "0.1.0"

        # The next healthy session still surfaces the one-time update notice and records v0.2.0.
        recovered, _ = _run_bootstrap(
            hub_root / "dist/claude/core", "claude", claude_env(server.base_url), expected_status="configured"
        )
        recovered_message = _json_string(recovered["systemMessage"], "systemMessage")
        assert "0.2.0" in recovered_message and "0.1.0" in recovered_message
        assert seen_claude_version() == "0.2.0"
    finally:
        server.stop()


def test_bootstrap_repeat_runs_stay_configured_without_config_writes(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        codex_home = tmp_path / "codex-home"
        codex_env = {
            "HOME": str(codex_home),
            "CODEX_HOME": str(codex_home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        _run_bootstrap(hub_root / "dist/codex/core", "codex", codex_env)
        _run_bootstrap(hub_root / "dist/codex/core", "codex", codex_env)
        assert not (codex_home / ".codex/config.toml").exists()

        claude_home = tmp_path / "claude-home"
        claude_env = {
            "HOME": str(claude_home),
            "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
            "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
            "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        _run_bootstrap(hub_root / "dist/claude/core", "claude", claude_env)
        settings_path = claude_home / ".claude/settings.json"
        _run_bootstrap(hub_root / "dist/claude/core", "claude", claude_env)
        assert not settings_path.exists()
        assert [check_in["status"] for check_in in server.check_ins] == [
            "configured",
            "configured",
            "configured",
            "configured",
        ]
        assert [request["target"] for request in server.session_requests] == ["codex", "claude"]
    finally:
        server.stop()


@pytest.mark.parametrize(
    "case",
    [
        "expired",
    ],
)
def test_bootstrap_rejects_invalid_worker_policy(tmp_path: Path, case: str) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    invalid_policy = _invalid_policy(case)
    invalid_policy["user_email"] = "Adit@GoPromptless.AI"
    server = _FakeWorkerServer(policy=invalid_policy)
    server.start()
    try:
        home = tmp_path / "home"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            env,
            expected_status="error",
        )

        assert not (home / ".codex/config.toml").exists()
        state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "host state"),
            "host state",
        )
        assert INTERNAL_WELCOME_SHOWN_AT_KEY not in state
        assert INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY not in state
        credentials = _json_mapping(state["credentials"], "credentials")
        credential = _json_mapping(
            credentials[_credential_cache_key(worker_base_url=server.base_url, target="codex")],
            "credential",
        )
        assert "is_internal_promptless_user" not in credential
        assert server.check_ins == []
    finally:
        server.stop()


def test_bootstrap_ignores_legacy_collector_policy_sections(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    # Hosted policies still carry the retired OTLP collector section for older
    # bootstraps; this runtime must tolerate any shape, including its absence.
    malformed_collector = _policy_with(collector={"otlp_http_logs_endpoint": "not-a-url"})
    policy_without_collector = _policy_with()
    policy_body = _json_mapping(policy_without_collector["policy"], "policy")
    policy_body.pop("collector", None)
    for policy_payload in (malformed_collector, policy_without_collector):
        server = _FakeWorkerServer(policy=policy_payload)
        server.start()
        try:
            home = tmp_path / f"home-{len(server.check_ins)}-{server.base_url.rsplit(':', 1)[1]}"
            _run_bootstrap(
                hub_root / "dist/codex/core",
                "codex",
                {
                    "HOME": str(home),
                    "CODEX_HOME": str(home / ".codex"),
                    "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                    "PROMPTLESS_WORKER_BASE_URL": server.base_url,
                },
            )
            assert server.check_ins[-1]["status"] == "configured"
        finally:
            server.stop()


def test_upload_only_policy_permissions_block_neither_ensure_nor_collect(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    # Hosted policies still carry the retired plugin_permissions section for older
    # bootstraps. An upload-only grant must not reject config cleanup or, worse,
    # silently drop every lifecycle trace upload for the org.
    upload_only_policy = _policy_with(plugin_permissions={"write_user_config": False, "repair_user_config": False})
    server = _FakeWorkerServer(policy=upload_only_policy)
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        transcript_path = tmp_path / "codex-session.jsonl"
        record = b'{"kind":"stop","message":"upload-only policy"}\n'
        transcript_path.write_bytes(record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_bootstrap(plugin_root, "codex", env)
        assert server.check_ins[-1]["status"] == "configured"

        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )
        assert len(server.trace_batches) == 1

        chunks = _json_list(server.trace_batches[0]["chunks"], "batch.chunks")
        chunk = _json_mapping(chunks[0], "batch.chunks[0]")
        assert chunk["start_offset"] == 0
        assert chunk["end_offset"] == len(record)
    finally:
        server.stop()


def test_bootstrap_blocks_when_worker_requires_different_runtime_version(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer(policy=_policy_with(required_bootstrap_version="0.3.0"))
    server.start()
    try:
        home = tmp_path / "home"
        _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="blocked",
        )

        assert not (home / ".codex/config.toml").exists()
        assert server.check_ins[0]["status"] == "blocked"
        drift_reports = _json_list(server.check_ins[0]["drift_reports"], "drift_reports")
        first_drift_report = _json_mapping(drift_reports[0], "drift_reports[0]")
        assert first_drift_report["kind"] == "bootstrap_upgrade_required"
    finally:
        server.stop()


def test_bootstrap_rejects_invalid_check_in_success_response(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer(post_response={"accepted": False, "policy_version": 1})
    server.start()
    try:
        home = tmp_path / "home"
        payload, _result = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "check-in response was not accepted" in str(payload["message"])
        assert len(server.check_ins) == 1
    finally:
        server.stop()


def test_collect_baselines_then_uploads_transcript_path_ranges(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        transcript_path = tmp_path / "codex-session.jsonl"
        first_record = b'{"kind":"session_start","message":"baseline"}\n'
        second_record = b'{"kind":"stop","message":"upload"}\n'
        transcript_path.write_bytes(first_record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"sessionId": "codex_session_1", "transcriptPath": str(transcript_path)},
        )
        assert server.trace_batches == []

        baseline_ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        baseline_sources = _json_mapping(baseline_ledger["sources"], "ledger.sources")
        baseline_source = _json_mapping(next(iter(baseline_sources.values())), "ledger.sources[0]")
        assert baseline_source["end_offset"] == len(first_record)

        third_record = b'{"kind":"note","message":"coalesced"}\n'
        transcript_path.write_bytes(first_record + second_record + third_record)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {
                "sessionId": "",
                "session": {"id": "codex_session_1"},
                "transcriptPath": "",
                "transcript": {"path": str(transcript_path)},
            },
        )

        assert len(server.trace_batches) == 1
        batch = server.trace_batches[0]
        assert batch["source"] == "codex"
        assert batch["host"] == "codex"
        assert batch["session_id"] == "codex_session_1"
        assert batch["policy_version"] == 1
        assert batch["collector_version"] == "0.2.5"
        chunks = _json_list(batch["chunks"], "batch.chunks")
        # contiguous complete lines coalesce into one contract-shaped range chunk
        assert len(chunks) == 1
        chunk = _json_mapping(chunks[0], "batch.chunks[0]")
        assert chunk["kind"] == "jsonl_range"
        assert chunk["start_offset"] == len(first_record)
        assert chunk["end_offset"] == len(first_record) + len(second_record) + len(third_record)
        assert chunk["line_count"] == 2
        assert chunk["lifecycle_event"] == "stop"
        assert chunk["content_encoding"] == "gzip"
        assert (
            gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content")))
            == second_record + third_record
        )

        advanced_ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        advanced_sources = _json_mapping(advanced_ledger["sources"], "ledger.sources")
        advanced_source = _json_mapping(
            advanced_sources[_json_string(chunk["source_path_hash"], "source_path_hash")], "source"
        )
        assert advanced_source["end_offset"] == transcript_path.stat().st_size
    finally:
        server.stop()


def test_collect_zero_source_first_baseline_persists_host_marker(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {},
        )

        assert server.trace_batches == []
        ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        assert _json_mapping(ledger["sources"], "ledger.sources") == {}
        assert _json_list(ledger["host_baselines"], "ledger.host_baselines") == ["codex"]
    finally:
        server.stop()


def test_collect_legacy_host_source_is_treated_as_existing_baseline(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        codex_home = home / ".codex"
        transcript_path = codex_home / "sessions/session.jsonl"
        transcript_path.parent.mkdir(parents=True)
        first_record = b'{"kind":"session_start","message":"legacy baseline"}\n'
        appended_record = b'{"kind":"stop","message":"must upload"}\n'
        transcript_path.write_bytes(first_record + appended_record)
        ledger_path = tmp_path / "ledger.json"
        source_path_hash = hashlib.sha256(str(transcript_path.resolve()).encode()).hexdigest()
        ledger_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sources": {
                        source_path_hash: {
                            "path": str(transcript_path.resolve()),
                            "end_offset": len(first_record),
                        }
                    },
                }
            )
        )
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        assert len(server.trace_batches) == 1
        chunks = _json_list(server.trace_batches[0]["chunks"], "batch.chunks")
        assert len(chunks) == 1
        chunk = _json_mapping(chunks[0], "batch.chunks[0]")
        assert chunk["start_offset"] == len(first_record)
        assert chunk["end_offset"] == len(first_record) + len(appended_record)
        assert gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content"))) == appended_record

        ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        assert _json_list(ledger["host_baselines"], "ledger.host_baselines") == ["codex"]
    finally:
        server.stop()


def test_claude_desktop_discovers_both_audit_stores_under_platform_config_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/core"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude", "claude-desktop"]))
    server.start()
    try:
        home = tmp_path / "home"
        config_root = tmp_path / "desktop-config"
        env = {
            "HOME": str(home),
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(tmp_path / "ledger.json"),
        }
        if os.name == "nt":
            env["APPDATA"] = str(config_root)
            claude_base = config_root / "Claude"
        elif sys.platform == "darwin":
            claude_base = home / "Library/Application Support/Claude"
        else:
            env["XDG_CONFIG_HOME"] = str(config_root)
            claude_base = config_root / "Claude"

        appended_records: dict[Path, bytes] = {}
        for store_name in ("local-agent-mode-sessions", "claude-code-sessions"):
            audit_path = claude_base / store_name / f"{store_name}-session/audit.jsonl"
            audit_path.parent.mkdir(parents=True)
            baseline_record = json.dumps({"store": store_name, "message": "baseline"}).encode() + b"\n"
            appended_record = json.dumps({"store": store_name, "message": "upload"}).encode() + b"\n"
            audit_path.write_bytes(baseline_record)
            appended_records[audit_path.resolve()] = appended_record

        _run_runtime_json(plugin_root, ["enroll", "--host", "claude-desktop"], env)
        _run_collect(
            plugin_root,
            [
                "collect",
                "--host",
                "claude-desktop",
                "--lifecycle",
                "session_start",
                "--baseline",
                "--quiet",
            ],
            env,
            {},
        )
        assert server.trace_batches == []

        stale_time = time.time() - (13 * 60 * 60)
        for audit_path, appended_record in appended_records.items():
            audit_path.write_bytes(audit_path.read_bytes() + appended_record)
            os.utime(audit_path, (stale_time, stale_time))
        _run_collect(
            plugin_root,
            ["collect", "--host", "claude-desktop", "--lifecycle", "stop", "--quiet"],
            env,
            {},
        )

        chunks = [
            _json_mapping(chunk, "batch.chunks[]")
            for batch in server.trace_batches
            for chunk in _json_list(batch["chunks"], "batch.chunks")
        ]
        assert len(chunks) == 2
        assert {
            gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content"))) for chunk in chunks
        } == set(appended_records.values())
    finally:
        server.stop()


def test_claude_desktop_ensure_if_sources_skips_without_audit_files(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/core"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude", "claude-desktop"]))
    server.start()
    try:
        home = tmp_path / "home"
        result = subprocess.run(
            [str(plugin_root / "runtime" / HOST_RUNTIME_BIN), "ensure", "--host", "claude-desktop", "--if-sources"],
            env=_clean_env(
                HOME=str(home),
                CLAUDE_PLUGIN_ROOT=str(plugin_root),
                PROMPTLESS_WORKER_BASE_URL=server.base_url,
            ),
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0
        assert result.stdout == ""
        payload = _assert_session_start_streams(result.stdout, result.stderr, "trace_upload_skipped")
        assert payload["host"] == "claude-desktop"
        assert payload["reason"] == "no_sources"
        assert server.session_requests == []
        assert server.policy_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


def test_claude_desktop_ensure_uses_shared_claude_enrollment_and_policy(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/core"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
    server.start()
    try:
        home = tmp_path / "home"
        audit_path = _claude_desktop_audit_path(home, "local-agent-mode-sessions", "session-1")
        audit_path.parent.mkdir(parents=True)
        audit_path.write_bytes(b'{"sessionId":"desktop_session_1","message":"baseline"}\n')
        env = {
            "HOME": str(home),
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        result = subprocess.run(
            [str(plugin_root / "runtime" / HOST_RUNTIME_BIN), "ensure", "--host", "claude-desktop", "--if-sources"],
            env=_clean_env(**env),
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0
        payload = _assert_session_start_streams(result.stdout, result.stderr, "configured")
        assert payload["host"] == "claude"
        assert len(server.session_requests) == 1
        assert server.session_requests[0]["target"] == "claude"
        assert server.policy_requests == ["/v0/host-enrollment/policy?target=claude"]
        assert len(server.check_ins) == 1
        assert server.check_ins[0]["host"] == "claude"
        effective_config = _json_mapping(server.check_ins[0]["effective_config"], "effective config")
        assert effective_config["host"] == "claude"

        enroll_payload, _ = _run_runtime_json(plugin_root, ["enroll", "--host", "claude"], env)
        assert enroll_payload["host"] == "claude"
        assert len(server.session_requests) == 1

        desktop_status, _ = _run_runtime_json(plugin_root, ["status", "--host", "claude-desktop"], env)
        status_state = _json_mapping(desktop_status["state"], "desktop status state")
        assert status_state["credential_count"] == 1
    finally:
        server.stop()


@pytest.mark.parametrize("reset_host", ["claude", "claude-desktop"])
def test_claude_reset_clears_shared_and_legacy_desktop_enrollment_state(
    tmp_path: Path,
    reset_host: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/core"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
    server.start()
    try:
        home = tmp_path / "home"
        env = {
            "HOME": str(home),
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "claude-desktop"], env)
        state_path = _host_state_path(home)
        state = json.loads(state_path.read_text())
        state["credentials"]["legacy-desktop"] = {
            "target": "claude-desktop",
            "value": "legacy-desktop-credential",
        }
        state["pending_enrollments"] = {
            "legacy-desktop": {"target": "claude-desktop"},
            "unrelated-codex": {"target": "codex"},
        }
        state[FIRST_SUCCESS_SHOWN_KEY] = {
            "claude": "2026-08-10T00:00:00Z",
            "claude-desktop": "2026-08-10T00:00:00Z",
            "codex": "2026-08-10T00:00:00Z",
        }
        state_path.write_text(json.dumps(state))

        reset_payload, _ = _run_runtime_json(plugin_root, ["reset", "--host", reset_host, "--yes"], env)

        assert reset_payload == {
            "credentials_removed": 2,
            "host": reset_host,
            "pending_enrollments_removed": 1,
            "status": "reset",
        }
        reset_state = json.loads(state_path.read_text())
        assert reset_state["credentials"] == {}
        assert reset_state["pending_enrollments"] == {"unrelated-codex": {"target": "codex"}}
        assert reset_state[FIRST_SUCCESS_SHOWN_KEY] == {"codex": "2026-08-10T00:00:00Z"}

        desktop_status, _ = _run_runtime_json(plugin_root, ["status", "--host", "claude-desktop"], env)
        desktop_state = _json_mapping(desktop_status["state"], "desktop status state")
        assert desktop_state["credential_count"] == 0
        assert desktop_state["pending_enrollment_count"] == 0
    finally:
        server.stop()


def test_claude_desktop_collect_skips_without_cached_credential(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/core"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
    server.start()
    try:
        home = tmp_path / "home"
        audit_path = _claude_desktop_audit_path(home, "local-agent-mode-sessions", "session-1")
        audit_path.parent.mkdir(parents=True)
        audit_path.write_bytes(b'{"sessionId":"desktop_session_1","message":"baseline"}\n')

        _run_collect(
            plugin_root,
            ["collect", "--host", "claude-desktop", "--lifecycle", "session_start", "--baseline", "--quiet"],
            {
                "HOME": str(home),
                "CLAUDE_PLUGIN_ROOT": str(plugin_root),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            {},
        )

        assert server.session_requests == []
        assert server.policy_requests == []
        assert server.trace_batches == []
    finally:
        server.stop()


def test_claude_desktop_collect_uploads_audit_jsonl_ranges(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/core"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        audit_path = _claude_desktop_audit_path(home, "local-agent-mode-sessions", "session-1")
        audit_path.parent.mkdir(parents=True)
        first_record = b'{"sessionId":"desktop_session_1","message":"baseline"}\n'
        second_record = b'{"sessionId":"desktop_session_1","message":"upload"}\n'
        audit_path.write_bytes(first_record)
        stale_time = time.time() - (13 * 60 * 60)
        env = {
            "HOME": str(home),
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "claude-desktop"], env)
        assert server.session_requests[-1]["target"] == "claude"

        _run_collect(
            plugin_root,
            ["collect", "--host", "claude-desktop", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {},
        )
        assert server.trace_batches == []

        audit_path.write_bytes(first_record + second_record)
        os.utime(audit_path, (stale_time, stale_time))
        _run_collect(
            plugin_root,
            ["collect", "--host", "claude-desktop", "--lifecycle", "stop", "--quiet"],
            env,
            {},
        )

        assert len(server.trace_batches) == 1
        assert server.policy_requests == [
            "/v0/host-enrollment/policy?target=claude",
            "/v0/host-enrollment/policy?target=claude",
        ]
        batch = server.trace_batches[0]
        assert batch["source"] == "claude-desktop"
        assert batch["host"] == "claude-desktop"
        assert batch["collector_version"] == "0.2.5"
        chunks = _json_list(batch["chunks"], "batch.chunks")
        assert len(chunks) == 1
        chunk = _json_mapping(chunks[0], "batch.chunks[0]")
        assert chunk["kind"] == "jsonl_range"
        assert chunk["start_offset"] == len(first_record)
        assert chunk["end_offset"] == len(first_record) + len(second_record)
        assert "lifecycle_event" not in chunk
        assert gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content"))) == second_record

        ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        assert "claude-desktop" in _json_list(ledger["host_baselines"], "ledger.host_baselines")
    finally:
        server.stop()


def test_claude_desktop_baseline_is_per_host_with_shared_ledger(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/core"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        claude_path = home / ".claude/projects/project-1/session.jsonl"
        desktop_path = _claude_desktop_audit_path(home, "claude-code-sessions", "session-1")
        claude_path.parent.mkdir(parents=True)
        desktop_path.parent.mkdir(parents=True)
        claude_path.write_bytes(b'{"sessionId":"claude_session_1","message":"baseline"}\n')
        desktop_path.write_bytes(b'{"sessionId":"desktop_session_1","message":"baseline"}\n')
        stale_time = time.time() - (13 * 60 * 60)
        os.utime(claude_path, (stale_time, stale_time))
        os.utime(desktop_path, (stale_time, stale_time))
        env = {
            "HOME": str(home),
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "claude"], env)
        _run_runtime_json(plugin_root, ["enroll", "--host", "claude-desktop"], env)

        _run_collect(
            plugin_root,
            ["collect", "--host", "claude", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {},
        )
        _run_collect(
            plugin_root,
            ["collect", "--host", "claude-desktop", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {},
        )

        assert server.trace_batches == []
        ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        assert set(_json_list(ledger["host_baselines"], "ledger.host_baselines")) == {"claude", "claude-desktop"}
        sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert len(sources) == 2
        assert [request["target"] for request in server.session_requests] == ["claude"]
    finally:
        server.stop()


def test_collect_without_baseline_uploads_new_ledger_sources_from_start(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        codex_home = home / ".codex"
        transcript_path = codex_home / "sessions/codex-session.jsonl"
        transcript_path.parent.mkdir(parents=True)
        first_record = b'{"kind":"session_start","message":"missed baseline"}\n'
        second_record = b'{"kind":"stop","message":"complete"}\n'
        transcript_path.write_bytes(first_record + second_record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        # A terminal hook is the first collection to see this ledger (missed SessionStart).
        # The completed transcript must upload from offset 0, not get baselined away.
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        assert len(server.trace_batches) == 1
        batch = server.trace_batches[0]
        chunks = _json_list(batch["chunks"], "batch.chunks")
        assert len(chunks) == 1
        chunk = _json_mapping(chunks[0], "batch.chunks[0]")
        assert chunk["kind"] == "jsonl_range"
        assert chunk["start_offset"] == 0
        assert chunk["end_offset"] == len(first_record) + len(second_record)
        assert chunk["line_count"] == 2
        assert (
            gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content")))
            == first_record + second_record
        )

        ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        sources = _json_mapping(ledger["sources"], "ledger.sources")
        source = _json_mapping(sources[_json_string(chunk["source_path_hash"], "source_path_hash")], "source")
        assert source["end_offset"] == transcript_path.stat().st_size

        # The ledger advanced through the normal ACK path, so a repeat collect uploads nothing.
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )
        assert len(server.trace_batches) == 1

        historical_path = codex_home / "archived_sessions/historical.jsonl"
        historical_path.parent.mkdir(parents=True)
        historical_path.write_bytes(b'{"kind":"response","message":"pre-baseline history"}\n')
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--include-active", "--quiet"],
            env,
            {},
        )
        guarded_ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        assert _json_list(guarded_ledger["host_baselines"], "ledger.host_baselines") == []
        assert len(_json_mapping(guarded_ledger["sources"], "ledger.sources")) == 1
        assert len(server.trace_batches) == 1
    finally:
        server.stop()


def test_collect_include_active_uploads_recent_root_source_without_lifecycle(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        codex_home = home / ".codex"
        ledger_path = tmp_path / "ledger.json"
        transcript_path = codex_home / "archived_sessions/recent.jsonl"
        transcript_path.parent.mkdir(parents=True)
        baseline_record = b'{"kind":"session_start"}\n'
        pending_record = b'{"kind":"response","message":"sync now"}\n'
        transcript_path.write_bytes(baseline_record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--include-active", "--quiet"],
            env,
            {},
        )
        assert server.trace_batches == []
        assert not ledger_path.exists()

        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {},
        )
        transcript_path.write_bytes(baseline_record + pending_record)

        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--quiet"],
            env,
            {},
        )
        assert server.trace_batches == []

        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--include-active", "--quiet"],
            env,
            {},
        )

        assert len(server.trace_batches) == 1
        chunk = _json_mapping(_json_list(server.trace_batches[0]["chunks"], "chunks")[0], "chunk")
        assert "lifecycle_event" not in chunk
        assert chunk["start_offset"] == len(baseline_record)
        assert chunk["end_offset"] == len(baseline_record) + len(pending_record)
        assert gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content"))) == pending_record
    finally:
        server.stop()


def test_collect_uploads_subagent_transcript_with_parent_identity(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        agent_transcript_path = tmp_path / "codex-subagent.jsonl"
        first_record = b'{"kind":"agent_start"}\n'
        second_record = b'{"kind":"agent_stop"}\n'
        agent_transcript_path.write_bytes(first_record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"session_id": "parent_session_1", "transcript_path": str(agent_transcript_path)},
        )
        agent_transcript_path.write_bytes(first_record + second_record)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "subagent_stop", "--quiet"],
            env,
            {
                "parent_session_id": "",
                "parentSessionId": "parent_session_1",
                "agentTranscriptPath": "",
                "agent": {"id": "agent_1", "type": "worker", "transcriptPath": str(agent_transcript_path)},
            },
        )

        assert len(server.trace_batches) == 1
        batch = server.trace_batches[0]
        assert "session_id" not in batch
        assert batch["parent_session_id"] == "parent_session_1"
        assert batch["agent_id"] == "agent_1"
        assert batch["agent_type"] == "worker"
        chunk = _json_mapping(_json_list(batch["chunks"], "batch.chunks")[0], "batch.chunks[0]")
        assert chunk["lifecycle_event"] == "subagent_stop"
        assert chunk["content_encoding"] == "gzip"
        assert gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content"))) == second_record
    finally:
        server.stop()


def test_collect_scopes_lifecycle_event_to_the_hook_subject_file(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        codex_home = home / ".codex"
        ledger_path = tmp_path / "ledger.json"
        subject_path = tmp_path / "codex-session.jsonl"
        idle_path = codex_home / "sessions/other-session.jsonl"
        idle_path.parent.mkdir(parents=True)
        subject_record = b'{"kind":"session_start"}\n'
        idle_record = b'{"kind":"other_session"}\n'
        subject_path.write_bytes(subject_record)
        idle_path.write_bytes(idle_record)
        stale = time.time() - (13 * 60 * 60)
        os.utime(idle_path, (stale, stale))
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(subject_path)},
        )
        subject_extra = b'{"kind":"stop"}\n'
        idle_extra = b'{"kind":"idle_tail"}\n'
        subject_path.write_bytes(subject_record + subject_extra)
        idle_path.write_bytes(idle_record + idle_extra)
        os.utime(idle_path, (stale, stale))
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(subject_path)},
        )

        uploaded_chunks = [
            _json_mapping(chunk_value, "chunk")
            for batch in server.trace_batches
            for chunk_value in _json_list(batch["chunks"], "chunks")
        ]
        assert len(uploaded_chunks) == 2
        # The hook's stop event describes only its own transcript; the idle-swept
        # file from another session must not be finalized by it.
        chunks_by_lifecycle = {chunk.get("lifecycle_event"): chunk for chunk in uploaded_chunks}
        assert set(chunks_by_lifecycle) == {"stop", None}
        subject_chunk = chunks_by_lifecycle["stop"]
        idle_chunk = chunks_by_lifecycle[None]
        assert gzip.decompress(base64.b64decode(_json_string(subject_chunk["content_base64"], "c"))) == subject_extra
        assert gzip.decompress(base64.b64decode(_json_string(idle_chunk["content_base64"], "c"))) == idle_extra
        assert "lifecycle_event" not in idle_chunk
    finally:
        server.stop()


def test_collect_reports_oversized_record_with_content_size_reason(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        transcript_path = tmp_path / "codex-session.jsonl"
        baseline_record = b'{"kind":"session_start"}\n'
        oversized_record = b'{"kind":"huge","payload":"' + b"x" * (10 * 1024 * 1024) + b'"}\n'
        trailing_record = b'{"kind":"stop"}\n'
        transcript_path.write_bytes(baseline_record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )
        transcript_path.write_bytes(baseline_record + oversized_record + trailing_record)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        uploaded_chunks = [
            _json_mapping(chunk_value, "chunk")
            for batch in server.trace_batches
            for chunk_value in _json_list(batch["chunks"], "chunks")
        ]
        assert [chunk["kind"] for chunk in uploaded_chunks] == ["oversized_record", "jsonl_range"]
        oversized_chunk = uploaded_chunks[0]
        assert oversized_chunk["oversized_reason"] == "content_size"
        assert oversized_chunk["byte_count"] == len(oversized_record)
        assert oversized_chunk["start_offset"] == len(baseline_record)
        assert oversized_chunk["end_offset"] == len(baseline_record) + len(oversized_record)

        advanced_ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        advanced_sources = _json_mapping(advanced_ledger["sources"], "ledger.sources")
        advanced_source = _json_mapping(next(iter(advanced_sources.values())), "ledger.sources[0]")
        assert advanced_source["end_offset"] == transcript_path.stat().st_size
    finally:
        server.stop()


def test_collect_reports_oversized_record_with_transport_size_reason(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        transcript_path = tmp_path / "codex-session.jsonl"
        baseline_record = b'{"kind":"session_start"}\n'
        # Under the 10 MiB raw-record cap, but incompressible: gzip+base64 grows it
        # ~4/3 past the request transport budget, so it cannot be sent as content.
        incompressible_record = os.urandom(8 * 1024 * 1024 + 512 * 1024).replace(b"\n", b"x") + b"\n"
        trailing_record = b'{"kind":"stop"}\n'
        transcript_path.write_bytes(baseline_record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )
        transcript_path.write_bytes(baseline_record + incompressible_record + trailing_record)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        uploaded_chunks = [
            _json_mapping(chunk_value, "chunk")
            for batch in server.trace_batches
            for chunk_value in _json_list(batch["chunks"], "chunks")
        ]
        assert [chunk["kind"] for chunk in uploaded_chunks] == ["oversized_record", "jsonl_range"]
        oversized_chunk = uploaded_chunks[0]
        assert oversized_chunk["oversized_reason"] == "transport_size"
        assert oversized_chunk["byte_count"] == len(incompressible_record)
        assert oversized_chunk["start_offset"] == len(baseline_record)
        assert oversized_chunk["end_offset"] == len(baseline_record) + len(incompressible_record)
        assert "content_base64" not in oversized_chunk
        trailing_chunk = uploaded_chunks[1]
        assert (
            gzip.decompress(base64.b64decode(_json_string(trailing_chunk["content_base64"], "content")))
            == trailing_record
        )

        # The skip advances the ledger past the unsendable record: no retry wedge.
        advanced_ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        advanced_sources = _json_mapping(advanced_ledger["sources"], "ledger.sources")
        advanced_source = _json_mapping(next(iter(advanced_sources.values())), "ledger.sources[0]")
        assert advanced_source["end_offset"] == transcript_path.stat().st_size
    finally:
        server.stop()


def test_collect_splits_batches_by_transport_size(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        transcript_path = tmp_path / "codex-session.jsonl"
        baseline_record = b'{"kind":"session_start"}\n'
        # Two incompressible 4 MiB records: 8 MiB decoded fits one batch, but each
        # encodes to ~5.6 MiB, so transport accounting must split them across two.
        first_blob = os.urandom(4 * 1024 * 1024).replace(b"\n", b"x") + b"\n"
        second_blob = os.urandom(4 * 1024 * 1024).replace(b"\n", b"x") + b"\n"
        transcript_path.write_bytes(baseline_record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )
        transcript_path.write_bytes(baseline_record + first_blob + second_blob)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        assert len(server.trace_batches) == 2
        uploaded_chunks = [
            _json_mapping(chunk_value, "chunk")
            for batch in server.trace_batches
            for chunk_value in _json_list(batch["chunks"], "chunks")
        ]
        assert [chunk["kind"] for chunk in uploaded_chunks] == ["jsonl_range", "jsonl_range"]
        reassembled = b"".join(
            gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content")))
            for chunk in uploaded_chunks
        )
        assert reassembled == first_blob + second_blob

        advanced_ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        advanced_sources = _json_mapping(advanced_ledger["sources"], "ledger.sources")
        advanced_source = _json_mapping(next(iter(advanced_sources.values())), "ledger.sources[0]")
        assert advanced_source["end_offset"] == transcript_path.stat().st_size
    finally:
        server.stop()


def test_collect_skips_unreadable_idle_source_and_uploads_the_rest(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        codex_home = home / ".codex"
        transcript_path = tmp_path / "codex-session.jsonl"
        first_record = b'{"kind":"session_start"}\n'
        transcript_path.write_bytes(first_record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        # Two idle files appear after the baseline; the alphabetically first one
        # stats fine but cannot be opened. It must not abort the run: the pending
        # hook-subject chunk and the idle file sorted after it still upload.
        unreadable_path = codex_home / "sessions/aaa-unreadable.jsonl"
        unreadable_path.parent.mkdir(parents=True)
        unreadable_path.write_bytes(b'{"kind":"locked"}\n')
        readable_path = codex_home / "sessions/zzz-readable.jsonl"
        readable_record = b'{"kind":"idle_after_bad_file"}\n'
        readable_path.write_bytes(readable_record)
        stale = time.time() - (13 * 60 * 60)
        os.utime(unreadable_path, (stale, stale))
        os.utime(readable_path, (stale, stale))
        unreadable_path.chmod(0)

        second_record = b'{"kind":"stop"}\n'
        transcript_path.write_bytes(first_record + second_record)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        uploaded_contents = {
            gzip.decompress(
                base64.b64decode(_json_string(_json_mapping(chunk_value, "chunk")["content_base64"], "content"))
            )
            for batch in server.trace_batches
            for chunk_value in _json_list(batch["chunks"], "chunks")
        }
        assert uploaded_contents == {second_record, readable_record}
        diagnostics = _diagnostic_log_entries(home)
        assert diagnostics[-1]["status"] == "trace_upload_complete"
        assert diagnostics[-1]["unreadable_source_count"] == 1
        assert diagnostics[-1]["batch_count"] == 1

        ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert len(sources) == 2
        advanced_offsets = sorted(
            _json_int(_json_mapping(source, "source")["end_offset"], "end_offset") for source in sources.values()
        )
        assert advanced_offsets == sorted([transcript_path.stat().st_size, len(readable_record)])
    finally:
        server.stop()


def test_collect_tolerates_unparsed_record_counts_and_advances_ledger(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    # The worker models undecodable ledger lines as informational counts; a nonzero
    # count must never fail the upload or hold the ledger back.
    server = _FakeWorkerServer(unparsed_record_count=2)
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        transcript_path = tmp_path / "codex-session.jsonl"
        first_record = b'{"kind":"session_start"}\n'
        second_record = b"not-json-but-complete-line\n"
        transcript_path.write_bytes(first_record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )
        transcript_path.write_bytes(first_record + second_record)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        assert len(server.trace_batches) == 1
        advanced_ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        advanced_sources = _json_mapping(advanced_ledger["sources"], "ledger.sources")
        advanced_source = _json_mapping(next(iter(advanced_sources.values())), "ledger.sources[0]")
        assert advanced_source["end_offset"] == transcript_path.stat().st_size
    finally:
        server.stop()


def test_collect_skips_when_ledger_lock_is_busy_and_logs_diagnostic(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("fcntl lock contention test is POSIX-only")
    import fcntl

    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        transcript_path = tmp_path / "codex-session.jsonl"
        first_record = b'{"kind":"session_start","message":"baseline"}\n'
        second_record = b'{"kind":"stop","message":"upload"}\n'
        transcript_path.write_bytes(first_record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )
        transcript_path.write_bytes(first_record + second_record)
        policy_request_count = len(server.policy_requests)

        lock_path = ledger_path.with_name(f"{ledger_path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            _run_collect(
                plugin_root,
                ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
                env,
                {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
            )
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        assert server.trace_batches == []
        assert len(server.policy_requests) == policy_request_count
        diagnostics = _diagnostic_log_entries(home)
        assert diagnostics[-1]["status"] == "trace_upload_skipped"
        assert diagnostics[-1]["reason"] == "ledger_lock_busy"
        diagnostic_log = _diagnostic_log_path(home)
        assert diagnostic_log.stat().st_mode & 0o777 == 0o600
        assert "plihost_localcredential" not in diagnostic_log.read_text()
    finally:
        server.stop()


def test_zero_deadline_collect_baselines_fully_and_uploads_hook_subject(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        codex_home = home / ".codex"
        transcript_path = tmp_path / "codex-session.jsonl"
        first_record = b'{"kind":"session_start","message":"baseline"}\n'
        transcript_path.write_bytes(first_record)
        idle_path = codex_home / "sessions/idle-history.jsonl"
        idle_path.parent.mkdir(parents=True)
        idle_path.write_bytes(b'{"kind":"idle_history"}\n')
        stale = time.time() - (13 * 60 * 60)
        os.utime(idle_path, (stale, stale))
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
            "PROMPTLESS_HOST_RUNTIME_COLLECT_DEADLINE_SECONDS": "0",
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        # A zero deadline truncates the idle scan, but the first-run baseline must
        # still inventory the full tree: a partial baseline would replay every
        # missed file from offset zero later as a surprise backfill.
        assert server.trace_batches == []
        ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert len(sources) == 2
        diagnostics = _diagnostic_log_entries(home)
        assert diagnostics[-1]["status"] == "trace_upload_baselined"
        assert diagnostics[-1]["source_count"] == 2

        second_record = b'{"kind":"stop","message":"upload"}\n'
        transcript_path.write_bytes(first_record + second_record)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        # The hook's own transcript still uploads with no budget at all: subject
        # paths are unmetered and the first pending batch is always sent.
        assert len(server.trace_batches) == 1
        chunk = _json_mapping(_json_list(server.trace_batches[0]["chunks"], "batch.chunks")[0], "batch.chunks[0]")
        assert gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content"))) == second_record
        diagnostics = _diagnostic_log_entries(home)
        assert diagnostics[-1]["status"] == "trace_upload_partial"
        assert diagnostics[-1]["reason"] == "collection_deadline_exceeded"
        assert diagnostics[-1]["batch_count"] == 1
    finally:
        server.stop()


def test_deadline_truncation_keeps_acked_progress_and_resumes(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        transcript_path = tmp_path / "codex-session.jsonl"
        first_record = b'{"kind":"session_start","message":"baseline"}\n'
        transcript_path.write_bytes(first_record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }
        zero_deadline_env = dict(env, PROMPTLESS_HOST_RUNTIME_COLLECT_DEADLINE_SECONDS="0")

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        # Multi-batch pending content: ~13 MiB so batch one fills at the 10 MiB cap.
        filler_record = b'{"kind":"note","payload":"' + b"x" * 65_000 + b'"}\n'
        appended_body = filler_record * 200
        transcript_path.write_bytes(first_record + appended_body)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            zero_deadline_env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        # The guaranteed first batch lands and is acked; the deadline stops the rest.
        assert len(server.trace_batches) == 1
        diagnostics = _diagnostic_log_entries(home)
        assert diagnostics[-1]["status"] == "trace_upload_partial"
        assert diagnostics[-1]["reason"] == "collection_deadline_exceeded"
        assert diagnostics[-1]["batch_count"] == 1
        truncated_ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        truncated_source = _json_mapping(
            next(iter(_json_mapping(truncated_ledger["sources"], "ledger.sources").values())), "ledger.sources[0]"
        )
        acked_offset = _json_int(truncated_source["end_offset"], "ledger.sources[0].end_offset")
        assert len(first_record) < acked_offset < transcript_path.stat().st_size

        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "codex_session_1", "transcript_path": str(transcript_path)},
        )

        # The next collect resumes from the acked watermark and drains the rest.
        diagnostics = _diagnostic_log_entries(home)
        assert diagnostics[-1]["status"] == "trace_upload_complete"
        drained_ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        drained_source = _json_mapping(
            next(iter(_json_mapping(drained_ledger["sources"], "ledger.sources").values())), "ledger.sources[0]"
        )
        assert drained_source["end_offset"] == transcript_path.stat().st_size
        uploaded_chunks = [
            _json_mapping(chunk, "chunk")
            for batch in server.trace_batches
            for chunk in _json_list(_json_mapping(batch, "batch")["chunks"], "batch.chunks")
        ]
        uploaded_chunks.sort(key=lambda chunk: _json_int(chunk["start_offset"], "chunk.start_offset"))
        reassembled = b"".join(
            gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content")))
            for chunk in uploaded_chunks
        )
        assert reassembled == appended_body
    finally:
        server.stop()


def test_truncated_empty_scan_still_reaches_first_run_baseline(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        codex_home = home / ".codex"
        idle_path = codex_home / "sessions/idle-history.jsonl"
        idle_path.parent.mkdir(parents=True)
        idle_path.write_bytes(b'{"kind":"idle_history"}\n')
        stale = time.time() - (13 * 60 * 60)
        os.utime(idle_path, (stale, stale))
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
            "PROMPTLESS_HOST_RUNTIME_COLLECT_DEADLINE_SECONDS": "0",
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        # No transcript path on hook stdin plus a zero budget: the truncated empty
        # scan must not return no_sources before the ledger exists, or the first
        # baseline never happens and later terminal hooks upload the pre-enrollment
        # tree from offset 0.
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {},
        )

        assert server.trace_batches == []
        ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert len(sources) == 1
        baselined_source = _json_mapping(next(iter(sources.values())), "ledger.sources[0]")
        assert baselined_source["end_offset"] == idle_path.stat().st_size
        diagnostics = _diagnostic_log_entries(home)
        assert diagnostics[-1]["status"] == "trace_upload_baselined"
        assert diagnostics[-1]["source_count"] == 1
    finally:
        server.stop()


# Codex validates SessionStart hook *stdout* against a strict schema (serde deny_unknown_fields) and
# rejects any key outside continue/stopReason/systemMessage/suppressOutput/hookSpecificOutput with
# "hook returned invalid session start JSON output". The bootstrap therefore keeps Codex stdout to
# the user-facing systemMessage alone (empty when silent) and writes its diagnostic status object —
# the status/host/needs_restart/reason fields Codex would reject — to stderr, which is not parsed.
# Claude also accepts terminalSequence, so Claude-only runs may include it to trigger a visible
# terminal notification when the TUI does not render the hook's systemMessage prominently.
CODEX_SAFE_STDOUT_KEYS = frozenset({"systemMessage"})
CLAUDE_SAFE_STDOUT_KEYS = frozenset({"systemMessage", "terminalSequence"})


def _bootstrap_diagnostics(stderr: str) -> list[dict[str, JsonValue]]:
    return [
        _json_mapping(validate_json_value(json.loads(line), "bootstrap diagnostic"), "bootstrap diagnostic")
        for line in stderr.splitlines()
        if line.strip()
    ]


def _diagnostic_log_entries(home: Path) -> list[dict[str, JsonValue]]:
    return [
        _json_mapping(validate_json_value(json.loads(line), "runtime diagnostic log entry"), "runtime diagnostic log")
        for line in _diagnostic_log_path(home).read_text().splitlines()
        if line.strip()
    ]


def _parse_session_start_streams(stdout: str, stderr: str) -> dict[str, JsonValue]:
    """Assert the SessionStart hook stream split and return the final stderr diagnostic object.

    stderr carries full diagnostics (status/host/...) as JSONL; stdout carries the selected
    schema-safe systemMessage/terminalSequence object and stays empty when there is no user-facing
    message.
    """

    diagnostics = _bootstrap_diagnostics(stderr)
    assert diagnostics, "bootstrap emitted no diagnostic status"
    diagnostic = diagnostics[-1]
    stdout_text = stdout.strip()
    if stdout_text:
        control = _json_mapping(validate_json_value(json.loads(stdout_text), "bootstrap stdout"), "bootstrap stdout")
        control_source = next(
            (emitted for emitted in diagnostics if all(emitted.get(key) == value for key, value in control.items())),
            None,
        )
        assert control_source is not None, "stdout control output did not match any diagnostic"
        allowed_keys = CLAUDE_SAFE_STDOUT_KEYS if control_source.get("host") == "claude" else CODEX_SAFE_STDOUT_KEYS
        assert set(control) <= allowed_keys, f"stdout leaks non-schema keys: {sorted(set(control))}"
    else:
        for emitted in diagnostics:
            assert "systemMessage" not in emitted
            assert "terminalSequence" not in emitted
    return diagnostic


def _assert_session_start_streams(stdout: str, stderr: str, expected_status: str) -> dict[str, JsonValue]:
    """Validate the stream split and pin the diagnostic status."""

    diagnostic = _parse_session_start_streams(stdout, stderr)
    assert diagnostic["status"] == expected_status
    return diagnostic


def _run_bootstrap(
    plugin_root: Path,
    host: str,
    env: dict[str, str],
    *,
    expected_status: str = "configured",
) -> tuple[dict[str, JsonValue], subprocess.CompletedProcess[str]]:
    result = subprocess.run(
        [str(plugin_root / "runtime" / HOST_RUNTIME_BIN), "ensure", "--host", host],
        env=_clean_env(**env),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "plihost_localcredential" not in result.stdout
    assert "plihost_localcredential" not in result.stderr
    assert "plihenroll_devicecode" not in result.stdout
    assert "plihenroll_devicecode" not in result.stderr
    payload = _assert_session_start_streams(result.stdout, result.stderr, expected_status)
    return payload, result


def _run_runtime_json(
    plugin_root: Path,
    args: list[str],
    env: dict[str, str],
    *,
    expected_returncode: int = 0,
) -> tuple[dict[str, JsonValue], subprocess.CompletedProcess[str]]:
    result = subprocess.run(
        [str(plugin_root / "runtime" / HOST_RUNTIME_BIN), *args],
        env=_clean_env(**env),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected_returncode
    assert "plihost_localcredential" not in result.stdout
    assert "plihost_localcredential" not in result.stderr
    assert "plihenroll_devicecode" not in result.stdout
    assert "plihenroll_devicecode" not in result.stderr
    assert result.stderr == ""
    payload = validate_json_value(json.loads(result.stdout), "runtime command stdout")
    return _json_mapping(payload, "runtime command stdout"), result


def _run_collect(
    plugin_root: Path,
    args: list[str],
    env: dict[str, str],
    stdin_payload: dict[str, JsonValue],
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(plugin_root / "runtime" / HOST_RUNTIME_BIN), *args],
        env=_clean_env(**env),
        input=json.dumps(stdin_payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "plihost_localcredential" not in result.stdout
    assert "plihost_localcredential" not in result.stderr
    assert "plihenroll_devicecode" not in result.stdout
    assert "plihenroll_devicecode" not in result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    return result


def _start_bootstrap(plugin_root: Path, host: str, env: dict[str, str]) -> subprocess.Popen[str]:
    process_env = _clean_env()
    process_env.update(env)
    if "PROMPTLESS_WORKER_BASE_URL" in process_env and "PROMPTLESS_DASHBOARD_BASE_URL" not in process_env:
        process_env["PROMPTLESS_DASHBOARD_BASE_URL"] = process_env["PROMPTLESS_WORKER_BASE_URL"]
    return subprocess.Popen(
        [str(plugin_root / "runtime" / HOST_RUNTIME_BIN), "ensure", "--host", host],
        env=process_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _read_bootstrap_process(
    process: subprocess.Popen[str],
    *,
    expected_status: str = "configured",
) -> dict[str, JsonValue]:
    payload = _read_any_bootstrap_status(process)
    assert payload["status"] == expected_status
    return payload


def _read_any_bootstrap_status(process: subprocess.Popen[str]) -> dict[str, JsonValue]:
    """Drain a background bootstrap and return its emitted payload without pinning the status.

    Used for concurrent runs where which process leads enrollment (and so its terminal status)
    depends on scheduling.
    """
    try:
        stdout, stderr = process.communicate(timeout=80)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        pytest.fail(f"bootstrap timed out with stdout={stdout!r} stderr={stderr!r}")
    assert process.returncode == 0
    assert "plihost_localcredential" not in stdout
    assert "plihost_localcredential" not in stderr
    assert "plihenroll_devicecode" not in stdout
    assert "plihenroll_devicecode" not in stderr
    return _parse_session_start_streams(stdout, stderr)


def _clone_plugin_with_identity(source_plugin: Path, destination: Path, *, plugin_id: str, package_id: str) -> Path:
    """Copy a built plugin and rewrite its managed-runtime identity to simulate a second hub plugin."""
    shutil.copytree(source_plugin, destination)
    manifest_path = destination / "hub.managed-runtimes.json"
    manifest = _json_mapping(validate_json_value(json.loads(manifest_path.read_text()), "manifest"), "manifest")
    runtimes = _json_list(manifest["managed_runtimes"], "managed_runtimes")
    runtime = _json_mapping(runtimes[0], "managed_runtimes[0]")
    runtime["plugin_id"] = plugin_id
    runtime["package_id"] = package_id
    manifest_path.write_text(json.dumps(manifest))
    return destination


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES": "1",
        "PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER": "0",
    }
    env.update(overrides)
    if "PROMPTLESS_WORKER_BASE_URL" in env and "PROMPTLESS_DASHBOARD_BASE_URL" not in env:
        env["PROMPTLESS_DASHBOARD_BASE_URL"] = env["PROMPTLESS_WORKER_BASE_URL"]
    return env


def _claude_desktop_audit_path(home: Path, store_name: str, session_name: str) -> Path:
    if os.name == "nt":
        claude_base = home / "AppData/Roaming/Claude"
    elif sys.platform == "darwin":
        claude_base = home / "Library/Application Support/Claude"
    else:
        claude_base = home / ".config/Claude"
    return claude_base / store_name / session_name / "audit.jsonl"


def _write_shell_script(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _write_python_forwarder(path: Path) -> None:
    _write_shell_script(path, f'exec {shlex.quote(sys.executable)} "$@"')


def _credential_cache_key(*, worker_base_url: str, target: str) -> str:
    cache_material = {
        "deployment_instance_id": "worker-local-1",
        "target": target,
        "worker_base_url": worker_base_url,
    }
    return hashlib.sha256(json.dumps(cache_material, sort_keys=True).encode()).hexdigest()


def _async_urlopen_browser_command(path: Path) -> str:
    path.write_text(
        """
import subprocess
import sys

target_url = sys.argv[1]
subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import sys, urllib.error, urllib.request\\n"
        "try:\\n"
        "    urllib.request.urlopen(sys.argv[1], timeout=10).read()\\n"
        "except urllib.error.HTTPError:\\n"
        "    pass\\n",
        target_url,
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
""".lstrip()
    )
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))} %s"


def _json_mapping(value: JsonValue, field_path: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{field_path} must be a JSON object"
    return value


def _json_list(value: JsonValue, field_path: str) -> list[JsonValue]:
    assert isinstance(value, list), f"{field_path} must be a JSON array"
    return value


def _json_string(value: JsonValue, field_path: str) -> str:
    assert isinstance(value, str), f"{field_path} must be a JSON string"
    return value


def _json_int(value: JsonValue, field_path: str) -> int:
    assert isinstance(value, int) and not isinstance(value, bool), f"{field_path} must be a JSON integer"
    return value


def _callback_state(callback_url_value: JsonValue, field_path: str) -> str:
    callback_url = _json_string(callback_url_value, field_path)
    state_values = parse_qs(urlsplit(callback_url).query).get("state")
    assert state_values is not None and len(state_values) == 1 and state_values[0] != ""
    return state_values[0]


def _url_with_query_params(url: str, params: dict[str, JsonValue]) -> str:
    parsed = urlsplit(url)
    query_pairs: list[tuple[str, str]] = []
    for key, values in parse_qs(parsed.query, keep_blank_values=False).items():
        query_pairs.extend((key, value) for value in values)
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, str):
            query_pairs.append((key, value))
        elif isinstance(value, (int, float, bool)):
            query_pairs.append((key, str(value)))
        else:
            raise AssertionError(f"{key} must be a query scalar")
    return parsed._replace(query=urlencode(query_pairs)).geturl()


def _callback_url_with_state(callback_url: str, state: str) -> str:
    parsed = urlsplit(callback_url)
    query_pairs: list[tuple[str, str]] = []
    for key, values in parse_qs(parsed.query, keep_blank_values=False).items():
        if key == "state":
            continue
        query_pairs.extend((key, value) for value in values)
    query_pairs.append(("state", state))
    return parsed._replace(query=urlencode(query_pairs)).geturl()


def _write_native_hook_asset(hub_root: Path, hooks: dict[str, JsonValue]) -> None:
    hooks_path = hub_root / "assets/hooks/hooks.json"
    hooks_path.write_text(json.dumps(hooks))
    (hub_root / "assets/hooks/hooks.asset.yaml").write_text(
        "\n".join(
            [
                "id: hooks",
                "type: hook",
                "support:",
                "  codex:",
                "    mode: native",
                "  claude:",
                "    mode: native",
                "  cursor:",
                "    mode: unsupported",
                "    reason: hooks are only native for Codex and Claude",
                "  gemini:",
                "    mode: unsupported",
                "    reason: hooks are only native for Codex and Claude",
                "",
            ]
        )
    )
    (hub_root / "packages/core.yaml").write_text("id: core\nname: Core\nincludes:\n  - hook:hooks\n")


def _policy_with(**policy_updates: JsonValue) -> dict[str, JsonValue]:
    payload = _json_mapping(
        validate_json_value(json.loads(json.dumps(_signed_policy())), "signed policy fixture"),
        "signed policy fixture",
    )
    policy = _json_mapping(payload["policy"], "policy")
    policy.update(policy_updates)
    return payload


def _invalid_policy(case: str) -> dict[str, JsonValue]:
    now = dt.datetime.now(dt.timezone.utc)
    payload = _policy_with()
    policy = _json_mapping(payload["policy"], "policy")

    if case == "expired":
        policy["expires_at"] = (now - dt.timedelta(minutes=1)).isoformat()
    else:
        raise AssertionError(f"unhandled invalid policy case: {case}")
    return payload


def _session_response() -> dict[str, JsonValue]:
    return {
        "session_id": "11111111-1111-4111-8111-111111111111",
        "deployment_instance_id": "worker-local-1",
        "device_code": "plihenroll_devicecode",
        "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
        "poll_interval_seconds": 1,
    }


def _approved_poll_response(**updates: JsonValue) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "status": "approved",
        "host_credential": "plihost_localcredential",
        "credential_id": "22222222-2222-4222-8222-222222222222",
        "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
    }
    payload.update(updates)
    return payload


class _FakeWorkerServer:
    def __init__(
        self,
        *,
        policy: dict[str, JsonValue] | None = None,
        poll_response: dict[str, JsonValue] | None = None,
        post_response: dict[str, JsonValue] | None = None,
        session_response: dict[str, JsonValue] | None = None,
        session_barrier_count: int = 0,
        callback_state_override: str | None = None,
        pending_approval_url_override: str | None = None,
        pending_approval_path: str = "/instruction-hub/enroll",
        unparsed_record_count: int = 0,
    ) -> None:
        self.check_ins: list[dict[str, JsonValue]] = []
        self.policy_requests: list[str] = []
        self.poll_requests: list[dict[str, JsonValue]] = []
        self.session_requests: list[dict[str, JsonValue]] = []
        self.trace_batches: list[dict[str, JsonValue]] = []
        self._session_condition = threading.Condition()
        _FakeWorkerHandler.check_ins = self.check_ins
        _FakeWorkerHandler.policy_requests = self.policy_requests
        _FakeWorkerHandler.poll_requests = self.poll_requests
        _FakeWorkerHandler.session_requests = self.session_requests
        _FakeWorkerHandler.trace_batches = self.trace_batches
        _FakeWorkerHandler.policy_response = policy or _signed_policy()
        _FakeWorkerHandler.poll_response = poll_response
        _FakeWorkerHandler.post_response = post_response
        _FakeWorkerHandler.session_response = session_response
        _FakeWorkerHandler.session_barrier_count = session_barrier_count
        _FakeWorkerHandler.session_condition = self._session_condition
        _FakeWorkerHandler.callback_state_override = callback_state_override
        _FakeWorkerHandler.pending_approval_url_override = pending_approval_url_override
        _FakeWorkerHandler.pending_approval_path = pending_approval_path
        _FakeWorkerHandler.unparsed_record_count = unparsed_record_count
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWorkerHandler)
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _FakeWorkerHandler(BaseHTTPRequestHandler):
    check_ins: ClassVar[list[dict[str, JsonValue]]] = []
    policy_requests: ClassVar[list[str]] = []
    poll_requests: ClassVar[list[dict[str, JsonValue]]] = []
    trace_batches: ClassVar[list[dict[str, JsonValue]]] = []
    policy_response: ClassVar[dict[str, JsonValue]]
    poll_response: ClassVar[dict[str, JsonValue] | None]
    post_response: ClassVar[dict[str, JsonValue] | None]
    session_response: ClassVar[dict[str, JsonValue] | None]
    session_barrier_count: ClassVar[int] = 0
    session_condition: ClassVar[threading.Condition | None] = None
    session_requests: ClassVar[list[dict[str, JsonValue]]] = []
    callback_state_override: ClassVar[str | None] = None
    pending_approval_url_override: ClassVar[str | None] = None
    pending_approval_path: ClassVar[str] = "/instruction-hub/enroll"
    unparsed_record_count: ClassVar[int] = 0

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            self._write_json(
                {
                    "status": "ok",
                    "deployment_instance_id": "worker-local-1",
                    "worker_version": "0.1.0-test",
                }
            )
            return
        if parsed.path == "/instruction-hub/enroll/start":
            payload = self._single_value_query_payload(parsed.query)
            callback_url = _json_string(payload.get("callback_url"), "callback_url")
            if callback_url is None:
                self.send_response(400)
                self.end_headers()
                return
            self._record_session_request(payload)
            session_response = self._session_response_payload()
            approval_params = {"callback_url": callback_url, **session_response}
            hosted_approval_url = self.pending_approval_url_override or (
                f"{self._base_url()}{self.pending_approval_path}?{urlencode(approval_params)}"
            )
            if payload.get("pending_callback") == "1":
                pending_params = {
                    "status": "pending",
                    "approval_url": hosted_approval_url,
                    **session_response,
                }
                self._redirect(_url_with_query_params(callback_url, pending_params))
                return
            self._redirect(hosted_approval_url)
            return
        if parsed.path == "/instruction-hub/enroll":
            payload = self._single_value_query_payload(parsed.query)
            callback_url = _json_string(payload.pop("callback_url", None), "callback_url")
            if callback_url is None:
                self.send_response(400)
                self.end_headers()
                return
            if self.callback_state_override is not None:
                callback_url = _callback_url_with_state(callback_url, self.callback_state_override)
            self._redirect(_url_with_query_params(callback_url, {"status": "approved", **payload}))
            return
        target = parse_qs(parsed.query).get("target")
        if (
            parsed.path != "/v0/host-enrollment/policy"
            or target not in (["codex"], ["claude"], ["claude-desktop"])
            or self.headers.get("Authorization") != "Bearer plihost_localcredential"
        ):
            self.send_response(401)
            self.end_headers()
            return
        self.policy_requests.append(self.path)
        self._write_json(self.policy_response)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if self.path == "/v1/instruction-hub/host-enrollments/sessions/11111111-1111-4111-8111-111111111111/poll":
            payload = self._read_json_request("session poll request")
            if payload.get("device_code") != "plihenroll_devicecode":
                self.send_response(401)
                self.end_headers()
                return
            self.poll_requests.append(payload)
            self._write_json(dict(self.poll_response or _approved_poll_response()))
            return
        if parsed.path == "/v0/traces/batches":
            target = parse_qs(parsed.query).get("target")
            if (
                target not in (["codex"], ["claude"], ["claude-desktop"])
                or self.headers.get("Authorization") != "Bearer plihost_localcredential"
            ):
                self.send_response(401)
                self.end_headers()
                return
            payload = self._read_json_request("trace batch request")
            self.trace_batches.append(payload)
            chunks = _json_list(payload["chunks"], "trace batch chunks")
            acknowledged_ranges: list[dict[str, JsonValue]] = []
            raw_artifact_count = 0
            skipped_record_count = 0
            for chunk_value in chunks:
                chunk = _json_mapping(chunk_value, "trace batch chunk")
                if chunk["kind"] == "jsonl_range":
                    raw_artifact_count += 1
                elif chunk["kind"] == "oversized_record":
                    skipped_record_count += 1
                acknowledged_ranges.append(
                    {
                        "kind": chunk["kind"],
                        "source_path_hash": chunk["source_path_hash"],
                        "start_offset": chunk["start_offset"],
                        "end_offset": chunk["end_offset"],
                        "content_sha256": chunk["content_sha256"],
                    }
                )
            self._write_json(
                {
                    "accepted": True,
                    "batch_id": payload["batch_id"],
                    "policy_version": payload["policy_version"],
                    "raw_artifact_count": raw_artifact_count,
                    "skipped_record_count": skipped_record_count,
                    "acknowledged_ranges": acknowledged_ranges,
                    "trace_count": raw_artifact_count,
                    "event_count": raw_artifact_count,
                    "unparsed_record_count": self.unparsed_record_count,
                }
            )
            return
        if (
            self.path != "/v0/host-enrollment/check-ins"
            or self.headers.get("Authorization") != "Bearer plihost_localcredential"
        ):
            self.send_response(401)
            self.end_headers()
            return
        payload = self._read_json_request("check-in request")
        self.check_ins.append(payload)
        self._write_json(self.post_response or {"accepted": True, "policy_version": 1})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write_json(self, payload: dict[str, JsonValue], *, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def _session_response_payload(self) -> dict[str, JsonValue]:
        payload = dict(self.session_response or _session_response())
        payload.setdefault(
            "poll_url",
            f"{self._base_url()}/v1/instruction-hub/host-enrollments/sessions/11111111-1111-4111-8111-111111111111/poll",
        )
        return payload

    def _single_value_query_payload(self, query: str) -> dict[str, JsonValue]:
        parsed_query = parse_qs(query, keep_blank_values=False)
        payload: dict[str, JsonValue] = {}
        for key, values in parsed_query.items():
            if len(values) == 1:
                payload[key] = values[0]
        return payload

    def _read_json_request(self, label: str) -> dict[str, JsonValue]:
        length = int(self.headers["Content-Length"])
        return _json_mapping(
            validate_json_value(json.loads(self.rfile.read(length)), label),
            label,
        )

    def _record_session_request(self, payload: dict[str, JsonValue]) -> None:
        condition = self.session_condition
        if condition is None or self.session_barrier_count <= 1:
            self.session_requests.append(payload)
            return
        with condition:
            self.session_requests.append(payload)
            if len(self.session_requests) >= self.session_barrier_count:
                condition.notify_all()
                return
            condition.wait_for(lambda: len(self.session_requests) >= self.session_barrier_count, timeout=10)


def _signed_policy(*, enabled_hosts: list[str] | None = None) -> dict[str, JsonValue]:
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "policy": {
            "schema_version": 1,
            "org_id": "org_test",
            "deployment_id": "worker-local-1",
            "policy_version": 1,
            "issued_at": now.isoformat(),
            "expires_at": (now + dt.timedelta(days=7)).isoformat(),
            "collector": {
                "otlp_http_logs_endpoint": "http://127.0.0.1:4318/v1/logs",
                "otlp_http_traces_endpoint": "http://127.0.0.1:4318/v1/traces",
                "otlp_http_metrics_endpoint": "http://127.0.0.1:4318/v1/metrics",
                "otlp_grpc_endpoint": "http://127.0.0.1:4317",
                "headers": {"Authorization": "Bearer otlp-token"},
                "tls": None,
            },
            "enabled_hosts": enabled_hosts or ["codex", "claude"],
            "required_bootstrap_version": "0.2.0",
        },
        "signature": "hmac-sha256-v1:test",
        "signed_at": now.isoformat(),
    }
