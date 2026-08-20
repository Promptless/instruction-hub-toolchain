from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.contracts import (
    BootstrapError,
    CollectDeadlineExceeded,
    HookTraceContext,
    HostPolicy,
    InstalledInstructionHubRelease,
    JsonValue,
    RuntimeMetadata,
    SourceEvent,
    SourceLedger,
    UploadBatch,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.metadata import (
    _load_installed_instruction_hub_release,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.traces import (
    _instruction_hub_release_markers,
    _iter_source_events,
    _iter_upload_batches,
    _ledger_has_host_source,
    _load_source_ledger,
    _record_ledger_offset,
    _record_session_release_marker,
    _write_source_ledger,
)
from promptless_instruction_hub.release.hashing import stable_hash

from .helpers import _FakeWorkerServer, _json_list, _json_mapping, _run_collect, _run_runtime_json


def _metadata(plugin_version: str) -> RuntimeMetadata:
    return RuntimeMetadata(
        bootstrap_version="0.2.8",
        toolchain_version="test",
        plugin_id="promptless-instruction-hub-pig",
        plugin_name="PIG",
        plugin_version=plugin_version,
        package_id="pig",
        target="codex",
    )


def _release_manifest(plugin_version: str) -> dict[str, JsonValue]:
    manifest: dict[str, JsonValue] = {
        "schema_version": 1,
        "org": "Promptless",
        "plugin": {
            "id": "promptless-instruction-hub",
            "name": "Promptless Instruction Hub",
            "version": plugin_version,
        },
        "managed_runtimes": [
            {
                "id": "host-runtime",
                "status": "included",
                "target": "codex",
                "package_id": "pig",
                "plugin_id": "promptless-instruction-hub-pig",
                "plugin_name": "PIG",
                "plugin_version": plugin_version,
            }
        ],
        "assets": [],
    }
    return _seal_release_manifest(manifest)


def _seal_release_manifest(manifest: dict[str, JsonValue]) -> dict[str, JsonValue]:
    manifest.pop("release_id", None)
    manifest.pop("release_hash", None)
    plugin = _json_mapping(manifest["plugin"], "plugin")
    plugin_version = plugin["version"]
    assert isinstance(plugin_version, str)
    manifest["release_id"] = f"{plugin_version}+{stable_hash(manifest)[:12]}"
    manifest["release_hash"] = stable_hash(manifest)
    return manifest


def _release(plugin_version: str, release_hash_prefix: str) -> InstalledInstructionHubRelease:
    return InstalledInstructionHubRelease(
        plugin_id="promptless-instruction-hub-pig",
        plugin_name="PIG",
        plugin_version=plugin_version,
        release_id=f"{plugin_version}+{release_hash_prefix}",
    )


def _ledger_with_release_markers(
    ledger_path: Path,
    transcript_path: Path,
    offsets: list[int],
) -> SourceLedger:
    path_hash = hashlib.sha256(str(transcript_path.resolve()).encode()).hexdigest()
    release = {
        "plugin_id": "promptless-instruction-hub-pig",
        "plugin_name": "PIG",
        "plugin_version": "1.0.0",
        "release_id": "1.0.0+aaaaaaaaaaaa",
    }
    markers: list[JsonValue] = [
        {
            "start_offset": offset,
            "session_id": f"session-{index}",
            "captured_at": "2026-08-11T12:00:00+00:00",
            "release": release,
        }
        for index, offset in enumerate(offsets)
    ]
    return SourceLedger(
        path=ledger_path,
        is_new=False,
        sources={
            path_hash: {
                "path": str(transcript_path.resolve()),
                "end_offset": 0,
                "instruction_hub_release_markers": markers,
            }
        },
    )


def test_loads_content_validated_release_from_installed_plugin_root(tmp_path: Path) -> None:
    plugin_root = tmp_path / "installed-plugin"
    plugin_root.mkdir()
    manifest = _release_manifest("1.2.3")
    (plugin_root / "hub.release.json").write_text(json.dumps(manifest))

    assert _load_installed_instruction_hub_release(plugin_root, _metadata("1.2.3")) == (
        InstalledInstructionHubRelease(
            plugin_id="promptless-instruction-hub-pig",
            plugin_name="PIG",
            plugin_version="1.2.3",
            release_id=str(manifest["release_id"]),
        )
    )


@pytest.mark.parametrize("case", ["missing", "corrupt", "invalid_utf8", "mismatched", "tampered"])
def test_rejects_unproven_installed_release_identity(tmp_path: Path, case: str) -> None:
    plugin_root = tmp_path / "installed-plugin"
    plugin_root.mkdir()
    manifest = _release_manifest("1.2.3")
    metadata = _metadata("1.2.3")
    if case == "corrupt":
        (plugin_root / "hub.release.json").write_text("{")
    elif case == "invalid_utf8":
        (plugin_root / "hub.release.json").write_bytes(b"\xff")
    elif case == "mismatched":
        (plugin_root / "hub.release.json").write_text(json.dumps(manifest))
        metadata = _metadata("1.2.4")
    elif case == "tampered":
        manifest["assets"] = [{"ref": "skill:changed"}]
        (plugin_root / "hub.release.json").write_text(json.dumps(manifest))

    assert _load_installed_instruction_hub_release(plugin_root, metadata) is None


def test_rejects_release_manifest_without_matching_distributed_plugin_identity(tmp_path: Path) -> None:
    plugin_root = tmp_path / "installed-plugin"
    plugin_root.mkdir()
    manifest = _release_manifest("1.2.3")
    runtime = _json_mapping(_json_list(manifest["managed_runtimes"], "managed_runtimes")[0], "runtime")
    runtime["plugin_name"] = "A different package"
    _seal_release_manifest(manifest)
    (plugin_root / "hub.release.json").write_text(json.dumps(manifest))

    assert _load_installed_instruction_hub_release(plugin_root, _metadata("1.2.3")) is None


def test_collect_emits_exact_release_snapshot_and_persists_marker(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root, plugin_version="1.2.3")
    plugin_root = hub_root / "dist/codex/pig"
    release_manifest = _json_mapping(json.loads((plugin_root / "hub.release.json").read_text()), "release")
    expected_release = {
        "plugin_id": "promptless-instruction-hub-pig",
        "plugin_name": "PIG",
        "plugin_version": "1.2.3",
        "release_id": release_manifest["release_id"],
    }
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        ledger_path = tmp_path / "ledger.json"
        transcript_path = tmp_path / "codex-session.jsonl"
        baseline_record = b'{"kind":"session_start"}\n'
        uploaded_record = b'{"kind":"stop"}\n'
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
            {"session_id": "session-1", "transcript_path": str(transcript_path)},
        )

        ledger = _json_mapping(json.loads(ledger_path.read_text()), "ledger")
        source = _json_mapping(next(iter(_json_mapping(ledger["sources"], "sources").values())), "source")
        marker = _json_mapping(_json_list(source["instruction_hub_release_markers"], "markers")[0], "marker")
        assert marker["start_offset"] == len(baseline_record)
        assert marker["session_id"] == "session-1"
        assert _json_mapping(marker["release"], "marker.release") == expected_release
        assert "provenance_only" not in source

        transcript_path.write_bytes(baseline_record + uploaded_record)
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "session-1", "transcript_path": str(transcript_path)},
        )

        batch = server.trace_batches[0]
        chunk = _json_mapping(_json_list(batch["chunks"], "chunks")[0], "chunk")
        snapshot = _json_mapping(_json_list(batch["snapshots"], "snapshots")[0], "snapshot")
        assert batch["plugin_version"] == "1.2.3"
        assert snapshot["source_path_hash"] == chunk["source_path_hash"]
        assert snapshot["session_id"] == "session-1"
        assert snapshot["start_offset"] == chunk["start_offset"] == len(baseline_record)
        assert snapshot["end_offset"] == chunk["end_offset"] == len(baseline_record + uploaded_record)
        assert _json_mapping(snapshot["installed_instruction_hub_release"], "snapshot.release") == expected_release
        assert "release_hash" not in _json_mapping(snapshot["installed_instruction_hub_release"], "snapshot.release")
    finally:
        server.stop()


def test_policy_auth_failure_keeps_session_start_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root, plugin_version="1.2.3")
    plugin_root = hub_root / "dist/codex/pig"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        codex_home = home / ".codex"
        transcript_path = codex_home / "sessions/session.jsonl"
        transcript_path.parent.mkdir(parents=True)
        transcript_path.write_bytes(b'{"kind":"session_start"}\n')
        ledger_path = tmp_path / "ledger.json"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
        }

        _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        state_path = home / ".promptless/instruction-hub/host-enrollment-state.json"
        state = _json_mapping(json.loads(state_path.read_text()), "state")
        credentials = _json_mapping(state["credentials"], "state.credentials")
        for credential_value in credentials.values():
            _json_mapping(credential_value, "credential")["value"] = "rejected"
        state_path.write_text(json.dumps(state))

        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "session_start", "--baseline", "--quiet"],
            env,
            {"session_id": "session-1", "transcript_path": str(transcript_path)},
        )

        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        ledger = _load_source_ledger(ledger_path)
        source = next(iter(ledger.sources.values()))
        markers = _instruction_hub_release_markers(source)
        assert [(marker.start_offset, marker.session_id) for marker in markers] == [
            (transcript_path.stat().st_size, "session-1")
        ]
        assert source["provenance_only"] is True
        assert not _ledger_has_host_source(ledger, "codex")
        assert server.trace_batches == []
    finally:
        server.stop()


def test_session_start_marker_is_persisted_without_a_cached_credential(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root, plugin_version="1.2.3")
    plugin_root = hub_root / "dist/codex/pig"
    home = tmp_path / "home"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_bytes(b"")
    ledger_path = tmp_path / "ledger.json"
    env = {
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "PLUGIN_ROOT": str(plugin_root),
        "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger_path),
    }

    _run_collect(
        plugin_root,
        ["collect", "--host", "codex", "--lifecycle", "session_start", "--quiet"],
        env,
        {"session_id": "session-1", "transcript_path": str(transcript_path)},
    )

    ledger = _load_source_ledger(ledger_path)
    source = next(iter(ledger.sources.values()))
    markers = _instruction_hub_release_markers(source)
    assert [(marker.start_offset, marker.session_id) for marker in markers] == [(0, "session-1")]
    assert source["provenance_only"] is True


def test_terminal_and_subagent_hooks_do_not_smear_release_onto_unmarked_sources(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root, plugin_version="1.2.3")
    plugin_root = hub_root / "dist/codex/pig"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        codex_home = home / ".codex"
        ledger_path = tmp_path / "ledger.json"
        transcript_path = tmp_path / "current.jsonl"
        idle_path = codex_home / "sessions/idle.jsonl"
        idle_path.parent.mkdir(parents=True)
        transcript_baseline = b'{"kind":"session_start"}\n'
        idle_baseline = b'{"kind":"other_session"}\n'
        transcript_path.write_bytes(transcript_baseline)
        idle_path.write_bytes(idle_baseline)
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
            {"session_id": "current-session", "transcript_path": str(transcript_path)},
        )
        transcript_path.write_bytes(transcript_baseline + b'{"kind":"stop"}\n')
        idle_path.write_bytes(idle_baseline + b'{"kind":"idle_tail"}\n')
        os.utime(idle_path, (stale, stale))
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"],
            env,
            {"session_id": "current-session", "transcript_path": str(transcript_path)},
        )

        transcript_batch = server.trace_batches[0]
        transcript_snapshots = [
            _json_mapping(value, "snapshot") for value in _json_list(transcript_batch["snapshots"], "snapshots")
        ]
        transcript_hash = hashlib.sha256(str(transcript_path.resolve()).encode()).hexdigest()
        idle_hash = hashlib.sha256(str(idle_path.resolve()).encode()).hexdigest()
        assert {snapshot["source_path_hash"] for snapshot in transcript_snapshots} == {transcript_hash}
        assert idle_hash not in {snapshot["source_path_hash"] for snapshot in transcript_snapshots}

        agent_path = tmp_path / "subagent.jsonl"
        agent_path.write_bytes(b'{"kind":"agent_stop"}\n')
        _run_collect(
            plugin_root,
            ["collect", "--host", "codex", "--lifecycle", "subagent_stop", "--quiet"],
            env,
            {
                "session_id": "parent-session",
                "agent_id": "agent-1",
                "agent_transcript_path": str(agent_path),
            },
        )
        subagent_batch = server.trace_batches[-1]
        assert subagent_batch["parent_session_id"] == "parent-session"
        assert "session_id" not in subagent_batch
        assert "snapshots" not in subagent_batch
    finally:
        server.stop()


def test_persisted_markers_split_snapshot_ranges_without_changing_upload_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / ".codex"
    transcript_path = codex_home / "sessions/session.jsonl"
    transcript_path.parent.mkdir(parents=True)
    first_record = b'{"kind":"before_upgrade"}\n'
    second_record = b'{"kind":"after_upgrade"}\n'
    transcript_path.write_bytes(b"")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    ledger_path = tmp_path / "ledger.json"
    ledger = SourceLedger(path=ledger_path, is_new=True, sources={})
    first_release = _release("1.0.0", "aaaaaaaaaaaa")
    second_release = _release("2.0.0", "bbbbbbbbbbbb")

    assert _record_session_release_marker(
        ledger,
        lifecycle_event="session_start",
        hook_context=HookTraceContext(transcript_path, None, "session-1", None, None, None),
        installed_release=first_release,
    )
    assert not _ledger_has_host_source(ledger, "codex")
    transcript_path.write_bytes(first_record)
    assert _record_session_release_marker(
        ledger,
        lifecycle_event="session_start",
        hook_context=HookTraceContext(transcript_path, None, "session-2", None, None, None),
        installed_release=second_release,
    )
    _write_source_ledger(ledger)
    transcript_path.write_bytes(first_record + second_record)

    hook_context = HookTraceContext(transcript_path, None, "session-2", None, None, None)

    def batches_from_disk() -> list[UploadBatch]:
        persisted = _load_source_ledger(ledger_path)
        return list(
            _iter_upload_batches(
                host="codex",
                metadata=_metadata("2.0.0"),
                policy=HostPolicy(policy_version=1, required_bootstrap_version=None),
                lifecycle_event="stop",
                hook_context=hook_context,
                ledger=persisted,
                source_paths=(transcript_path.resolve(),),
                deadline=float("inf"),
            )
        )

    first_attempt = batches_from_disk()
    retry_attempt = batches_from_disk()
    assert len(first_attempt) == len(retry_attempt) == 1
    first_batch = first_attempt[0]
    retry_batch = retry_attempt[0]
    assert first_batch.request["batch_id"] == retry_batch.request["batch_id"]
    assert first_batch.request["chunks"] == retry_batch.request["chunks"]
    assert first_batch.request["snapshots"] == retry_batch.request["snapshots"]
    assert len(first_batch.events) == 1
    snapshots = [
        _json_mapping(value, "snapshot") for value in _json_list(first_batch.request["snapshots"], "snapshots")
    ]
    assert [(snapshot["session_id"], snapshot["start_offset"], snapshot["end_offset"]) for snapshot in snapshots] == [
        ("session-1", 0, len(first_record)),
        ("session-2", len(first_record), len(first_record + second_record)),
    ]

    persisted = _load_source_ledger(ledger_path)
    _record_ledger_offset(persisted, transcript_path.resolve(), len(first_record + second_record))
    source = next(iter(persisted.sources.values()))
    assert source.get("provenance_only") is None
    assert len(_instruction_hub_release_markers(source)) == 2
    assert _ledger_has_host_source(persisted, "codex")


def test_current_transcript_upload_finishes_before_expired_idle_catch_up(tmp_path: Path) -> None:
    transcript_path = (tmp_path / "current.jsonl").resolve()
    idle_path = (tmp_path / "idle.jsonl").resolve()
    transcript_record = b'{"kind":"current"}\n'
    idle_record = b'{"kind":"idle"}\n'
    transcript_path.write_bytes(transcript_record)
    idle_path.write_bytes(idle_record)
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=False, sources={})
    _record_ledger_offset(ledger, transcript_path, 0)
    _record_ledger_offset(ledger, idle_path, 0)
    hook_context = HookTraceContext(transcript_path, None, "session-1", None, None, None)

    current_batches = list(
        _iter_upload_batches(
            host="codex",
            metadata=_metadata("1.0.0"),
            policy=HostPolicy(policy_version=1, required_bootstrap_version=None),
            lifecycle_event="stop",
            hook_context=hook_context,
            ledger=ledger,
            source_paths=(transcript_path,),
            deadline=float("inf"),
        )
    )
    assert len(current_batches) == 1
    assert [event.path for event in current_batches[0].events] == [transcript_path]
    _record_ledger_offset(ledger, transcript_path, current_batches[0].events[-1].end_offset)
    with pytest.raises(CollectDeadlineExceeded, match="native trace collection exceeded deadline"):
        list(
            _iter_upload_batches(
                host="codex",
                metadata=_metadata("1.0.0"),
                policy=HostPolicy(policy_version=1, required_bootstrap_version=None),
                lifecycle_event="stop",
                hook_context=hook_context,
                ledger=ledger,
                source_paths=(idle_path,),
                deadline=0,
            )
        )

    transcript_hash = hashlib.sha256(str(transcript_path).encode()).hexdigest()
    idle_hash = hashlib.sha256(str(idle_path).encode()).hexdigest()
    assert ledger.sources[transcript_hash]["end_offset"] == len(transcript_record)
    assert ledger.sources[idle_hash]["end_offset"] == 0

    resumed_batches = list(
        _iter_upload_batches(
            host="codex",
            metadata=_metadata("1.0.0"),
            policy=HostPolicy(policy_version=1, required_bootstrap_version=None),
            lifecycle_event="stop",
            hook_context=hook_context,
            ledger=ledger,
            source_paths=(idle_path,),
            deadline=float("inf"),
        )
    )
    assert len(resumed_batches) == 1
    assert [event.path for event in resumed_batches[0].events] == [idle_path]


def test_expired_deadline_stops_before_idle_source_iteration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    idle_path = (tmp_path / "idle.jsonl").resolve()
    idle_path.write_bytes(b'{"kind":"incomplete"}')
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=False, sources={})
    _record_ledger_offset(ledger, idle_path, 0)
    iterated_paths: list[Path] = []
    original_iter_source_events = _iter_source_events

    def track_iterated_paths(source_ledger: SourceLedger, paths: tuple[Path, ...]) -> Iterator[SourceEvent]:
        for path in paths:
            iterated_paths.append(path)
            yield from original_iter_source_events(source_ledger, (path,))

    monkeypatch.setattr(
        "promptless_instruction_hub.managed_runtime_assets.host_enrollment."
        "promptless_host_runtime.traces._iter_source_events",
        track_iterated_paths,
    )
    with pytest.raises(CollectDeadlineExceeded, match="native trace collection exceeded deadline"):
        list(
            _iter_upload_batches(
                host="codex",
                metadata=_metadata("1.0.0"),
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
    first_idle_path = (tmp_path / "first-idle.jsonl").resolve()
    second_idle_path = (tmp_path / "second-idle.jsonl").resolve()
    first_idle_path.write_bytes(b'{"kind":"first_idle"}\n')
    second_idle_path.write_bytes(b'{"kind":"second_idle"}\n')
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=False, sources={})
    _record_ledger_offset(ledger, first_idle_path, 0)
    _record_ledger_offset(ledger, second_idle_path, 0)
    monotonic_values = iter((1.0, 1.0, 3.0))
    monkeypatch.setattr(
        "promptless_instruction_hub.managed_runtime_assets.host_enrollment."
        "promptless_host_runtime.traces.time.monotonic",
        lambda: next(monotonic_values),
    )

    with pytest.raises(CollectDeadlineExceeded, match="native trace collection exceeded deadline"):
        list(
            _iter_upload_batches(
                host="codex",
                metadata=_metadata("1.0.0"),
                policy=HostPolicy(policy_version=1, required_bootstrap_version=None),
                lifecycle_event="stop",
                hook_context=HookTraceContext(None, None, None, None, None, None),
                ledger=ledger,
                source_paths=(first_idle_path, second_idle_path),
                deadline=2.0,
            )
        )


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
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=False, sources={})
    for idle_path in idle_paths:
        idle_path.write_bytes(b'{"kind":"idle"}\n')
        _record_ledger_offset(ledger, idle_path, 0)
    if max_chunks is not None:
        monkeypatch.setattr(
            "promptless_instruction_hub.managed_runtime_assets.host_enrollment."
            "promptless_host_runtime.traces.MAX_UPLOAD_CHUNKS_PER_BATCH",
            max_chunks,
        )
    pending_monotonic_values = iter(monotonic_values)
    monkeypatch.setattr(
        "promptless_instruction_hub.managed_runtime_assets.host_enrollment."
        "promptless_host_runtime.traces.time.monotonic",
        lambda: next(pending_monotonic_values),
    )

    with pytest.raises(CollectDeadlineExceeded, match="native trace collection exceeded deadline"):
        list(
            _iter_upload_batches(
                host="codex",
                metadata=_metadata("1.0.0"),
                policy=HostPolicy(policy_version=1, required_bootstrap_version=None),
                lifecycle_event="stop",
                hook_context=HookTraceContext(None, None, None, None, None, None),
                ledger=ledger,
                source_paths=idle_paths,
                deadline=2.0,
            )
        )


def test_snapshot_limit_pages_whole_events_without_loss_and_retries_stably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript_path = tmp_path / "session.jsonl"
    records = [f'{{"i":{index:03d}}}\n'.encode() for index in range(202)]
    offsets: list[int] = []
    current_offset = 0
    for record in records:
        offsets.append(current_offset)
        current_offset += len(record)
    transcript_path.write_bytes(b"".join(records))
    ledger = _ledger_with_release_markers(tmp_path / "ledger.json", transcript_path, offsets)
    monkeypatch.setattr(
        "promptless_instruction_hub.managed_runtime_assets.host_enrollment."
        "promptless_host_runtime.traces.CHUNK_TARGET_BYTES",
        len(records[0]) * 101,
    )

    def batches() -> list[UploadBatch]:
        return list(
            _iter_upload_batches(
                host="codex",
                metadata=_metadata("1.0.0"),
                policy=HostPolicy(policy_version=1, required_bootstrap_version=None),
                lifecycle_event="stop",
                hook_context=HookTraceContext(transcript_path, None, "session-201", None, None, None),
                ledger=ledger,
                source_paths=(transcript_path.resolve(),),
                deadline=float("inf"),
            )
        )

    first_attempt = batches()
    retry_attempt = batches()
    assert len(first_attempt) == len(retry_attempt) == 2
    assert [batch.request["batch_id"] for batch in first_attempt] == [
        batch.request["batch_id"] for batch in retry_attempt
    ]
    assert [batch.request["chunks"] for batch in first_attempt] == [batch.request["chunks"] for batch in retry_attempt]
    assert [batch.request["snapshots"] for batch in first_attempt] == [
        batch.request["snapshots"] for batch in retry_attempt
    ]

    snapshot_pages = [_json_list(batch.request["snapshots"], "snapshots") for batch in first_attempt]
    assert [len(page) for page in snapshot_pages] == [101, 101]
    snapshots = [_json_mapping(value, "snapshot") for page in snapshot_pages for value in page]
    assert [snapshot["session_id"] for snapshot in snapshots] == [f"session-{index}" for index in range(202)]
    assert [(batch.events[0].start_offset, batch.events[-1].end_offset) for batch in first_attempt] == [
        (0, offsets[101]),
        (offsets[101], current_offset),
    ]

    _record_ledger_offset(ledger, transcript_path.resolve(), first_attempt[0].events[-1].end_offset)
    resumed_attempt = batches()
    assert len(resumed_attempt) == 1
    assert resumed_attempt[0].request["batch_id"] == first_attempt[1].request["batch_id"]
    assert resumed_attempt[0].request["chunks"] == first_attempt[1].request["chunks"]
    assert resumed_attempt[0].request["snapshots"] == first_attempt[1].request["snapshots"]


def test_single_event_over_snapshot_limit_fails_before_ack(tmp_path: Path) -> None:
    transcript_path = tmp_path / "session.jsonl"
    records = [f'{{"i":{index:03d}}}\n'.encode() for index in range(201)]
    offsets: list[int] = []
    current_offset = 0
    for record in records:
        offsets.append(current_offset)
        current_offset += len(record)
    transcript_path.write_bytes(b"".join(records))
    ledger = _ledger_with_release_markers(tmp_path / "ledger.json", transcript_path, offsets)

    with pytest.raises(BootstrapError, match="one native trace JSONL range intersects more than 200"):
        list(
            _iter_upload_batches(
                host="codex",
                metadata=_metadata("1.0.0"),
                policy=HostPolicy(policy_version=1, required_bootstrap_version=None),
                lifecycle_event="stop",
                hook_context=HookTraceContext(transcript_path, None, "session-200", None, None, None),
                ledger=ledger,
                source_paths=(transcript_path.resolve(),),
                deadline=float("inf"),
            )
        )

    source = next(iter(ledger.sources.values()))
    assert source["end_offset"] == 0


def test_duplicate_session_start_does_not_append_redundant_release_marker(tmp_path: Path) -> None:
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_bytes(b"")
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=True, sources={})
    release = _release("1.0.0", "aaaaaaaaaaaa")
    context = HookTraceContext(transcript_path, None, "session-1", None, None, None)

    assert _record_session_release_marker(
        ledger,
        lifecycle_event="session_start",
        hook_context=context,
        installed_release=release,
    )
    transcript_path.write_bytes(b'{"kind":"resume"}\n')
    assert not _record_session_release_marker(
        ledger,
        lifecycle_event="session_start",
        hook_context=context,
        installed_release=release,
    )

    source = next(iter(ledger.sources.values()))
    markers = _instruction_hub_release_markers(source)
    assert [(marker.start_offset, marker.session_id, marker.release) for marker in markers] == [
        (0, "session-1", release)
    ]


def test_resumed_unseen_transcript_starts_release_provenance_at_captured_eof(tmp_path: Path) -> None:
    transcript_path = tmp_path / "resumed-session.jsonl"
    existing_content = b'{"kind":"history-before-resume"}\n'
    transcript_path.write_bytes(existing_content)
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=True, sources={})
    release = _release("1.0.0", "aaaaaaaaaaaa")

    assert _record_session_release_marker(
        ledger,
        lifecycle_event="session_start",
        hook_context=HookTraceContext(transcript_path, None, "session-1", None, None, None),
        installed_release=release,
    )

    source = next(iter(ledger.sources.values()))
    markers = _instruction_hub_release_markers(source)
    assert [(marker.start_offset, marker.session_id, marker.release) for marker in markers] == [
        (len(existing_content), "session-1", release)
    ]
    assert source["end_offset"] == 0
    assert source["provenance_only"] is True


def test_rewound_source_marks_current_release_from_zero(tmp_path: Path) -> None:
    transcript_path = tmp_path / "session.jsonl"
    old_content = b'{"kind":"old-content-longer-than-new"}\n'
    new_content = b'{"kind":"new"}\n'
    transcript_path.write_bytes(old_content)
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=True, sources={})
    old_release = _release("1.0.0", "aaaaaaaaaaaa")
    new_release = _release("2.0.0", "bbbbbbbbbbbb")

    assert _record_session_release_marker(
        ledger,
        lifecycle_event="session_start",
        hook_context=HookTraceContext(transcript_path, None, "old-session", None, None, None),
        installed_release=old_release,
    )
    _record_ledger_offset(ledger, transcript_path.resolve(), len(old_content))
    transcript_path.write_bytes(new_content)
    assert _record_session_release_marker(
        ledger,
        lifecycle_event="session_start",
        hook_context=HookTraceContext(transcript_path, None, "new-session", None, None, None),
        installed_release=new_release,
    )

    source = next(iter(ledger.sources.values()))
    markers = _instruction_hub_release_markers(source)
    assert [(marker.start_offset, marker.session_id, marker.release) for marker in markers] == [
        (0, "new-session", new_release)
    ]
    assert source["end_offset"] == 0
    assert ledger.reset_sources


def test_rewound_provenance_only_source_marks_current_release_from_zero(tmp_path: Path) -> None:
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_bytes(b"")
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=True, sources={})
    first_release = _release("1.0.0", "aaaaaaaaaaaa")
    second_release = _release("2.0.0", "bbbbbbbbbbbb")
    current_release = _release("3.0.0", "cccccccccccc")

    assert _record_session_release_marker(
        ledger,
        lifecycle_event="session_start",
        hook_context=HookTraceContext(transcript_path, None, "session-1", None, None, None),
        installed_release=first_release,
    )
    old_content = b'{"kind":"old-content-longer-than-new"}\n'
    transcript_path.write_bytes(old_content)
    assert _record_session_release_marker(
        ledger,
        lifecycle_event="session_start",
        hook_context=HookTraceContext(transcript_path, None, "session-2", None, None, None),
        installed_release=second_release,
    )

    source = next(iter(ledger.sources.values()))
    assert source["end_offset"] == 0
    assert source["provenance_only"] is True
    assert _instruction_hub_release_markers(source)[-1].start_offset == len(old_content)

    transcript_path.write_bytes(b'{"kind":"new"}\n')
    assert _record_session_release_marker(
        ledger,
        lifecycle_event="session_start",
        hook_context=HookTraceContext(transcript_path, None, "session-3", None, None, None),
        installed_release=current_release,
    )

    source = next(iter(ledger.sources.values()))
    markers = _instruction_hub_release_markers(source)
    assert [(marker.start_offset, marker.session_id, marker.release) for marker in markers] == [
        (0, "session-3", current_release)
    ]
    assert source["end_offset"] == 0
    assert source["provenance_only"] is True
    assert ledger.reset_sources


def test_corrupt_persisted_marker_timestamp_is_not_uploaded(tmp_path: Path) -> None:
    source = {
        "path": str(tmp_path / "session.jsonl"),
        "end_offset": 0,
        "instruction_hub_release_markers": [
            {
                "start_offset": 0,
                "session_id": "session-1",
                "captured_at": "not-a-datetime",
                "release": {
                    "plugin_id": "promptless-instruction-hub-pig",
                    "plugin_name": "PIG",
                    "plugin_version": "1.0.0",
                    "release_id": "1.0.0+aaaaaaaaaaaa",
                },
            }
        ],
    }
    assert _instruction_hub_release_markers(source) == ()


@pytest.mark.parametrize("missing_field", ["plugin_id", "plugin_name", "plugin_version", "release_id"])
def test_persisted_marker_requires_complete_release_identity(tmp_path: Path, missing_field: str) -> None:
    release: dict[str, JsonValue] = {
        "plugin_id": "promptless-instruction-hub-pig",
        "plugin_name": "PIG",
        "plugin_version": "1.0.0",
        "release_id": "1.0.0+aaaaaaaaaaaa",
    }
    release.pop(missing_field)
    source: dict[str, JsonValue] = {
        "path": str(tmp_path / "session.jsonl"),
        "end_offset": 0,
        "instruction_hub_release_markers": [
            {
                "start_offset": 0,
                "session_id": "session-1",
                "captured_at": "2026-08-11T12:00:00+00:00",
                "release": release,
            }
        ],
    }

    assert _instruction_hub_release_markers(source) == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plugin_id", "Not Kebab Case"),
        ("plugin_id", "a" * 121),
        ("plugin_name", ""),
        ("plugin_name", "a" * 201),
    ],
)
def test_persisted_marker_rejects_invalid_plugin_identity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    release: dict[str, JsonValue] = {
        "plugin_id": "promptless-instruction-hub-pig",
        "plugin_name": "PIG",
        "plugin_version": "1.0.0",
        "release_id": "1.0.0+aaaaaaaaaaaa",
    }
    release[field] = value
    source: dict[str, JsonValue] = {
        "path": str(tmp_path / "session.jsonl"),
        "end_offset": 0,
        "instruction_hub_release_markers": [
            {
                "start_offset": 0,
                "session_id": "session-1",
                "captured_at": "2026-08-11T12:00:00+00:00",
                "release": release,
            }
        ],
    }

    assert _instruction_hub_release_markers(source) == ()
