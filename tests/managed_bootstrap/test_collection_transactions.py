from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.contracts import (
    CHUNK_TARGET_BYTES,
    CollectDeadlineExceeded,
    HookTraceContext,
    Host,
    HostPolicy,
    JsonValue,
    RuntimeMetadata,
    SourceEvent,
    SourceLedger,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.traces import (
    _iter_source_events,
    _iter_upload_batches,
    _load_source_ledger,
    _record_ledger_offset,
    _run_collect as _run_collect_runtime,
    _write_source_ledger,
)

from .helpers import _FakeWorkerServer, _diagnostic_log_entries, _run_runtime_json


def _metadata(plugin_version: str = "1.0.0") -> RuntimeMetadata:
    return RuntimeMetadata(
        bootstrap_version="0.2.9",
        toolchain_version="test",
        plugin_id="promptless-instruction-hub-pig",
        plugin_name="PIG",
        plugin_version=plugin_version,
        package_id="pig",
        target="codex",
    )


def _runtime_env(
    tmp_path: Path,
    plugin_root: Path,
    server: _FakeWorkerServer,
    ledger_path: Path,
) -> dict[str, str]:
    home = tmp_path / "home"
    return {
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "PLUGIN_ROOT": str(plugin_root),
        "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        "PROMPTLESS_DASHBOARD_BASE_URL": server.base_url,
        "PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES": "1",
        "PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER": "0",
        "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
    }


def _seed_offset(ledger_path: Path, transcript_path: Path, end_offset: int) -> None:
    ledger = SourceLedger(path=ledger_path, is_new=False, sources={})
    _record_ledger_offset(ledger, transcript_path, end_offset)
    _write_source_ledger(ledger)


def test_legacy_ledger_preserves_offset_and_discards_obsolete_provenance(tmp_path: Path) -> None:
    transcript_path = (tmp_path / "session.jsonl").resolve()
    path_hash = hashlib.sha256(str(transcript_path).encode()).hexdigest()
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "host_baselines": ["codex"],
                "sources": {
                    path_hash: {
                        "path": str(transcript_path),
                        "end_offset": 123,
                        "provenance_only": True,
                        "instruction_hub_release_markers": [
                            {
                                "start_offset": 123,
                                "session_id": "old-session",
                                "captured_at": "2026-08-20T00:00:00Z",
                                "release": {"plugin_version": "0.1.0"},
                            }
                        ],
                    }
                },
            }
        )
    )

    ledger = _load_source_ledger(ledger_path)
    assert ledger.sources[path_hash] == {
        "path": str(transcript_path),
        "end_offset": 123,
    }

    _write_source_ledger(ledger)
    persisted: JsonValue = json.loads(ledger_path.read_text())
    assert isinstance(persisted, dict)
    assert "host_baselines" not in persisted


def test_current_transcript_ack_is_persisted_before_idle_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/pig"
    server = _FakeWorkerServer()
    server.start()
    try:
        ledger_path = tmp_path / "ledger.json"
        transcript_path = (tmp_path / "current.jsonl").resolve()
        previous_record = b'{"kind":"previous"}\n'
        current_record = b'{"kind":"current"}\n'
        transcript_path.write_bytes(previous_record + current_record)
        _seed_offset(ledger_path, transcript_path, len(previous_record))
        runtime_env = _runtime_env(tmp_path, plugin_root, server, ledger_path)
        for key, value in runtime_env.items():
            monkeypatch.setenv(key, value)

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], runtime_env)
        idle_discovery_observed = False

        def observe_idle_discovery(
            host: Host, *, deadline: float, include_active: bool = False
        ) -> tuple[tuple[Path, ...], bool]:
            del host, deadline, include_active
            nonlocal idle_discovery_observed
            idle_discovery_observed = True
            assert len(server.trace_batches) == 1
            source = next(iter(_load_source_ledger(ledger_path).sources.values()))
            assert source["end_offset"] == len(previous_record + current_record)
            return (), True

        monkeypatch.setattr(
            "promptless_instruction_hub.managed_runtime_assets.host_enrollment."
            "promptless_host_runtime.traces._idle_root_scan_paths",
            observe_idle_discovery,
        )

        assert (
            _run_collect_runtime(
                "codex",
                lifecycle_event="stop",
                hook_context=HookTraceContext(transcript_path, None, "session-1", None, None, None),
                include_active=False,
                quiet=True,
            )
            == 0
        )
        assert idle_discovery_observed
    finally:
        server.stop()


def test_first_current_transcript_lock_timeout_reports_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/pig"
    server = _FakeWorkerServer()
    server.start()
    try:
        ledger_path = tmp_path / "ledger.json"
        transcript_path = (tmp_path / "current.jsonl").resolve()
        previous_record = b'{"kind":"previous"}\n'
        transcript_path.write_bytes(previous_record + b'{"kind":"current"}\n')
        _seed_offset(ledger_path, transcript_path, len(previous_record))
        runtime_env = _runtime_env(tmp_path, plugin_root, server, ledger_path)
        for key, value in runtime_env.items():
            monkeypatch.setenv(key, value)

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], runtime_env)
        lock_call_count = 0

        @contextmanager
        def time_out_upload_lock(path: Path, *, wait_for_lock: bool, deadline: float) -> Iterator[bool]:
            del path, wait_for_lock, deadline
            nonlocal lock_call_count
            lock_call_count += 1
            yield False

        monkeypatch.setattr(
            "promptless_instruction_hub.managed_runtime_assets.host_enrollment."
            "promptless_host_runtime.traces._source_ledger_lock",
            time_out_upload_lock,
        )

        assert (
            _run_collect_runtime(
                "codex",
                lifecycle_event="stop",
                hook_context=HookTraceContext(transcript_path, None, "session-1", None, None, None),
                include_active=False,
                quiet=True,
            )
            == 0
        )
        assert server.trace_batches == []
        assert lock_call_count == 1
        diagnostics = _diagnostic_log_entries(tmp_path / "home")
        assert diagnostics[-1]["status"] == "trace_upload_partial"
        assert diagnostics[-1]["reason"] == "collection_deadline_exceeded"
    finally:
        server.stop()


def test_expired_deadline_stops_before_idle_source_iteration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    idle_path = (tmp_path / "idle.jsonl").resolve()
    idle_path.write_bytes(b'{"kind":"incomplete"}')
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=False, sources={})
    iterated_paths: list[Path] = []
    original_iter_source_events = _iter_source_events

    def track_iterated_paths(source_ledger: SourceLedger, paths: tuple[Path, ...]) -> Iterator[SourceEvent]:
        iterated_paths.extend(paths)
        yield from original_iter_source_events(source_ledger, paths)

    monkeypatch.setattr(
        "promptless_instruction_hub.managed_runtime_assets.host_enrollment."
        "promptless_host_runtime.traces._iter_source_events",
        track_iterated_paths,
    )
    with pytest.raises(CollectDeadlineExceeded, match="native trace collection exceeded deadline"):
        list(
            _iter_upload_batches(
                host="codex",
                metadata=_metadata(),
                policy=HostPolicy(policy_version=1, required_bootstrap_version=None),
                lifecycle_event="stop",
                hook_context=HookTraceContext(None, None, None, None, None, None),
                ledger=ledger,
                source_paths=(idle_path,),
                deadline=0,
            )
        )
    assert iterated_paths == []


def test_idle_catch_up_checks_deadline_between_source_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    idle_paths = tuple((tmp_path / f"idle-{index}.jsonl").resolve() for index in range(2))
    for idle_path in idle_paths:
        idle_path.write_bytes(b'{"kind":"idle"}\n')
    monotonic_values = iter((1.0, 1.0, 3.0))
    monkeypatch.setattr(
        "promptless_instruction_hub.managed_runtime_assets.host_enrollment."
        "promptless_host_runtime.traces.time.monotonic",
        lambda: next(monotonic_values),
    )

    batches = _iter_upload_batches(
        host="codex",
        metadata=_metadata(),
        policy=HostPolicy(policy_version=1, required_bootstrap_version=None),
        lifecycle_event="stop",
        hook_context=HookTraceContext(None, None, None, None, None, None),
        ledger=SourceLedger(path=tmp_path / "ledger.json", is_new=False, sources={}),
        source_paths=idle_paths,
        deadline=2.0,
    )
    with pytest.raises(CollectDeadlineExceeded, match="native trace collection exceeded deadline"):
        next(batches)


@pytest.mark.parametrize(
    ("idle_path_count", "max_chunks", "monotonic_values"),
    (
        pytest.param(1, None, (1.0, 1.0, 3.0), id="final-batch"),
        pytest.param(2, 1, (1.0, 1.0, 1.0, 1.0, 3.0), id="batch-cap"),
    ),
)
def test_idle_catch_up_rechecks_deadline_before_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    idle_path_count: int,
    max_chunks: int | None,
    monotonic_values: tuple[float, ...],
) -> None:
    idle_paths = tuple((tmp_path / f"idle-{index}.jsonl").resolve() for index in range(idle_path_count))
    for idle_path in idle_paths:
        idle_path.write_bytes(b'{"kind":"idle"}\n')
    if max_chunks is not None:
        monkeypatch.setattr(
            "promptless_instruction_hub.managed_runtime_assets.host_enrollment."
            "promptless_host_runtime.traces.MAX_UPLOAD_CHUNKS_PER_BATCH",
            max_chunks,
        )
    pending_values = iter(monotonic_values)
    monkeypatch.setattr(
        "promptless_instruction_hub.managed_runtime_assets.host_enrollment."
        "promptless_host_runtime.traces.time.monotonic",
        lambda: next(pending_values),
    )

    batches = _iter_upload_batches(
        host="codex",
        metadata=_metadata(),
        policy=HostPolicy(policy_version=1, required_bootstrap_version=None),
        lifecycle_event="stop",
        hook_context=HookTraceContext(None, None, None, None, None, None),
        ledger=SourceLedger(path=tmp_path / "ledger.json", is_new=False, sources={}),
        source_paths=idle_paths,
        deadline=2.0,
    )
    with pytest.raises(CollectDeadlineExceeded, match="native trace collection exceeded deadline"):
        next(batches)


def test_source_range_boundaries_are_stable_for_unchanged_source(tmp_path: Path) -> None:
    transcript_path = (tmp_path / "session.jsonl").resolve()
    record = b"x" * 69_999 + b"\n"
    transcript_path.write_bytes(record * 65)
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=False, sources={})

    first_attempt = list(_iter_source_events(ledger, (transcript_path,)))
    retry_attempt = list(_iter_source_events(ledger, (transcript_path,)))

    assert CHUNK_TARGET_BYTES == 4 * 1024 * 1024
    assert len(first_attempt) == len(retry_attempt) == 2
    assert first_attempt == retry_attempt
    assert (first_attempt[0].start_offset, first_attempt[0].end_offset) == (0, 59 * len(record))
    assert first_attempt[1].start_offset == first_attempt[0].end_offset
