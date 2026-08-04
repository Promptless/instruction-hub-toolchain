from __future__ import annotations

import hashlib
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
