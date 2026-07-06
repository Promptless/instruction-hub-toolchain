from __future__ import annotations

import base64
import datetime as dt
import hashlib
import gzip
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, validate_json_value
from promptless_instruction_hub.managed_runtime import (
    MISSING_PYTHON_MESSAGE,
    MISSING_RUNTIME_FILE_MESSAGE,
    MISSING_RUNTIME_ROOT_MESSAGE,
    UNSUPPORTED_PYTHON_MESSAGE,
)

HOST_RUNTIME_BIN = "promptless-host-runtime"
HOST_STATE_REL_PATH = Path(".promptless/instruction-hub/host-enrollment-state.json")
LAST_STATUS_REL_PATH = Path(".promptless/instruction-hub/last-bootstrap-status.json")
DIAGNOSTIC_LOG_REL_PATH = Path(".promptless/instruction-hub/host-runtime-diagnostics.jsonl")
INTERNAL_WELCOME_SHOWN_AT_KEY = "internal_promptless_welcome_shown_at"
INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY = "internal_promptless_welcome_shown_at_by_version"
BROWSER_ENROLLMENT_MESSAGE = (
    "Promptless Instruction Governance telemetry is starting browser-based enrollment. "
    "Approve the Promptless browser tab to continue."
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
        bootstrap_path = plugin_root / "bin" / HOST_RUNTIME_BIN
        assert bootstrap_path.exists()
        assert os.access(bootstrap_path, os.X_OK)
        hooks = json.loads((plugin_root / "hooks/hooks.json").read_text())
        hook_events = hooks["hooks"]
        expected_events = ("SessionStart", "Stop", "SubagentStop")
        if target == "claude":
            expected_events = ("SessionStart", "Stop", "SessionEnd", "SubagentStop")
        assert set(hook_events) == set(expected_events)
        session_start_hook = hook_events["SessionStart"][0]["hooks"][0]
        if target == "claude":
            command_prefix = f'python3 "${{CLAUDE_PLUGIN_ROOT}}/bin/{HOST_RUNTIME_BIN}"'
        else:
            command_prefix = f'python3 "${{PLUGIN_ROOT}}/bin/{HOST_RUNTIME_BIN}"'
        bootstrap_source = bootstrap_path.read_text()
        assert "ENROLLMENT_CALLBACK_DEADLINE_SECONDS" not in bootstrap_source
        assert "ThreadingHTTPServer" not in bootstrap_source
        assert session_start_hook["timeout"] == 150
        assert hook_events["SessionStart"][0]["matcher"] == "startup|resume"
        for event_name, lifecycle in (
            ("Stop", "stop"),
            ("SessionEnd", "session_end"),
            ("SubagentStop", "subagent_stop"),
        ):
            if event_name not in hook_events:
                continue
            hook = hook_events[event_name][0]["hooks"][0]
            assert hook["command"] == f"{command_prefix} collect --host {target} --lifecycle {lifecycle} --quiet"
            assert hook["timeout"] == 150

        stub_root = tmp_path / f"{target}-stub-plugin"
        stub_runtime = stub_root / "bin" / HOST_RUNTIME_BIN
        stub_call_log = tmp_path / f"{target}-stub-calls.jsonl"
        stub_runtime.parent.mkdir(parents=True)
        stub_runtime.write_text(
            "import json, os, sys\n"
            "with open(os.environ['PROMPTLESS_STUB_CALL_LOG'], 'a') as call_log:\n"
            "    call_log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:2] == ['ensure']:\n"
            "    print(json.dumps({'argv': sys.argv[1:]}))\n"
        )
        stub_runtime.chmod(0o644)

        missing_runtime_root = tmp_path / f"{target}-missing-runtime-plugin"
        missing_runtime_root.mkdir()

        def reset_stub_calls() -> None:
            stub_call_log.unlink(missing_ok=True)

        def assert_startup_calls() -> None:
            assert [json.loads(line) for line in stub_call_log.read_text().splitlines()] == [
                ["ensure", "--host", target],
                ["collect", "--host", target, "--lifecycle", "session_start", "--baseline", "--quiet"],
            ]

        if target == "claude":
            hook_args = session_start_hook["args"]
            assert session_start_hook["command"] == "node"
            assert hook_args[0] == "-e"
            assert len(hook_args) == 3
            hook_script = hook_args[1]
            assert hook_args[2] == "${CLAUDE_PLUGIN_ROOT}"
            assert "CLAUDE_PLUGIN_ROOT" in hook_script
            assert "PLUGIN_ROOT" in hook_script
            assert f"path.join(root, 'bin', {HOST_RUNTIME_BIN!r})" in hook_script
            assert "spawnSync" in hook_script
            assert "sys.version_info >= (3, 9)" in hook_script
            assert MISSING_PYTHON_MESSAGE in hook_script
            assert UNSUPPORTED_PYTHON_MESSAGE in hook_script
            assert "'collect'" in hook_script
            assert "'--baseline'" in hook_script
            assert "'--quiet'" in hook_script

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
            assert f'runtime="$root/bin/{HOST_RUNTIME_BIN}"' in hook_command
            assert '[ ! -r "$runtime" ]' in hook_command

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
        metadata = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())
        assert not (plugin_root / ".promptless").exists()
        runtime = metadata["managed_runtimes"][0]
        assert runtime["id"] == "host-runtime"
        assert runtime["status"] == "included"
        assert runtime["target"] == target
        assert runtime["version"] == "0.3.0"
        assert runtime["channel"] == "stable"
        assert runtime["path"] == f"bin/{HOST_RUNTIME_BIN}"
        assert len(runtime["sha256"]) == 64

    codex_manifest = json.loads((hub_root / "dist/codex/core/.codex-plugin/plugin.json").read_text())
    assert codex_manifest["hooks"] == "./hooks/hooks.json"

    for target in ("cursor", "gemini"):
        plugin_root = hub_root / "dist" / target / "core"
        assert not (plugin_root / "bin" / HOST_RUNTIME_BIN).exists()
        assert not (plugin_root / "hub.managed-runtimes.json").exists()

    release_manifest = json.loads((hub_root / "hub.release.json").read_text())
    assert {runtime["target"] for runtime in release_manifest["managed_runtimes"]} == {"codex", "claude"}
    _assert_no_promptless_directory(hub_root)


def test_host_runtime_requires_subcommand_and_reports_version(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    runtime_path = plugin_root / "bin" / HOST_RUNTIME_BIN
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
    assert payload["version"] == "0.3.0"
    assert payload["channel"] == "stable"
    assert len(_json_string(payload["sha256"], "sha256")) == 64

    text_version = subprocess.run(
        [str(runtime_path), "version"],
        env=_clean_env(HOME=str(home), PLUGIN_ROOT=str(plugin_root)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert text_version.returncode == 0
    assert text_version.stdout == f"{HOST_RUNTIME_BIN} 0.3.0\n"
    assert text_version.stderr == ""


def test_host_runtime_enroll_status_and_reset_commands(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"
    server = _FakeControlPlane()
    server.start()
    try:
        home = tmp_path / "home"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
        }

        enroll_payload, _ = _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        assert enroll_payload["status"] == "enrolled"
        assert enroll_payload["host"] == "codex"
        assert enroll_payload["credential_id"] == "22222222-2222-4222-8222-222222222222"
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert len(server.credential_requests) == 1
        assert server.poll_requests == []
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
        assert len(server.credential_requests) == 1
        assert server.poll_requests == []
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
        [str(hub_root / "dist/codex/core/bin" / HOST_RUNTIME_BIN), "ensure", "--host", "codex"],
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
        [str(hub_root / "dist/codex/core/bin" / HOST_RUNTIME_BIN), "ensure", "--host", "codex", "--quiet"],
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
    server = _FakeControlPlane()
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
        assert server.poll_requests == []
        assert server.worker_not_found_requests == []
        assert server.policy_requests == ["/v0/host-enrollment/policy?target=codex"]
        assert len(server.check_ins) == 1
    finally:
        server.stop()


@pytest.mark.parametrize(
    "identity_location",
    ["envelope", "policy"],
    ids=["identity-envelope", "identity-policy"],
)
def test_bootstrap_welcomes_internal_promptless_user_once_per_plugin_version(
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
        assert credential["internal_promptless_user"] is True
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
        assert "Promptless Instruction Governance updated to v0.2.0 (was v0.1.0)." in upgraded_message
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
        assert "systemMessage" not in payload

        state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "host state"),
            "host state",
        )
        assert INTERNAL_WELCOME_SHOWN_AT_KEY not in state
        assert INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY not in state
        credentials = _json_mapping(state["credentials"], "credentials")
        credential = _json_mapping(next(iter(credentials.values())), "credential")
        assert "internal_promptless_user" not in credential
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
        credential.pop("internal_promptless_user", None)
        state_path.write_text(json.dumps(state))

        payload, _ = _run_bootstrap(plugin_root, "codex", env)
        assert "systemMessage" not in payload

        updated_state = _json_mapping(
            validate_json_value(json.loads(state_path.read_text()), "updated host state"),
            "updated host state",
        )
        assert INTERNAL_WELCOME_SHOWN_AT_KEY not in updated_state
        assert INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY not in updated_state
        updated_credentials = _json_mapping(updated_state["credentials"], "updated credentials")
        updated_credential = _json_mapping(updated_credentials[credential_key], "updated credential")
        assert "internal_promptless_user" not in updated_credential
    finally:
        server.stop()


def test_bootstrap_welcomes_internal_promptless_user_from_credential_response(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(credential_response_extra={"user_email": "Adit@GoPromptless.AI"})
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
        assert credential["internal_promptless_user"] is True
    finally:
        server.stop()


def test_bootstrap_surfaces_browser_open_disabled(tmp_path: Path) -> None:
    # PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER=0 with a non-loopback approval URL must surface
    # its own reason (distinct from a failed browser launch) that names the env var.
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeControlPlane(
        approval_url_override="https://app.gopromptless.ai/instruction-hub/enroll?approval_token=plihenroll_approvalcode"
    )
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
                "PROMPTLESS_DASHBOARD_BASE_URL": "https://app.gopromptless.ai",
            },
            expected_status="setup_pending",
        )

        assert payload["reason"] == "browser_open_disabled"
        message = _json_string(payload["systemMessage"], "systemMessage")
        assert "PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER" in message
        assert "promptless-host-runtime enroll" in message
        state = json.loads(_host_state_path(home).read_text())
        assert _json_string(state["host_instance_id"], "host_instance_id").startswith("host-")
        assert "credentials" not in state
        pending_enrollments = _json_mapping(
            validate_json_value(state["pending_enrollments"], "pending enrollments"),
            "pending enrollments",
        )
        assert len(pending_enrollments) == 1
        pending_session = _json_mapping(next(iter(pending_enrollments.values())), "pending enrollment")
        assert pending_session["deployment_instance_id"] == "worker-local-1"
        assert pending_session["device_code"] == "plihenroll_devicecode"
        assert _json_string(pending_session["approval_url"], "approval_url").startswith(
            "https://app.gopromptless.ai/instruction-hub/enroll?"
        )
        assert _json_string(pending_session["staged_credential"], "staged_credential").startswith("plihost_")
        assert pending_session["browser_opened"] is False
        assert "poll_url" not in pending_session
        assert "credential_url" not in pending_session
        seen_versions = _json_mapping(
            validate_json_value(state["last_seen_plugin_versions"], "last seen plugin versions"),
            "last seen plugin versions",
        )
        assert seen_versions["claude"] == "0.1.0"
        assert len(server.session_requests) == 1
        assert server.poll_requests == []
        assert server.credential_requests == []
        assert server.policy_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


def test_bootstrap_persists_host_global_state_file(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeControlPlane()
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
    server = _FakeControlPlane(session_barrier_count=2)
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
    server = _FakeControlPlane()
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
    # The loopback browser-open fails (the hosted approval page answers HTTP 500), which the
    # runtime reports as a failed browser launch without marking the session's browser opened.
    server = _FakeControlPlane(approval_http_statuses=[500])
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
        pending_enrollments = _json_mapping(
            validate_json_value(
                json.loads(_host_state_path(home).read_text())["pending_enrollments"],
                "pending enrollments",
            ),
            "pending enrollments",
        )
        assert len(pending_enrollments) == 1
        pending_session = _json_mapping(next(iter(pending_enrollments.values())), "pending enrollment")
        assert pending_session["browser_opened"] is False
        assert _json_string(pending_session["staged_credential"], "staged_credential").startswith("plihost_")
        assert len(server.session_requests) == 1
        assert server.policy_requests == []
        assert server.poll_requests == []
        assert server.credential_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


def test_bootstrap_configures_codex_and_claude_and_reports_metadata(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeControlPlane()
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
            },
        )
        assert not (claude_home / ".claude/settings.json").exists()

        assert len(server.session_requests) == 2
        assert server.session_requests[0]["deployment_instance_id"] == "worker-local-1"
        assert server.session_requests[0]["target"] == "codex"
        assert server.session_requests[0]["plugin_id"] == "promptless-instruction-hub-core"
        assert server.session_requests[0]["plugin_version"] == "0.1.0"
        assert server.session_requests[0]["package_id"] == "core"
        assert server.session_requests[0]["bootstrap_version"] == "0.3.0"
        assert server.session_requests[0]["toolchain_version"] != "unknown"
        assert server.session_requests[1]["target"] == "claude"
        for session_request in server.session_requests:
            assert "callback_url" not in session_request
            assert "pending_callback" not in session_request
        assert server.poll_requests == []
        assert len(server.credential_requests) == 2
        assert server.credential_requests[0]["credential_hash"] != server.credential_requests[1]["credential_hash"]
        for credential_request in server.credential_requests:
            assert credential_request["device_code"] == "plihenroll_devicecode"
            credential_hash = _json_string(credential_request["credential_hash"], "credential_hash")
            assert len(credential_hash) == 64
            int(credential_hash, 16)
            assert _json_string(credential_request["credential_prefix"], "credential_prefix").startswith("plihost_")
            assert "host_credential" not in credential_request
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
            assert check_in["bootstrap_version"] == "0.3.0"
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
            assert effective_config["trace_upload_endpoint"] == f"{server.worker_base_url}/v0/traces/batches"
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


def test_bootstrap_creates_device_session_without_callback_handoff(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeControlPlane()
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
            },
        )

        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert "callback_url" not in server.session_requests[0]
        assert "pending_callback" not in server.session_requests[0]
        assert server.poll_requests == []
        assert len(server.credential_requests) == 1
        assert server.worker_not_found_requests == []
        assert server.policy_requests == ["/v0/host-enrollment/policy?target=codex"]
        assert len(server.check_ins) == 1
    finally:
        server.stop()


def test_bootstrap_resumes_pending_enrollment_across_runs(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    # Run 1's loopback browser-open fails (HTTP 500 from the approval page); run 2 must resume
    # the persisted session, open the one approval page, and register the same staged credential.
    server = _FakeControlPlane(approval_http_statuses=[500])
    server.start()
    try:
        home = tmp_path / "home"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
        }

        first_payload, _ = _run_bootstrap(
            hub_root / "dist/codex/core", "codex", env, expected_status="setup_pending"
        )
        assert first_payload["reason"] == "browser_launch_failed"
        first_state = json.loads(_host_state_path(home).read_text())
        pending_enrollments = _json_mapping(
            validate_json_value(first_state["pending_enrollments"], "pending enrollments"),
            "pending enrollments",
        )
        assert len(pending_enrollments) == 1
        pending_session = _json_mapping(next(iter(pending_enrollments.values())), "pending enrollment")
        assert pending_session["browser_opened"] is False
        assert "poll_url" not in pending_session
        assert "credential_url" not in pending_session
        staged_credential = _json_string(pending_session["staged_credential"], "staged_credential")
        assert len(server.session_requests) == 1
        assert server.credential_requests == []
        assert server.approval_opens == []

        second_payload, _ = _run_bootstrap(hub_root / "dist/codex/core", "codex", env)

        assert second_payload["status"] == "configured"
        # The pending session was resumed: exactly one device session was ever created, the
        # second run opened the one approval page, and the legacy poll endpoint was never hit.
        assert len(server.session_requests) == 1
        assert len(server.approval_opens) == 1
        assert server.poll_requests == []
        registered_hash = _json_string(server.credential_requests[-1]["credential_hash"], "credential_hash")
        assert registered_hash == hashlib.sha256(staged_credential.encode()).hexdigest()
        second_state = json.loads(_host_state_path(home).read_text())
        credentials = _json_mapping(validate_json_value(second_state["credentials"], "credentials"), "credentials")
        stored_credential = _json_mapping(next(iter(credentials.values())), "stored credential")
        assert stored_credential["value"] == staged_credential
        assert second_state["pending_enrollments"] == {}
    finally:
        server.stop()


def test_bootstrap_pending_credential_response_retains_session(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeControlPlane(credential_responses=["pending"] * 8)
    server.start()
    try:
        home = tmp_path / "home"
        payload, _ = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
                "PROMPTLESS_HOST_ENROLLMENT_POLL_DEADLINE_SECONDS": "1",
            },
            expected_status="setup_pending",
        )

        assert payload["reason"] == "approval_pending"
        assert "promptless-host-runtime enroll" in _json_string(payload["systemMessage"], "systemMessage")
        state = json.loads(_host_state_path(home).read_text())
        pending_enrollments = _json_mapping(
            validate_json_value(state["pending_enrollments"], "pending enrollments"),
            "pending enrollments",
        )
        assert len(pending_enrollments) == 1
        pending_session = _json_mapping(next(iter(pending_enrollments.values())), "pending enrollment")
        assert pending_session["browser_opened"] is True
        assert len(server.session_requests) == 1
        assert len(server.credential_requests) >= 1
        assert server.poll_requests == []
    finally:
        server.stop()


def test_bootstrap_expired_credential_response_forgets_pending(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeControlPlane(credential_responses=["expired"])
    server.start()
    try:
        home = tmp_path / "home"
        payload, _ = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
            },
            expected_status="setup_pending",
        )

        assert payload["reason"] == "approval_expired"
        state = json.loads(_host_state_path(home).read_text())
        assert state["pending_enrollments"] == {}
        assert "credentials" not in state
        assert len(server.credential_requests) == 1
        assert server.poll_requests == []
    finally:
        server.stop()


def test_bootstrap_credential_conflict_restarts_with_fresh_session(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    # The first registration answers HTTP 409 (a different credential already registered);
    # the next run must start a brand-new session with a freshly staged credential.
    server = _FakeControlPlane(credential_responses=[409])
    server.start()
    try:
        home = tmp_path / "home"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
        }

        first_payload, _ = _run_bootstrap(hub_root / "dist/codex/core", "codex", env, expected_status="setup_pending")
        assert first_payload["reason"] == "credential_conflict"
        assert "promptless-host-runtime enroll" in _json_string(first_payload["systemMessage"], "systemMessage")
        state = json.loads(_host_state_path(home).read_text())
        assert state["pending_enrollments"] == {}
        assert len(server.session_requests) == 1
        assert len(server.credential_requests) == 1

        second_payload, _ = _run_bootstrap(hub_root / "dist/codex/core", "codex", env)

        assert second_payload["status"] == "configured"
        assert len(server.session_requests) == 2
        first_hash = _json_string(server.credential_requests[0]["credential_hash"], "credential_hash")
        second_hash = _json_string(server.credential_requests[-1]["credential_hash"], "credential_hash")
        assert first_hash != second_hash
        assert server.poll_requests == []
    finally:
        server.stop()


def test_bootstrap_transient_credential_errors_retain_pending(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    # HTTP 500 responses retry until the wait deadline and keep the pending session for the
    # next run instead of discarding it or surfacing a hard error.
    server = _FakeControlPlane(credential_responses=[500] * 12)
    server.start()
    try:
        home = tmp_path / "home"
        payload, _ = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
                "PROMPTLESS_HOST_ENROLLMENT_POLL_DEADLINE_SECONDS": "2",
            },
            expected_status="setup_pending",
        )

        assert payload["reason"] == "approval_pending"
        assert len(server.credential_requests) >= 2
        state = json.loads(_host_state_path(home).read_text())
        pending_enrollments = _json_mapping(
            validate_json_value(state["pending_enrollments"], "pending enrollments"),
            "pending enrollments",
        )
        assert len(pending_enrollments) == 1
        assert server.poll_requests == []
    finally:
        server.stop()


def test_bootstrap_never_leaks_credential_material(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeControlPlane()
    server.start()
    try:
        home = tmp_path / "home"
        _, result = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
            },
        )

        state = json.loads(_host_state_path(home).read_text())
        credentials = _json_mapping(validate_json_value(state["credentials"], "credentials"), "credentials")
        stored_credential = _json_mapping(next(iter(credentials.values())), "stored credential")
        credential_value = _json_string(stored_credential["value"], "stored credential value")
        assert credential_value.startswith("plihost_")
        credential_hash = hashlib.sha256(credential_value.encode()).hexdigest()
        last_status_text = _last_status_path(home).read_text()
        for output in (result.stdout, result.stderr, last_status_text):
            assert credential_value not in output
            assert credential_hash not in output
    finally:
        server.stop()


def test_bootstrap_reuses_legacy_credential_without_reenrollment(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeControlPlane()
    server.start()
    try:
        # Pre-seed the state file with a 0.2.0-format credential entry under the same cache key
        # ({deployment_instance_id, target, worker_base_url} sha256). The 0.3.0 runtime must use
        # it as-is: config written with that credential and zero enrollment traffic.
        home = tmp_path / "home"
        legacy_credential = "plihost_legacy0credential0value"
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "deployment_instance_id": "worker-local-1",
                    "target": "codex",
                    "worker_base_url": server.worker_base_url,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        state_path = _host_state_path(home)
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "credentials": {
                        cache_key: {
                            "value": legacy_credential,
                            "credential_id": "legacy-credential-id",
                            "deployment_instance_id": "worker-local-1",
                            "worker_base_url": server.worker_base_url,
                        }
                    }
                }
            )
        )
        server.registered_credential_hashes.add(hashlib.sha256(legacy_credential.encode()).hexdigest())

        payload, _ = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
            },
        )

        assert payload["status"] == "configured"
        assert not (home / ".codex/config.toml").exists()
        assert server.session_requests == []
        assert server.credential_requests == []
        assert server.poll_requests == []
        assert server.policy_requests == ["/v0/host-enrollment/policy?target=codex"]
        assert len(server.check_ins) == 1
        state = json.loads(state_path.read_text())
        credentials = _json_mapping(validate_json_value(state["credentials"], "credentials"), "credentials")
        stored_credential = _json_mapping(credentials[cache_key], "stored credential")
        assert stored_credential["value"] == legacy_credential
        assert stored_credential["credential_id"] == "legacy-credential-id"
    finally:
        server.stop()


@pytest.mark.parametrize(
    ("approval_url_override", "approval_path"),
    [
        ("https://attacker.example/instruction-hub/enroll", "/instruction-hub/enroll"),
        (None, "/attacker/enroll"),
    ],
    ids=["wrong-origin", "wrong-path"],
)
def test_bootstrap_rejects_device_session_approval_url_outside_dashboard_route(
    tmp_path: Path,
    approval_url_override: str | None,
    approval_path: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeControlPlane(
        approval_url_override=approval_url_override,
        approval_path=approval_path,
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
            },
            expected_status="error",
        )

        assert "host enrollment approval URL did not match dashboard enrollment route" in str(payload["message"])
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert server.poll_requests == []
        assert server.credential_requests == []
        assert server.policy_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


def test_bootstrap_fails_fast_when_browser_opening_rejects_device_approval_url(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeControlPlane(approval_url_override="https://attacker.example/instruction-hub/enroll")
    server.start()
    try:
        home = tmp_path / "home"
        result = subprocess.run(
            [str(hub_root / "dist/codex/core/bin" / HOST_RUNTIME_BIN), "ensure", "--host", "codex"],
            env=_clean_env(
                HOME=str(home),
                CODEX_HOME=str(home / ".codex"),
                PLUGIN_ROOT=str(hub_root / "dist/codex/core"),
                PROMPTLESS_WORKER_BASE_URL=server.worker_base_url,
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
        assert server.credential_requests == []
        assert server.policy_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


def test_bootstrap_requires_device_session_approval_url(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeControlPlane(
        session_response={
            "session_id": "11111111-1111-4111-8111-111111111111",
            "device_code": "plihenroll_devicecode",
            "approval_url": None,
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
            },
            expected_status="error",
        )

        assert "host enrollment session response missing required fields" in str(payload["message"])
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert server.poll_requests == []
        assert server.credential_requests == []
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
    server = _FakeControlPlane()
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
    server = _FakeControlPlane()
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
    server = _FakeControlPlane()
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
    server = _FakeControlPlane()
    server.start()
    try:
        codex_home = tmp_path / "codex-home"
        codex_config = codex_home / ".codex/config.toml"
        codex_config.parent.mkdir(parents=True)
        original_codex_config = (
            'model = "gpt-5"\n# BEGIN PROMPTLESS MANAGED HOST ENROLLMENT\n[otel]\nenvironment = "prod"\n'
        )
        codex_config.write_text(original_codex_config)

        codex_payload, _ = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(codex_home),
                "CODEX_HOME": str(codex_home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
            },
            expected_status="blocked",
        )

        assert codex_config.read_text() == original_codex_config
        assert list(codex_config.parent.glob("config.toml.*.bak")) == []
        assert codex_payload["status"] == "blocked"
        drift_reports = _json_list(server.check_ins[-1]["drift_reports"], "drift_reports")
        first_drift_report = _json_mapping(drift_reports[0], "drift_reports[0]")
        assert first_drift_report["kind"] == "manual_config_required"
        assert "malformed" in _json_string(first_drift_report["message"], "drift_reports[0].message")
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
    assert f"bin/{HOST_RUNTIME_BIN}" in session_start[1]["hooks"][0]["command"]


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
    server = _FakeControlPlane()
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
    server = _FakeControlPlane()
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
            "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
            "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
    server = _FakeControlPlane()
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
    server = _FakeControlPlane()
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
            "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
    server = _FakeControlPlane()
    server.start()
    try:
        claude_env = {
            "HOME": str(tmp_path / "claude-home"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home/.claude"),
            "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
            "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
        }
        codex_env = {
            "HOME": str(tmp_path / "codex-home"),
            "CODEX_HOME": str(tmp_path / "codex-home/.codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
        }

        # First install on each host records the version silently (an install is not an update):
        # nothing is written, so there is no message at all.
        first_claude, _ = _run_bootstrap(hub_root / "dist/claude/core", "claude", claude_env)
        assert "systemMessage" not in first_claude
        first_codex, _ = _run_bootstrap(hub_root / "dist/codex/core", "codex", codex_env)
        assert "systemMessage" not in first_codex

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
            "PROMPTLESS_WORKER_BASE_URL": "https://pig.promptless.ai",
        },
        expected_status="error",
    )

    assert "invalid JSON" in _json_string(payload["message"], "message")
    assert "Promptless host enrollment failed for Codex" in _json_string(payload["systemMessage"], "systemMessage")
    assert result.stdout != ""


def test_bootstrap_defers_recording_update_until_notice_surfaces(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root, plugin_version="0.1.0")
    server = _FakeControlPlane()
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
        _run_bootstrap(hub_root / "dist/claude/core", "claude", claude_env(server.worker_base_url))
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
            hub_root / "dist/claude/core", "claude", claude_env(server.worker_base_url), expected_status="configured"
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
    server = _FakeControlPlane()
    server.start()
    try:
        codex_home = tmp_path / "codex-home"
        codex_env = {
            "HOME": str(codex_home),
            "CODEX_HOME": str(codex_home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
            "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
        "missing-write-permission",
    ],
)
def test_bootstrap_rejects_invalid_worker_policy(tmp_path: Path, case: str) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    invalid_policy = _invalid_policy(case)
    invalid_policy["user_email"] = "Adit@GoPromptless.AI"
    server = _FakeControlPlane(policy=invalid_policy)
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
        assert "internal_promptless_user" not in credential
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


def test_bootstrap_blocks_when_worker_requires_different_runtime_version(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeControlPlane(policy=_policy_with(required_bootstrap_version="0.4.0"))
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
    server = _FakeControlPlane(post_response={"accepted": False, "policy_version": 1})
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
                "PROMPTLESS_WORKER_BASE_URL": server.worker_base_url,
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
        assert batch["collector_version"] == "0.3.0"
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
        transcript_path = tmp_path / "codex-session.jsonl"
        first_record = b'{"kind":"session_start","message":"missed baseline"}\n'
        second_record = b'{"kind":"stop","message":"complete"}\n'
        transcript_path.write_bytes(first_record + second_record)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
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
        [str(plugin_root / "bin" / HOST_RUNTIME_BIN), "ensure", "--host", host],
        env=_clean_env(**env),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
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
        [str(plugin_root / "bin" / HOST_RUNTIME_BIN), *args],
        env=_clean_env(**env),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected_returncode
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
        [str(plugin_root / "bin" / HOST_RUNTIME_BIN), *args],
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
    return subprocess.Popen(
        [str(plugin_root / "bin" / HOST_RUNTIME_BIN), "ensure", "--host", host],
        env=_clean_env(**env),
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


_ACTIVE_CONTROL_PLANE: _FakeControlPlane | None = None


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES": "1",
        "PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER": "0",
    }
    env.update(overrides)
    if "PROMPTLESS_WORKER_BASE_URL" in env:
        # Device-flow enrollment and its approval page live on the api server, so both the API
        # and dashboard bases default there whenever a test overrides the worker base.
        api_default = (
            _ACTIVE_CONTROL_PLANE.api_base_url
            if _ACTIVE_CONTROL_PLANE is not None
            else env["PROMPTLESS_WORKER_BASE_URL"]
        )
        env.setdefault("PROMPTLESS_API_BASE_URL", api_default)
        env.setdefault("PROMPTLESS_DASHBOARD_BASE_URL", env["PROMPTLESS_API_BASE_URL"])
    return env


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
    collector = _json_mapping(policy["collector"], "policy.collector")
    permissions = _json_mapping(policy["plugin_permissions"], "policy.plugin_permissions")

    if case == "expired":
        policy["expires_at"] = (now - dt.timedelta(minutes=1)).isoformat()
    elif case == "missing-write-permission":
        permissions["write_user_config"] = False
    else:
        raise AssertionError(f"unhandled invalid policy case: {case}")
    del collector
    return payload


def _session_response() -> dict[str, JsonValue]:
    return {
        "device_code": "plihenroll_devicecode",
        "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
        "poll_interval_seconds": 1,
    }


_FAKE_CREDENTIAL_ID = "22222222-2222-4222-8222-222222222222"


class _FakeControlPlane:
    """Fake worker + public API server pair backing the device-flow enrollment tests.

    The worker server mirrors the production ALB: it serves /healthz, the host-enrollment
    policy, and check-ins, and answers every /v1/instruction-hub/* path with a bare 404 so a
    client that regresses to targeting enrollment at the worker base fails loudly. The api
    server owns the device-flow endpoints: device-session creation, the register-as-poll
    credential endpoint, the hosted approval page fetched by the loopback browser-open, and
    the legacy poll endpoint, which answers 404 while counting calls so tests can assert the
    client never polls.
    """

    def __init__(
        self,
        *,
        policy: dict[str, JsonValue] | None = None,
        post_response: dict[str, JsonValue] | None = None,
        session_response: dict[str, JsonValue] | None = None,
        session_barrier_count: int = 0,
        approval_url_override: str | None = None,
        approval_path: str = "/instruction-hub/enroll",
        credential_responses: list[str | int] | None = None,
        credential_response_extra: dict[str, JsonValue] | None = None,
        approval_http_statuses: list[int] | None = None,
        unparsed_record_count: int = 0,
    ) -> None:
        self.check_ins: list[dict[str, JsonValue]] = []
        self.credential_requests: list[dict[str, JsonValue]] = []
        self.policy_requests: list[str] = []
        self.poll_requests: list[dict[str, JsonValue]] = []
        self.session_requests: list[dict[str, JsonValue]] = []
        self.trace_batches: list[dict[str, JsonValue]] = []
        self.worker_not_found_requests: list[str] = []
        self.approval_opens: list[str] = []
        self.policy_response = policy or _signed_policy()
        self.post_response = post_response
        self.session_response = session_response
        self.session_barrier_count = session_barrier_count
        self.session_condition = threading.Condition()
        self.approval_url_override = approval_url_override
        self.approval_path = approval_path
        # Scriptable credential endpoint: each entry answers one call, either as a body status
        # ("pending"/"approved"/"expired"/"consumed") or a forced HTTP status (int, e.g. 409 or
        # 500). An exhausted script falls back to the register-as-poll default semantics.
        self.credential_responses: list[str | int] = list(credential_responses or [])
        self.credential_response_extra: dict[str, JsonValue] = dict(credential_response_extra or {})
        # Forced HTTP statuses for the hosted approval page, consumed one per GET, so tests can
        # fail the loopback browser-open on one run and let a later run succeed.
        self.approval_http_statuses: list[int] = list(approval_http_statuses or [])
        self.unparsed_record_count = unparsed_record_count
        self.session_device_codes: dict[str, str] = {}
        self.session_credential_hashes: dict[str, str] = {}
        self.approved_sessions: set[str] = set()
        self.registered_credential_hashes: set[str] = set()
        self.lock = threading.Lock()
        _FakeWorkerHandler.plane = self
        _FakeApiHandler.plane = self
        self._worker_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWorkerHandler)
        self._api_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeApiHandler)
        worker_host, worker_port = self._worker_server.server_address
        api_host, api_port = self._api_server.server_address
        self.worker_base_url = f"http://{worker_host}:{worker_port}"
        self.api_base_url = f"http://{api_host}:{api_port}"
        self.base_url = self.worker_base_url
        self._worker_thread = threading.Thread(target=self._worker_server.serve_forever)
        self._api_thread = threading.Thread(target=self._api_server.serve_forever)

    def start(self) -> None:
        global _ACTIVE_CONTROL_PLANE
        _ACTIVE_CONTROL_PLANE = self
        self._worker_thread.start()
        self._api_thread.start()

    def stop(self) -> None:
        global _ACTIVE_CONTROL_PLANE
        for http_server in (self._worker_server, self._api_server):
            http_server.shutdown()
            http_server.server_close()
        for thread in (self._worker_thread, self._api_thread):
            thread.join(timeout=5)
        if _ACTIVE_CONTROL_PLANE is self:
            _ACTIVE_CONTROL_PLANE = None


class _FakeWorkerServer(_FakeControlPlane):
    pass


class _ControlPlaneHandler(BaseHTTPRequestHandler):
    plane: ClassVar[_FakeControlPlane]

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write_json(self, payload: dict[str, JsonValue], *, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_plain(self, body: bytes, *, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_request(self, label: str) -> dict[str, JsonValue]:
        length = int(self.headers["Content-Length"])
        return _json_mapping(
            validate_json_value(json.loads(self.rfile.read(length)), label),
            label,
        )


class _FakeWorkerHandler(_ControlPlaneHandler):
    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/v1/instruction-hub/"):
            self._write_worker_not_found()
            return
        if parsed.path == "/healthz":
            self._write_json(
                {
                    "status": "ok",
                    "deployment_instance_id": "worker-local-1",
                    "worker_version": "0.1.0-test",
                }
            )
            return
        target = parse_qs(parsed.query).get("target")
        if (
            parsed.path != "/v0/host-enrollment/policy"
            or target not in (["codex"], ["claude"])
            or not self._authorized_host_credential()
        ):
            self.send_response(401)
            self.end_headers()
            return
        self.plane.policy_requests.append(self.path)
        self._write_json(self.plane.policy_response)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/v1/instruction-hub/"):
            self._write_worker_not_found()
            return
        if parsed.path == "/v0/traces/batches":
            self._handle_trace_batch(parsed.query)
            return
        if self.path != "/v0/host-enrollment/check-ins" or not self._authorized_host_credential():
            self.send_response(401)
            self.end_headers()
            return
        payload = self._read_json_request("check-in request")
        self.plane.check_ins.append(payload)
        self._write_json(self.plane.post_response or {"accepted": True, "policy_version": 1})

    def _handle_trace_batch(self, query: str) -> None:
        target = parse_qs(query).get("target")
        if target not in (["codex"], ["claude"]) or not self._authorized_host_credential():
            self.send_response(401)
            self.end_headers()
            return
        payload = self._read_json_request("trace batch request")
        self.plane.trace_batches.append(payload)
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
                "unparsed_record_count": self.plane.unparsed_record_count,
            }
        )

    def _write_worker_not_found(self) -> None:
        # The production worker ALB answers /v1/instruction-hub/* with a bare 404; a client
        # that regresses to enrolling against the worker base must fail loudly instead of
        # finding a helpful fake endpoint here.
        self.plane.worker_not_found_requests.append(self.path)
        self._write_plain(b"not found", status=404)

    def _authorized_host_credential(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        prefix = "Bearer "
        if not auth.startswith(prefix):
            return False
        token = auth[len(prefix) :]
        return hashlib.sha256(token.encode()).hexdigest() in self.plane.registered_credential_hashes


class _FakeApiHandler(_ControlPlaneHandler):
    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        plane = self.plane
        if parsed.path == "/instruction-hub/enroll":
            with plane.lock:
                forced_status = plane.approval_http_statuses.pop(0) if plane.approval_http_statuses else None
                if forced_status is None:
                    plane.approval_opens.append(self.path)
                    plane.approved_sessions.update(plane.session_device_codes)
            if forced_status is not None:
                self._write_plain(b"approval unavailable", status=forced_status)
                return
            self._write_json({"status": "approved"})
            return
        self._write_json({"detail": "not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/v1/instruction-hub/host-enrollments/device-sessions":
            payload = self._read_json_request("session request")
            response_payload = self._session_response_payload()
            self._record_session_request(payload)
            self._write_json(response_payload)
            return
        poll_session_id = _enrollment_session_id_from_path(parsed.path, "poll")
        if poll_session_id is not None:
            # The device-flow server has no poll endpoint. Count the call so tests can assert
            # the client never polls, and answer 404 like the production router.
            self.plane.poll_requests.append(self._read_json_request("session poll request"))
            self._write_json({"detail": "not found"}, status=404)
            return
        credential_session_id = _enrollment_session_id_from_path(parsed.path, "credential")
        if credential_session_id is not None:
            self._handle_credential(credential_session_id)
            return
        self._write_json({"detail": "not found"}, status=404)

    def _handle_credential(self, session_id: str) -> None:
        plane = self.plane
        payload = self._read_json_request("credential registration request")
        with plane.lock:
            plane.credential_requests.append(payload)
            expected_device_code = plane.session_device_codes.get(session_id)
        if expected_device_code is None:
            self._write_json(
                {"detail": {"code": "instruction_hub_host_enrollment_session_not_found"}},
                status=404,
            )
            return
        if payload.get("device_code") != expected_device_code:
            self.send_response(401)
            self.end_headers()
            return
        with plane.lock:
            scripted = plane.credential_responses.pop(0) if plane.credential_responses else None
        if isinstance(scripted, int):
            self._write_forced_credential_status(scripted)
            return
        if scripted in ("pending", "approved", "expired"):
            self._write_json({"status": scripted, "expires_at": _future_expiry()})
            return
        if scripted is None and session_id not in plane.approved_sessions:
            # Register-as-poll: an unapproved session passes its approval state through.
            self._write_json({"status": "pending", "expires_at": _future_expiry()})
            return
        credential_hash = _json_string(payload.get("credential_hash"), "credential_hash")
        with plane.lock:
            existing_hash = plane.session_credential_hashes.get(session_id)
            conflict = existing_hash is not None and existing_hash != credential_hash
            if not conflict:
                # First registration wins; a same-hash retry is idempotent.
                plane.session_credential_hashes[session_id] = credential_hash
                plane.registered_credential_hashes.add(credential_hash)
        if conflict:
            self._write_json(
                {
                    "detail": {
                        "code": "instruction_hub_host_enrollment_credential_conflict",
                        "message": "Host enrollment session already registered a different credential",
                    }
                },
                status=409,
            )
            return
        response_payload: dict[str, JsonValue] = {
            "status": "consumed",
            "credential_id": _FAKE_CREDENTIAL_ID,
            "expires_at": _future_expiry(),
        }
        response_payload.update(plane.credential_response_extra)
        self._write_json(response_payload)

    def _write_forced_credential_status(self, status: int) -> None:
        if status == 409:
            self._write_json(
                {"detail": {"code": "instruction_hub_host_enrollment_credential_conflict"}},
                status=409,
            )
            return
        if status == 410:
            self._write_json(
                {"detail": {"code": "instruction_hub_host_enrollment_credential_revoked"}},
                status=410,
            )
            return
        # Transient failures answer a non-JSON body to exercise best-effort error parsing.
        self._write_plain(b"internal error", status=status)

    def _session_response_payload(self) -> dict[str, JsonValue]:
        plane = self.plane
        payload = dict(plane.session_response or _session_response())
        payload.setdefault("session_id", str(uuid.uuid4()))
        payload.setdefault(
            "approval_url",
            plane.approval_url_override
            or f"{plane.api_base_url}{plane.approval_path}?approval_token=plihenroll_approvalcode",
        )
        session_id = _json_string(payload["session_id"], "session_id")
        device_code = _json_string(payload["device_code"], "device_code")
        with plane.lock:
            plane.session_device_codes[session_id] = device_code
        return payload

    def _record_session_request(self, payload: dict[str, JsonValue]) -> None:
        plane = self.plane
        condition = plane.session_condition
        if plane.session_barrier_count <= 1:
            plane.session_requests.append(payload)
            return
        with condition:
            plane.session_requests.append(payload)
            if len(plane.session_requests) >= plane.session_barrier_count:
                condition.notify_all()
                return
            condition.wait_for(lambda: len(plane.session_requests) >= plane.session_barrier_count, timeout=10)


def _enrollment_session_id_from_path(path: str, endpoint: str) -> str | None:
    prefix = "/v1/instruction-hub/host-enrollments/sessions/"
    suffix = f"/{endpoint}"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    session_id = path[len(prefix) : -len(suffix)]
    return session_id or None


def _future_expiry() -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()


def _signed_policy() -> dict[str, JsonValue]:
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
            "enabled_hosts": ["codex", "claude"],
            "plugin_permissions": {
                "write_user_config": True,
                "repair_user_config": True,
            },
            "required_bootstrap_version": "0.2.0",
        },
        "signature": "hmac-sha256-v1:test",
        "signed_at": now.isoformat(),
    }
