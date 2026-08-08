from __future__ import annotations

import hashlib
import io
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.contracts import (
    BootstrapError,
    SourceEvent,
    SourceLedger,
    TraceSourceSequenceConflict,
    UploadBatch,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.traces import (
    _reconcile_source_sequence_conflict,
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
    )

    with pytest.raises(BootstrapError, match="interior worker watermark"):
        _reconcile_source_sequence_conflict(ledger, batch, conflict)

    assert ledger.sources == {}
