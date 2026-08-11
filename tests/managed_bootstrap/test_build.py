from __future__ import annotations

import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

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
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.contracts import (
    MAX_DIAGNOSTIC_LOG_BYTES,
)

from .helpers import (
    HOST_RUNTIME_BIN,
    HOST_RUNTIME_PACKAGE,
    _assert_hook_argv,
    _assert_hook_system_message,
    _assert_no_promptless_directory,
    _clean_env,
    _diagnostic_log_entries,
    _diagnostic_log_path,
    _json_int,
    _json_mapping,
    _json_string,
    _last_status_path,
    _run_runtime_json,
    _runtime_bundle_sha256,
    _write_native_hook_asset,
    _write_python_forwarder,
    _write_shell_script,
)


def test_build_injects_managed_bootstrap_runtime(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")

    build_hub(hub_root)

    for target in ("codex", "claude"):
        plugin_root = hub_root / "dist" / target / "pig"
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
                    f"const collectArgs = [runtime, 'collect', '--host', 'claude', '--lifecycle', {lifecycle!r}, '--detach', '--quiet'];"
                    in hook_script
                )
                assert "'claude-desktop'" not in hook_script
            else:
                hook_command = hook["command"]
                assert hook_command.startswith("sh -c '")
                assert "root=${PLUGIN_ROOT:-}" in hook_command
                assert "root_parent=${root%/*}" in hook_command
                assert f'"$runtime" collect --host codex --lifecycle {lifecycle} --detach --quiet' in hook_command
                assert "ensure --host" not in hook_command
                assert "--baseline" not in hook_command

        stub_root = tmp_path / f"{target}-stub-plugin"
        stub_runtime = stub_root / "runtime" / HOST_RUNTIME_BIN
        stub_runtime_package = stub_root / "runtime" / HOST_RUNTIME_PACKAGE
        stub_call_log = tmp_path / f"{target}-stub-calls.jsonl"
        stub_attempt_log = tmp_path / f"{target}-stub-attempts.jsonl"
        stub_started_log = tmp_path / f"{target}-stub-started.json"
        stub_stdin_log = tmp_path / f"{target}-stub-stdin.jsonl"
        stub_runtime.parent.mkdir(parents=True)
        assert f"{HOST_RUNTIME_PACKAGE}/contracts.py" in HOST_RUNTIME_BUNDLE_RELATIVE_PATHS
        for relative_path in HOST_RUNTIME_BUNDLE_RELATIVE_PATHS:
            if relative_path == HOST_RUNTIME_BIN:
                continue
            stub_path = stub_root / "runtime" / relative_path
            stub_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(plugin_root / "runtime" / relative_path, stub_path)
        stub_runtime.write_text(
            "import json, os, sys, time\n"
            "if sys.argv[1:2] == ['collect'] and ('--detach' in sys.argv or '--supervised' in sys.argv):\n"
            "    from promptless_host_runtime.cli import main\n"
            "    sys.exit(main(sys.argv[1:]))\n"
            "host = sys.argv[sys.argv.index('--host') + 1] if '--host' in sys.argv else ''\n"
            "baseline_guard = os.environ['PROMPTLESS_STUB_CALL_LOG'] + '.' + host + '.baseline-pending'\n"
            "attempt_log_path = os.environ.get('PROMPTLESS_STUB_ATTEMPT_LOG')\n"
            "if attempt_log_path:\n"
            "    with open(attempt_log_path, 'a') as attempt_log:\n"
            "        attempt_log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "ensure_failed = sys.argv[1:2] == ['ensure'] and os.environ.get('PROMPTLESS_STUB_ENSURE_FAILURE_HOST') == host\n"
            "if sys.argv[1:2] == ['ensure'] and '--prepare-baseline' in sys.argv and not ensure_failed:\n"
            "    time.sleep(float(os.environ.get('PROMPTLESS_STUB_ENSURE_GUARD_DELAY_SECONDS', '0')))\n"
            "    with open(baseline_guard, 'w') as guard:\n"
            "        guard.write('pending\\n')\n"
            "if sys.argv[1:2] == ['collect'] and '--baseline' in sys.argv and not os.path.exists(baseline_guard):\n"
            "    sys.exit(96)\n"
            "if sys.argv[1:2] == ['collect']:\n"
            "    started_log_path = os.environ.get('PROMPTLESS_STUB_STARTED_LOG')\n"
            "    if started_log_path:\n"
            "        started_temp_path = started_log_path + '.tmp'\n"
            "        with open(started_temp_path, 'w') as started_log:\n"
            "            started_log.write(json.dumps({'argv': sys.argv[1:], 'process_group_id': os.getpgrp()}) + '\\n')\n"
            "        os.replace(started_temp_path, started_log_path)\n"
            "    release_path = os.environ.get('PROMPTLESS_STUB_COLLECT_RELEASE_FILE')\n"
            "    while release_path and not os.path.exists(release_path):\n"
            "        time.sleep(0.01)\n"
            "    time.sleep(float(os.environ.get('PROMPTLESS_STUB_COLLECT_DELAY_SECONDS', '0')))\n"
            "with open(os.environ['PROMPTLESS_STUB_CALL_LOG'], 'a') as call_log:\n"
            "    call_log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:2] == ['ensure']:\n"
            "    print(json.dumps({'argv': sys.argv[1:]}))\n"
            "    if ensure_failed:\n"
            "        sys.exit(1)\n"
            "elif sys.argv[1:2] == ['collect'] and ('--baseline' not in sys.argv or os.environ.get('PROMPTLESS_STUB_CAPTURE_BASELINE_STDIN') == '1'):\n"
            "    with open(os.environ['PROMPTLESS_STUB_STDIN_LOG'], 'a') as stdin_log:\n"
            "        stdin_log.write(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}) + '\\n')\n"
            "if sys.argv[1:2] == ['collect']:\n"
            "    sys.exit(int(os.environ.get('PROMPTLESS_STUB_COLLECT_EXIT_CODE', '0')))\n"
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
            stub_attempt_log.unlink(missing_ok=True)
            stub_started_log.unlink(missing_ok=True)
            stub_stdin_log.unlink(missing_ok=True)
            for guarded_host in ("codex", "claude", "claude-desktop"):
                Path(f"{stub_call_log}.{guarded_host}.baseline-pending").unlink(missing_ok=True)

        def stub_calls() -> list[list[str]]:
            if not stub_call_log.exists():
                return []
            return [json.loads(line) for line in stub_call_log.read_text().splitlines()]

        def stub_attempts() -> list[list[str]]:
            if not stub_attempt_log.exists():
                return []
            return [json.loads(line) for line in stub_attempt_log.read_text().splitlines()]

        def stub_stdin_entries() -> list[dict[str, JsonValue]]:
            if not stub_stdin_log.exists():
                return []
            return [json.loads(line) for line in stub_stdin_log.read_text().splitlines()]

        def assert_calls_eventually(expected_calls: list[list[str]]) -> None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                actual_calls = stub_calls()
                if sorted(actual_calls) == sorted(expected_calls):
                    return
                time.sleep(0.01)
            assert sorted(stub_calls()) == sorted(expected_calls)

        def assert_stdin_entries_eventually(expected_entries: list[dict[str, JsonValue]]) -> None:
            expected_entries = sorted(expected_entries, key=lambda entry: json.dumps(entry, sort_keys=True))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                actual_entries = sorted(stub_stdin_entries(), key=lambda entry: json.dumps(entry, sort_keys=True))
                if actual_entries == expected_entries:
                    return
                time.sleep(0.01)
            assert sorted(stub_stdin_entries(), key=lambda entry: json.dumps(entry, sort_keys=True)) == expected_entries

        def last_status_eventually(home: Path) -> dict[str, JsonValue]:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    value = validate_json_value(json.loads(_last_status_path(home).read_text()), "last status")
                except FileNotFoundError:
                    time.sleep(0.01)
                    continue
                return _json_mapping(value, "last status")
            return _json_mapping(
                validate_json_value(json.loads(_last_status_path(home).read_text()), "last status"),
                "last status",
            )

        def assert_startup_calls() -> None:
            expected_calls = [
                ["ensure", "--host", target, "--prepare-baseline"],
                ["collect", "--host", target, "--lifecycle", "session_start", "--baseline", "--quiet"],
            ]
            if target == "claude":
                expected_calls.extend(
                    [
                        ["ensure", "--host", "claude-desktop", "--if-sources", "--prepare-baseline"],
                        [
                            "collect",
                            "--host",
                            "claude-desktop",
                            "--lifecycle",
                            "session_start",
                            "--baseline",
                            "--quiet",
                        ],
                    ]
                )
            assert_calls_eventually(expected_calls)

        def assert_terminal_calls(lifecycle: str) -> None:
            assert_calls_eventually([["collect", "--host", target, "--lifecycle", lifecycle, "--quiet"]])

        def assert_quiet_success(result: subprocess.CompletedProcess[str]) -> None:
            assert result.returncode == 0
            assert result.stdout == ""
            assert result.stderr == ""

        def startup_hook_result(
            *,
            root: Path,
            home: Path,
            input_text: str | None = None,
            collect_delay_seconds: float = 0,
            ensure_guard_delay_seconds: float = 0,
            ensure_failure_host: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            env_vars = {
                "HOME": str(home),
                "PROMPTLESS_STUB_CALL_LOG": str(stub_call_log),
                "PROMPTLESS_STUB_ATTEMPT_LOG": str(stub_attempt_log),
                "PROMPTLESS_STUB_STDIN_LOG": str(stub_stdin_log),
                "PROMPTLESS_STUB_COLLECT_DELAY_SECONDS": str(collect_delay_seconds),
                "PROMPTLESS_STUB_ENSURE_GUARD_DELAY_SECONDS": str(ensure_guard_delay_seconds),
            }
            if ensure_failure_host is not None:
                env_vars["PROMPTLESS_STUB_ENSURE_FAILURE_HOST"] = ensure_failure_host
            if input_text is not None:
                env_vars["PROMPTLESS_STUB_CAPTURE_BASELINE_STDIN"] = "1"
            if target == "claude":
                env_vars["CLAUDE_PLUGIN_ROOT"] = str(root)
                return subprocess.run(
                    [session_start_hook["command"], *session_start_hook["args"]],
                    env=_clean_env(**env_vars),
                    text=True,
                    input=input_text,
                    capture_output=True,
                    check=False,
                )
            env_vars["PLUGIN_ROOT"] = str(root)
            return subprocess.run(
                session_start_hook["command"],
                shell=True,
                env=_clean_env(**env_vars),
                text=True,
                capture_output=True,
                check=False,
            )

        def terminal_hook_result(
            event_name: str,
            *,
            root: Path | None,
            home: Path,
            input_text: str | None = None,
            collect_delay_seconds: float = 0,
            collect_exit_code: int = 0,
            collect_release_file: Path | None = None,
        ) -> subprocess.CompletedProcess[str]:
            hook = hook_events[event_name][0]["hooks"][0]
            env_vars = {
                "HOME": str(home),
                "PROMPTLESS_STUB_CALL_LOG": str(stub_call_log),
                "PROMPTLESS_STUB_ATTEMPT_LOG": str(stub_attempt_log),
                "PROMPTLESS_STUB_STDIN_LOG": str(stub_stdin_log),
                "PROMPTLESS_STUB_COLLECT_DELAY_SECONDS": str(collect_delay_seconds),
                "PROMPTLESS_STUB_COLLECT_EXIT_CODE": str(collect_exit_code),
            }
            if collect_release_file is not None:
                env_vars["PROMPTLESS_STUB_COLLECT_RELEASE_FILE"] = str(collect_release_file)
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
            assert "const { spawnSync } = require('child_process');" in hook_script
            assert "sys.version_info >= (3, 9)" in hook_script
            assert MISSING_PYTHON_MESSAGE in hook_script
            assert UNSUPPORTED_PYTHON_MESSAGE in hook_script
            assert "'collect'" in hook_script
            assert "'--baseline'" in hook_script
            assert "'--detach'" in hook_script
            assert "'--quiet'" in hook_script
            assert "'claude-desktop'" in hook_script
            assert "'--if-sources'" in hook_script
            assert "desktopEnsure" in hook_script
            assert "timeout: 5000" not in hook_script
            assert "['ignore', 'ignore', 'inherit']" in hook_script

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
            assert '"$runtime" ensure --host codex --prepare-baseline' in hook_command
            assert (
                '"$runtime" collect --host codex --lifecycle session_start --baseline --detach --quiet' in hook_command
            )
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

        if target == "codex" and os.name == "posix":
            reset_stub_calls()
            stop_hook_command = hook_events["Stop"][0]["hooks"][0]["command"]
            collect_release_file = tmp_path / "codex-collect-release"
            stdin_payload = json.dumps(
                {
                    "session_id": "codex_session_1",
                    "transcript_path": str(tmp_path / "codex-session.jsonl"),
                }
            )
            hook_process: subprocess.Popen[str] | None = None
            try:
                hook_process = subprocess.Popen(
                    stop_hook_command,
                    shell=True,
                    env=_clean_env(
                        HOME=str(tmp_path / "codex-process-group-home"),
                        PLUGIN_ROOT=str(stub_root),
                        PROMPTLESS_STUB_CALL_LOG=str(stub_call_log),
                        PROMPTLESS_STUB_ATTEMPT_LOG=str(stub_attempt_log),
                        PROMPTLESS_STUB_STARTED_LOG=str(stub_started_log),
                        PROMPTLESS_STUB_STDIN_LOG=str(stub_stdin_log),
                        PROMPTLESS_STUB_COLLECT_RELEASE_FILE=str(collect_release_file),
                    ),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                stdout, stderr = hook_process.communicate(input=stdin_payload, timeout=5)
                assert hook_process.returncode == 0
                assert stdout == ""
                assert stderr == ""
                assert hook_process.pid != os.getpgrp()
                started_deadline = time.monotonic() + 5
                while not stub_started_log.exists() and time.monotonic() < started_deadline:
                    time.sleep(0.01)
                started_value = validate_json_value(json.loads(stub_started_log.read_text()), "started collector")
                started_collector = _json_mapping(started_value, "started collector")
                assert (
                    _json_int(started_collector["process_group_id"], "started collector process group")
                    != hook_process.pid
                )
                try:
                    os.killpg(hook_process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            finally:
                collect_release_file.touch()
                if hook_process is not None and hook_process.poll() is None:
                    hook_process.kill()
                    hook_process.communicate(timeout=5)
            assert_terminal_calls("stop")
            assert_stdin_entries_eventually(
                [
                    {
                        "argv": ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
                        "stdin": stdin_payload,
                    }
                ]
            )

        reset_stub_calls()
        started_at = time.monotonic()
        delayed_startup_collect = startup_hook_result(
            root=stub_root,
            home=tmp_path / f"{target}-delayed-startup-collect-home",
            collect_delay_seconds=3,
            ensure_guard_delay_seconds=0.3,
        )
        elapsed_seconds = time.monotonic() - started_at
        _assert_hook_argv(delayed_startup_collect, target)
        assert elapsed_seconds < 2
        assert Path(f"{stub_call_log}.{target}.baseline-pending").exists()
        if target == "claude":
            assert Path(f"{stub_call_log}.claude-desktop.baseline-pending").exists()
        assert_startup_calls()

        reset_stub_calls()
        failed_primary_ensure = startup_hook_result(
            root=stub_root,
            home=tmp_path / f"{target}-failed-primary-ensure-home",
            ensure_failure_host=target,
        )
        assert failed_primary_ensure.returncode == 1
        time.sleep(0.5)
        assert stub_calls() == [["ensure", "--host", target, "--prepare-baseline"]]
        assert stub_attempts() == [["ensure", "--host", target, "--prepare-baseline"]]

        if target == "claude":
            reset_stub_calls()
            failed_desktop_ensure = startup_hook_result(
                root=stub_root,
                home=tmp_path / "claude-failed-desktop-ensure-home",
                ensure_failure_host="claude-desktop",
            )
            _assert_hook_argv(failed_desktop_ensure, target)
            assert_calls_eventually(
                [
                    ["ensure", "--host", "claude", "--prepare-baseline"],
                    ["collect", "--host", "claude", "--lifecycle", "session_start", "--baseline", "--quiet"],
                    ["ensure", "--host", "claude-desktop", "--if-sources", "--prepare-baseline"],
                ]
            )
            time.sleep(0.5)
            assert sorted(stub_attempts()) == sorted(
                [
                    ["ensure", "--host", "claude", "--prepare-baseline"],
                    ["collect", "--host", "claude", "--lifecycle", "session_start", "--baseline", "--quiet"],
                    ["ensure", "--host", "claude-desktop", "--if-sources", "--prepare-baseline"],
                ]
            )

        if target == "claude":
            reset_stub_calls()
            stdin_payload = json.dumps(
                {
                    "session_id": "claude_session_1",
                    "transcript_path": str(tmp_path / "claude-session.jsonl"),
                }
            )
            session_start_result = startup_hook_result(
                root=stub_root,
                home=tmp_path / "claude-session-start-stdin-home",
                input_text=stdin_payload,
            )
            _assert_hook_argv(session_start_result, target)
            assert_startup_calls()
            assert_stdin_entries_eventually(
                [
                    {
                        "argv": [
                            "collect",
                            "--host",
                            "claude",
                            "--lifecycle",
                            "session_start",
                            "--baseline",
                            "--quiet",
                        ],
                        "stdin": stdin_payload,
                    },
                    {
                        "argv": [
                            "collect",
                            "--host",
                            "claude-desktop",
                            "--lifecycle",
                            "session_start",
                            "--baseline",
                            "--quiet",
                        ],
                        "stdin": "",
                    },
                ]
            )

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

        reset_stub_calls()
        started_at = time.monotonic()
        delayed_collect = terminal_hook_result(
            "Stop",
            root=stub_root,
            home=tmp_path / f"{target}-delayed-collect-home",
            collect_delay_seconds=2,
        )
        elapsed_seconds = time.monotonic() - started_at
        assert_quiet_success(delayed_collect)
        assert elapsed_seconds < 1
        assert_terminal_calls("stop")

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
            assert_stdin_entries_eventually(
                [
                    {
                        "argv": ["collect", "--host", "claude", "--lifecycle", "stop", "--quiet"],
                        "stdin": stdin_payload,
                    }
                ]
            )

        reset_stub_calls()
        hook_failure_home = tmp_path / f"{target}-hook-failed-collector-home"
        failed_hook = terminal_hook_result(
            "Stop",
            root=stub_root,
            home=hook_failure_home,
            collect_exit_code=7,
        )
        assert_quiet_success(failed_hook)
        hook_failure_status = last_status_eventually(hook_failure_home)
        assert hook_failure_status["reason"] == "collector_process_failed"
        assert hook_failure_status["exit_code"] == 7

        reset_stub_calls()
        failure_home = tmp_path / f"{target}-concurrent-failed-collector-home"
        diagnostic_log = _diagnostic_log_path(failure_home)
        diagnostic_log.parent.mkdir(parents=True)
        full_diagnostic_log = "x" * MAX_DIAGNOSTIC_LOG_BYTES
        diagnostic_log.write_text(full_diagnostic_log)
        diagnostic_log.chmod(0o600)
        diagnostic_lock = diagnostic_log.with_name(f"{diagnostic_log.name}.lock")
        diagnostic_lock_ready = tmp_path / f"{target}-diagnostic-lock-ready"
        diagnostic_lock_release = tmp_path / f"{target}-diagnostic-lock-release"
        lock_holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys,time\n"
                    "from pathlib import Path\n"
                    "from promptless_host_runtime.storage import _state_file_lock\n"
                    "with _state_file_lock(Path(sys.argv[1])):\n"
                    "    Path(sys.argv[2]).touch()\n"
                    "    while not Path(sys.argv[3]).exists():\n"
                    "        time.sleep(0.01)\n"
                ),
                str(diagnostic_log),
                str(diagnostic_lock_ready),
                str(diagnostic_lock_release),
            ],
            env={**os.environ, "PYTHONPATH": str(stub_root / "runtime")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        lock_deadline = time.monotonic() + 5
        while not diagnostic_lock_ready.exists() and time.monotonic() < lock_deadline:
            time.sleep(0.01)
        assert diagnostic_lock_ready.exists()
        collect_release_file = tmp_path / f"{target}-failed-collector-release"
        terminal_call = ["collect", "--host", target, "--lifecycle", "stop", "--quiet"]
        supervisor_command = [sys.executable, str(stub_runtime), *terminal_call, "--supervised"]
        supervisor_env = _clean_env(
            HOME=str(failure_home),
            PROMPTLESS_STUB_CALL_LOG=str(stub_call_log),
            PROMPTLESS_STUB_ATTEMPT_LOG=str(stub_attempt_log),
            PROMPTLESS_STUB_STDIN_LOG=str(stub_stdin_log),
            PROMPTLESS_STUB_COLLECT_EXIT_CODE="7",
            PROMPTLESS_STUB_COLLECT_RELEASE_FILE=str(collect_release_file),
        )
        supervisors: list[subprocess.Popen[str]] = []
        try:
            supervisors = [
                subprocess.Popen(
                    supervisor_command,
                    env=supervisor_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                for _ in range(2)
            ]
            attempt_deadline = time.monotonic() + 5
            while stub_attempts() != [terminal_call, terminal_call] and time.monotonic() < attempt_deadline:
                time.sleep(0.01)
            assert stub_attempts() == [terminal_call, terminal_call]
            collect_release_file.touch()
            assert_calls_eventually([terminal_call, terminal_call])
            for supervisor in supervisors:
                with pytest.raises(subprocess.TimeoutExpired):
                    supervisor.wait(timeout=0.5)
            assert not _last_status_path(failure_home).exists()
        finally:
            collect_release_file.touch()
            diagnostic_lock_release.touch()
            for supervisor in supervisors:
                try:
                    supervisor.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    supervisor.kill()
                    supervisor.wait(timeout=5)
            lock_holder.wait(timeout=5)
        assert [supervisor.returncode for supervisor in supervisors] == [7, 7]

        rotated_diagnostic_log = diagnostic_log.with_name(f"{diagnostic_log.name}.1")
        diagnostic_deadline = time.monotonic() + 5
        failure_diagnostics: list[dict[str, JsonValue]] = []
        while time.monotonic() < diagnostic_deadline:
            diagnostic_text = diagnostic_log.read_text() if diagnostic_log.exists() else ""
            diagnostic_lines = [line for line in diagnostic_text.splitlines() if line.strip()]
            if rotated_diagnostic_log.exists() and diagnostic_text.endswith("\n") and len(diagnostic_lines) == 2:
                failure_diagnostics = _diagnostic_log_entries(failure_home)
                break
            time.sleep(0.01)
        assert len(failure_diagnostics) == 2

        last_status = last_status_eventually(failure_home)
        assert last_status["status"] == "error"
        assert last_status["reason"] == "collector_process_failed"
        assert last_status["host"] == target
        assert last_status["exit_code"] == 7
        assert _json_string(last_status["emitted_at"], "last_status.emitted_at")
        assert _last_status_path(failure_home).stat().st_mode & 0o777 == 0o600

        assert rotated_diagnostic_log.read_text() == full_diagnostic_log
        assert all(diagnostic["reason"] == "collector_process_failed" for diagnostic in failure_diagnostics)
        assert all(diagnostic["host"] == target for diagnostic in failure_diagnostics)
        assert all(diagnostic["exit_code"] == 7 for diagnostic in failure_diagnostics)
        assert diagnostic_log.stat().st_mode & 0o777 == 0o600
        assert diagnostic_lock.is_file()

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

    codex_manifest = json.loads((hub_root / "dist/codex/pig/.codex-plugin/plugin.json").read_text())
    assert codex_manifest["hooks"] == "./hooks/hooks.json"

    for target in ("cursor", "gemini"):
        plugin_root = hub_root / "dist" / target / "pig"
        assert not (plugin_root / "runtime" / HOST_RUNTIME_BIN).exists()
        assert not (plugin_root / "runtime" / HOST_RUNTIME_PACKAGE).exists()
        assert not (plugin_root / "hub.managed-runtimes.json").exists()

    release_manifest = json.loads((hub_root / "hub.release.json").read_text())
    assert {runtime["target"] for runtime in release_manifest["managed_runtimes"]} == {"codex", "claude"}
    _assert_no_promptless_directory(hub_root)


def test_host_runtime_bundle_digest_tracks_runtime_files_only(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/pig"
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

    hooks = json.loads((hub_root / "dist/codex/pig/hooks/hooks.json").read_text())
    session_start = hooks["hooks"]["SessionStart"]
    assert session_start[0]["hooks"][0]["command"] == "existing-hook"
    assert f"runtime/{HOST_RUNTIME_BIN}" in session_start[1]["hooks"][0]["command"]


def test_build_leaves_customer_package_hook_unmanaged(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    _write_native_hook_asset(
        hub_root,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [{"type": "command", "command": "customer-hook"}],
                    }
                ]
            }
        },
        package_id="customer",
    )

    build_hub(hub_root)

    hooks = json.loads((hub_root / "dist/codex/customer/hooks/hooks.json").read_text())
    assert hooks["hooks"]["SessionStart"] == [
        {
            "matcher": "startup",
            "hooks": [{"type": "command", "command": "customer-hook"}],
        }
    ]
    assert not (hub_root / "dist/codex/customer/runtime").exists()
    assert not (hub_root / "dist/codex/customer/hub.managed-runtimes.json").exists()


def test_build_rejects_malformed_existing_hook_asset(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    _write_native_hook_asset(hub_root, {"hooks": []})

    with pytest.raises(InstructionHubError, match="field hooks must be a JSON object"):
        build_hub(hub_root)
