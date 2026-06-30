from __future__ import annotations

import base64
import gzip
import json
import os
import subprocess
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.fs import JsonValue, validate_json_value

COLLECTOR_BIN = "promptless-trace-collector"
TRACE_COLLECTOR_ID = "native-trace-collector"


def _assert_no_promptless_directory(root: Path) -> None:
    assert list(root.rglob(".promptless")) == []


def test_build_injects_managed_trace_collector_runtime(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")

    build_hub(hub_root)

    for target in ("codex", "claude"):
        plugin_root = hub_root / "dist" / target / "core"
        collector_path = plugin_root / "bin" / COLLECTOR_BIN
        assert collector_path.exists()
        assert os.access(collector_path, os.X_OK)

        hooks = _json_mapping(json.loads((plugin_root / "hooks/hooks.json").read_text()), "hooks")
        hook_events = _json_mapping(hooks["hooks"], "hooks.hooks")
        expected_events = ("SessionStart", "Stop") if target == "codex" else ("SessionStart", "Stop", "SessionEnd")
        assert set(hook_events) == set(expected_events)
        for event_name in expected_events:
            entries = _json_array(hook_events[event_name], f"hooks.{event_name}")
            entry = _json_mapping(entries[0], f"hooks.{event_name}[0]")
            command_hooks = _json_array(entry["hooks"], f"hooks.{event_name}[0].hooks")
            hook = _json_mapping(command_hooks[0], f"hooks.{event_name}[0].hooks[0]")
            assert hook["command"] == _collector_command(target, event_name)
            assert "--quiet" not in str(hook["command"])
            assert hook["timeout"] == 45
            assert hook["statusMessage"] == "Uploading Promptless traces"
            if event_name == "SessionStart":
                assert entry["matcher"] == "startup|resume"
            else:
                assert "matcher" not in entry

        metadata = _json_mapping(json.loads((plugin_root / "hub.managed-runtimes.json").read_text()), "metadata")
        assert not (plugin_root / ".promptless").exists()
        runtimes = _json_array(metadata["managed_runtimes"], "metadata.managed_runtimes")
        runtime = _json_mapping(runtimes[0], "metadata.managed_runtimes[0]")
        assert runtime["id"] == TRACE_COLLECTOR_ID
        assert runtime["status"] == "included"
        assert runtime["target"] == target
        assert runtime["version"] == "0.1.0"
        assert runtime["channel"] == "stable"
        assert runtime["path"] == f"bin/{COLLECTOR_BIN}"
        assert runtime["executable"] == COLLECTOR_BIN
        assert len(str(runtime["sha256"])) == 64

    codex_manifest = json.loads((hub_root / "dist/codex/core/.codex-plugin/plugin.json").read_text())
    assert codex_manifest["hooks"] == "./hooks/hooks.json"

    for target in ("cursor", "gemini"):
        plugin_root = hub_root / "dist" / target / "core"
        assert not (plugin_root / "bin" / COLLECTOR_BIN).exists()
        assert not (plugin_root / "hub.managed-runtimes.json").exists()

    release_manifest = _json_mapping(json.loads((hub_root / "hub.release.json").read_text()), "release manifest")
    managed_runtimes = _json_array(release_manifest["managed_runtimes"], "managed_runtimes")
    assert {_json_mapping(runtime, "runtime")["target"] for runtime in managed_runtimes} == {"codex", "claude"}
    _assert_no_promptless_directory(hub_root)


def test_collector_missing_token_exits_zero_without_ledger_write(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"
    plugin_data = tmp_path / "plugin-data"

    result = subprocess.run(
        [str(hub_root / "dist/codex/core/bin" / COLLECTOR_BIN), "--host", "codex"],
        env=_clean_env(
            HOME=str(home),
            CODEX_HOME=str(home / ".codex"),
            PLUGIN_ROOT=str(hub_root / "dist/codex/core"),
            PLUGIN_DATA=str(plugin_data),
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "setup_needed"
    assert not (plugin_data / "trace-collector-ledger.json").exists()
    assert "plugin-token" not in result.stdout

    quiet_result = subprocess.run(
        [str(hub_root / "dist/codex/core/bin" / COLLECTOR_BIN), "--host", "codex", "--quiet"],
        env=_clean_env(
            HOME=str(home),
            CODEX_HOME=str(home / ".codex"),
            PLUGIN_ROOT=str(hub_root / "dist/codex/core"),
            PLUGIN_DATA=str(plugin_data),
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert quiet_result.returncode == 0
    assert quiet_result.stdout == ""
    assert quiet_result.stderr == ""


def test_collector_loads_seed_from_plugin_data_file_and_checkins(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        (plugin_data / "trace-collector-seed.json").write_text(
            json.dumps({"plugin_enrollment_token": "plugin-token", "worker_base_url": server.base_url})
        )

        home = tmp_path / "home"
        _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            },
        )

        assert len(server.check_ins) == 1
        check_in = server.check_ins[0]
        assert check_in["host"] == "codex"
        assert check_in["status"] == "configured"
        assert check_in["needs_restart"] is False
        effective_config = _json_mapping(check_in["effective_config"], "effective_config")
        assert effective_config["trace_upload_endpoint"] == f"{server.base_url}/v0/traces/batches"
        assert effective_config["native_root_count"] == 1
        assert effective_config["source_ledger_path"] == str(plugin_data / "trace-collector-ledger.json")
        assert effective_config["raw_native_artifacts_enabled"] is True
    finally:
        server.stop()


def test_collector_does_not_upload_when_check_in_rejected(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(check_in_response={"accepted": False, "policy_version": 7})
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        rollout_path.write_text('{"type":"turn","id":"blocked"}\n')

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
            expected_status="error",
        )

        assert "did not accept request for /v0/host-enrollment/check-ins" in str(payload["message"])
        assert len(server.check_ins) == 1
        assert server.uploads == []
        assert not (plugin_data / "trace-collector-ledger.json").exists()
    finally:
        server.stop()


def test_collector_does_not_upload_when_check_in_policy_version_mismatches(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(check_in_response={"accepted": True, "policy_version": 6})
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        rollout_path.write_text('{"type":"turn","id":"wrong-policy"}\n')

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
            expected_status="error",
        )

        assert "policy_version did not match request" in str(payload["message"])
        assert len(server.check_ins) == 1
        assert server.uploads == []
        assert not (plugin_data / "trace-collector-ledger.json").exists()
    finally:
        server.stop()


def test_collector_baselines_first_run_and_uploads_new_complete_lines(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        existing_line = b'{"type":"session_meta","id":"old"}\n'
        rollout_path.write_bytes(existing_line)

        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_DATA": str(plugin_data),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }
        payload, _result = _run_collector(hub_root / "dist/codex/core", "codex", env, lifecycle="session_start")
        assert payload["baseline_only"] is True
        assert payload["uploaded_chunks"] == 0
        assert server.uploads == []
        ledger = _json_mapping(json.loads((plugin_data / "trace-collector-ledger.json").read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == len(existing_line)

        new_line = b'{"type":"turn","id":"new"}\n'
        partial_line = b'{"type":"turn","id":"partial"}'
        with rollout_path.open("ab") as file:
            file.write(new_line)
            file.write(partial_line)

        payload, _result = _run_collector(hub_root / "dist/codex/core", "codex", env, lifecycle="stop")

        assert payload["baseline_only"] is False
        assert payload["uploaded_chunks"] == 1
        assert len(server.uploads) == 1
        upload = server.uploads[0]
        assert upload["source"] == "codex"
        assert upload["host"] == "codex"
        assert upload["collector_version"] == "0.1.0"
        assert upload["plugin_version"] == "0.1.0"
        chunks = _json_array(upload["chunks"], "chunks")
        chunk = _json_mapping(chunks[0], "chunks[0]")
        assert chunk["start_offset"] == len(existing_line)
        assert chunk["end_offset"] == len(existing_line) + len(new_line)
        assert chunk["line_count"] == 1
        assert chunk["lifecycle_event"] == "stop"
        assert _decode_chunk(chunk) == new_line

        ledger = _json_mapping(json.loads((plugin_data / "trace-collector-ledger.json").read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == len(existing_line) + len(new_line)
        assert len(server.check_ins) == 2
    finally:
        server.stop()


def test_collector_uploads_existing_lines_on_first_run_when_forward_only_disabled(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(forward_only_first_install=False)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        existing_line = b'{"type":"turn","id":"existing"}\n'
        rollout_path.write_bytes(existing_line)

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["baseline_only"] is False
        assert payload["uploaded_chunks"] == 1
        assert len(server.uploads) == 1
        upload = server.uploads[0]
        chunk = _json_mapping(_json_array(upload["chunks"], "chunks")[0], "chunks[0]")
        assert _decode_chunk(chunk) == existing_line
        ledger = _json_mapping(json.loads((plugin_data / "trace-collector-ledger.json").read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == len(existing_line)
    finally:
        server.stop()


def test_collector_does_not_advance_ledger_when_upload_fails(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(upload_status=503)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        rollout_path.write_text('{"type":"turn","id":"retry"}\n')

        payload, result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert result.returncode == 0
        assert "HTTP 503" in str(payload["message"])
        assert len(server.uploads) == 1
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        assert _json_mapping(ledger["sources"], "ledger.sources") == {}
    finally:
        server.stop()


def test_collector_does_not_advance_ledger_when_upload_response_lacks_acceptance(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(upload_response={"trace_count": 1}, forward_only_first_install=False)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        rollout_path.write_text('{"type":"turn","id":"retry"}\n')

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "did not accept request" in str(payload["message"])
        assert len(server.uploads) == 1
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        assert _json_mapping(ledger["sources"], "ledger.sources") == {}
    finally:
        server.stop()


@pytest.mark.parametrize(
    ("upload_response", "message"),
    [
        (
            {
                "accepted": True,
                "batch_id": "wrong-batch",
                "policy_version": 7,
                "raw_artifact_count": 1,
                "trace_count": 1,
                "event_count": 1,
                "unparsed_record_count": 0,
            },
            "batch_id did not match request",
        ),
        (
            {
                "accepted": True,
                "batch_id": "filled-by-test",
                "policy_version": 6,
                "raw_artifact_count": 1,
                "trace_count": 1,
                "event_count": 1,
                "unparsed_record_count": 0,
            },
            "policy_version did not match request",
        ),
        (
            {
                "accepted": True,
                "batch_id": "filled-by-test",
                "policy_version": 7,
                "raw_artifact_count": 0,
                "trace_count": 1,
                "event_count": 1,
                "unparsed_record_count": 0,
            },
            "raw_artifact_count did not match request",
        ),
        (
            {
                "accepted": True,
                "batch_id": "filled-by-test",
                "policy_version": 7,
                "raw_artifact_count": 1,
                "trace_count": 1,
                "unparsed_record_count": 0,
            },
            "event_count must be a non-negative integer",
        ),
    ],
)
def test_collector_does_not_advance_ledger_when_upload_ack_is_invalid(
    tmp_path: Path,
    upload_response: dict[str, JsonValue],
    message: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)

    def fill_batch_id(payload: dict[str, JsonValue]) -> None:
        if upload_response.get("batch_id") == "filled-by-test":
            upload_response["batch_id"] = payload["batch_id"]

    server = _FakeWorkerServer(
        upload_response=upload_response,
        before_upload_response=fill_batch_id,
        forward_only_first_install=False,
    )
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        rollout_path.write_text('{"type":"turn","id":"retry"}\n')

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert message in str(payload["message"])
        assert len(server.uploads) == 1
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        assert _json_mapping(ledger["sources"], "ledger.sources") == {}
    finally:
        server.stop()


def test_collector_quiet_failure_reports_error_check_in(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    upload_response: dict[str, JsonValue] = {
        "accepted": True,
        "batch_id": "filled-by-test",
        "policy_version": 7,
        "raw_artifact_count": 0,
        "trace_count": 1,
        "event_count": 1,
        "unparsed_record_count": 0,
    }

    def fill_batch_id(payload: dict[str, JsonValue]) -> None:
        upload_response["batch_id"] = payload["batch_id"]

    server = _FakeWorkerServer(
        upload_response=upload_response,
        before_upload_response=fill_batch_id,
        forward_only_first_install=False,
    )
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        rollout_path.write_text('{"type":"turn","id":"quiet-error"}\n')

        result = subprocess.run(
            [str(hub_root / "dist/codex/core/bin" / COLLECTOR_BIN), "--host", "codex", "--quiet"],
            env=_clean_env(
                HOME=str(home),
                CODEX_HOME=str(home / ".codex"),
                PLUGIN_DATA=str(plugin_data),
                PLUGIN_ROOT=str(hub_root / "dist/codex/core"),
                PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN="plugin-token",
                PROMPTLESS_WORKER_BASE_URL=server.base_url,
            ),
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""
        assert [check_in["status"] for check_in in server.check_ins] == ["configured", "error"]
        error_check_in = server.check_ins[1]
        drift_reports = _json_array(error_check_in["drift_reports"], "drift_reports")
        first_drift_report = _json_mapping(drift_reports[0], "drift_reports[0]")
        assert first_drift_report["kind"] == "trace_collector_error"
        assert "raw_artifact_count" in str(_json_mapping(first_drift_report["details"], "details")["error"])
    finally:
        server.stop()


def test_collector_recovers_truncated_ledger_by_retrying_from_start(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text("{")

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        complete_line = b'{"type":"turn","id":"recover"}\n'
        rollout_path.write_bytes(complete_line)

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["baseline_only"] is False
        assert payload["uploaded_chunks"] == 1
        assert len(server.uploads) == 1
        chunk = _json_mapping(_json_array(server.uploads[0]["chunks"], "chunks")[0], "chunks[0]")
        assert _decode_chunk(chunk) == complete_line
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == len(complete_line)
    finally:
        server.stop()


def test_claude_stop_suppresses_uploads_when_policy_requires_terminal_trace(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(include_in_progress_traces=False, forward_only_first_install=False)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".claude/projects/acme/session.jsonl"
        rollout_path.parent.mkdir(parents=True)
        rollout_path.write_text('{"type":"turn","id":"wait"}\n')

        payload, _result = _run_collector(
            hub_root / "dist/claude/core",
            "claude",
            {
                "HOME": str(home),
                "CLAUDE_PLUGIN_DATA": str(plugin_data),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["trace_upload_suppressed"] is True
        assert payload["suppression_reason"] == "in_progress_trace"
        assert payload["uploaded_chunks"] == 0
        assert server.uploads == []
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        assert _json_mapping(ledger["sources"], "ledger.sources") == {}
        drift_reports = _json_array(server.check_ins[0]["drift_reports"], "drift_reports")
        assert _json_mapping(drift_reports[0], "drift_reports[0]")["kind"] == "trace_upload_waiting_for_session_end"
    finally:
        server.stop()


def test_codex_stop_uploads_when_policy_requires_terminal_trace(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(include_in_progress_traces=False, forward_only_first_install=False)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        complete_line = b'{"type":"turn","id":"terminal"}\n'
        rollout_path.write_bytes(complete_line)

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["uploaded_chunks"] == 1
        assert "trace_upload_suppressed" not in payload
        assert len(server.uploads) == 1
        upload = server.uploads[0]
        chunk = _json_mapping(_json_array(upload["chunks"], "chunks")[0], "chunks[0]")
        assert chunk["lifecycle_event"] == "stop"
        assert _decode_chunk(chunk) == complete_line
    finally:
        server.stop()


def test_claude_session_end_uploads_when_policy_requires_terminal_trace(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(include_in_progress_traces=False, forward_only_first_install=False)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        rollout_path = home / ".claude/projects/acme/session.jsonl"
        rollout_path.parent.mkdir(parents=True)
        complete_line = b'{"type":"turn","id":"done"}\n'
        rollout_path.write_bytes(complete_line)

        payload, _result = _run_collector(
            hub_root / "dist/claude/core",
            "claude",
            {
                "HOME": str(home),
                "CLAUDE_PLUGIN_DATA": str(plugin_data),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="session_end",
        )

        assert payload["uploaded_chunks"] == 1
        assert "trace_upload_suppressed" not in payload
        assert len(server.uploads) == 1
        upload = server.uploads[0]
        chunk = _json_mapping(_json_array(upload["chunks"], "chunks")[0], "chunks[0]")
        assert chunk["lifecycle_event"] == "session_end"
        assert _decode_chunk(chunk) == complete_line
    finally:
        server.stop()


def test_collector_suppresses_trace_upload_when_capture_policy_cannot_store_raw_trace(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(
        capture_policy_overrides={"user_prompts": "disabled"},
        forward_only_first_install=False,
    )
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        complete_line = b'{"type":"turn","id":"private"}\n'
        rollout_path.write_bytes(complete_line)

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["trace_upload_suppressed"] is True
        assert payload["suppression_reason"] == "capture_policy"
        assert payload["uploaded_chunks"] == 0
        assert server.uploads == []
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == len(complete_line)
        drift_reports = _json_array(server.check_ins[0]["drift_reports"], "drift_reports")
        drift_report = _json_mapping(drift_reports[0], "drift_reports[0]")
        assert drift_report["kind"] == "trace_upload_suppressed_by_capture_policy"
    finally:
        server.stop()


def test_collector_splits_uploads_by_policy_batch_limit(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(forward_only_first_install=False, max_batch_bytes=200)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        lines = [
            (json.dumps({"type": "turn", "id": f"id-{index}", "payload": "x" * 50}) + "\n").encode("utf-8")
            for index in range(4)
        ]
        rollout_path.write_bytes(b"".join(lines))

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["uploaded_chunks"] == len(lines)
        assert len(server.uploads) == 2
        uploaded_chunks = [
            _json_mapping(chunk, "chunk")
            for upload in server.uploads
            for chunk in _json_array(upload["chunks"], "chunks")
        ]
        assert [_decode_chunk(chunk) for chunk in uploaded_chunks] == lines
        for upload in server.uploads:
            chunks = [_json_mapping(chunk, "chunk") for chunk in _json_array(upload["chunks"], "chunks")]
            decoded_size = sum(len(_decode_chunk(chunk)) for chunk in chunks)
            assert decoded_size <= 200
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == sum(len(line) for line in lines)
    finally:
        server.stop()


def test_collector_advances_ledger_through_first_accepted_batch_when_later_batch_fails(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(
        forward_only_first_install=False,
        max_batch_bytes=200,
        upload_responses=[(200, None), (503, None)],
    )
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        lines = [
            (json.dumps({"type": "turn", "id": f"id-{index}", "payload": "x" * 50}) + "\n").encode("utf-8")
            for index in range(4)
        ]
        rollout_path.write_bytes(b"".join(lines))

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
            expected_status="error",
        )

        assert "HTTP 503" in str(payload["message"])
        assert len(server.uploads) == 2
        first_upload_chunks = [
            _json_mapping(chunk, "chunk") for chunk in _json_array(server.uploads[0]["chunks"], "chunks")
        ]
        first_batch_end = max(int(chunk["end_offset"]) for chunk in first_upload_chunks)
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == first_batch_end
    finally:
        server.stop()


def test_collector_ledger_save_preserves_higher_existing_offsets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"
    plugin_data = tmp_path / "plugin-data"
    plugin_data.mkdir()
    ledger_path = plugin_data / "trace-collector-ledger.json"
    rollout_path = home / ".codex/sessions/rollout.jsonl"
    rollout_path.parent.mkdir(parents=True)
    complete_line = b'{"type":"turn","id":"current"}\n'
    rollout_path.write_bytes(complete_line)
    higher_offset = len(complete_line) + 10

    def write_higher_ledger(_payload: dict[str, JsonValue]) -> None:
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {str(rollout_path): higher_offset}}))

    server = _FakeWorkerServer(
        forward_only_first_install=False,
        before_upload_response=write_higher_ledger,
    )
    server.start()
    try:
        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["uploaded_chunks"] == 1
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == higher_offset
    finally:
        server.stop()


def test_collector_rejects_plaintext_non_loopback_worker_base_url(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"

    payload, result = _run_collector(
        hub_root / "dist/codex/core",
        "codex",
        {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
            "PROMPTLESS_WORKER_BASE_URL": "http://example.com",
            "PROMPTLESS_TRACE_COLLECTOR_ALLOW_TEST_URL_OVERRIDES": "0",
        },
        expected_status="error",
    )

    assert "worker base URL must use HTTPS unless" in str(payload["message"])
    assert result.stdout == ""


def test_build_appends_collector_hooks_to_existing_hook_asset(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    existing_hook_path = hub_root / "assets/hooks/hooks.json"
    existing_hook_path.parent.mkdir(parents=True, exist_ok=True)
    existing_hook_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [{"type": "command", "command": "echo existing", "timeout": 1}],
                        }
                    ]
                }
            }
        )
    )
    (hub_root / "assets/hooks/hooks.asset.yaml").write_text(
        "\n".join(
            [
                "title: Existing Hooks",
                "support:",
                "  codex:",
                "    mode: native",
                "  claude:",
                "    mode: unsupported",
                "    reason: test fixture is codex-only",
                "  gemini:",
                "    mode: unsupported",
                "    reason: test fixture is codex-only",
                "  cursor:",
                "    mode: unsupported",
                "    reason: test fixture is codex-only",
                "",
            ]
        )
    )
    (hub_root / "packages/core.yaml").write_text(
        "\n".join(
            [
                "id: core",
                "name: Core",
                "owners: []",
                "includes:",
                "  - hook:hooks",
                "",
            ]
        )
    )

    build_hub(hub_root)

    hooks = _json_mapping(json.loads((hub_root / "dist/codex/core/hooks/hooks.json").read_text()), "hooks")
    hook_events = _json_mapping(hooks["hooks"], "hooks.hooks")
    session_start = _json_array(hook_events["SessionStart"], "SessionStart")
    stop_hooks = _json_array(hook_events["Stop"], "Stop")
    assert "SessionEnd" not in hook_events
    assert (
        _json_mapping(_json_array(_json_mapping(session_start[0], "existing")["hooks"], "existing.hooks")[0], "hook")[
            "command"
        ]
        == "echo existing"
    )
    collector_entry = _json_mapping(session_start[1], "collector")
    collector_hook = _json_mapping(_json_array(collector_entry["hooks"], "collector.hooks")[0], "collector hook")
    assert collector_entry["matcher"] == "startup|resume"
    assert collector_hook["command"] == _collector_command("codex", "SessionStart")
    assert _json_mapping(_json_array(_json_mapping(stop_hooks[0], "stop")["hooks"], "stop.hooks")[0], "stop hook")[
        "command"
    ] == _collector_command("codex", "Stop")


def test_collector_blocks_when_worker_requires_newer_runtime(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(required_bootstrap_version="0.2.0")
    server.start()
    try:
        home = tmp_path / "home"
        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="blocked",
        )

        assert payload["reason"] == "collector_upgrade_required"
        assert server.uploads == []
        assert len(server.check_ins) == 1
        check_in = server.check_ins[0]
        assert check_in["status"] == "blocked"
        drift_reports = _json_array(check_in["drift_reports"], "drift_reports")
        first_drift_report = _json_mapping(drift_reports[0], "drift_reports[0]")
        assert first_drift_report["kind"] == "collector_upgrade_required"
    finally:
        server.stop()


def test_collector_allows_worker_required_runtime_older_than_current(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(required_bootstrap_version="0.0.1")
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )

        assert payload["status"] == "configured"
        assert server.uploads == []
        assert [check_in["status"] for check_in in server.check_ins] == ["configured"]
    finally:
        server.stop()


def test_collector_rejects_policy_without_signed_envelope_fields(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer()
    del _FakeWorkerHandler.policy_response["signature"]
    server.start()
    try:
        home = tmp_path / "home"
        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "missing signature" in str(payload["message"])
        assert server.check_ins == []
        assert server.uploads == []
    finally:
        server.stop()


def _run_collector(
    plugin_root: Path,
    host: str,
    env: dict[str, str],
    *,
    expected_status: str = "configured",
    lifecycle: str | None = None,
    input_payload: dict[str, JsonValue] | None = None,
) -> tuple[dict[str, JsonValue], subprocess.CompletedProcess[str]]:
    command = [str(plugin_root / "bin" / COLLECTOR_BIN), "--host", host]
    if lifecycle is not None:
        command.extend(["--lifecycle", lifecycle])
    result = subprocess.run(
        command,
        env=_clean_env(**env),
        input=json.dumps(input_payload) if input_payload is not None else "",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    payload_text = result.stdout.strip() or result.stderr.strip()
    assert payload_text
    payload = _json_mapping(validate_json_value(json.loads(payload_text), "collector output"), "collector output")
    assert payload["status"] == expected_status
    return payload, result


def _clean_env(**overrides: str) -> dict[str, str]:
    clean_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    for key in ("SystemRoot", "SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            clean_env[key] = os.environ[key]
    clean_env.update(overrides)
    return clean_env


def _collector_command(target: str, event_name: str) -> str:
    lifecycle = {"SessionStart": "session_start", "Stop": "stop", "SessionEnd": "session_end"}[event_name]
    if target == "claude":
        return f'python3 "${{CLAUDE_PLUGIN_ROOT}}/bin/{COLLECTOR_BIN}" --host claude --lifecycle {lifecycle}'
    return f'python3 "${{PLUGIN_ROOT}}/bin/{COLLECTOR_BIN}" --host codex --lifecycle {lifecycle}'


def _decode_chunk(chunk: dict[str, JsonValue]) -> bytes:
    encoded = chunk["content_gzip_base64"]
    assert isinstance(encoded, str)
    return gzip.decompress(base64.b64decode(encoded))


def _policy_with(
    base_url: str,
    *,
    required_bootstrap_version: str = "0.1.0",
    forward_only_first_install: bool = True,
    include_in_progress_traces: bool = True,
    max_batch_bytes: int = 1048576,
    capture_policy_overrides: dict[str, str] | None = None,
) -> dict[str, JsonValue]:
    capture_policy: dict[str, JsonValue] = {
        "user_prompts": "full_local_default",
        "assistant_messages": "full_local_default",
        "reasoning": "full_local_default",
        "tool_inputs": "full_local_default",
        "tool_outputs": "full_local_default",
        "raw_native_artifacts": "full_local_default",
    }
    if capture_policy_overrides is not None:
        capture_policy.update(capture_policy_overrides)
    return {
        "policy": {
            "schema_version": 2,
            "policy_version": 7,
            "enabled_hosts": ["codex", "claude"],
            "required_bootstrap_version": required_bootstrap_version,
            "trace_collection": {
                "enabled_sources": ["codex", "claude"],
                "upload_endpoint": f"{base_url}/v0/traces/batches",
                "native_roots": [
                    {"source": "codex", "glob": "~/.codex/**/*.jsonl"},
                    {"source": "claude", "glob": "~/.claude/projects/**/*.jsonl"},
                ],
                "forward_only_first_install": forward_only_first_install,
                "include_in_progress_traces": include_in_progress_traces,
                "max_batch_bytes": max_batch_bytes,
            },
            "capture_policy": capture_policy,
            "plugin_permissions": {
                "allow_network": True,
                "allowed_hosts": ["127.0.0.1"],
                "allow_local_file_read": True,
                "allowed_read_roots": ["~/.codex", "~/.claude"],
            },
            "created_at": "2026-06-26T00:00:00Z",
        },
        "signature": "test-signature",
        "signed_at": "2026-06-26T00:00:00Z",
        "key_id": "test-key",
    }


class _FakeWorkerServer:
    def __init__(
        self,
        *,
        required_bootstrap_version: str = "0.1.0",
        check_in_status: int = 200,
        check_in_response: dict[str, JsonValue] | None = None,
        upload_status: int = 200,
        upload_response: dict[str, JsonValue] | None = None,
        upload_responses: list[tuple[int, dict[str, JsonValue] | None]] | None = None,
        before_upload_response: Callable[[dict[str, JsonValue]], None] | None = None,
        forward_only_first_install: bool = True,
        include_in_progress_traces: bool = True,
        max_batch_bytes: int = 1048576,
        capture_policy_overrides: dict[str, str] | None = None,
    ) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWorkerHandler)
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}"
        self.check_ins: list[dict[str, JsonValue]] = []
        self.uploads: list[dict[str, JsonValue]] = []
        self._thread: threading.Thread | None = None

        _FakeWorkerHandler.policy_response = _policy_with(
            self.base_url,
            required_bootstrap_version=required_bootstrap_version,
            forward_only_first_install=forward_only_first_install,
            include_in_progress_traces=include_in_progress_traces,
            max_batch_bytes=max_batch_bytes,
            capture_policy_overrides=capture_policy_overrides,
        )
        _FakeWorkerHandler.check_in_status = check_in_status
        _FakeWorkerHandler.check_in_response = check_in_response
        _FakeWorkerHandler.upload_status = upload_status
        _FakeWorkerHandler.upload_response = upload_response
        _FakeWorkerHandler.upload_responses = upload_responses
        _FakeWorkerHandler.before_upload_response = before_upload_response
        _FakeWorkerHandler.check_ins = self.check_ins
        _FakeWorkerHandler.uploads = self.uploads

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


class _FakeWorkerHandler(BaseHTTPRequestHandler):
    policy_response: ClassVar[dict[str, JsonValue]]
    check_in_status: ClassVar[int]
    check_in_response: ClassVar[dict[str, JsonValue] | None]
    upload_status: ClassVar[int]
    upload_response: ClassVar[dict[str, JsonValue] | None]
    upload_responses: ClassVar[list[tuple[int, dict[str, JsonValue] | None]] | None]
    before_upload_response: ClassVar[Callable[[dict[str, JsonValue]], None] | None]
    check_ins: ClassVar[list[dict[str, JsonValue]]]
    uploads: ClassVar[list[dict[str, JsonValue]]]

    def do_GET(self) -> None:
        if self.path == "/v0/host-enrollment/policy":
            self._assert_authorized()
            self._send_json(200, self.policy_response)
            return
        self._send_json(404, {"accepted": False})

    def do_POST(self) -> None:
        self._assert_authorized()
        payload = self._read_json_body()
        if self.path == "/v0/host-enrollment/check-ins":
            self.check_ins.append(payload)
            response = self.check_in_response
            if response is None:
                response = {
                    "accepted": self.check_in_status < 400,
                    "policy_version": payload.get("policy_version"),
                }
            self._send_json(self.check_in_status, response)
            return
        if self.path == "/v0/traces/batches":
            self.uploads.append(payload)
            before_upload_response = type(self).before_upload_response
            if before_upload_response is not None:
                before_upload_response(payload)
            status = self.upload_status
            response = self.upload_response
            if self.upload_responses is not None:
                index = len(self.uploads) - 1
                status, response = self.upload_responses[min(index, len(self.upload_responses) - 1)]
            if response is None:
                response = self._default_upload_response(status, payload)
            self._send_json(status, response)
            return
        self._send_json(404, {"accepted": False})

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _assert_authorized(self) -> None:
        assert self.headers.get("Authorization") == "Bearer plugin-token"

    def _read_json_body(self) -> dict[str, JsonValue]:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        return _json_mapping(validate_json_value(json.loads(body.decode("utf-8")), "request body"), "request body")

    def _send_json(self, status: int, payload: dict[str, JsonValue]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _default_upload_response(self, status: int, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        chunks = payload.get("chunks")
        chunk_count = len(chunks) if isinstance(chunks, list) else 0
        return {
            "accepted": status < 400,
            "batch_id": payload.get("batch_id"),
            "policy_version": payload.get("policy_version"),
            "raw_artifact_count": chunk_count if status < 400 else 0,
            "trace_count": chunk_count if status < 400 else 0,
            "event_count": chunk_count if status < 400 else 0,
            "unparsed_record_count": 0,
        }


def _json_mapping(value: JsonValue, path: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{path} must be a JSON object"
    return value


def _json_array(value: JsonValue, path: str) -> list[JsonValue]:
    assert isinstance(value, list), f"{path} must be a JSON array"
    return value
