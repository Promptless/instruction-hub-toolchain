from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.fs import validate_json_value

from .helpers import (
    FIRST_SUCCESS_SHOWN_KEY,
    HOST_RUNTIME_BIN,
    _FakeWorkerServer,
    _assert_session_start_streams,
    _claude_desktop_audit_path,
    _clean_env,
    _host_state_path,
    _json_list,
    _json_mapping,
    _json_string,
    _run_collect,
    _run_runtime_json,
    _signed_policy,
)


def _seed_ledger_offsets(ledger_path: Path, *source_paths: Path) -> None:
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {
                    hashlib.sha256(str(path.resolve()).encode()).hexdigest(): {
                        "path": str(path.resolve()),
                        "end_offset": path.stat().st_size,
                    }
                    for path in source_paths
                },
            }
        )
    )


def test_claude_desktop_discovers_both_audit_stores_under_platform_config_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/pig"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
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

        records: dict[Path, bytes] = {}
        for store_name in ("local-agent-mode-sessions", "claude-code-sessions"):
            audit_path = claude_base / store_name / f"{store_name}-session/audit.jsonl"
            audit_path.parent.mkdir(parents=True)
            record = json.dumps({"store": store_name, "message": "existing history"}).encode() + b"\n"
            audit_path.write_bytes(record)
            records[audit_path.resolve()] = record

        _run_runtime_json(plugin_root, ["enroll", "--host", "claude-desktop"], env)
        _run_collect(
            plugin_root,
            [
                "collect",
                "--host",
                "claude-desktop",
                "--lifecycle",
                "session_start",
                "--include-active",
                "--quiet",
            ],
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
        } == set(records.values())
        assert {chunk["start_offset"] for chunk in chunks} == {0}
    finally:
        server.stop()


def test_claude_uploads_current_transcript_before_idle_history(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/pig"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        project_root = home / ".claude/projects/project-1"
        transcript_path = project_root / "current-session.jsonl"
        idle_path = project_root / "idle-session.jsonl"
        project_root.mkdir(parents=True)
        transcript_baseline = b'{"sessionId":"current","message":"baseline"}\n'
        idle_baseline = b'{"sessionId":"idle","message":"baseline"}\n'
        transcript_path.write_bytes(transcript_baseline)
        idle_path.write_bytes(idle_baseline)
        env = {
            "HOME": str(home),
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "claude"], env)
        _seed_ledger_offsets(ledger_path, transcript_path, idle_path)

        transcript_extra = b'{"sessionId":"current","message":"stop"}\n'
        idle_extra = b'{"sessionId":"idle","message":"catch-up"}\n'
        transcript_path.write_bytes(transcript_baseline + transcript_extra)
        idle_path.write_bytes(idle_baseline + idle_extra)
        stale_time = time.time() - (13 * 60 * 60)
        os.utime(idle_path, (stale_time, stale_time))
        _run_collect(
            plugin_root,
            ["collect", "--host", "claude", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "current", "transcript_path": str(transcript_path)},
        )

        assert len(server.trace_batches) == 2
        transcript_batch, idle_batch = server.trace_batches
        transcript_chunk = _json_mapping(
            _json_list(transcript_batch["chunks"], "transcript_batch.chunks")[0], "transcript_chunk"
        )
        idle_chunk = _json_mapping(_json_list(idle_batch["chunks"], "idle_batch.chunks")[0], "idle_chunk")
        assert transcript_batch["host"] == "claude"
        assert transcript_chunk["lifecycle_event"] == "stop"
        assert (
            gzip.decompress(base64.b64decode(_json_string(transcript_chunk["content_base64"], "content_base64")))
            == transcript_extra
        )
        assert "snapshots" not in transcript_batch
        assert "lifecycle_event" not in idle_chunk
        assert (
            gzip.decompress(base64.b64decode(_json_string(idle_chunk["content_base64"], "content_base64")))
            == idle_extra
        )
    finally:
        server.stop()


def test_claude_desktop_ensure_if_sources_skips_without_audit_files(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/pig"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        result = subprocess.run(
            [
                str(plugin_root / "runtime" / HOST_RUNTIME_BIN),
                "ensure",
                "--host",
                "claude-desktop",
                "--if-sources",
            ],
            env=_clean_env(
                HOME=str(home),
                CLAUDE_PLUGIN_ROOT=str(plugin_root),
                PROMPTLESS_WORKER_BASE_URL=server.base_url,
                PROMPTLESS_HOST_RUNTIME_LEDGER=str(ledger_path),
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
        assert not ledger_path.exists()
    finally:
        server.stop()


def test_claude_desktop_ensure_uses_shared_claude_enrollment_and_policy(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/pig"
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
        assert [request["target"] for request in server.session_requests] == ["claude"]
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
    plugin_root = hub_root / "dist/claude/pig"
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
    plugin_root = hub_root / "dist/claude/pig"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
    server.start()
    try:
        home = tmp_path / "home"
        audit_path = _claude_desktop_audit_path(home, "local-agent-mode-sessions", "session-1")
        audit_path.parent.mkdir(parents=True)
        audit_path.write_bytes(b'{"sessionId":"desktop_session_1","message":"baseline"}\n')

        _run_collect(
            plugin_root,
            ["collect", "--host", "claude-desktop", "--lifecycle", "session_start", "--quiet"],
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
    plugin_root = hub_root / "dist/claude/pig"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        audit_path = _claude_desktop_audit_path(home, "local-agent-mode-sessions", "session-1")
        audit_path.parent.mkdir(parents=True)
        first_record = b'{"sessionId":"desktop_session_1","message":"existing"}\n'
        second_record = b'{"sessionId":"desktop_session_1","message":"upload"}\n'
        audit_path.write_bytes(first_record + second_record)
        stale_time = time.time() - (13 * 60 * 60)
        os.utime(audit_path, (stale_time, stale_time))
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
            ["collect", "--host", "claude-desktop", "--lifecycle", "stop", "--quiet"],
            env,
            {},
        )

        assert len(server.trace_batches) == 1
        assert server.policy_requests == ["/v0/host-enrollment/policy?target=claude"]
        batch = server.trace_batches[0]
        assert batch["source"] == "claude-desktop"
        assert batch["host"] == "claude-desktop"
        assert batch["collector_version"] == "0.2.8"
        chunks = _json_list(batch["chunks"], "batch.chunks")
        assert len(chunks) == 1
        chunk = _json_mapping(chunks[0], "batch.chunks[0]")
        assert chunk["kind"] == "jsonl_range"
        assert chunk["start_offset"] == 0
        assert chunk["end_offset"] == len(first_record) + len(second_record)
        assert "lifecycle_event" not in chunk
        assert (
            gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content")))
            == first_record + second_record
        )

        ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        assert "host_baselines" not in ledger
    finally:
        server.stop()


def test_claude_and_desktop_collections_share_one_offset_ledger(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/pig"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        claude_path = home / ".claude/projects/project-1/session.jsonl"
        desktop_path = _claude_desktop_audit_path(home, "claude-code-sessions", "session-1")
        claude_path.parent.mkdir(parents=True)
        desktop_path.parent.mkdir(parents=True)
        claude_path.write_bytes(b'{"sessionId":"claude_session_1","message":"history"}\n')
        desktop_path.write_bytes(b'{"sessionId":"desktop_session_1","message":"history"}\n')
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
            [
                "collect",
                "--host",
                "claude",
                "--lifecycle",
                "session_start",
                "--include-active",
                "--quiet",
            ],
            env,
            {},
        )
        _run_collect(
            plugin_root,
            [
                "collect",
                "--host",
                "claude-desktop",
                "--lifecycle",
                "session_start",
                "--include-active",
                "--quiet",
            ],
            env,
            {},
        )

        assert {batch["host"] for batch in server.trace_batches} == {"claude", "claude-desktop"}
        ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        assert "host_baselines" not in ledger
        sources = _json_mapping(ledger["sources"], "ledger.sources")
        assert len(sources) == 2
        assert [request["target"] for request in server.session_requests] == ["claude"]
    finally:
        server.stop()


def test_concurrent_claude_collections_wait_for_shared_ledger_lock(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("fcntl lock contention test is POSIX-only")
    import fcntl

    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/pig"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
    server.start()
    processes: list[subprocess.Popen[str]] = []
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        claude_path = home / ".claude/projects/project-1/session.jsonl"
        desktop_path = _claude_desktop_audit_path(home, "claude-code-sessions", "session-1")
        claude_path.parent.mkdir(parents=True)
        desktop_path.parent.mkdir(parents=True)
        claude_path.write_bytes(b'{"sessionId":"claude_session_1","message":"history"}\n')
        desktop_path.write_bytes(b'{"sessionId":"desktop_session_1","message":"history"}\n')
        env = {
            "HOME": str(home),
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "claude"], env)
        _run_runtime_json(plugin_root, ["enroll", "--host", "claude-desktop"], env)

        lock_path = ledger_path.with_name(f"{ledger_path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            for host in ("claude", "claude-desktop"):
                processes.append(
                    subprocess.Popen(
                        [
                            str(plugin_root / "runtime" / HOST_RUNTIME_BIN),
                            "collect",
                            "--host",
                            host,
                            "--lifecycle",
                            "session_start",
                            "--include-active",
                            "--quiet",
                        ],
                        env=_clean_env(**env),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )

            time.sleep(1)
            assert all(process.poll() is None for process in processes)
            assert len(server.policy_requests) == 2
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0
            assert stdout == ""
            assert stderr == ""

        ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        assert "host_baselines" not in ledger
        assert len(_json_mapping(ledger["sources"], "ledger.sources")) == 2
        assert len(server.policy_requests) == 2
        assert {batch["host"] for batch in server.trace_batches} == {"claude", "claude-desktop"}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        server.stop()


def test_claude_session_start_supervisor_collects_code_and_desktop(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/claude/pig"
    server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude"]))
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        claude_path = home / ".claude/projects/project-1/session.jsonl"
        desktop_path = _claude_desktop_audit_path(home, "claude-code-sessions", "session-1")
        claude_path.parent.mkdir(parents=True)
        desktop_path.parent.mkdir(parents=True)
        claude_record = b'{"sessionId":"claude_session_1","message":"history"}\n'
        desktop_record = b'{"sessionId":"desktop_session_1","message":"history"}\n'
        claude_path.write_bytes(claude_record)
        desktop_path.write_bytes(desktop_record)
        env = {
            "HOME": str(home),
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }
        _run_runtime_json(plugin_root, ["enroll", "--host", "claude"], env)
        server.policy_requests.clear()

        result = subprocess.run(
            [
                str(plugin_root / "runtime" / HOST_RUNTIME_BIN),
                "session-start",
                "--host",
                "claude",
                "--supervised",
            ],
            env=_clean_env(**env),
            input=json.dumps({"session_id": "claude_session_1", "transcript_path": str(claude_path)}),
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0
        payload = _assert_session_start_streams(result.stdout, result.stderr, "configured")
        assert payload["host"] == "claude"
        assert [request["target"] for request in server.session_requests] == ["claude"]
        assert len(server.check_ins) == 1
        assert server.policy_requests == [
            "/v0/host-enrollment/policy?target=claude",
            "/v0/host-enrollment/policy?target=claude",
            "/v0/host-enrollment/policy?target=claude",
        ]
        assert {batch["host"] for batch in server.trace_batches} == {"claude", "claude-desktop"}
        uploaded_content = {
            gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content")))
            for batch in server.trace_batches
            for chunk in _json_list(batch["chunks"], "chunks")
        }
        assert uploaded_content == {claude_record, desktop_record}
    finally:
        server.stop()


def test_removed_baseline_flags_are_rejected(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/pig"
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
        commands = (
            ["ensure", "--host", "codex", "--prepare-baseline"],
            ["collect", "--host", "codex", "--baseline"],
        )
        for command in commands:
            result = subprocess.run(
                [str(plugin_root / "runtime" / HOST_RUNTIME_BIN), *command],
                env=_clean_env(**env),
                text=True,
                capture_output=True,
                check=False,
            )
            assert result.returncode == 2
            assert "unrecognized arguments" in result.stderr
        assert server.session_requests == []
        assert server.policy_requests == []
        assert server.trace_batches == []
    finally:
        server.stop()


def test_idle_collect_stops_waiting_for_ledger_lock_at_deadline(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("fcntl lock contention test is POSIX-only")
    import fcntl

    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/pig"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        transcript_path = home / ".codex/sessions/pre-enrollment.jsonl"
        transcript_path.parent.mkdir(parents=True)
        transcript_path.write_text('{"kind":"response","message":"pre-enrollment history"}\n')
        stale_time = time.time() - (13 * 60 * 60)
        os.utime(transcript_path, (stale_time, stale_time))
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
            "PROMPTLESS_HOST_RUNTIME_COLLECT_DEADLINE_SECONDS": "0.1",
        }
        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        server.policy_requests.clear()

        lock_path = ledger_path.with_name(f"{ledger_path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            started_at = time.monotonic()
            _run_collect(
                plugin_root,
                ["collect", "--host", "codex", "--lifecycle", "session_start", "--quiet"],
                env,
                {},
                timeout_seconds=2,
            )
            elapsed_seconds = time.monotonic() - started_at
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        assert elapsed_seconds < 1
        assert len(server.policy_requests) == 1
        assert server.trace_batches == []
        assert not ledger_path.exists()

        normal_env = dict(env)
        normal_env.pop("PROMPTLESS_HOST_RUNTIME_COLLECT_DEADLINE_SECONDS")
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            normal_env,
            {},
        )

        assert len(server.trace_batches) == 1
        chunk = _json_mapping(_json_list(server.trace_batches[0]["chunks"], "chunks")[0], "chunk")
        assert chunk["start_offset"] == 0
        ledger = _json_mapping(validate_json_value(json.loads(ledger_path.read_text()), "ledger"), "ledger")
        assert len(_json_mapping(ledger["sources"], "ledger.sources")) == 1
    finally:
        server.stop()
