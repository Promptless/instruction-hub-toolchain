from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, validate_json_value

HOST_RUNTIME_BIN = "promptless-host-runtime"
HOST_STATE_REL_PATH = Path(".promptless/instruction-hub/host-enrollment-state.json")
LAST_STATUS_REL_PATH = Path(".promptless/instruction-hub/last-bootstrap-status.json")
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


def _assert_no_promptless_directory(root: Path) -> None:
    assert list(root.rglob(".promptless")) == []


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
        hook = hooks["hooks"]["SessionStart"][0]["hooks"][0]
        if target == "claude":
            hook_command = hook["command"]
            assert hook_command == f'python3 "${{CLAUDE_PLUGIN_ROOT}}/bin/{HOST_RUNTIME_BIN}" ensure --host claude'
        else:
            hook_command = hook["command"]
            assert hook_command == f'python3 "${{PLUGIN_ROOT}}/bin/{HOST_RUNTIME_BIN}" ensure --host codex'
        assert "--quiet" not in hook_command
        assert hook["timeout"] == 90
        metadata = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())
        assert not (plugin_root / ".promptless").exists()
        runtime = metadata["managed_runtimes"][0]
        assert runtime["id"] == "host-runtime"
        assert runtime["status"] == "included"
        assert runtime["target"] == target
        assert runtime["version"] == "0.2.0"
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
    assert payload["version"] == "0.2.0"
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
    assert text_version.stdout == f"{HOST_RUNTIME_BIN} 0.2.0\n"
    assert text_version.stderr == ""


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
        assert (home / ".codex/config.toml").exists()
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
        assert configured_config["managed_config_detected"] is True
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
        assert (home / ".codex/config.toml").exists()
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
        assert reset_config["managed_config_detected"] is True
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

        stdout_payload = _json_mapping(validate_json_value(json.loads(result.stdout), "bootstrap stdout"), "stdout")
        stdout_message = _json_string(stdout_payload["systemMessage"], "systemMessage")
        assert stdout_message == _json_string(payload["systemMessage"], "systemMessage")
        assert "Restart Codex" in stdout_message
        assert any(
            diagnostic.get("status") == "browser_enrollment_starting"
            and diagnostic.get("systemMessage") == BROWSER_ENROLLMENT_MESSAGE
            for diagnostic in _bootstrap_diagnostics(result.stderr)
        )
        assert payload["status"] == "needs_restart"
        assert (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert server.policy_requests == ["/v0/host-enrollment/policy?target=codex"]
        assert len(server.check_ins) == 1
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
        codex_config = (codex_home / ".codex/config.toml").read_text()
        assert "BEGIN PROMPTLESS MANAGED HOST ENROLLMENT" in codex_config
        assert 'endpoint = "http://127.0.0.1:4318/v1/logs"' in codex_config
        assert 'endpoint = "http://127.0.0.1:4318/v1/traces"' in codex_config
        assert codex_config.count('protocol = "binary"') == 2
        assert "metrics_exporter" not in codex_config
        assert "plihost_localcredential" not in codex_config
        codex_otel = tomllib.loads(codex_config)["otel"]
        assert codex_otel["exporter"]["otlp-http"]["protocol"] == "binary"
        assert codex_otel["trace_exporter"]["otlp-http"]["protocol"] == "binary"

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
        claude_settings = json.loads((claude_home / ".claude/settings.json").read_text())
        assert claude_settings["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert claude_settings["env"]["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
        assert claude_settings["env"]["ENABLE_BETA_TRACING_DETAILED"] == "1"
        assert claude_settings["env"]["BETA_TRACING_ENDPOINT"] == "http://127.0.0.1:4318/v1/traces"
        assert claude_settings["env"]["PROMPTLESS_MANAGED_HOST_ENROLLMENT"] == "1"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_LOGS_PROTOCOL"] == "http/protobuf"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"] == "http://127.0.0.1:4318/v1/logs"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] == "http/protobuf"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "http://127.0.0.1:4318/v1/traces"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_METRICS_PROTOCOL"] == "http/protobuf"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] == "http://127.0.0.1:4318/v1/metrics"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer otlp-token"
        assert "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT" not in claude_settings["env"]
        assert "OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT" not in claude_settings["env"]
        assert "OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT" not in claude_settings["env"]
        assert claude_settings["env"]["OTEL_LOG_USER_PROMPTS"] == "1"
        assert claude_settings["env"]["OTEL_LOG_ASSISTANT_RESPONSES"] == "1"
        assert claude_settings["env"]["OTEL_LOG_TOOL_DETAILS"] == "1"
        assert claude_settings["env"]["OTEL_LOG_TOOL_CONTENT"] == "1"
        assert claude_settings["env"]["OTEL_LOG_RAW_API_BODIES"] == (
            f"file:{claude_home / '.promptless/instruction-hub/claude-raw-api-bodies'}"
        )

        assert len(server.session_requests) == 2
        codex_callback_state = _callback_state(server.session_requests[0]["callback_url"], "codex callback_url")
        claude_callback_state = _callback_state(server.session_requests[1]["callback_url"], "claude callback_url")
        assert codex_callback_state != claude_callback_state
        assert server.session_requests[0]["deployment_instance_id"] == "worker-local-1"
        assert server.session_requests[0]["target"] == "codex"
        assert server.session_requests[0]["plugin_id"] == "promptless-instruction-hub-core"
        assert server.session_requests[0]["plugin_version"] == "0.1.0"
        assert server.session_requests[0]["package_id"] == "core"
        assert server.session_requests[0]["bootstrap_version"] == "0.2.0"
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
            assert check_in["bootstrap_version"] == "0.2.0"
            assert check_in["plugin_version"] == "0.1.0"
            assert check_in["status"] == "needs_restart"
            assert check_in["needs_restart"] is True
            effective_config = _json_mapping(check_in["effective_config"], "effective_config")
            assert effective_config["configured"] is True
            assert not {
                "user_prompts_enabled",
                "tool_inputs_enabled",
                "tool_outputs_enabled",
                "raw_api_bodies_enabled",
            }.intersection(effective_config)
        codex_effective_config = _json_mapping(server.check_ins[0]["effective_config"], "codex effective_config")
        claude_effective_config = _json_mapping(server.check_ins[1]["effective_config"], "claude effective_config")
        assert codex_effective_config["collector_metrics_endpoint"] is None
        assert claude_effective_config["collector_metrics_endpoint"] == "http://127.0.0.1:4318/v1/metrics"
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

        assert (home / ".codex/config.toml").exists()
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
        original_codex_config = 'model = "gpt-5"\n[profiles.local]\nmodel = "gpt-5-codex"\n'
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
        )

        assert original_codex_config.rstrip() in codex_config.read_text()
        codex_backups = list(codex_config.parent.glob("config.toml.*.bak"))
        assert len(codex_backups) == 1
        assert codex_backups[0].read_text() == original_codex_config
        assert list(codex_config.parent.glob(".config.toml.*.tmp")) == []

        claude_home = tmp_path / "claude-home"
        claude_settings = claude_home / ".claude/settings.json"
        claude_settings.parent.mkdir(parents=True)
        original_claude_settings = {"env": {"CUSTOM_ENV": "1"}, "theme": "dark"}
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
        )

        updated_claude_settings = json.loads(claude_settings.read_text())
        assert updated_claude_settings["theme"] == "dark"
        assert updated_claude_settings["env"]["CUSTOM_ENV"] == "1"
        assert updated_claude_settings["env"]["PROMPTLESS_MANAGED_HOST_ENROLLMENT"] == "1"
        claude_backups = list(claude_settings.parent.glob("settings.json.*.bak"))
        assert len(claude_backups) == 1
        assert json.loads(claude_backups[0].read_text()) == original_claude_settings
        assert list(claude_settings.parent.glob(".settings.json.*.tmp")) == []
    finally:
        server.stop()


def test_bootstrap_repairs_stale_managed_host_otel_config(tmp_path: Path) -> None:
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
                "",
                "[otel.trace_exporter.otlp-http]",
                'endpoint = "http://stale.local:4318/v1/traces"',
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
        )

        updated_codex_config = codex_config.read_text()
        assert updated_codex_config.count(managed_begin) == 1
        assert updated_codex_config.count(managed_end) == 1
        assert 'model = "gpt-5"' in updated_codex_config
        assert "[profiles.local]" in updated_codex_config
        assert "stale.local" not in updated_codex_config
        assert 'endpoint = "http://127.0.0.1:4318/v1/logs"' in updated_codex_config
        assert 'endpoint = "http://127.0.0.1:4318/v1/traces"' in updated_codex_config
        assert updated_codex_config.count('protocol = "binary"') == 2
        codex_otel = tomllib.loads(updated_codex_config)["otel"]
        assert codex_otel["environment"] == "prod"
        assert codex_otel["log_user_prompt"] is True
        assert codex_otel["exporter"]["otlp-http"]["headers"] == {"Authorization": "Bearer otlp-token"}
        assert codex_otel["trace_exporter"]["otlp-http"]["headers"] == {"Authorization": "Bearer otlp-token"}
        codex_backups = list(codex_config.parent.glob("config.toml.*.bak"))
        assert len(codex_backups) == 1
        assert codex_backups[0].read_text() == original_codex_config
        assert codex_payload["status"] == "needs_restart"
        codex_drift_reports = _json_list(server.check_ins[-1]["drift_reports"], "codex drift_reports")
        codex_report = _json_mapping(codex_drift_reports[0], "codex drift_reports[0]")
        assert codex_report["kind"] == "repaired_user_config"
        assert codex_report["repaired"] is True

        original_claude_settings = {
            "env": {
                "CUSTOM_ENV": "1",
                "PROMPTLESS_MANAGED_HOST_ENROLLMENT": "1",
                "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
                "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": False,
                "ENABLE_BETA_TRACING_DETAILED": "0",
                "BETA_TRACING_ENDPOINT": "http://stale.local:4318/v1/traces",
                "OTEL_LOGS_EXPORTER": "none",
                "OTEL_METRICS_EXPORTER": ["bad"],
                "OTEL_TRACES_EXPORTER": "none",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://stale.local:4318/v1/logs",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://stale.local:4318/v1/traces",
                "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://stale.local:4318/v1/metrics",
                "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer stale-token",
                "OTEL_LOG_USER_PROMPTS": "0",
                "OTEL_LOG_ASSISTANT_RESPONSES": "1",
                "OTEL_LOG_TOOL_DETAILS": {"bad": "type"},
                "OTEL_LOG_TOOL_CONTENT": "0",
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
        )

        updated_claude_settings = json.loads(claude_settings.read_text())
        updated_env = updated_claude_settings["env"]
        assert updated_claude_settings["theme"] == "dark"
        assert updated_env["CUSTOM_ENV"] == "1"
        assert updated_env["PROMPTLESS_MANAGED_HOST_ENROLLMENT"] == "1"
        assert updated_env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert updated_env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
        assert updated_env["ENABLE_BETA_TRACING_DETAILED"] == "1"
        assert updated_env["BETA_TRACING_ENDPOINT"] == "http://127.0.0.1:4318/v1/traces"
        assert updated_env["OTEL_LOGS_EXPORTER"] == "otlp"
        assert updated_env["OTEL_METRICS_EXPORTER"] == "otlp"
        assert updated_env["OTEL_TRACES_EXPORTER"] == "otlp"
        assert updated_env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
        assert updated_env["OTEL_EXPORTER_OTLP_LOGS_PROTOCOL"] == "http/protobuf"
        assert updated_env["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"] == "http://127.0.0.1:4318/v1/logs"
        assert updated_env["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] == "http/protobuf"
        assert updated_env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "http://127.0.0.1:4318/v1/traces"
        assert updated_env["OTEL_EXPORTER_OTLP_METRICS_PROTOCOL"] == "http/protobuf"
        assert updated_env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] == "http://127.0.0.1:4318/v1/metrics"
        assert updated_env["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer otlp-token"
        assert updated_env["OTEL_LOG_USER_PROMPTS"] == "1"
        assert updated_env["OTEL_LOG_ASSISTANT_RESPONSES"] == "1"
        assert updated_env["OTEL_LOG_TOOL_DETAILS"] == "1"
        assert updated_env["OTEL_LOG_TOOL_CONTENT"] == "1"
        assert updated_env["OTEL_LOG_RAW_API_BODIES"] == (
            f"file:{claude_home / '.promptless/instruction-hub/claude-raw-api-bodies'}"
        )
        claude_backups = list(claude_settings.parent.glob("settings.json.*.bak"))
        assert len(claude_backups) == 1
        assert json.loads(claude_backups[0].read_text()) == original_claude_settings
        assert claude_payload["status"] == "needs_restart"
        claude_drift_reports = _json_list(server.check_ins[-1]["drift_reports"], "claude drift_reports")
        claude_report = _json_mapping(claude_drift_reports[0], "claude drift_reports[0]")
        assert claude_report["kind"] == "repaired_user_config"
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

        codex_payload, _ = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(codex_home),
                "CODEX_HOME": str(codex_home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
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


def test_bootstrap_preserves_unmanaged_host_config(tmp_path: Path) -> None:
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

        codex_payload, _ = _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(codex_home),
                "CODEX_HOME": str(codex_home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="blocked",
        )

        assert codex_config.read_text() == '[otel]\nenvironment = "local"\n'
        assert server.check_ins[-1]["status"] == "blocked"
        assert "blocked" in _json_string(codex_payload["systemMessage"], "systemMessage").lower()

        claude_home = tmp_path / "claude-home"
        claude_settings = claude_home / ".claude/settings.json"
        claude_settings.parent.mkdir(parents=True)
        claude_settings.write_text('{"env":{"OTEL_EXPORTER_OTLP_HEADERS":"Authorization=Bearer customer-token"}}\n')

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
            expected_status="blocked",
        )

        assert (
            claude_settings.read_text()
            == '{"env":{"OTEL_EXPORTER_OTLP_HEADERS":"Authorization=Bearer customer-token"}}\n'
        )
        assert server.check_ins[-1]["status"] == "blocked"
        assert "blocked" in _json_string(claude_payload["systemMessage"], "systemMessage").lower()
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
        claude_env = {
            "HOME": str(claude_home),
            "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
            "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
            "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        # Fresh config write surfaces a restart prompt naming the host; the steady state is silent.
        first_claude, _ = _run_bootstrap(hub_root / "dist/claude/core", "claude", claude_env)
        claude_message = _json_string(first_claude["systemMessage"], "systemMessage")
        assert "Claude Code" in claude_message
        assert "to start telemetry" in claude_message

        steady_claude, _ = _run_bootstrap(
            hub_root / "dist/claude/core", "claude", claude_env, expected_status="configured"
        )
        assert "systemMessage" not in steady_claude

        codex_home = tmp_path / "codex-home"
        codex_env = {
            "HOME": str(codex_home),
            "CODEX_HOME": str(codex_home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        first_codex, _ = _run_bootstrap(hub_root / "dist/codex/core", "codex", codex_env)
        codex_message = _json_string(first_codex["systemMessage"], "systemMessage")
        assert "Codex" in codex_message
        assert "to start telemetry" in codex_message

        steady_codex, _ = _run_bootstrap(hub_root / "dist/codex/core", "codex", codex_env, expected_status="configured")
        assert "systemMessage" not in steady_codex
    finally:
        server.stop()


def test_bootstrap_configures_claude_raw_api_bodies_file_capture(tmp_path: Path) -> None:
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

        claude_settings = json.loads((claude_home / ".claude/settings.json").read_text())
        raw_api_bodies_env = _json_string(claude_settings["env"]["OTEL_LOG_RAW_API_BODIES"], "raw API bodies env")
        assert raw_api_bodies_env.startswith("file:")
        raw_api_bodies_path = Path(raw_api_bodies_env.removeprefix("file:"))
        assert raw_api_bodies_path == claude_home / ".promptless/instruction-hub/claude-raw-api-bodies"
        assert claude_settings["env"]["OTEL_LOG_TOOL_CONTENT"] == "1"
        assert "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT" not in claude_settings["env"]
        assert raw_api_bodies_path.is_dir()
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
        codex_env = {
            "HOME": str(tmp_path / "codex-home"),
            "CODEX_HOME": str(tmp_path / "codex-home/.codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        # Fresh browser enrollment records the start banner in diagnostics but leaves stdout for
        # the final actionable restart message.
        configured_payload, configured_result = _run_bootstrap(hub_root / "dist/codex/core", "codex", codex_env)
        configured_stdout = _json_mapping(
            validate_json_value(json.loads(configured_result.stdout), "codex stdout"), "codex stdout"
        )
        assert set(configured_stdout) == {"systemMessage"}
        assert configured_stdout["systemMessage"] == configured_payload["systemMessage"]
        assert "Restart Codex" in _json_string(configured_stdout["systemMessage"], "systemMessage")
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

        # First install on each host records the version silently (an install is not an update),
        # so the only message is the fresh-config restart prompt, never an "updated" notice.
        first_claude, _ = _run_bootstrap(hub_root / "dist/claude/core", "claude", claude_env)
        assert "updated" not in _json_string(first_claude["systemMessage"], "systemMessage").lower()
        first_codex, _ = _run_bootstrap(hub_root / "dist/codex/core", "codex", codex_env)
        assert "updated" not in _json_string(first_codex["systemMessage"], "systemMessage").lower()

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


def test_bootstrap_second_run_reports_configured_without_duplicate_config(tmp_path: Path) -> None:
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
        _run_bootstrap(hub_root / "dist/codex/core", "codex", codex_env, expected_status="configured")
        codex_config = (codex_home / ".codex/config.toml").read_text()
        assert codex_config.count("BEGIN PROMPTLESS MANAGED HOST ENROLLMENT") == 1

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
        first_settings = settings_path.read_text()
        _run_bootstrap(hub_root / "dist/claude/core", "claude", claude_env, expected_status="configured")
        assert settings_path.read_text() == first_settings
        assert [check_in["status"] for check_in in server.check_ins] == [
            "needs_restart",
            "configured",
            "needs_restart",
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
        "wrong-logs-path",
    ],
)
def test_bootstrap_rejects_invalid_worker_policy(tmp_path: Path, case: str) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer(policy=_invalid_policy(case))
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
            expected_status="error",
        )

        assert not (home / ".codex/config.toml").exists()
        assert server.check_ins == []
    finally:
        server.stop()


def test_bootstrap_blocks_when_worker_requires_newer_runtime(tmp_path: Path) -> None:
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
    expected_status: str = "needs_restart",
) -> tuple[dict[str, JsonValue], subprocess.CompletedProcess[str]]:
    result = subprocess.run(
        [str(plugin_root / "bin" / HOST_RUNTIME_BIN), "ensure", "--host", host],
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
        [str(plugin_root / "bin" / HOST_RUNTIME_BIN), *args],
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


def _start_bootstrap(plugin_root: Path, host: str, env: dict[str, str]) -> subprocess.Popen[str]:
    process_env = _clean_env()
    process_env.update(env)
    if "PROMPTLESS_WORKER_BASE_URL" in process_env and "PROMPTLESS_DASHBOARD_BASE_URL" not in process_env:
        process_env["PROMPTLESS_DASHBOARD_BASE_URL"] = process_env["PROMPTLESS_WORKER_BASE_URL"]
    return subprocess.Popen(
        [str(plugin_root / "bin" / HOST_RUNTIME_BIN), "ensure", "--host", host],
        env=process_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _read_bootstrap_process(
    process: subprocess.Popen[str],
    *,
    expected_status: str = "needs_restart",
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


def _json_mapping(value: JsonValue, field_path: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{field_path} must be a JSON object"
    return value


def _json_list(value: JsonValue, field_path: str) -> list[JsonValue]:
    assert isinstance(value, list), f"{field_path} must be a JSON array"
    return value


def _json_string(value: JsonValue, field_path: str) -> str:
    assert isinstance(value, str), f"{field_path} must be a JSON string"
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
    collector = _json_mapping(policy["collector"], "policy.collector")
    permissions = _json_mapping(policy["plugin_permissions"], "policy.plugin_permissions")

    if case == "expired":
        policy["expires_at"] = (now - dt.timedelta(minutes=1)).isoformat()
    elif case == "missing-write-permission":
        permissions["write_user_config"] = False
    elif case == "wrong-logs-path":
        collector["otlp_http_logs_endpoint"] = "http://127.0.0.1:4318/not-logs"
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


class _FakeWorkerServer:
    def __init__(
        self,
        *,
        policy: dict[str, JsonValue] | None = None,
        post_response: dict[str, JsonValue] | None = None,
        session_response: dict[str, JsonValue] | None = None,
        session_barrier_count: int = 0,
        callback_state_override: str | None = None,
    ) -> None:
        self.check_ins: list[dict[str, JsonValue]] = []
        self.policy_requests: list[str] = []
        self.poll_requests: list[dict[str, JsonValue]] = []
        self.session_requests: list[dict[str, JsonValue]] = []
        self._session_condition = threading.Condition()
        _FakeWorkerHandler.check_ins = self.check_ins
        _FakeWorkerHandler.policy_requests = self.policy_requests
        _FakeWorkerHandler.poll_requests = self.poll_requests
        _FakeWorkerHandler.session_requests = self.session_requests
        _FakeWorkerHandler.policy_response = policy or _signed_policy()
        _FakeWorkerHandler.post_response = post_response
        _FakeWorkerHandler.session_response = session_response
        _FakeWorkerHandler.session_barrier_count = session_barrier_count
        _FakeWorkerHandler.session_condition = self._session_condition
        _FakeWorkerHandler.callback_state_override = callback_state_override
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
    policy_response: ClassVar[dict[str, JsonValue]]
    post_response: ClassVar[dict[str, JsonValue] | None]
    session_response: ClassVar[dict[str, JsonValue] | None]
    session_barrier_count: ClassVar[int] = 0
    session_condition: ClassVar[threading.Condition | None] = None
    session_requests: ClassVar[list[dict[str, JsonValue]]] = []
    callback_state_override: ClassVar[str | None] = None

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
            hosted_approval_url = f"{self._base_url()}/instruction-hub/enroll?{urlencode(approval_params)}"
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
            or target not in (["codex"], ["claude"])
            or self.headers.get("Authorization") != "Bearer plihost_localcredential"
        ):
            self.send_response(401)
            self.end_headers()
            return
        self.policy_requests.append(self.path)
        self._write_json(self.policy_response)

    def do_POST(self) -> None:
        if self.path == "/v1/instruction-hub/host-enrollments/sessions/11111111-1111-4111-8111-111111111111/poll":
            payload = self._read_json_request("session poll request")
            if payload.get("device_code") != "plihenroll_devicecode":
                self.send_response(401)
                self.end_headers()
                return
            self.poll_requests.append(payload)
            self._write_json(
                {
                    "status": "approved",
                    "host_credential": "plihost_localcredential",
                    "credential_id": "22222222-2222-4222-8222-222222222222",
                    "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
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
