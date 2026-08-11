from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.fs import validate_json_value

from .helpers import (
    _FakeWorkerServer,
    _diagnostic_log_entries,
    _diagnostic_log_path,
    _json_int,
    _json_list,
    _json_mapping,
    _json_string,
    _run_collect,
    _run_runtime_json,
)


def test_collect_baselines_then_uploads_transcript_path_ranges(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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


def test_collect_without_baseline_uploads_new_ledger_sources_from_start(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
    plugin_root = hub_root / "dist/codex/pig"
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
