from __future__ import annotations

import json
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.fs import validate_json_value

from .helpers import (
    BROWSER_ENROLLMENT_MESSAGE,
    FIRST_SUCCESS_ACTIVE_FRAGMENT,
    FIRST_SUCCESS_NO_RESTART_FRAGMENT,
    FIRST_SUCCESS_SHOWN_KEY,
    INTERNAL_WELCOME_SHOWN_AT_KEY,
    INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY,
    _FakeWorkerServer,
    _bootstrap_diagnostics,
    _credential_cache_key,
    _host_state_path,
    _invalid_policy,
    _json_list,
    _json_mapping,
    _json_string,
    _policy_with,
    _run_bootstrap,
    _run_collect,
)


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
            hub_root / "dist/codex/pig",
            "codex",
            {
                "HOME": str(codex_home),
                "CODEX_HOME": str(codex_home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
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
            hub_root / "dist/claude/pig",
            "claude",
            {
                "HOME": str(claude_home),
                "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
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
            hub_root / "dist/codex/pig",
            "codex",
            {
                "HOME": str(codex_home),
                "CODEX_HOME": str(codex_home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
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
            hub_root / "dist/claude/pig",
            "claude",
            {
                "HOME": str(claude_home),
                "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
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
            "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        codex_payload, _ = _run_bootstrap(
            hub_root / "dist/codex/pig",
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
        repaired_payload, _ = _run_bootstrap(hub_root / "dist/codex/pig", "codex", codex_env)
        repaired_message = _json_string(repaired_payload["systemMessage"], "systemMessage")
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT in repaired_message
        repaired_state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(codex_home).read_text()), "host state"),
            "host state",
        )
        assert "codex" in _json_mapping(repaired_state[FIRST_SUCCESS_SHOWN_KEY], "first-success shown")
    finally:
        server.stop()


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
            hub_root / "dist/codex/pig",
            "codex",
            {
                "HOME": str(codex_home),
                "CODEX_HOME": str(codex_home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
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
            hub_root / "dist/claude/pig",
            "claude",
            {
                "HOME": str(claude_home),
                "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
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
            "PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
            "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        # Removing legacy managed config surfaces a restart prompt naming the host; the
        # steady state is silent.
        first_claude, _ = _run_bootstrap(
            hub_root / "dist/claude/pig", "claude", claude_env, expected_status="needs_restart"
        )
        claude_message = _json_string(first_claude["systemMessage"], "systemMessage")
        assert "Claude Code" in claude_message
        assert "removed" in claude_message.lower()

        steady_claude, _ = _run_bootstrap(hub_root / "dist/claude/pig", "claude", claude_env)
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
            "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        first_codex, _ = _run_bootstrap(
            hub_root / "dist/codex/pig", "codex", codex_env, expected_status="needs_restart"
        )
        codex_message = _json_string(first_codex["systemMessage"], "systemMessage")
        assert "Codex" in codex_message
        assert "removed" in codex_message.lower()

        steady_codex, _ = _run_bootstrap(hub_root / "dist/codex/pig", "codex", codex_env)
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
            hub_root / "dist/claude/pig",
            "claude",
            {
                "HOME": str(claude_home),
                "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
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
            "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        # Fresh browser enrollment records the start banner in diagnostics but leaves stdout for
        # the final actionable restart message emitted by the managed-config cleanup.
        configured_payload, configured_result = _run_bootstrap(
            hub_root / "dist/codex/pig", "codex", codex_env, expected_status="needs_restart"
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
        _, steady_result = _run_bootstrap(hub_root / "dist/codex/pig", "codex", codex_env, expected_status="configured")
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
            "PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
            "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        codex_env = {
            "HOME": str(tmp_path / "codex-home"),
            "CODEX_HOME": str(tmp_path / "codex-home/.codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        # First install on each host records the version but is not an update, so it shows the
        # one-time first-success confirmation without any version-change notice.
        first_claude, _ = _run_bootstrap(hub_root / "dist/claude/pig", "claude", claude_env)
        first_claude_message = _json_string(first_claude["systemMessage"], "systemMessage")
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT in first_claude_message
        assert "updated to" not in first_claude_message
        first_codex, _ = _run_bootstrap(hub_root / "dist/codex/pig", "codex", codex_env)
        first_codex_message = _json_string(first_codex["systemMessage"], "systemMessage")
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT in first_codex_message
        assert "updated to" not in first_codex_message

        # Rebuild the same hub at a newer version, then re-run: each host announces the change once.
        build_hub(hub_root, plugin_version="0.2.0")
        upgraded_claude, _ = _run_bootstrap(
            hub_root / "dist/claude/pig", "claude", claude_env, expected_status="configured"
        )
        claude_message = _json_string(upgraded_claude["systemMessage"], "systemMessage")
        assert "0.2.0" in claude_message and "0.1.0" in claude_message

        upgraded_codex, _ = _run_bootstrap(
            hub_root / "dist/codex/pig", "codex", codex_env, expected_status="configured"
        )
        codex_message = _json_string(upgraded_codex["systemMessage"], "systemMessage")
        assert "0.2.0" in codex_message and "0.1.0" in codex_message

        # A subsequent run at the same version is silent again.
        steady_claude, _ = _run_bootstrap(
            hub_root / "dist/claude/pig", "claude", claude_env, expected_status="configured"
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
            hub_root / "dist/codex/pig",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
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
                "PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
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
        _run_bootstrap(hub_root / "dist/claude/pig", "claude", claude_env(server.base_url))
        assert seen_claude_version() == "0.1.0"

        # Upgrade, then hit a failing session (unreachable worker): the new version must NOT be
        # marked seen, because its update notice was never surfaced.
        build_hub(hub_root, plugin_version="0.2.0")
        _run_bootstrap(
            hub_root / "dist/claude/pig",
            "claude",
            claude_env("http://127.0.0.1:9"),
            expected_status="error",
        )
        assert seen_claude_version() == "0.1.0"

        # The next healthy session still surfaces the one-time update notice and records v0.2.0.
        recovered, _ = _run_bootstrap(
            hub_root / "dist/claude/pig", "claude", claude_env(server.base_url), expected_status="configured"
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
            "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        _run_bootstrap(hub_root / "dist/codex/pig", "codex", codex_env)
        _run_bootstrap(hub_root / "dist/codex/pig", "codex", codex_env)
        assert not (codex_home / ".codex/config.toml").exists()

        claude_home = tmp_path / "claude-home"
        claude_env = {
            "HOME": str(claude_home),
            "CLAUDE_CONFIG_DIR": str(claude_home / ".claude"),
            "PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
            "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        _run_bootstrap(hub_root / "dist/claude/pig", "claude", claude_env)
        settings_path = claude_home / ".claude/settings.json"
        _run_bootstrap(hub_root / "dist/claude/pig", "claude", claude_env)
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
            "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        _run_bootstrap(
            hub_root / "dist/codex/pig",
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
                hub_root / "dist/codex/pig",
                "codex",
                {
                    "HOME": str(home),
                    "CODEX_HOME": str(home / ".codex"),
                    "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
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
    plugin_root = hub_root / "dist/codex/pig"
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
            hub_root / "dist/codex/pig",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
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
            hub_root / "dist/codex/pig",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "check-in response was not accepted" in str(payload["message"])
        assert len(server.check_ins) == 1
    finally:
        server.stop()
