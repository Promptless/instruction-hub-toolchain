from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.contracts import (
    BootstrapError,
    SourceEvent,
    SourceLedger,
    TraceSourceRangeProof,
    TraceSourceSequenceConflict,
    UploadBatch,
    WorkerResponseError,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.traces import (
    _advance_ledger_from_response,
    _iter_source_events,
    _reconcile_source_sequence_conflict,
    _trace_source_sequence_conflict,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.worker import (
    _MAX_WORKER_ERROR_RESPONSE_BYTES,
    _worker_response_error,
)


def test_worker_response_error_rejects_oversized_response_body() -> None:
    response_stream = io.BytesIO(b"x" * (_MAX_WORKER_ERROR_RESPONSE_BYTES + 2))
    http_error = urllib.error.HTTPError(
        url="https://worker.example.test/traces/batch",
        code=409,
        msg="Conflict",
        hdrs=Message(),
        fp=response_stream,
    )

    error = _worker_response_error(http_error, "trace batch response")

    assert error.response_body == b""
    assert response_stream.tell() == _MAX_WORKER_ERROR_RESPONSE_BYTES + 1


def test_sequence_conflict_without_a_range_proof_is_not_reconciled(tmp_path: Path) -> None:
    source_path = tmp_path / "session.jsonl"
    content = b'{"kind":"session_start"}\n{"kind":"stop"}\n'
    source_path.write_bytes(content)
    path_hash = hashlib.sha256(str(source_path).encode()).hexdigest()
    event = SourceEvent(
        kind="jsonl_range",
        path=source_path,
        path_hash=path_hash,
        start_offset=0,
        end_offset=len(content),
        byte_count=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    batch = UploadBatch(request={"source": "codex"}, events=(event,))
    error = WorkerResponseError(
        "conflict",
        status_code=409,
        response_body=json.dumps(
            {
                "detail": {
                    "code": "trace_source_sequence_conflict",
                    "source": "codex",
                    "source_path_hash": path_hash,
                    "requested_start_offset": 0,
                    "requested_end_offset": len(content),
                    "acknowledged_offset": content.index(b'{"kind":"stop"}'),
                    "acknowledged_range": None,
                }
            }
        ).encode(),
    )

    assert _trace_source_sequence_conflict(error, batch) is None


@pytest.mark.parametrize("acknowledge_end", [False, True])
def test_sequence_conflict_does_not_reconcile_a_non_interior_watermark(
    tmp_path: Path,
    acknowledge_end: bool,
) -> None:
    source_path = tmp_path / "session.jsonl"
    content = b'{"kind":"session_start"}\n'
    source_path.write_bytes(content)
    end_offset = len(content)
    acknowledged_offset = end_offset if acknowledge_end else 0
    path_hash = hashlib.sha256(str(source_path).encode()).hexdigest()
    event = SourceEvent(
        kind="jsonl_range",
        path=source_path,
        path_hash=path_hash,
        start_offset=0,
        end_offset=end_offset,
        byte_count=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    batch = UploadBatch(request={"source": "codex"}, events=(event,))
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=True, sources={})
    conflict = TraceSourceSequenceConflict(
        source="codex",
        source_path_hash=path_hash,
        requested_start_offset=0,
        requested_end_offset=end_offset,
        acknowledged_offset=acknowledged_offset,
        acknowledged_range=TraceSourceRangeProof(
            start_offset=0,
            end_offset=acknowledged_offset,
            content_sha256=hashlib.sha256(content[:acknowledged_offset]).hexdigest(),
        ),
    )

    with pytest.raises(BootstrapError, match="interior worker watermark"):
        _reconcile_source_sequence_conflict(ledger, batch, conflict)

    assert ledger.sources == {}


def test_sequence_conflict_reconciles_only_when_committed_bytes_match(tmp_path: Path) -> None:
    source_path = tmp_path / "session.jsonl"
    committed = b'{"kind":"session_start"}\n'
    pending = b'{"kind":"stop"}\n'
    content = committed + pending
    source_path.write_bytes(content)
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=True, sources={})
    event = next(_iter_source_events(ledger, (source_path,)))
    path_hash = event.path_hash
    batch = UploadBatch(request={"source": "codex"}, events=(event,))
    conflict = TraceSourceSequenceConflict(
        source="codex",
        source_path_hash=path_hash,
        requested_start_offset=0,
        requested_end_offset=len(content),
        acknowledged_offset=len(committed),
        acknowledged_range=TraceSourceRangeProof(
            start_offset=0,
            end_offset=len(committed),
            content_sha256=hashlib.sha256(committed).hexdigest(),
        ),
    )

    _reconcile_source_sequence_conflict(ledger, batch, conflict)

    assert ledger.sources[path_hash]["end_offset"] == len(committed)
    assert ledger.sources[path_hash]["prefix_sha256"] == hashlib.sha256(committed).hexdigest()


def test_sequence_conflict_rejects_a_watermark_inside_a_record(tmp_path: Path) -> None:
    source_path = tmp_path / "session.jsonl"
    content = b'{"kind":"session_start"}\n'
    source_path.write_bytes(content)
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=True, sources={})
    event = next(_iter_source_events(ledger, (source_path,)))
    batch = UploadBatch(request={"source": "codex"}, events=(event,))
    acknowledged_offset = content.index(b"session_start")
    conflict = TraceSourceSequenceConflict(
        source="codex",
        source_path_hash=event.path_hash,
        requested_start_offset=0,
        requested_end_offset=len(content),
        acknowledged_offset=acknowledged_offset,
        acknowledged_range=TraceSourceRangeProof(
            start_offset=0,
            end_offset=acknowledged_offset,
            content_sha256=hashlib.sha256(content[:acknowledged_offset]).hexdigest(),
        ),
    )

    with pytest.raises(BootstrapError, match="record boundary"):
        _reconcile_source_sequence_conflict(ledger, batch, conflict)

    assert ledger.sources == {}


def test_sequence_conflict_uses_the_uploaded_snapshot_after_the_source_is_replaced(tmp_path: Path) -> None:
    source_path = tmp_path / "session.jsonl"
    committed = b'{"kind":"session_start","message":"docs"}\n'
    pending = b'{"kind":"stop"}\n'
    content = committed + pending
    source_path.write_bytes(content)
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=True, sources={})
    event = next(_iter_source_events(ledger, (source_path,)))
    path_hash = event.path_hash
    batch = UploadBatch(request={"source": "codex"}, events=(event,))
    conflict = TraceSourceSequenceConflict(
        source="codex",
        source_path_hash=path_hash,
        requested_start_offset=0,
        requested_end_offset=len(content),
        acknowledged_offset=len(committed),
        acknowledged_range=TraceSourceRangeProof(
            start_offset=0,
            end_offset=len(committed),
            content_sha256=hashlib.sha256(committed).hexdigest(),
        ),
    )
    source_path.write_bytes(content.replace(b"docs", b"code"))

    _reconcile_source_sequence_conflict(ledger, batch, conflict)

    assert ledger.sources[path_hash]["end_offset"] == len(committed)
    assert ledger.sources[path_hash]["prefix_sha256"] == hashlib.sha256(committed).hexdigest()


def test_successful_ack_uses_the_uploaded_snapshot_after_the_source_disappears(tmp_path: Path) -> None:
    source_path = tmp_path / "session.jsonl"
    content = b'{"kind":"session_start"}\n'
    source_path.write_bytes(content)
    ledger = SourceLedger(path=tmp_path / "ledger.json", is_new=True, sources={})
    event = next(_iter_source_events(ledger, (source_path,)))
    batch = UploadBatch(request={"source": "codex"}, events=(event,))
    response = {
        "acknowledged_ranges": [
            {
                "kind": event.kind,
                "source_path_hash": event.path_hash,
                "start_offset": event.start_offset,
                "end_offset": event.end_offset,
                "content_sha256": event.content_sha256,
            }
        ]
    }
    source_path.unlink()

    _advance_ledger_from_response(ledger, batch, response)

    assert ledger.sources[event.path_hash]["end_offset"] == len(content)
    assert ledger.sources[event.path_hash]["prefix_sha256"] == hashlib.sha256(content).hexdigest()


def test_source_scan_restarts_when_an_acknowledged_prefix_changes_at_the_same_path(tmp_path: Path) -> None:
    source_path = tmp_path / "session.jsonl"
    original = b'{"kind":"session_start","message":"docs"}\n'
    replacement = original.replace(b"docs", b"code")
    source_path.write_bytes(replacement)
    path_hash = hashlib.sha256(str(source_path).encode()).hexdigest()
    ledger = SourceLedger(
        path=tmp_path / "ledger.json",
        is_new=False,
        sources={
            path_hash: {
                "path": str(source_path),
                "end_offset": len(original),
                "prefix_sha256": hashlib.sha256(original).hexdigest(),
                "instruction_hub_release_markers": [
                    {
                        "start_offset": 0,
                        "session_id": "old-session",
                        "captured_at": "2026-08-19T12:00:00+00:00",
                        "release": {
                            "plugin_id": "promptless-instruction-hub-pig",
                            "plugin_name": "PIG",
                            "plugin_version": "1.0.0",
                            "release_id": "1.0.0+aaaaaaaaaaaa",
                        },
                    }
                ],
            }
        },
    )

    events = list(_iter_source_events(ledger, (source_path,)))

    assert [(event.start_offset, event.end_offset, event.content) for event in events] == [
        (0, len(replacement), replacement)
    ]
    assert path_hash in ledger.reset_sources
    assert "instruction_hub_release_markers" not in ledger.sources[path_hash]
    assert ledger.drift_reports == [
        {
            "kind": "native_trace_source_identity_changed",
            "source_path_hash": path_hash,
            "previous_end_offset": len(original),
            "current_size": len(replacement),
        }
    ]
