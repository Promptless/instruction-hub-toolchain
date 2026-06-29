from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, validate_json_value

BOOTSTRAP_BIN = "promptless-host-enrollment-bootstrap"


def _assert_no_promptless_directory(root: Path) -> None:
    assert list(root.rglob(".promptless")) == []


def test_build_injects_managed_bootstrap_runtime(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")

    build_hub(hub_root)

    for target in ("codex", "claude"):
        plugin_root = hub_root / "dist" / target / "core"
        bootstrap_path = plugin_root / "bin" / BOOTSTRAP_BIN
        assert bootstrap_path.exists()
        assert os.access(bootstrap_path, os.X_OK)
        hooks = json.loads((plugin_root / "hooks/hooks.json").read_text())
        hook = hooks["hooks"]["SessionStart"][0]["hooks"][0]
        if target == "claude":
            hook_command = hook["command"]
            assert hook_command == f'python3 "${{CLAUDE_PLUGIN_ROOT}}/bin/{BOOTSTRAP_BIN}" --host claude --quiet'
        else:
            hook_command = hook["command"]
            assert hook_command == f'python3 "${{PLUGIN_ROOT}}/bin/{BOOTSTRAP_BIN}" --host codex --quiet'
        assert "--quiet" in hook_command
        assert hook["timeout"] == 45
        metadata = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())
        assert not (plugin_root / ".promptless").exists()
        runtime = metadata["managed_runtimes"][0]
        assert runtime["id"] == "host-enrollment-bootstrap"
        assert runtime["status"] == "included"
        assert runtime["target"] == target
        assert runtime["version"] == "0.1.0"
        assert runtime["channel"] == "stable"
        assert runtime["path"] == f"bin/{BOOTSTRAP_BIN}"
        assert len(runtime["sha256"]) == 64

    codex_manifest = json.loads((hub_root / "dist/codex/core/.codex-plugin/plugin.json").read_text())
    assert codex_manifest["hooks"] == "./hooks/hooks.json"

    for target in ("cursor", "gemini"):
        plugin_root = hub_root / "dist" / target / "core"
        assert not (plugin_root / "bin" / BOOTSTRAP_BIN).exists()
        assert not (plugin_root / "hub.managed-runtimes.json").exists()

    release_manifest = json.loads((hub_root / "hub.release.json").read_text())
    assert {runtime["target"] for runtime in release_manifest["managed_runtimes"]} == {"codex", "claude"}
    _assert_no_promptless_directory(hub_root)


def test_bootstrap_unreachable_worker_exits_zero_without_config_write(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"

    result = subprocess.run(
        [str(hub_root / "dist/codex/core/bin" / BOOTSTRAP_BIN), "--host", "codex"],
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
    assert json.loads(result.stderr)["status"] == "error"
    assert not (home / ".codex/config.toml").exists()

    quiet_result = subprocess.run(
        [str(hub_root / "dist/codex/core/bin" / BOOTSTRAP_BIN), "--host", "codex", "--quiet"],
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


def test_bootstrap_requires_local_pigs_fly_flag_before_auth_flow(tmp_path: Path) -> None:
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
            expected_status="disabled",
            enable_bootstrap=False,
        )

        assert payload["reason"] == "pigs_fly_not_enabled"
        assert result.stderr == ""
        assert not (home / ".codex/config.toml").exists()
        assert server.session_requests == []
        assert server.policy_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


def test_bootstrap_uses_plugin_data_for_state_file(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()

        home = tmp_path / "home"
        _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )

        assert len(server.check_ins) == 1
        assert server.check_ins[0]["host"] == "codex"
        assert len(server.session_requests) == 1
        assert (plugin_data / "host-enrollment-state.json").exists()
    finally:
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
    assert result.stdout == ""


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
        assert claude_settings["env"]["PROMPTLESS_MANAGED_HOST_ENROLLMENT"] == "1"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"] == "http://127.0.0.1:4318/v1/logs"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer otlp-token"
        assert claude_settings["env"]["OTEL_LOG_USER_PROMPTS"] == "1"
        assert claude_settings["env"]["OTEL_LOG_ASSISTANT_RESPONSES"] == "0"

        assert len(server.session_requests) == 2
        assert server.session_requests[0]["deployment_instance_id"] == "worker-local-1"
        assert server.session_requests[0]["target"] == "codex"
        assert server.session_requests[0]["plugin_id"] == "promptless-instruction-hub-core"
        assert server.session_requests[0]["plugin_version"] == "0.1.0"
        assert server.session_requests[0]["package_id"] == "core"
        assert server.session_requests[0]["bootstrap_version"] == "0.1.0"
        assert server.session_requests[0]["toolchain_version"] != "unknown"
        assert server.session_requests[1]["target"] == "claude"
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
            assert check_in["bootstrap_version"] == "0.1.0"
            assert check_in["plugin_version"] == "0.1.0"
            assert check_in["status"] == "needs_restart"
            assert check_in["needs_restart"] is True
            effective_config = _json_mapping(check_in["effective_config"], "effective_config")
            assert effective_config["configured"] is True
        codex_effective_config = _json_mapping(server.check_ins[0]["effective_config"], "codex effective_config")
        claude_effective_config = _json_mapping(server.check_ins[1]["effective_config"], "claude effective_config")
        assert codex_effective_config["collector_metrics_endpoint"] is None
        assert claude_effective_config["collector_metrics_endpoint"] == "http://127.0.0.1:4318/v1/metrics"
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


def test_bootstrap_blocks_unsupported_codex_capture_policy_values(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer(
        policy=_policy_with(
            capture_policy={
                "user_prompts": "full_local_default",
                "tool_inputs": "disabled",
                "tool_outputs": "full_local_default",
                "raw_api_bodies": "disabled",
            }
        )
    )
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
            expected_status="blocked",
        )

        assert not (codex_home / ".codex/config.toml").exists()
        drift_reports = _json_list(server.check_ins[0]["drift_reports"], "drift_reports")
        first_drift_report = _json_mapping(drift_reports[0], "drift_reports[0]")
        details = _json_mapping(first_drift_report["details"], "drift_reports[0].details")
        assert details["capture_policy_keys"] == ["tool_inputs"]
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
    assert f"bin/{BOOTSTRAP_BIN}" in session_start[1]["hooks"][0]["command"]


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

        _run_bootstrap(
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
            expected_status="blocked",
        )

        assert (
            claude_settings.read_text()
            == '{"env":{"OTEL_EXPORTER_OTLP_HEADERS":"Authorization=Bearer customer-token"}}\n'
        )
        assert server.check_ins[-1]["status"] == "blocked"
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
        "invalid-capture-value",
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
    server = _FakeWorkerServer(policy=_policy_with(required_bootstrap_version="0.2.0"))
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


def _run_bootstrap(
    plugin_root: Path,
    host: str,
    env: dict[str, str],
    *,
    expected_status: str = "needs_restart",
    enable_bootstrap: bool = True,
) -> tuple[dict[str, JsonValue], subprocess.CompletedProcess[str]]:
    result = subprocess.run(
        [str(plugin_root / "bin" / BOOTSTRAP_BIN), "--host", host],
        env=_clean_env(enable_bootstrap=enable_bootstrap, **env),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "plihost_localcredential" not in result.stdout
    assert "plihost_localcredential" not in result.stderr
    assert "plihenroll_devicecode" not in result.stdout
    assert "plihenroll_devicecode" not in result.stderr
    payload_text = result.stdout.strip() or result.stderr.strip()
    payload = (
        _json_mapping(validate_json_value(json.loads(payload_text), "bootstrap output"), "bootstrap output")
        if payload_text
        else {}
    )
    assert payload["status"] == expected_status
    return payload, result


def _clean_env(*, enable_bootstrap: bool = True, **overrides: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES": "1",
        "PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER": "0",
    }
    if enable_bootstrap:
        env["PIGS_FLY"] = "True"
    env.update(overrides)
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
    capture_policy = _json_mapping(policy["capture_policy"], "policy.capture_policy")
    permissions = _json_mapping(policy["plugin_permissions"], "policy.plugin_permissions")

    if case == "expired":
        policy["expires_at"] = (now - dt.timedelta(minutes=1)).isoformat()
    elif case == "missing-write-permission":
        permissions["write_user_config"] = False
    elif case == "wrong-logs-path":
        collector["otlp_http_logs_endpoint"] = "http://127.0.0.1:4318/not-logs"
    elif case == "invalid-capture-value":
        capture_policy["tool_outputs"] = "full"
    else:
        raise AssertionError(f"unhandled invalid policy case: {case}")
    return payload


class _FakeWorkerServer:
    def __init__(
        self,
        *,
        policy: dict[str, JsonValue] | None = None,
        post_response: dict[str, JsonValue] | None = None,
    ) -> None:
        self.check_ins: list[dict[str, JsonValue]] = []
        self.policy_requests: list[str] = []
        self.poll_requests: list[dict[str, JsonValue]] = []
        self.session_requests: list[dict[str, JsonValue]] = []
        _FakeWorkerHandler.check_ins = self.check_ins
        _FakeWorkerHandler.policy_requests = self.policy_requests
        _FakeWorkerHandler.poll_requests = self.poll_requests
        _FakeWorkerHandler.session_requests = self.session_requests
        _FakeWorkerHandler.policy_response = policy or _signed_policy()
        _FakeWorkerHandler.post_response = post_response
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
    session_requests: ClassVar[list[dict[str, JsonValue]]] = []

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._write_json(
                {
                    "status": "ok",
                    "deployment_instance_id": "worker-local-1",
                    "worker_version": "test",
                }
            )
            return
        parsed = urlsplit(self.path)
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
        if self.path == "/v0/host-enrollment/sessions":
            payload = self._read_json_request("session create request")
            self.session_requests.append(payload)
            self._write_json(
                {
                    "session_id": "11111111-1111-4111-8111-111111111111",
                    "device_code": "plihenroll_devicecode",
                    "approval_url": "https://app.promptless.ai/instruction-hub/enroll?token=approval-token",
                    "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
                    "poll_interval_seconds": 1,
                }
            )
            return
        if self.path == "/v0/host-enrollment/sessions/11111111-1111-4111-8111-111111111111/poll":
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

    def _read_json_request(self, label: str) -> dict[str, JsonValue]:
        length = int(self.headers["Content-Length"])
        return _json_mapping(
            validate_json_value(json.loads(self.rfile.read(length)), label),
            label,
        )


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
            "capture_policy": {
                "user_prompts": "full_local_default",
                "tool_inputs": "full_local_default",
                "tool_outputs": "full_local_default",
                "raw_api_bodies": "disabled",
            },
            "plugin_permissions": {
                "write_user_config": True,
                "repair_user_config": True,
            },
            "required_bootstrap_version": "0.1.0",
        },
        "signature": "hmac-sha256-v1:test",
        "signed_at": now.isoformat(),
    }
