from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.fs import validate_json_value
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime import (
    enrollment as host_enrollment,
)

from .helpers import (
    BROWSER_ENROLLMENT_MESSAGE,
    FIRST_SUCCESS_ACTIVE_FRAGMENT,
    FIRST_SUCCESS_NO_RESTART_FRAGMENT,
    FIRST_SUCCESS_SHOWN_KEY,
    HOST_RUNTIME_BIN,
    INTERNAL_WELCOME_SHOWN_AT_KEY,
    INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY,
    PENDING_FIRST_SUCCESS_KEY,
    _FakeWorkerServer,
    _approved_poll_response,
    _assert_session_start_streams,
    _async_urlopen_browser_command,
    _bootstrap_diagnostics,
    _callback_state,
    _clean_env,
    _clone_plugin_with_identity,
    _credential_cache_key,
    _host_state_path,
    _json_mapping,
    _json_string,
    _last_status_path,
    _policy_with,
    _read_any_bootstrap_status,
    _read_bootstrap_process,
    _run_bootstrap,
    _run_runtime_json,
    _start_bootstrap,
)


def test_bootstrap_unreachable_worker_exits_zero_without_config_write(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"

    result = subprocess.run(
        [str(hub_root / "dist/codex/pig/runtime" / HOST_RUNTIME_BIN), "ensure", "--host", "codex"],
        env=_clean_env(
            HOME=str(home),
            CODEX_HOME=str(home / ".codex"),
            PLUGIN_ROOT=str(hub_root / "dist/codex/pig"),
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
        [str(hub_root / "dist/codex/pig/runtime" / HOST_RUNTIME_BIN), "ensure", "--host", "codex", "--quiet"],
        env=_clean_env(
            HOME=str(home),
            CODEX_HOME=str(home / ".codex"),
            PLUGIN_ROOT=str(hub_root / "dist/codex/pig"),
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
            hub_root / "dist/codex/pig",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
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
            "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        first_payload, first_result = _run_bootstrap(hub_root / "dist/codex/pig", "codex", env)
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
            hub_root / "dist/codex/pig", "codex", env, expected_status="configured"
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
            hub_root / "dist/codex/pig",
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
            hub_root / "dist/codex/pig",
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
            "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        payload, _ = _run_bootstrap(hub_root / "dist/codex/pig", "codex", env)
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
        plugin_root = hub_root / "dist/codex/pig"
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
            "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        payload, _ = _run_bootstrap(hub_root / "dist/codex/pig", "codex", env)
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
            "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        # The first healthy enrollment confirms once and reassures the user no restart is needed.
        first_payload, first_result = _run_bootstrap(hub_root / "dist/codex/pig", "codex", codex_env)
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
            hub_root / "dist/codex/pig", "codex", codex_env, expected_status="configured"
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
            "PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
            "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        claude_payload, _ = _run_bootstrap(hub_root / "dist/claude/pig", "claude", claude_env)
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


def test_session_start_supervisor_records_status_without_consuming_user_notices(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        plugin_root = hub_root / "dist/codex/pig"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        background = subprocess.run(
            [
                str(plugin_root / "runtime" / HOST_RUNTIME_BIN),
                "session-start",
                "--host",
                "codex",
                "--supervised",
            ],
            env=_clean_env(**env),
            input="{}",
            text=True,
            capture_output=True,
            check=False,
        )

        assert background.returncode == 0
        background_status = _json_mapping(
            validate_json_value(json.loads(_last_status_path(home).read_text()), "background status"),
            "background status",
        )
        assert background_status["status"] == "configured"
        state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "host state"),
            "host state",
        )
        assert FIRST_SUCCESS_SHOWN_KEY not in state
        assert _json_mapping(state[PENDING_FIRST_SUCCESS_KEY], "pending first-success") == {"codex": "configured"}

        foreground_payload, _ = _run_bootstrap(plugin_root, "codex", env)
        foreground_message = _json_string(foreground_payload["systemMessage"], "systemMessage")
        assert FIRST_SUCCESS_ACTIVE_FRAGMENT in foreground_message
        foreground_state = _json_mapping(
            validate_json_value(json.loads(_host_state_path(home).read_text()), "host state"),
            "host state",
        )
        assert "codex" in _json_mapping(foreground_state[FIRST_SUCCESS_SHOWN_KEY], "first-success shown")
        assert _json_mapping(foreground_state[PENDING_FIRST_SUCCESS_KEY], "pending first-success") == {}
        assert _json_mapping(foreground_state["last_seen_plugin_versions"], "last seen versions")["codex"] == "0.1.0"
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
        plugin_root = hub_root / "dist/codex/pig"
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
            hub_root / "dist/claude/pig",
            "claude",
            {
                "HOME": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
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


def test_headless_linux_does_not_attempt_browser_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "MIR_SOCKET",
        "WSL_INTEROP",
        "BROWSER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(host_enrollment.platform, "system", lambda: "Linux")

    def unexpected_browser_open(*args: object, **kwargs: object) -> bool:
        pytest.fail(f"headless enrollment attempted browser launch: args={args!r} kwargs={kwargs!r}")

    monkeypatch.setattr(host_enrollment.webbrowser, "open", unexpected_browser_open)

    assert not host_enrollment._open_hosted_enrollment_url("https://app.gopromptless.ai/instruction-hub/enroll")
    assert not host_enrollment._browser_session_available({"BROWSER": "xdg-open"}, "Linux")


@pytest.mark.parametrize("display_variable", ("DISPLAY", "WAYLAND_DISPLAY", "MIR_SOCKET", "WSL_INTEROP"))
def test_linux_browser_session_detection_accepts_graphical_or_wsl_session(display_variable: str) -> None:
    assert host_enrollment._browser_session_available({display_variable: "available"}, "Linux")
    assert host_enrollment._browser_session_available(
        {"PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER": "1"},
        "Linux",
    )


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
            hub_root / "dist/codex/pig",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(tmp_path / "plugin-data"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
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
            hub_root / "dist/codex/pig",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )
        claude_process = _start_bootstrap(
            hub_root / "dist/claude/pig",
            "claude",
            {
                "HOME": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
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


def test_bootstrap_concurrent_pig_versions_enroll_once(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    older_process: subprocess.Popen[str] | None = None
    newer_process: subprocess.Popen[str] | None = None
    try:
        # Overlapping starts from two installed pig versions share one host credential.
        # Starting both at once must open exactly one browser approval.
        home = tmp_path / "home"
        older_plugin = _clone_plugin_with_identity(
            hub_root / "dist/claude/pig",
            tmp_path / "pig-older",
            plugin_id="promptless-instruction-hub-pig",
            package_id="pig",
        )
        newer_plugin = _clone_plugin_with_identity(
            hub_root / "dist/claude/pig",
            tmp_path / "pig-newer",
            plugin_id="promptless-instruction-hub-pig",
            package_id="pig",
        )

        def claude_plugin_env(plugin_root: Path) -> dict[str, str]:
            return {
                "HOME": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "PLUGIN_ROOT": str(plugin_root),
                "CLAUDE_PLUGIN_ROOT": str(plugin_root),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            }

        older_process = _start_bootstrap(older_plugin, "claude", claude_plugin_env(older_plugin))
        newer_process = _start_bootstrap(newer_plugin, "claude", claude_plugin_env(newer_plugin))

        older_payload = _read_any_bootstrap_status(older_process)
        newer_payload = _read_any_bootstrap_status(newer_process)

        # Exactly one browser approval (one /start) and one shared host credential, no matter
        # which installed version won the enrollment-leader lock.
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
        statuses = {
            _json_string(older_payload["status"], "status"),
            _json_string(newer_payload["status"], "status"),
        }
        assert statuses & leader_statuses
        assert statuses <= leader_statuses | {"setup_pending"}
    finally:
        for process in (older_process, newer_process):
            if process is not None and process.poll() is None:
                process.kill()
        server.stop()


def test_bootstrap_rejects_plaintext_non_loopback_worker_base_url(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"

    payload, result = _run_bootstrap(
        hub_root / "dist/codex/pig",
        "codex",
        {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
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
            hub_root / "dist/claude/pig",
            "claude",
            {
                "HOME": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/pig"),
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
            hub_root / "dist/codex/pig",
            "codex",
            {
                "HOME": str(codex_home),
                "CODEX_HOME": str(codex_home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/pig"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )
        assert not (codex_home / ".codex/config.toml").exists()

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
        assert not (claude_home / ".claude/settings.json").exists()

        assert len(server.session_requests) == 2
        codex_callback_state = _callback_state(server.session_requests[0]["callback_url"], "codex callback_url")
        claude_callback_state = _callback_state(server.session_requests[1]["callback_url"], "claude callback_url")
        assert codex_callback_state != claude_callback_state
        assert server.session_requests[0]["deployment_instance_id"] == "worker-local-1"
        assert server.session_requests[0]["target"] == "codex"
        assert server.session_requests[0]["plugin_id"] == "promptless-instruction-hub-pig"
        assert server.session_requests[0]["plugin_version"] == "0.1.0"
        assert server.session_requests[0]["package_id"] == "pig"
        assert server.session_requests[0]["bootstrap_version"] == "0.2.9"
        assert server.session_requests[0]["toolchain_version"] != "unknown"
        assert server.session_requests[0]["pending_callback"] == "1"
        assert server.session_requests[1]["target"] == "claude"
        assert server.session_requests[1]["pending_callback"] == "1"
        assert server.policy_requests == [
            "/v0/host-enrollment/policy?target=codex",
            "/v0/host-enrollment/policy?target=claude",
        ]
        assert len(server.check_ins) == 2
        expected_native_root_counts = {"codex": 2, "claude": 1}
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
            assert check_in["bootstrap_version"] == "0.2.9"
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
            host = _json_string(check_in["host"], "host")
            assert effective_config["native_root_count"] == expected_native_root_counts[host]
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
            [str(hub_root / "dist/codex/pig/runtime" / HOST_RUNTIME_BIN), "ensure", "--host", "codex"],
            env=_clean_env(
                HOME=str(home),
                CODEX_HOME=str(home / ".codex"),
                PLUGIN_ROOT=str(hub_root / "dist/codex/pig"),
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
    plugin_root = hub_root / "dist/codex/pig"
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
