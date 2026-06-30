from __future__ import annotations

import ast
import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import threading
import urllib.error
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, ClassVar
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.fs import JsonValue, validate_json_value

COLLECTOR_BIN = "promptless-trace-collector"
TRACE_COLLECTOR_ID = "local-trace-collector"
HOST_CREDENTIAL = "plihost_localcredential"
HOST_STATE_REL_PATH = Path(".promptless/instruction-hub/host-enrollment-state.json")
COLLECTOR_ASSET_PATH = (
    Path(__file__).parents[1] / "src/promptless_instruction_hub/managed_runtime_assets/trace-collector" / COLLECTOR_BIN
)


class _ReadForbiddenBinaryFile:
    def __init__(
        self,
        file: BinaryIO,
        *,
        max_read_size: int | None = None,
        forbidden_before_offset: int | None = None,
    ) -> None:
        self._file = file
        self._max_read_size = max_read_size
        self._forbidden_before_offset = forbidden_before_offset

    def __enter__(self) -> "_ReadForbiddenBinaryFile":
        self._file.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._file.__exit__(exc_type, exc_value, traceback)

    def read(self, *_args: object, **_kwargs: object) -> bytes:
        size = _args[0] if _args else -1
        if not isinstance(size, int) or size < 0:
            raise AssertionError("collector must not read an unbounded trace file tail")
        if self._max_read_size is not None and size > self._max_read_size:
            raise AssertionError("collector trace file reads must stay within the configured block size")
        self._assert_read_allowed()
        return self._file.read(size)

    def readline(self, size: int = -1) -> bytes:
        if size < 0:
            raise AssertionError("collector must bound trace file line reads")
        if self._max_read_size is not None and size > self._max_read_size:
            raise AssertionError("collector trace file line reads must stay within the configured block size")
        self._assert_read_allowed()
        return self._file.readline(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        return self._file.tell()

    def _assert_read_allowed(self) -> None:
        if self._forbidden_before_offset is None:
            return
        if self._file.tell() < self._forbidden_before_offset:
            raise AssertionError("collector must not read trace bytes before the ledger offset")


def _assert_no_promptless_directory(root: Path) -> None:
    assert list(root.rglob(".promptless")) == []


def _host_state_path(home: Path) -> Path:
    return home / HOST_STATE_REL_PATH


def _last_seen_plugin_versions(home: Path) -> dict[str, JsonValue]:
    state = _json_mapping(json.loads(_host_state_path(home).read_text()), "state")
    return _json_mapping(state["last_seen_plugin_versions"], "last_seen_plugin_versions")


def _assert_stdout_system_message_only(result: subprocess.CompletedProcess[str], expected_substring: str) -> str:
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    message_payload = _json_mapping(validate_json_value(json.loads(lines[0]), "stdout"), "stdout")
    assert set(message_payload) == {"systemMessage"}
    system_message = message_payload["systemMessage"]
    assert isinstance(system_message, str)
    assert expected_substring in system_message
    return system_message


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
            assert hook["timeout"] == 90
            assert hook["statusMessage"] == "Checking Promptless trace collection"
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


def test_collector_asset_remains_python39_compatible() -> None:
    source = COLLECTOR_ASSET_PATH.read_text()

    ast.parse(source, filename=str(COLLECTOR_ASSET_PATH), feature_version=(3, 9))

    assert " | " not in source
    assert "strict=" not in source


def test_collector_asset_imports_without_posix_fcntl_available() -> None:
    script = f"""
import builtins
import runpy

real_import = builtins.__import__

def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "fcntl":
        raise ModuleNotFoundError("No module named 'fcntl'")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = blocked_import
runpy.run_path({json.dumps(str(COLLECTOR_ASSET_PATH))}, run_name="promptless_trace_collector_import_test")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_collector_directory_fsync_is_noop_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    collector_module = runpy.run_path(str(COLLECTOR_ASSET_PATH), run_name="promptless_trace_collector_fsync_test")
    collector_os = collector_module["os"]
    fsync_directory = collector_module["_fsync_directory"]

    def fail_open(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("directory fsync should not try os.open on Windows")

    monkeypatch.setattr(collector_os, "name", "nt")
    monkeypatch.setattr(collector_os, "open", fail_open)

    fsync_directory(tmp_path)


def test_collector_asset_runs_under_python39_when_available(tmp_path: Path) -> None:
    python39 = _python39_executable()
    if python39 is None:
        pytest.skip("python3.9 is not installed")

    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"
    plugin_data = tmp_path / "plugin-data"

    result = subprocess.run(
        [python39, str(hub_root / "dist/codex/core/bin" / COLLECTOR_BIN), "--host", "codex"],
        env=_clean_env(
            HOME=str(home),
            PLUGIN_ROOT=str(hub_root / "dist/codex/core"),
            PLUGIN_DATA=str(plugin_data),
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stderr)["status"] == "disabled"
    assert json.loads(result.stderr)["reason"] == "pigs_fly_not_enabled"
    assert result.stdout == ""
    assert not (plugin_data / "trace-collector-ledger.json").exists()
    assert _last_seen_plugin_versions(home) == {"codex": "0.1.0"}


def test_collector_disabled_without_bootstrap_flag_exits_zero_without_ledger(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"
    plugin_data = tmp_path / "plugin-data"

    result = subprocess.run(
        [str(hub_root / "dist/codex/core/bin" / COLLECTOR_BIN), "--host", "codex"],
        env=_clean_env(
            HOME=str(home),
            PLUGIN_ROOT=str(hub_root / "dist/codex/core"),
            PLUGIN_DATA=str(plugin_data),
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stderr)["status"] == "disabled"
    assert json.loads(result.stderr)["reason"] == "pigs_fly_not_enabled"
    assert result.stdout == ""
    assert not (plugin_data / "trace-collector-ledger.json").exists()
    assert _last_seen_plugin_versions(home) == {"codex": "0.1.0"}
    assert "plihost_" not in result.stderr

    quiet_result = subprocess.run(
        [str(hub_root / "dist/codex/core/bin" / COLLECTOR_BIN), "--host", "codex", "--quiet"],
        env=_clean_env(
            HOME=str(home),
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


def test_collector_default_ledger_path_is_host_global(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    collector_module = runpy.run_path(str(COLLECTOR_ASSET_PATH), run_name="promptless_trace_collector_ledger_path_test")
    ledger_path = collector_module["_ledger_path"]

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PROMPTLESS_TRACE_COLLECTOR_LEDGER", raising=False)
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "plugin-data"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "claude-plugin-data"))

    assert ledger_path() == tmp_path / "home/.promptless/instruction-hub/trace-collector-ledger.json"


def test_collector_migrates_legacy_plugin_ledger_before_uploading_pending_ranges(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        legacy_ledger_path = plugin_data / "trace-collector-ledger.json"
        global_ledger_path = home / ".promptless/instruction-hub/trace-collector-ledger.json"

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        acknowledged_line = b'{"type":"turn","id":"already-uploaded"}\n'
        pending_line = b'{"type":"turn","id":"pending"}\n'
        rollout_path.write_bytes(acknowledged_line + pending_line)
        legacy_ledger_path.write_text(
            json.dumps({"schema_version": 1, "sources": {str(rollout_path): len(acknowledged_line)}})
        )

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
                "PROMPTLESS_TRACE_COLLECTOR_LEDGER": "",
            },
            lifecycle="stop",
        )

        assert payload["baseline_only"] is False
        assert payload["uploaded_chunks"] == 1
        assert server.upload_requests == ["/v0/traces/batches?target=codex"]
        assert len(server.uploads) == 1
        upload_chunks = _json_array(server.uploads[0]["chunks"], "chunks")
        uploaded_chunk = _json_mapping(upload_chunks[0], "chunks[0]")
        assert uploaded_chunk["start_offset"] == len(acknowledged_line)
        assert uploaded_chunk["end_offset"] == len(acknowledged_line) + len(pending_line)
        assert _decode_chunk(uploaded_chunk) == pending_line
        global_ledger = _json_mapping(json.loads(global_ledger_path.read_text()), "ledger")
        global_sources = _json_mapping(global_ledger["sources"], "ledger.sources")
        assert global_sources[str(rollout_path)] == len(acknowledged_line) + len(pending_line)
        effective_config = _json_mapping(server.check_ins[0]["effective_config"], "effective_config")
        assert effective_config["source_ledger_path"] == str(global_ledger_path)
    finally:
        server.stop()


def test_collector_migrates_legacy_plugin_ledger_without_duplicate_backfill(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(forward_only_first_install=False)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        legacy_ledger_path = plugin_data / "trace-collector-ledger.json"
        global_ledger_path = home / ".promptless/instruction-hub/trace-collector-ledger.json"

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        complete_line = b'{"type":"turn","id":"already-uploaded"}\n'
        rollout_path.write_bytes(complete_line)
        legacy_ledger_path.write_text(
            json.dumps({"schema_version": 1, "sources": {str(rollout_path): len(complete_line)}})
        )

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
                "PROMPTLESS_TRACE_COLLECTOR_LEDGER": "",
            },
            lifecycle="stop",
        )

        assert payload["baseline_only"] is False
        assert payload["uploaded_chunks"] == 0
        assert server.uploads == []
        global_ledger = _json_mapping(json.loads(global_ledger_path.read_text()), "ledger")
        global_sources = _json_mapping(global_ledger["sources"], "ledger.sources")
        assert global_sources[str(rollout_path)] == len(complete_line)
    finally:
        server.stop()


def test_collector_imports_late_legacy_plugin_ledger_into_existing_global_ledger(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        codex_plugin_data = tmp_path / "codex-plugin-data"
        claude_plugin_data = tmp_path / "claude-plugin-data"
        codex_plugin_data.mkdir()
        claude_plugin_data.mkdir()
        codex_legacy_ledger_path = codex_plugin_data / "trace-collector-ledger.json"
        claude_legacy_ledger_path = claude_plugin_data / "trace-collector-ledger.json"
        global_ledger_path = home / ".promptless/instruction-hub/trace-collector-ledger.json"
        global_ledger_path.parent.mkdir(parents=True)

        codex_rollout_path = home / ".codex/sessions/rollout.jsonl"
        codex_rollout_path.parent.mkdir(parents=True)
        codex_line = b'{"type":"turn","id":"codex-uploaded"}\n'
        codex_rollout_path.write_bytes(codex_line)

        claude_rollout_path = home / ".claude/projects/acme/session.jsonl"
        claude_rollout_path.parent.mkdir(parents=True)
        acknowledged_claude_line = b'{"type":"turn","id":"claude-uploaded"}\n'
        pending_claude_line = b'{"type":"turn","id":"claude-pending"}\n'
        claude_rollout_path.write_bytes(acknowledged_claude_line + pending_claude_line)

        global_ledger_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sources": {str(codex_rollout_path): len(codex_line)},
                    "legacy_ledger_imports": [str(codex_legacy_ledger_path)],
                }
            )
        )
        claude_legacy_ledger_path.write_text(
            json.dumps({"schema_version": 1, "sources": {str(claude_rollout_path): len(acknowledged_claude_line)}})
        )

        payload, _result = _run_collector(
            hub_root / "dist/claude/core",
            "claude",
            {
                "HOME": str(home),
                "CLAUDE_PLUGIN_DATA": str(claude_plugin_data),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
                "PROMPTLESS_TRACE_COLLECTOR_LEDGER": "",
            },
            lifecycle="stop",
        )

        assert payload["baseline_only"] is False
        assert payload["uploaded_chunks"] == 1
        assert server.upload_requests == ["/v0/traces/batches?target=claude"]
        assert len(server.uploads) == 1
        upload_chunks = _json_array(server.uploads[0]["chunks"], "chunks")
        uploaded_chunk = _json_mapping(upload_chunks[0], "chunks[0]")
        assert uploaded_chunk["start_offset"] == len(acknowledged_claude_line)
        assert uploaded_chunk["end_offset"] == len(acknowledged_claude_line) + len(pending_claude_line)
        assert _decode_chunk(uploaded_chunk) == pending_claude_line

        global_ledger = _json_mapping(json.loads(global_ledger_path.read_text()), "ledger")
        global_sources = _json_mapping(global_ledger["sources"], "ledger.sources")
        assert global_sources[str(codex_rollout_path)] == len(codex_line)
        assert global_sources[str(claude_rollout_path)] == len(acknowledged_claude_line) + len(pending_claude_line)
        legacy_imports = _json_array(global_ledger["legacy_ledger_imports"], "ledger.legacy_ledger_imports")
        assert set(legacy_imports) == {str(codex_legacy_ledger_path), str(claude_legacy_ledger_path)}
    finally:
        server.stop()


def test_collector_announces_plugin_update_per_host(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"

    def plugin_env(plugin_root: Path) -> dict[str, str]:
        env = {
            "HOME": str(home),
            "PLUGIN_ROOT": str(plugin_root),
        }
        if "claude" in plugin_root.parts:
            env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        return env

    for host in ("codex", "claude"):
        plugin_root = hub_root / f"dist/{host}/core"
        _payload, result = _run_collector(plugin_root, host, plugin_env(plugin_root), expected_status="disabled")
        assert result.stdout == ""

    assert _last_seen_plugin_versions(home) == {"codex": "0.1.0", "claude": "0.1.0"}

    _rewrite_hub_plugin_version(hub_root, "0.1.0", "0.2.0")
    build_hub(hub_root)

    for host in ("codex", "claude"):
        plugin_root = hub_root / f"dist/{host}/core"
        payload, result = _run_collector(plugin_root, host, plugin_env(plugin_root), expected_status="disabled")
        message = _assert_stdout_system_message_only(
            result,
            "Promptless Instruction Hub updated to v0.2.0 (was v0.1.0).",
        )
        assert payload["systemMessage"] == message

    assert _last_seen_plugin_versions(home) == {"codex": "0.2.0", "claude": "0.2.0"}

    for host in ("codex", "claude"):
        plugin_root = hub_root / f"dist/{host}/core"
        _payload, result = _run_collector(plugin_root, host, plugin_env(plugin_root), expected_status="disabled")
        assert result.stdout == ""


def test_collector_update_notice_tolerates_unreadable_state(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"
    state_path = _host_state_path(home)
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{")

    _payload, result = _run_collector(
        hub_root / "dist/codex/core",
        "codex",
        {
            "HOME": str(home),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
        },
        expected_status="disabled",
    )

    assert result.stdout == ""
    assert state_path.read_text() == "{"


def test_collector_defers_recording_update_until_notice_surfaces(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"
    plugin_root = hub_root / "dist/codex/core"

    _run_collector(
        plugin_root,
        "codex",
        {
            "HOME": str(home),
            "PLUGIN_ROOT": str(plugin_root),
        },
        expected_status="disabled",
    )
    assert _last_seen_plugin_versions(home) == {"codex": "0.1.0"}

    _rewrite_hub_plugin_version(hub_root, "0.1.0", "0.2.0")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/core"

    _payload, failed_result = _run_collector(
        plugin_root,
        "codex",
        {
            "HOME": str(home),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": "http://example.com",
            "PROMPTLESS_TRACE_COLLECTOR_ALLOW_TEST_URL_OVERRIDES": "0",
        },
        expected_status="error",
    )
    _assert_stdout_system_message_only(failed_result, "Promptless trace collection failed")
    assert _last_seen_plugin_versions(home) == {"codex": "0.1.0"}

    payload, healthy_result = _run_collector(
        plugin_root,
        "codex",
        {
            "HOME": str(home),
            "PLUGIN_ROOT": str(plugin_root),
        },
        expected_status="disabled",
    )

    message = _assert_stdout_system_message_only(
        healthy_result,
        "Promptless Instruction Hub updated to v0.2.0 (was v0.1.0).",
    )
    assert payload["systemMessage"] == message
    assert _last_seen_plugin_versions(home) == {"codex": "0.2.0"}


def test_collector_enrolls_host_credential_and_checkins(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        plugin_data = tmp_path / "plugin-data"
        home = tmp_path / "home"
        _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )

        assert len(server.session_requests) == 1
        enrollment_request = server.session_requests[0]
        assert enrollment_request["deployment_instance_id"] == "worker-local-1"
        assert enrollment_request["target"] == "codex"
        assert enrollment_request["plugin_version"] == "0.1.0"
        assert enrollment_request["bootstrap_version"] == "0.1.0"
        assert server.policy_requests == ["/v0/host-enrollment/policy?target=codex"]
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
        assert not (plugin_data / "host-enrollment-state.json").exists()
        state = _json_mapping(json.loads(_host_state_path(home).read_text()), "state")
        credentials = _json_mapping(state["credentials"], "credentials")
        credential = _json_mapping(next(iter(credentials.values())), "credential")
        assert credential["value"] == HOST_CREDENTIAL
        assert credential["deployment_instance_id"] == "worker-local-1"
    finally:
        server.stop()


def test_collector_rejects_loopback_callback_with_wrong_state(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer(callback_state_override="attacker-state")
    server.start()
    try:
        home = tmp_path / "home"
        payload, result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "hosted enrollment start request failed with HTTP 403" in str(payload["message"])
        _assert_stdout_system_message_only(result, "Promptless trace collection failed")
        assert server.poll_requests == []
        assert server.policy_requests == []
        assert server.check_ins == []
        state = _json_mapping(json.loads(_host_state_path(home).read_text()), "state")
        assert "credentials" not in state
    finally:
        server.stop()


def test_collector_requires_callback_deployment_instance_id(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer(callback_payload_overrides={"deployment_instance_id": None})
    server.start()
    try:
        home = tmp_path / "home"
        payload, result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "host enrollment callback missing required fields" in str(payload["message"])
        _assert_stdout_system_message_only(result, "Promptless trace collection failed")
        assert server.policy_requests == []
        assert server.check_ins == []
        state = _json_mapping(json.loads(_host_state_path(home).read_text()), "state")
        assert "credentials" not in state
    finally:
        server.stop()


def test_collector_concurrent_same_host_plugins_enroll_once(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    dev_process: subprocess.Popen[str] | None = None
    ops_process: subprocess.Popen[str] | None = None
    try:
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
                "PLUGIN_ROOT": str(plugin_root),
                "CLAUDE_PLUGIN_ROOT": str(plugin_root),
                "CLAUDE_PLUGIN_DATA": str(tmp_path / f"{plugin_root.name}-data"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            }

        dev_process = _start_collector(dev_plugin, "claude", claude_plugin_env(dev_plugin))
        ops_process = _start_collector(ops_plugin, "claude", claude_plugin_env(ops_plugin))

        dev_payload = _read_any_collector_status(dev_process)
        ops_payload = _read_any_collector_status(ops_process)

        assert len(server.session_requests) == 1
        state = _json_mapping(json.loads(_host_state_path(home).read_text()), "state")
        credentials = _json_mapping(state["credentials"], "credentials")
        assert len(credentials) == 1
        stored_credential = _json_mapping(next(iter(credentials.values())), "stored credential")
        assert stored_credential["target"] == "claude"
        assert _json_mapping(state["pending_enrollments"], "pending_enrollments") == {}
        statuses = {dev_payload["status"], ops_payload["status"]}
        assert statuses & {"configured"}
        assert statuses <= {"configured", "setup_pending"}
    finally:
        for process in (dev_process, ops_process):
            if process is not None and process.poll() is None:
                process.kill()
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
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
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
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
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
            "PLUGIN_DATA": str(plugin_data),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
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
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
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
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
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


def test_collector_reuses_stable_batch_id_when_retrying_same_source_range(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(upload_status=503, forward_only_first_install=False)
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
        env = {
            "HOME": str(home),
            "PLUGIN_DATA": str(plugin_data),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        _run_collector(hub_root / "dist/codex/core", "codex", env, lifecycle="stop", expected_status="error")
        _run_collector(hub_root / "dist/codex/core", "codex", env, lifecycle="stop", expected_status="error")

        assert len(server.uploads) == 2
        assert server.uploads[0]["batch_id"] == server.uploads[1]["batch_id"]
    finally:
        server.stop()


def test_collector_changes_stable_batch_id_when_same_range_content_changes(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(upload_status=503, forward_only_first_install=False)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        first_line = b'{"type":"turn","id":"same-a"}\n'
        second_line = b'{"type":"turn","id":"same-b"}\n'
        assert len(first_line) == len(second_line)
        env = {
            "HOME": str(home),
            "PLUGIN_DATA": str(plugin_data),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        rollout_path.write_bytes(first_line)
        _run_collector(hub_root / "dist/codex/core", "codex", env, lifecycle="stop", expected_status="error")
        rollout_path.write_bytes(second_line)
        _run_collector(hub_root / "dist/codex/core", "codex", env, lifecycle="stop", expected_status="error")

        assert len(server.uploads) == 2
        assert server.uploads[0]["batch_id"] != server.uploads[1]["batch_id"]
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
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
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
                "skipped_record_count": 0,
                "acknowledged_ranges": "filled-by-test",
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
                "skipped_record_count": 0,
                "acknowledged_ranges": "filled-by-test",
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
                "skipped_record_count": 0,
                "acknowledged_ranges": "filled-by-test",
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
                "skipped_record_count": 0,
                "acknowledged_ranges": "filled-by-test",
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
        if upload_response.get("acknowledged_ranges") == "filled-by-test":
            upload_response["acknowledged_ranges"] = _acknowledged_ranges_for_payload(payload)

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
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
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


def test_collector_does_not_advance_ledger_when_upload_response_lacks_acknowledged_ranges(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    upload_response: dict[str, JsonValue] = {
        "accepted": True,
        "batch_id": "filled-by-test",
        "policy_version": 7,
        "raw_artifact_count": 1,
        "skipped_record_count": 0,
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
        rollout_path.write_text('{"type":"turn","id":"retry"}\n')

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "acknowledged_ranges must be an array" in str(payload["message"])
        assert len(server.uploads) == 1
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        assert _json_mapping(ledger["sources"], "ledger.sources") == {}
    finally:
        server.stop()


def test_collector_does_not_advance_ledger_when_upload_acknowledged_ranges_mismatch(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    upload_response: dict[str, JsonValue] = {
        "accepted": True,
        "batch_id": "filled-by-test",
        "policy_version": 7,
        "raw_artifact_count": 1,
        "skipped_record_count": 0,
        "acknowledged_ranges": [],
        "trace_count": 1,
        "event_count": 1,
        "unparsed_record_count": 0,
    }

    def fill_ack_with_wrong_end_offset(payload: dict[str, JsonValue]) -> None:
        upload_response["batch_id"] = payload["batch_id"]
        acknowledged_ranges = _acknowledged_ranges_for_payload(payload)
        first_range = _json_mapping(acknowledged_ranges[0], "acknowledged_ranges[0]")
        end_offset = first_range["end_offset"]
        assert isinstance(end_offset, int)
        first_range["end_offset"] = end_offset + 1
        upload_response["acknowledged_ranges"] = acknowledged_ranges

    server = _FakeWorkerServer(
        upload_response=upload_response,
        before_upload_response=fill_ack_with_wrong_end_offset,
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
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "acknowledged_ranges did not match request" in str(payload["message"])
        assert len(server.uploads) == 1
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        assert _json_mapping(ledger["sources"], "ledger.sources") == {}
    finally:
        server.stop()


def test_collector_rejects_unexpected_skipped_record_count_without_oversized_request(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    upload_response: dict[str, JsonValue] = {
        "accepted": True,
        "batch_id": "filled-by-test",
        "policy_version": 7,
        "raw_artifact_count": 1,
        "skipped_record_count": 1,
        "acknowledged_ranges": "filled-by-test",
        "trace_count": 1,
        "event_count": 1,
        "unparsed_record_count": 0,
    }

    def fill_dynamic_response_fields(payload: dict[str, JsonValue]) -> None:
        upload_response["batch_id"] = payload["batch_id"]
        upload_response["acknowledged_ranges"] = _acknowledged_ranges_for_payload(payload)

    server = _FakeWorkerServer(
        upload_response=upload_response,
        before_upload_response=fill_dynamic_response_fields,
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
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "skipped_record_count did not match request" in str(payload["message"])
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
        "skipped_record_count": 0,
        "acknowledged_ranges": "filled-by-test",
        "trace_count": 1,
        "event_count": 1,
        "unparsed_record_count": 0,
    }

    def fill_batch_id(payload: dict[str, JsonValue]) -> None:
        upload_response["batch_id"] = payload["batch_id"]
        upload_response["acknowledged_ranges"] = _acknowledged_ranges_for_payload(payload)

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
                PLUGIN_DATA=str(plugin_data),
                PLUGIN_ROOT=str(hub_root / "dist/codex/core"),
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


@pytest.mark.parametrize(
    "ledger_content",
    [
        "{",
        "{}",
        json.dumps({"schema_version": 1, "sources": []}),
        json.dumps({"schema_version": 1, "sources": {"rollout.jsonl": -1}}),
    ],
)
def test_collector_recovers_corrupt_ledger_by_baselining_current_end(tmp_path: Path, ledger_content: str) -> None:
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
        ledger_path.write_text(ledger_content)

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        complete_line = b'{"type":"turn","id":"recover"}\n'
        rollout_path.write_bytes(complete_line)

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["baseline_only"] is True
        assert payload["ledger_recovered"] is True
        assert payload["uploaded_chunks"] == 0
        assert server.uploads == []
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == len(complete_line)
        assert list(plugin_data.glob("trace-collector-ledger.json.corrupt-*"))
        drift_reports = _json_array(server.check_ins[0]["drift_reports"], "drift_reports")
        drift_report = _json_mapping(drift_reports[0], "drift_reports[0]")
        assert drift_report["kind"] == "trace_ledger_corrupt"
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
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
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
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
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
        effective_config = _json_mapping(server.check_ins[0]["effective_config"], "effective_config")
        assert effective_config["user_prompts_enabled"] is False
        assert effective_config["raw_native_artifacts_enabled"] is True
    finally:
        server.stop()


@pytest.mark.parametrize(
    ("capture_policy_overrides", "expected_message"),
    [
        ({"raw_native_artifacts": "enabled"}, "policy.capture_policy.raw_native_artifacts must be one of"),
        ({"raw_native_artifacts": None}, "policy.capture_policy.raw_native_artifacts must be a string"),
    ],
)
def test_collector_rejects_invalid_capture_policy_before_upload_or_ledger(
    tmp_path: Path,
    capture_policy_overrides: dict[str, str | None],
    expected_message: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(
        capture_policy_overrides=capture_policy_overrides,
        forward_only_first_install=False,
    )
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        rollout_path.write_text('{"type":"turn","id":"bad-policy"}\n')

        payload, result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
            expected_status="error",
        )

        assert expected_message in str(payload["message"])
        _assert_stdout_system_message_only(result, "Promptless trace collection failed")
        assert server.check_ins == []
        assert server.uploads == []
        assert not (plugin_data / "trace-collector-ledger.json").exists()
    finally:
        server.stop()


def test_collector_splits_uploads_by_policy_batch_limit(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(forward_only_first_install=False, max_batch_bytes=220)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        lines = []
        for index in range(4):
            payload = base64.b64encode(bytes(((index * 53 + offset) % 256 for offset in range(72)))).decode("ascii")
            lines.append((json.dumps({"type": "turn", "id": f"id-{index}", "payload": payload}) + "\n").encode("utf-8"))
        rollout_path.write_bytes(b"".join(lines))

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        uploaded_chunks = [
            _json_mapping(chunk, "chunk")
            for upload in server.uploads
            for chunk in _json_array(upload["chunks"], "chunks")
        ]
        assert payload["uploaded_chunks"] == len(uploaded_chunks)
        decoded_chunks = [_decode_chunk(chunk) for chunk in uploaded_chunks]
        assert b"".join(decoded_chunks) == b"".join(lines)
        assert len(server.uploads) > 1
        for upload in server.uploads:
            chunks = [_json_mapping(chunk, "chunk") for chunk in _json_array(upload["chunks"], "chunks")]
            encoded_size = sum(len(str(chunk["content_gzip_base64"]).encode("ascii")) for chunk in chunks)
            decoded_size = sum(len(_decode_chunk(chunk)) for chunk in chunks)
            assert encoded_size <= 220
            assert decoded_size <= 220
        assert all(len(str(chunk["content_gzip_base64"]).encode("ascii")) <= 220 for chunk in uploaded_chunks)
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == sum(len(line) for line in lines)
    finally:
        server.stop()


def test_collector_splits_uploads_by_decoded_policy_batch_limit(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    max_batch_bytes = 220
    server = _FakeWorkerServer(forward_only_first_install=False, max_batch_bytes=max_batch_bytes)
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
            (json.dumps({"type": "turn", "id": f"id-{index}", "payload": "x" * 110}) + "\n").encode("utf-8")
            for index in range(3)
        ]
        assert all(len(line) <= max_batch_bytes for line in lines)
        rollout_path.write_bytes(b"".join(lines))

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["uploaded_chunks"] == 3
        assert len(server.uploads) == 3
        for upload in server.uploads:
            chunks = [_json_mapping(chunk, "chunk") for chunk in _json_array(upload["chunks"], "chunks")]
            assert sum(len(_decode_chunk(chunk)) for chunk in chunks) <= max_batch_bytes
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == sum(len(line) for line in lines)
    finally:
        server.stop()


def test_collector_uploads_single_record_up_to_policy_batch_limit(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(forward_only_first_install=False, max_batch_bytes=300)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        large_line = (json.dumps({"type": "turn", "id": "large", "payload": "x" * 180}) + "\n").encode("utf-8")
        assert 150 < len(large_line) <= 300
        rollout_path.write_bytes(large_line)

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["uploaded_chunks"] == 1
        assert len(server.uploads) == 1
        chunks = _json_array(server.uploads[0]["chunks"], "chunks")
        chunk = _json_mapping(chunks[0], "chunks[0]")
        assert _decode_chunk(chunk) == large_line
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == len(large_line)
    finally:
        server.stop()


def test_collector_reports_oversized_record_and_advances_ledger(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(forward_only_first_install=False, max_batch_bytes=220)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        first_line = b'{"type":"turn","id":"before"}\n'
        oversized_line = (json.dumps({"type": "turn", "id": "huge", "payload": "x" * 260}) + "\n").encode("utf-8")
        second_line = b'{"type":"turn","id":"after"}\n'
        assert len(oversized_line) > 220
        rollout_path.write_bytes(first_line + oversized_line + second_line)

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["uploaded_chunks"] == 2
        assert payload["skipped_records"] == 1
        chunks = [
            _json_mapping(chunk, "chunk")
            for upload in server.uploads
            for chunk in _json_array(upload["chunks"], "chunks")
        ]
        assert [chunk["kind"] for chunk in chunks] == ["jsonl_range", "oversized_record", "jsonl_range"]
        skipped_chunk = chunks[1]
        assert skipped_chunk["start_offset"] == len(first_line)
        assert skipped_chunk["end_offset"] == len(first_line) + len(oversized_line)
        assert skipped_chunk["byte_count"] == len(oversized_line)
        assert skipped_chunk["oversized_reason"] == "decoded_size"
        assert _decode_chunk(chunks[0]) == first_line
        assert _decode_chunk(chunks[2]) == second_line
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == len(first_line) + len(oversized_line) + len(second_line)
    finally:
        server.stop()


def test_collector_does_not_advance_ledger_when_skipped_record_ack_miscounts(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    upload_response: dict[str, JsonValue] = {
        "accepted": True,
        "batch_id": "filled-by-test",
        "policy_version": 7,
        "raw_artifact_count": 0,
        "skipped_record_count": 0,
        "acknowledged_ranges": "filled-by-test",
        "trace_count": 0,
        "event_count": 0,
        "unparsed_record_count": 0,
    }

    def fill_batch_id(payload: dict[str, JsonValue]) -> None:
        upload_response["batch_id"] = payload["batch_id"]
        upload_response["acknowledged_ranges"] = _acknowledged_ranges_for_payload(payload)

    server = _FakeWorkerServer(
        upload_response=upload_response,
        before_upload_response=fill_batch_id,
        forward_only_first_install=False,
        max_batch_bytes=220,
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
        oversized_line = (json.dumps({"type": "turn", "id": "huge", "payload": "x" * 260}) + "\n").encode("utf-8")
        assert len(oversized_line) > 220
        rollout_path.write_bytes(oversized_line)

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
            expected_status="error",
        )

        assert "skipped_record_count did not match request" in str(payload["message"])
        assert len(server.uploads) == 1
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        assert _json_mapping(ledger["sources"], "ledger.sources") == {}
    finally:
        server.stop()


def test_collector_reports_record_that_exceeds_encoded_upload_limit(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    max_batch_bytes = 180
    server = _FakeWorkerServer(forward_only_first_install=False, max_batch_bytes=max_batch_bytes)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {}}))

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        encoded_oversized_line = b""
        for payload_size in range(32, 160):
            candidate_payload = base64.b64encode(bytes((offset % 251 for offset in range(payload_size)))).decode(
                "ascii"
            )
            candidate = (json.dumps({"type": "turn", "id": "encoded", "payload": candidate_payload}) + "\n").encode(
                "utf-8"
            )
            encoded_size = len(base64.b64encode(gzip.compress(candidate)).decode("ascii").encode("ascii"))
            if len(candidate) <= max_batch_bytes and encoded_size > max_batch_bytes:
                encoded_oversized_line = candidate
                break
        assert encoded_oversized_line
        rollout_path.write_bytes(encoded_oversized_line)

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["uploaded_chunks"] == 0
        assert payload["skipped_records"] == 1
        chunks = _json_array(server.uploads[0]["chunks"], "chunks")
        skipped_chunk = _json_mapping(chunks[0], "chunks[0]")
        assert skipped_chunk["kind"] == "oversized_record"
        assert skipped_chunk["oversized_reason"] == "encoded_size"
        assert skipped_chunk["byte_count"] == len(encoded_oversized_line)
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == len(encoded_oversized_line)
    finally:
        server.stop()


def test_collector_streams_source_events_without_unbounded_tail_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector_module = runpy.run_path(str(COLLECTOR_ASSET_PATH), run_name="promptless_trace_collector_stream_test")
    iter_source_events = collector_module["_iter_source_events"]
    source_ledger = collector_module["SourceLedger"]

    rollout_path = tmp_path / "rollout.jsonl"
    lines = []
    for index in range(4):
        payload = base64.b64encode(bytes(((index * 47 + offset) % 256 for offset in range(72)))).decode("ascii")
        lines.append((json.dumps({"type": "turn", "id": f"id-{index}", "payload": payload}) + "\n").encode("utf-8"))
    rollout_path.write_bytes(b"".join(lines))
    original_path_open = Path.open

    def guarded_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> object:
        if path == rollout_path and "b" in mode:
            return _ReadForbiddenBinaryFile(open(path, "rb"))
        return original_path_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", guarded_open)

    ledger = source_ledger(path=tmp_path / "ledger.json", is_new=False, sources={})
    events = list(iter_source_events((rollout_path,), ledger, max_chunk_bytes=220))

    assert len(events) > 1
    assert all(source_event.kind == "jsonl_range" for source_event in events)
    contents = []
    for source_event in events:
        assert source_event.content is not None
        contents.append(source_event.content)
    assert b"".join(contents) == b"".join(lines)
    expected_start_offsets = [0]
    for source_event in events[:-1]:
        expected_start_offsets.append(source_event.end_offset)
    assert [source_event.start_offset for source_event in events] == expected_start_offsets
    assert events[-1].end_offset == sum(len(line) for line in lines)
    assert all(
        len(base64.b64encode(gzip.compress(content)).decode("ascii").encode("ascii")) <= 220 for content in contents
    )


def test_collector_streams_from_ledger_offset_without_prefix_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector_module = runpy.run_path(str(COLLECTOR_ASSET_PATH), run_name="promptless_trace_collector_offset_test")
    iter_source_events = collector_module["_iter_source_events"]
    source_ledger = collector_module["SourceLedger"]

    rollout_path = tmp_path / "rollout.jsonl"
    prefix = b"".join(
        (json.dumps({"type": "turn", "id": f"old-{index}", "payload": "x" * 80}) + "\n").encode("utf-8")
        for index in range(40)
    )
    start_offset = len(prefix)
    pending_lines = [
        (json.dumps({"type": "turn", "id": "new-1", "payload": "ready"}) + "\n").encode("utf-8"),
        (json.dumps({"type": "turn", "id": "new-2", "payload": "done"}) + "\n").encode("utf-8"),
    ]
    incomplete_line = b'{"type":"turn","id":"partial"'
    rollout_path.write_bytes(prefix + b"".join(pending_lines) + incomplete_line)
    original_path_open = Path.open

    def guarded_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> object:
        if path == rollout_path and "b" in mode:
            return _ReadForbiddenBinaryFile(open(path, "rb"), forbidden_before_offset=start_offset)
        return original_path_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", guarded_open)

    ledger = source_ledger(path=tmp_path / "ledger.json", is_new=False, sources={str(rollout_path): start_offset})
    events = list(iter_source_events((rollout_path,), ledger, max_chunk_bytes=4096))

    assert len(events) == 1
    source_event = events[0]
    assert source_event.kind == "jsonl_range"
    assert source_event.start_offset == start_offset
    assert source_event.end_offset == start_offset + sum(len(line) for line in pending_lines)
    assert source_event.content == b"".join(pending_lines)


def test_collector_hashes_oversized_source_event_with_bounded_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector_module = runpy.run_path(str(COLLECTOR_ASSET_PATH), run_name="promptless_trace_collector_oversized_test")
    iter_source_events = collector_module["_iter_source_events"]
    source_ledger = collector_module["SourceLedger"]
    source_read_block_bytes = collector_module["SOURCE_READ_BLOCK_BYTES"]

    rollout_path = tmp_path / "rollout.jsonl"
    oversized_line = (
        json.dumps({"type": "turn", "id": "huge", "payload": "x" * (source_read_block_bytes + 2048)}) + "\n"
    ).encode("utf-8")
    rollout_path.write_bytes(oversized_line)
    original_path_open = Path.open

    def guarded_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> object:
        if path == rollout_path and "b" in mode:
            return _ReadForbiddenBinaryFile(open(path, "rb"), max_read_size=source_read_block_bytes)
        return original_path_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", guarded_open)

    ledger = source_ledger(path=tmp_path / "ledger.json", is_new=False, sources={})
    events = list(iter_source_events((rollout_path,), ledger, max_chunk_bytes=128))

    assert len(events) == 1
    event = events[0]
    assert event.kind == "oversized_record"
    assert event.content is None
    assert event.byte_count == len(oversized_line)
    assert event.end_offset == len(oversized_line)
    assert event.content_sha256 == hashlib.sha256(oversized_line).hexdigest()


def test_collector_advances_ledger_through_first_accepted_batch_when_later_batch_fails(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(
        forward_only_first_install=False,
        max_batch_bytes=220,
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
        lines = []
        for index in range(4):
            payload = base64.b64encode(bytes(((index * 59 + offset) % 256 for offset in range(72)))).decode("ascii")
            lines.append((json.dumps({"type": "turn", "id": f"id-{index}", "payload": payload}) + "\n").encode("utf-8"))
        rollout_path.write_bytes(b"".join(lines))

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
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
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
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


def test_collector_persists_cursor_reset_after_trace_file_truncation(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(forward_only_first_install=False)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        ledger_path = plugin_data / "trace-collector-ledger.json"

        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        complete_line = b'{"type":"turn","id":"replacement"}\n'
        rollout_path.write_bytes(complete_line)
        ledger_path.write_text(json.dumps({"schema_version": 1, "sources": {str(rollout_path): 150}}))

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["uploaded_chunks"] == 1
        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        ledger_sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert ledger_sources[str(rollout_path)] == len(complete_line)

        server.uploads.clear()
        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
        )

        assert payload["uploaded_chunks"] == 0
        assert server.uploads == []
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
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
            "PROMPTLESS_WORKER_BASE_URL": "http://example.com",
            "PROMPTLESS_TRACE_COLLECTOR_ALLOW_TEST_URL_OVERRIDES": "0",
        },
        expected_status="error",
    )

    assert "worker base URL must use HTTPS unless" in str(payload["message"])
    _assert_stdout_system_message_only(result, "Promptless trace collection failed")


def test_collector_stdout_stays_codex_schema_safe_for_setup_pending(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(poll_response={"status": "expired"})
    server.start()
    try:
        home = tmp_path / "home"
        payload, result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            lifecycle="stop",
            expected_status="setup_pending",
        )

        message = _assert_stdout_system_message_only(
            result,
            "Promptless trace collection is waiting for browser approval.",
        )
        assert payload["systemMessage"] == message
        assert server.policy_requests == []
        assert server.check_ins == []
    finally:
        server.stop()


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
        payload, result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="blocked",
        )

        assert payload["reason"] == "collector_upgrade_required"
        _assert_stdout_system_message_only(
            result,
            "Promptless trace collection needs a newer Instruction Hub plugin before it can run.",
        )
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
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
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
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "missing signature" in str(payload["message"])
        assert server.check_ins == []
        assert server.uploads == []
    finally:
        server.stop()


def test_collector_rejects_policy_that_disallows_local_trace_reads(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(
        plugin_permissions_overrides={
            "allow_local_file_read": False,
            "allowed_read_roots": [],
        }
    )
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        rollout_path.write_text('{"type":"turn","id":"blocked"}\n')

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PLUGIN_DATA": str(plugin_data),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "does not allow local native trace file reads" in str(payload["message"])
        assert server.check_ins == []
        assert server.uploads == []
    finally:
        server.stop()


def test_collector_rejects_policy_with_trace_roots_outside_allowed_read_roots(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(plugin_permissions_overrides={"allowed_read_roots": ["~/.claude"]})
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        rollout_path = home / ".codex/sessions/rollout.jsonl"
        rollout_path.parent.mkdir(parents=True)
        rollout_path.write_text('{"type":"turn","id":"blocked"}\n')

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PLUGIN_DATA": str(plugin_data),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "native_roots glob is outside plugin_permissions.allowed_read_roots" in str(payload["message"])
        assert server.check_ins == []
        assert server.uploads == []
    finally:
        server.stop()


def test_collector_rejects_policy_with_disallowed_network_host(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(plugin_permissions_overrides={"allowed_hosts": ["worker.example.com"]})
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PLUGIN_DATA": str(plugin_data),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "host is not allowed by plugin_permissions.allowed_hosts" in str(payload["message"])
        assert server.check_ins == []
        assert server.uploads == []
    finally:
        server.stop()


def test_collector_rejects_policy_above_supported_batch_limit(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    server = _FakeWorkerServer(max_batch_bytes=100 * 1024 * 1024 + 1)
    server.start()
    try:
        home = tmp_path / "home"
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()

        payload, _result = _run_collector(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(home),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PLUGIN_DATA": str(plugin_data),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
            expected_status="error",
        )

        assert "max_batch_bytes must not exceed" in str(payload["message"])
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
    payload = _last_collector_payload(result)
    assert payload is not None
    assert payload["status"] == expected_status
    return payload, result


def _start_collector(
    plugin_root: Path,
    host: str,
    env: dict[str, str],
    *,
    lifecycle: str | None = None,
) -> subprocess.Popen[str]:
    command = [str(plugin_root / "bin" / COLLECTOR_BIN), "--host", host]
    if lifecycle is not None:
        command.extend(["--lifecycle", lifecycle])
    return subprocess.Popen(
        command,
        env=_clean_env(**env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _read_any_collector_status(process: subprocess.Popen[str]) -> dict[str, JsonValue]:
    try:
        stdout, stderr = process.communicate(timeout=80)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        pytest.fail(f"collector timed out with stdout={stdout!r} stderr={stderr!r}")
    assert process.returncode == 0
    assert HOST_CREDENTIAL not in stdout
    assert HOST_CREDENTIAL not in stderr
    result = subprocess.CompletedProcess(args=[], returncode=process.returncode, stdout=stdout, stderr=stderr)
    payload = _last_collector_payload(result)
    assert payload is not None
    return payload


def _clean_env(**overrides: str) -> dict[str, str]:
    clean_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    for key in ("SystemRoot", "SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            clean_env[key] = os.environ[key]
    overrides = _collector_test_overrides(overrides)
    clean_env.update(overrides)
    return clean_env


def _python39_executable() -> str | None:
    for candidate in ("python3.9", "python3"):
        executable = shutil.which(candidate)
        if executable is None:
            continue
        result = subprocess.run(
            [executable, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == "3.9":
            return executable
    return None


def _collector_test_overrides(overrides: dict[str, str]) -> dict[str, str]:
    result = dict(overrides)
    result.pop("CODEX_HOME", None)
    result.pop("PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN", None)
    ledger_parent = result.get("PLUGIN_DATA") or result.get("CLAUDE_PLUGIN_DATA")
    if ledger_parent is not None:
        result.setdefault("PROMPTLESS_TRACE_COLLECTOR_LEDGER", str(Path(ledger_parent) / "trace-collector-ledger.json"))
    worker_base_url = result.get("PROMPTLESS_WORKER_BASE_URL")
    if worker_base_url is not None:
        result.setdefault("PIGS_FLY", "1")
        result.setdefault("PROMPTLESS_DASHBOARD_BASE_URL", worker_base_url)
        result.setdefault("PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER", "0")
        result.setdefault("PROMPTLESS_TRACE_COLLECTOR_ALLOW_TEST_URL_OVERRIDES", "1")
        result.setdefault("PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES", "1")
    return result


def _last_collector_payload(result: subprocess.CompletedProcess[str]) -> dict[str, JsonValue] | None:
    for output_name, output in (("stderr", result.stderr), ("stdout", result.stdout)):
        for line in reversed(output.splitlines()):
            candidate = line.strip()
            if not candidate:
                continue
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            payload = _json_mapping(validate_json_value(value, f"collector {output_name}"), f"collector {output_name}")
            if "status" in payload:
                return payload
    return None


def _clone_plugin_with_identity(source_plugin: Path, destination: Path, *, plugin_id: str, package_id: str) -> Path:
    shutil.copytree(source_plugin, destination)
    manifest_path = destination / "hub.managed-runtimes.json"
    manifest = _json_mapping(validate_json_value(json.loads(manifest_path.read_text()), "manifest"), "manifest")
    runtimes = _json_array(manifest["managed_runtimes"], "managed_runtimes")
    runtime = _json_mapping(runtimes[0], "managed_runtimes[0]")
    runtime["plugin_id"] = plugin_id
    runtime["package_id"] = package_id
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return destination


def _collector_command(target: str, event_name: str) -> str:
    lifecycle = {"SessionStart": "session_start", "Stop": "stop", "SessionEnd": "session_end"}[event_name]
    if target == "claude":
        return f'python3 "${{CLAUDE_PLUGIN_ROOT}}/bin/{COLLECTOR_BIN}" --host claude --lifecycle {lifecycle}'
    return f'python3 "${{PLUGIN_ROOT}}/bin/{COLLECTOR_BIN}" --host codex --lifecycle {lifecycle}'


def _rewrite_hub_plugin_version(hub_root: Path, old_version: str, new_version: str) -> None:
    hub_yaml = hub_root / "hub.yaml"
    body = hub_yaml.read_text()
    old_line = f"plugin_version: {old_version}"
    assert old_line in body
    hub_yaml.write_text(body.replace(old_line, f"plugin_version: {new_version}", 1))


def _callback_url_with_state(callback_url: str, state: str) -> str:
    parsed = urlsplit(callback_url)
    query_pairs: list[tuple[str, str]] = []
    for key, values in parse_qs(parsed.query, keep_blank_values=False).items():
        if key == "state":
            continue
        query_pairs.extend((key, value) for value in values)
    query_pairs.append(("state", state))
    return parsed._replace(query=urlencode(query_pairs)).geturl()


def _decode_chunk(chunk: dict[str, JsonValue]) -> bytes:
    encoded = chunk["content_gzip_base64"]
    assert isinstance(encoded, str)
    return gzip.decompress(base64.b64decode(encoded))


def _acknowledged_ranges_for_payload(payload: dict[str, JsonValue]) -> list[JsonValue]:
    chunks = payload.get("chunks")
    ranges: list[JsonValue] = []
    if not isinstance(chunks, list):
        return ranges
    for chunk_value in chunks:
        chunk = _json_mapping(chunk_value, "chunk")
        ranges.append(
            {
                "kind": chunk.get("kind"),
                "source_path_hash": chunk.get("source_path_hash"),
                "start_offset": chunk.get("start_offset"),
                "end_offset": chunk.get("end_offset"),
                "content_sha256": chunk.get("content_sha256"),
            }
        )
    return ranges


def _policy_with(
    base_url: str,
    *,
    required_bootstrap_version: str = "0.1.0",
    forward_only_first_install: bool = True,
    include_in_progress_traces: bool = True,
    max_batch_bytes: int = 1048576,
    capture_policy_overrides: dict[str, str | None] | None = None,
    plugin_permissions_overrides: dict[str, JsonValue] | None = None,
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
        for key, value in capture_policy_overrides.items():
            if value is None:
                capture_policy.pop(key, None)
            else:
                capture_policy[key] = value
    plugin_permissions: dict[str, JsonValue] = {
        "write_user_config": True,
        "repair_user_config": True,
        "allow_network": True,
        "allowed_hosts": ["127.0.0.1"],
        "allow_local_file_read": True,
        "allowed_read_roots": ["~/.codex", "~/.claude"],
    }
    if plugin_permissions_overrides is not None:
        plugin_permissions.update(plugin_permissions_overrides)
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
            "plugin_permissions": plugin_permissions,
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
        capture_policy_overrides: dict[str, str | None] | None = None,
        plugin_permissions_overrides: dict[str, JsonValue] | None = None,
        poll_response: dict[str, JsonValue] | None = None,
        callback_payload_overrides: dict[str, str | None] | None = None,
        callback_state_override: str | None = None,
    ) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWorkerHandler)
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}"
        self.session_requests: list[dict[str, JsonValue]] = []
        self.policy_requests: list[str] = []
        self.poll_requests: list[dict[str, JsonValue]] = []
        self.check_ins: list[dict[str, JsonValue]] = []
        self.upload_requests: list[str] = []
        self.uploads: list[dict[str, JsonValue]] = []
        self._thread: threading.Thread | None = None

        _FakeWorkerHandler.base_url = self.base_url
        _FakeWorkerHandler.policy_response = _policy_with(
            self.base_url,
            required_bootstrap_version=required_bootstrap_version,
            forward_only_first_install=forward_only_first_install,
            include_in_progress_traces=include_in_progress_traces,
            max_batch_bytes=max_batch_bytes,
            capture_policy_overrides=capture_policy_overrides,
            plugin_permissions_overrides=plugin_permissions_overrides,
        )
        _FakeWorkerHandler.check_in_status = check_in_status
        _FakeWorkerHandler.check_in_response = check_in_response
        _FakeWorkerHandler.upload_status = upload_status
        _FakeWorkerHandler.upload_response = upload_response
        _FakeWorkerHandler.upload_responses = upload_responses
        _FakeWorkerHandler.before_upload_response = before_upload_response
        _FakeWorkerHandler.poll_response = poll_response
        _FakeWorkerHandler.callback_payload_overrides = callback_payload_overrides
        _FakeWorkerHandler.callback_state_override = callback_state_override
        _FakeWorkerHandler.session_requests = self.session_requests
        _FakeWorkerHandler.policy_requests = self.policy_requests
        _FakeWorkerHandler.poll_requests = self.poll_requests
        _FakeWorkerHandler.check_ins = self.check_ins
        _FakeWorkerHandler.upload_requests = self.upload_requests
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
    base_url: ClassVar[str]
    policy_response: ClassVar[dict[str, JsonValue]]
    check_in_status: ClassVar[int]
    check_in_response: ClassVar[dict[str, JsonValue] | None]
    upload_status: ClassVar[int]
    upload_response: ClassVar[dict[str, JsonValue] | None]
    upload_responses: ClassVar[list[tuple[int, dict[str, JsonValue] | None]] | None]
    before_upload_response: ClassVar[Callable[[dict[str, JsonValue]], None] | None]
    poll_response: ClassVar[dict[str, JsonValue] | None]
    callback_payload_overrides: ClassVar[dict[str, str | None] | None]
    callback_state_override: ClassVar[str | None]
    session_requests: ClassVar[list[dict[str, JsonValue]]]
    policy_requests: ClassVar[list[str]]
    poll_requests: ClassVar[list[dict[str, JsonValue]]]
    check_ins: ClassVar[list[dict[str, JsonValue]]]
    upload_requests: ClassVar[list[str]]
    uploads: ClassVar[list[dict[str, JsonValue]]]

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            self._send_json(200, {"status": "ok", "deployment_instance_id": "worker-local-1"})
            return
        if parsed.path == "/instruction-hub/enroll/start":
            self._handle_enrollment_start(parsed.query)
            return
        if parsed.path == "/v0/host-enrollment/policy":
            self._assert_authorized()
            query = parse_qs(parsed.query)
            assert query.get("target") in (["codex"], ["claude"])
            self.policy_requests.append(self.path)
            self._send_json(200, self.policy_response)
            return
        self._send_json(404, {"accepted": False})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        payload = self._read_json_body()
        if parsed.path.startswith("/v1/instruction-hub/host-enrollments/sessions/"):
            self.poll_requests.append(payload)
            response = self.poll_response
            if response is None:
                response = {
                    "status": "approved",
                    "host_credential": HOST_CREDENTIAL,
                    "credential_id": "credential-local-1",
                }
            self._send_json(200, response)
            return
        self._assert_authorized()
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
        if parsed.path == "/v0/traces/batches":
            query = parse_qs(parsed.query)
            target_values = query.get("target")
            assert target_values in (["codex"], ["claude"])
            assert payload.get("host") == target_values[0]
            assert payload.get("source") == target_values[0]
            self.upload_requests.append(self.path)
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
        assert self.headers.get("Authorization") == f"Bearer {HOST_CREDENTIAL}"

    def _handle_enrollment_start(self, query_string: str) -> None:
        query = parse_qs(query_string)
        enrollment_request: dict[str, JsonValue] = {key: values[0] for key, values in query.items() if len(values) == 1}
        self.session_requests.append(enrollment_request)
        callback_url = enrollment_request.get("callback_url")
        assert isinstance(callback_url, str)
        expires_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
        callback_payload = {
            "status": "approved",
            "session_id": "session-local-1",
            "deployment_instance_id": "worker-local-1",
            "device_code": "device-local-1",
            "poll_url": f"{self.base_url}/v1/instruction-hub/host-enrollments/sessions/session-local-1/poll",
            "expires_at": expires_at,
            "poll_interval_seconds": "1",
        }
        if self.callback_payload_overrides is not None:
            for key, value in self.callback_payload_overrides.items():
                if value is None:
                    callback_payload.pop(key, None)
                else:
                    callback_payload[key] = value
        if self.callback_state_override is not None:
            callback_url = _callback_url_with_state(callback_url, self.callback_state_override)
        separator = "&" if urlsplit(callback_url).query else "?"
        try:
            with urlopen(f"{callback_url}{separator}{urlencode(callback_payload)}", timeout=5) as response:
                response.read()
        except urllib.error.URLError:
            self._send_json(403, {"accepted": False})
            return
        self._send_json(200, {"accepted": True})

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
        raw_artifact_count = 0
        skipped_record_count = 0
        if isinstance(chunks, list):
            for chunk_value in chunks:
                chunk = _json_mapping(chunk_value, "chunk")
                if chunk.get("kind") == "oversized_record":
                    skipped_record_count += 1
                else:
                    raw_artifact_count += 1
        return {
            "accepted": status < 400,
            "batch_id": payload.get("batch_id"),
            "policy_version": payload.get("policy_version"),
            "raw_artifact_count": raw_artifact_count if status < 400 else 0,
            "skipped_record_count": skipped_record_count if status < 400 else 0,
            "acknowledged_ranges": _acknowledged_ranges_for_payload(payload) if status < 400 else [],
            "trace_count": raw_artifact_count if status < 400 else 0,
            "event_count": raw_artifact_count if status < 400 else 0,
            "unparsed_record_count": 0,
        }


def _json_mapping(value: JsonValue, path: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{path} must be a JSON object"
    return value


def _json_array(value: JsonValue, path: str) -> list[JsonValue]:
    assert isinstance(value, list), f"{path} must be a JSON array"
    return value
