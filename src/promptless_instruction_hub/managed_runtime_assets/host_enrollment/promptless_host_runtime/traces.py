"""Native trace discovery, batching, upload, and ledger advancement."""

from __future__ import annotations

import base64
import datetime as dt
import glob
import gzip
import hashlib
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

from .contracts import (
    BootstrapAuthError,
    BootstrapError,
    CHUNK_TARGET_BYTES,
    COLLECT_DEADLINE_ENV,
    COLLECT_DEADLINE_SECONDS,
    CollectDeadlineExceeded,
    HookTraceContext,
    Host,
    HostCredential,
    HostPolicy,
    IDLE_SESSION_GRACE_SECONDS,
    JsonValue,
    LifecycleEvent,
    MAX_RECORD_BYTES,
    MAX_STDIN_BYTES,
    MAX_TRACE_BATCH_BYTES,
    MAX_TRANSPORT_BATCH_BYTES,
    MAX_UPLOAD_CHUNKS_PER_BATCH,
    OversizedReason,
    RUNTIME_VERSION,
    RuntimeMetadata,
    SourceEvent,
    SourceLedger,
    TARGET_TRANSPORT_BATCH_BYTES,
    UploadBatch,
    _enrollment_host,
)
from .enrollment import (
    _cached_host_credential,
    _enrollment_context,
    _forget_cached_host_credential,
)
from .host_config import _native_trace_globs
from .metadata import (
    _dashboard_base_url,
    _load_runtime_metadata,
    _plugin_root,
    _worker_base_url,
)
from .output import _emit
from .storage import _atomic_write_text, _ledger_path, _try_lock_state_file, _unlock_state_file
from .validation import (
    _decode_json_object,
    _json_mapping_or_empty,
    _non_empty,
    _optional_int_value,
    _requires_newer_bootstrap,
    _string_value,
)
from .worker import _get_json, _post_json_response, _validate_signed_policy, _worker_url


def _run_collect(
    host: Host,
    *,
    lifecycle_event: LifecycleEvent,
    hook_context: HookTraceContext,
    include_active: bool,
    quiet: bool,
) -> int:
    plugin_root = _plugin_root()
    metadata = _load_runtime_metadata(plugin_root, host)
    ledger_path = _ledger_path()
    worker_base_url = _worker_base_url()
    dashboard_base_url = _dashboard_base_url()
    enrollment_target = _enrollment_host(host)
    enrollment_metadata = (
        metadata if enrollment_target == host else _load_runtime_metadata(plugin_root, enrollment_target)
    )
    context = _enrollment_context(worker_base_url, dashboard_base_url, enrollment_metadata)
    credential = _cached_host_credential(context)
    if credential is None:
        _emit({"status": "trace_upload_skipped", "reason": "not_enrolled", "host": host}, quiet=quiet)
        return 0

    policy_url = _worker_url(
        worker_base_url,
        f"/v0/host-enrollment/policy?{urlencode({'target': enrollment_target})}",
    )
    try:
        signed_policy = _get_json(policy_url, credential.value, label="policy response")
    except BootstrapAuthError:
        _forget_cached_host_credential(context)
        _emit({"status": "trace_upload_skipped", "reason": "credential_rejected", "host": host}, quiet=quiet)
        return 0
    policy = _validate_signed_policy(signed_policy, enrollment_target)
    if _requires_newer_bootstrap(policy.required_bootstrap_version, RUNTIME_VERSION):
        _emit({"status": "blocked", "reason": "bootstrap_upgrade_required", "host": host}, quiet=quiet)
        return 0

    current_transcript_paths = _current_transcript_paths(hook_context, lifecycle_event)
    upload_url = _worker_url(worker_base_url, f"/v0/traces/batches?{urlencode({'target': host})}")
    uploaded_batch_count = 0
    uploaded_chunk_count = 0
    unparsed_record_count = 0
    unreadable_source_hashes: set[str] = set()

    # Keep the hook subject independent from the configurable catch-up budget
    # while bounding its local read, encoding, and ledger-lock work.
    first_current_deadline = time.monotonic() + COLLECT_DEADLINE_SECONDS
    try:
        first_current_counts = _upload_source_paths(
            current_transcript_paths,
            upload_url=upload_url,
            credential=credential,
            host=host,
            metadata=metadata,
            policy=policy,
            lifecycle_event=lifecycle_event,
            hook_context=hook_context,
            ledger_path=ledger_path,
            deadline=first_current_deadline,
            batch_limit=1,
        )
    except CollectDeadlineExceeded:
        _emit(
            {"status": "trace_upload_partial", "reason": "collection_deadline_exceeded", "host": host},
            quiet=quiet,
        )
        return 0
    uploaded_batch_count += first_current_counts[0]
    uploaded_chunk_count += first_current_counts[1]
    unparsed_record_count += first_current_counts[2]
    unreadable_source_hashes.update(first_current_counts[3])

    catch_up_deadline = _collect_deadline()
    deadline_exceeded = False
    try:
        current_counts = _upload_source_paths(
            current_transcript_paths,
            upload_url=upload_url,
            credential=credential,
            host=host,
            metadata=metadata,
            policy=policy,
            lifecycle_event=lifecycle_event,
            hook_context=hook_context,
            ledger_path=ledger_path,
            deadline=catch_up_deadline,
        )
        uploaded_batch_count += current_counts[0]
        uploaded_chunk_count += current_counts[1]
        unparsed_record_count += current_counts[2]
        unreadable_source_hashes.update(current_counts[3])
    except CollectDeadlineExceeded:
        deadline_exceeded = True

    idle_source_paths: tuple[Path, ...] = ()
    idle_scan_complete = False
    if not deadline_exceeded:
        discovered_idle_paths, idle_scan_complete = _idle_root_scan_paths(
            host,
            deadline=catch_up_deadline,
            include_active=include_active,
        )
        all_source_paths = _ordered_unique_source_paths(current_transcript_paths, discovered_idle_paths)
        idle_source_paths = all_source_paths[len(current_transcript_paths) :]
        deadline_exceeded = not idle_scan_complete
    if not current_transcript_paths and not idle_source_paths and idle_scan_complete:
        _emit({"status": "trace_upload_skipped", "reason": "no_sources", "host": host}, quiet=quiet)
        return 0

    if not deadline_exceeded:
        try:
            idle_counts = _upload_source_paths(
                idle_source_paths,
                upload_url=upload_url,
                credential=credential,
                host=host,
                metadata=metadata,
                policy=policy,
                lifecycle_event=lifecycle_event,
                hook_context=hook_context,
                ledger_path=ledger_path,
                deadline=catch_up_deadline,
            )
            uploaded_batch_count += idle_counts[0]
            uploaded_chunk_count += idle_counts[1]
            unparsed_record_count += idle_counts[2]
            unreadable_source_hashes.update(idle_counts[3])
        except CollectDeadlineExceeded:
            deadline_exceeded = True

    payload: dict[str, JsonValue] = {
        "status": "trace_upload_partial" if deadline_exceeded else "trace_upload_complete",
        "host": host,
        "batch_count": uploaded_batch_count,
        "chunk_count": uploaded_chunk_count,
        "unparsed_record_count": unparsed_record_count,
    }
    if unreadable_source_hashes:
        payload["unreadable_source_count"] = len(unreadable_source_hashes)
    if deadline_exceeded:
        payload["reason"] = "collection_deadline_exceeded"
    _emit(payload, quiet=quiet)
    return 0


def _lifecycle_event(value: str | None) -> LifecycleEvent:
    if value in {"session_start", "stop", "session_end", "subagent_stop"}:
        return value
    return "session_start"


def _read_hook_input() -> bytes:
    if sys.stdin.isatty():
        return b""
    body = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(body) > MAX_STDIN_BYTES:
        raise BootstrapError("hook stdin JSON exceeds maximum supported size")
    return body


def _read_hook_context(body: bytes | None = None) -> dict[str, JsonValue]:
    if body is None:
        body = _read_hook_input()
    if body == b"":
        return {}
    return _decode_json_object(body, "hook stdin")


def _hook_trace_context(context: dict[str, JsonValue]) -> HookTraceContext:
    transcript_path = _optional_hook_path(
        _first_context_value(context, ("transcript_path", "transcriptPath", "transcript.path"))
    )
    agent_transcript_path = _optional_hook_path(
        _first_context_value(
            context,
            (
                "agent_transcript_path",
                "agentTranscriptPath",
                "agent.transcript_path",
                "agent.transcriptPath",
            ),
        )
    )
    return HookTraceContext(
        transcript_path=transcript_path,
        agent_transcript_path=agent_transcript_path,
        session_id=_non_empty(
            _first_string(context, ("session_id", "sessionId", "conversation_id", "conversationId", "session.id"))
        ),
        parent_session_id=_non_empty(
            _first_string(
                context,
                (
                    "parent_session_id",
                    "parentSessionId",
                    "parent_conversation_id",
                    "parentConversationId",
                    "parent_session.id",
                    "parentSession.id",
                ),
            )
        ),
        agent_id=_non_empty(
            _first_string(
                context,
                ("agent_id", "agentId", "subagent_id", "subagentId", "agent_name", "agentName", "agent.id"),
            )
        ),
        agent_type=_non_empty(
            _first_string(context, ("agent_type", "agentType", "subagent_type", "subagentType", "agent.type"))
        ),
    )


def _optional_hook_path(value: JsonValue | None) -> Path | None:
    text = _non_empty(_string_value(value))
    if text is None:
        return None
    return Path(text).expanduser()


def _first_string(context: dict[str, JsonValue], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _non_empty(_string_value(_context_value(context, key)))
        if value is not None:
            return value
    return None


def _first_context_value(context: dict[str, JsonValue], keys: tuple[str, ...]) -> JsonValue | None:
    for key in keys:
        value = _context_value(context, key)
        if isinstance(value, str) and _non_empty(value) is None:
            continue
        if value is not None:
            return value
    return None


def _context_value(context: dict[str, JsonValue], key: str) -> JsonValue | None:
    if "." not in key:
        return context.get(key)
    value: JsonValue | None = context
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _current_transcript_paths(
    hook_context: HookTraceContext,
    lifecycle_event: LifecycleEvent,
) -> tuple[Path, ...]:
    """Return the explicit transcript paths carried by the current hook."""

    explicit_paths: list[Path] = []
    if lifecycle_event == "subagent_stop" and hook_context.agent_transcript_path is not None:
        explicit_paths.append(hook_context.agent_transcript_path)
    elif hook_context.transcript_path is not None:
        explicit_paths.append(hook_context.transcript_path)
    if hook_context.agent_transcript_path is not None and hook_context.agent_transcript_path not in explicit_paths:
        explicit_paths.append(hook_context.agent_transcript_path)

    ordered_paths: list[Path] = []
    seen: set[str] = set()
    for path in explicit_paths:
        _append_unique_path(ordered_paths, seen, path)
    return tuple(ordered_paths)


def _ordered_unique_source_paths(*path_groups: tuple[Path, ...]) -> tuple[Path, ...]:
    ordered_paths: list[Path] = []
    seen: set[str] = set()
    for path_group in path_groups:
        for path in path_group:
            _append_unique_path(ordered_paths, seen, path)
    return tuple(ordered_paths)


def _append_unique_path(paths: list[Path], seen: set[str], path: Path) -> None:
    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        resolved = path.expanduser().absolute()
    key = str(resolved)
    if key in seen:
        return
    seen.add(key)
    paths.append(resolved)


def _idle_root_scan_paths(
    host: Host, *, deadline: float, include_active: bool = False
) -> tuple[tuple[Path, ...], bool]:
    """Scan native transcript roots for files, truncating at the deadline.

    The idle sweep is catch-up work: on deadline pressure it returns whatever
    it found so far instead of failing the whole collect. Returns the sorted
    paths plus whether the scan covered the full tree.
    """

    now = time.time()
    result: list[Path] = []
    complete = True
    try:
        for pattern in _native_trace_globs(host):
            for raw_path in glob.iglob(pattern, recursive=True):
                _ensure_collect_deadline(deadline)
                path = Path(raw_path)
                if not path.is_file():
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if not include_active and now - mtime < IDLE_SESSION_GRACE_SECONDS:
                    continue
                result.append(path)
    except CollectDeadlineExceeded:
        complete = False
    return tuple(sorted(result)), complete


@contextmanager
def _source_ledger_lock(path: Path, *, wait_for_lock: bool, deadline: float) -> Iterator[bool]:
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        acquired = _try_lock_state_file(lock_file)
        while wait_for_lock and not acquired:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            time.sleep(min(0.05, remaining_seconds))
            acquired = _try_lock_state_file(lock_file)
        try:
            yield acquired
        finally:
            if acquired:
                _unlock_state_file(lock_file)


def _load_source_ledger(path: Path) -> SourceLedger:
    if not path.exists():
        return SourceLedger(path=path, is_new=True, sources={})
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        _quarantine_corrupt_ledger(path)
        return SourceLedger(
            path=path,
            is_new=True,
            sources={},
            drift_reports=[{"kind": "native_trace_ledger_reset", "reason": "invalid_json"}],
        )
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        _quarantine_corrupt_ledger(path)
        return SourceLedger(
            path=path,
            is_new=True,
            sources={},
            drift_reports=[{"kind": "native_trace_ledger_reset", "reason": "unsupported_schema"}],
        )
    sources = value.get("sources")
    if not isinstance(sources, dict):
        _quarantine_corrupt_ledger(path)
        return SourceLedger(
            path=path,
            is_new=True,
            sources={},
            drift_reports=[{"kind": "native_trace_ledger_reset", "reason": "invalid_sources"}],
        )
    valid_sources: dict[str, dict[str, JsonValue]] = {}
    for raw_key, raw_source in sources.items():
        key = _string_value(raw_key)
        source = _json_mapping_or_empty(raw_source if isinstance(raw_source, dict) else None)
        end_offset = _optional_int_value(source.get("end_offset"))
        if key is None or len(key) != 64 or end_offset is None or end_offset < 0:
            continue
        cleaned_source = dict(source)
        cleaned_source.pop("instruction_hub_release_markers", None)
        cleaned_source.pop("provenance_only", None)
        valid_sources[key] = cleaned_source
    return SourceLedger(path=path, is_new=False, sources=valid_sources)


def _quarantine_corrupt_ledger(path: Path) -> None:
    try:
        if not path.exists():
            return
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")
        path.rename(path.with_name(f"{path.name}.corrupt-{timestamp}"))
    except OSError:
        return


def _write_source_ledger(ledger: SourceLedger) -> None:
    payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "updated_at": _utc_now_iso(),
        "sources": ledger.sources,
    }
    _atomic_write_text(ledger.path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    try:
        ledger.path.chmod(0o600)
    except OSError:
        pass


def _iter_upload_batches(
    *,
    host: Host,
    metadata: RuntimeMetadata,
    policy: HostPolicy,
    lifecycle_event: LifecycleEvent,
    hook_context: HookTraceContext,
    ledger: SourceLedger,
    source_paths: tuple[Path, ...],
    deadline: float,
) -> Iterator[UploadBatch]:
    """Yield exact-wire-bounded batches from the supplied paths until the deadline."""

    event_transcript_paths = _event_transcript_paths(hook_context, lifecycle_event)
    pending_events: list[SourceEvent] = []
    pending_payloads: list[dict[str, JsonValue]] = []
    pending_decoded_bytes = 0
    pending_batch: UploadBatch | None = None
    for source_path in source_paths:
        _ensure_collect_deadline(deadline)
        for event in _iter_source_events(ledger, (source_path,)):
            _ensure_collect_deadline(deadline)
            chunk_lifecycle = lifecycle_event if event.path in event_transcript_paths else None
            payload = _chunk_payload(event, chunk_lifecycle)
            event_batch = _upload_batch(
                host=host,
                metadata=metadata,
                policy=policy,
                lifecycle_event=lifecycle_event,
                hook_context=hook_context,
                events=(event,),
                chunks=(payload,),
            )
            if event.kind == "jsonl_range" and _serialized_upload_batch_bytes(event_batch) > MAX_TRANSPORT_BATCH_BYTES:
                # Passing the raw-size cap does not guarantee the wire fits: high-entropy
                # content grows under gzip+base64. Skip-report the record so the ledger
                # advances past it instead of retrying an unsendable chunk forever.
                event = _oversized_event(
                    event.path,
                    event.path_hash,
                    event.start_offset,
                    event.end_offset,
                    event.content or b"",
                    reason="transport_size",
                )
                payload = _chunk_payload(event, chunk_lifecycle)
                event_batch = _upload_batch(
                    host=host,
                    metadata=metadata,
                    policy=policy,
                    lifecycle_event=lifecycle_event,
                    hook_context=hook_context,
                    events=(event,),
                    chunks=(payload,),
                )
            if _serialized_upload_batch_bytes(event_batch) > MAX_TRANSPORT_BATCH_BYTES:
                raise BootstrapError("one native trace event exceeds the request transport limit")
            exceeds_structural_limit = (
                len(pending_payloads) >= MAX_UPLOAD_CHUNKS_PER_BATCH
                or pending_decoded_bytes + event.byte_count > MAX_TRACE_BATCH_BYTES
            )
            if pending_batch is not None and exceeds_structural_limit:
                _ensure_collect_deadline(deadline)
                yield pending_batch
                pending_events = []
                pending_payloads = []
                pending_decoded_bytes = 0
                pending_batch = None
                _ensure_collect_deadline(deadline)

            next_batch = event_batch
            if pending_batch is not None:
                combined_batch = _upload_batch(
                    host=host,
                    metadata=metadata,
                    policy=policy,
                    lifecycle_event=lifecycle_event,
                    hook_context=hook_context,
                    events=(*pending_events, event),
                    chunks=(*pending_payloads, payload),
                )
                if _serialized_upload_batch_bytes(combined_batch) <= TARGET_TRANSPORT_BATCH_BYTES:
                    next_batch = combined_batch
                else:
                    _ensure_collect_deadline(deadline)
                    yield pending_batch
                    pending_events = []
                    pending_payloads = []
                    pending_decoded_bytes = 0
                    _ensure_collect_deadline(deadline)
            pending_events.append(event)
            pending_payloads.append(payload)
            pending_decoded_bytes += event.byte_count
            pending_batch = next_batch
    if pending_batch is not None:
        _ensure_collect_deadline(deadline)
        yield pending_batch


def _upload_source_paths(
    source_paths: tuple[Path, ...],
    *,
    upload_url: str,
    credential: HostCredential,
    host: Host,
    metadata: RuntimeMetadata,
    policy: HostPolicy,
    lifecycle_event: LifecycleEvent,
    hook_context: HookTraceContext,
    ledger_path: Path,
    deadline: float,
    batch_limit: int | None = None,
) -> tuple[int, int, int, frozenset[str]]:
    """Upload paths with one ledger-locked request and acknowledgement at a time."""

    uploaded_batch_count = 0
    uploaded_chunk_count = 0
    unparsed_record_count = 0
    unreadable_source_hashes: set[str] = set()
    while source_paths and (batch_limit is None or uploaded_batch_count < batch_limit):
        _ensure_collect_deadline(deadline)
        with _source_ledger_lock(ledger_path, wait_for_lock=True, deadline=deadline) as lock_acquired:
            if not lock_acquired:
                raise CollectDeadlineExceeded("native trace collection exceeded deadline waiting for ledger lock")
            ledger = _load_source_ledger(ledger_path)
            batch = next(
                _iter_upload_batches(
                    host=host,
                    metadata=metadata,
                    policy=policy,
                    lifecycle_event=lifecycle_event,
                    hook_context=hook_context,
                    ledger=ledger,
                    source_paths=source_paths,
                    deadline=deadline,
                ),
                None,
            )
            unreadable_source_hashes.update(
                source_path_hash
                for report in ledger.drift_reports
                if report.get("kind") == "native_trace_source_unreadable"
                if (source_path_hash := _string_value(report.get("source_path_hash"))) is not None
            )
            if batch is None:
                if ledger.drift_reports:
                    _write_source_ledger(ledger)
                break
            response = _post_upload_batch(upload_url, credential, policy, batch)
            _advance_ledger_from_response(ledger, source_paths, response)
            _write_source_ledger(ledger)
        uploaded_batch_count += 1
        uploaded_chunk_count += len(batch.events)
        unparsed_record_count += _optional_int_value(response.get("unparsed_record_count")) or 0
    return uploaded_batch_count, uploaded_chunk_count, unparsed_record_count, frozenset(unreadable_source_hashes)


def _iter_source_events(ledger: SourceLedger, source_paths: tuple[Path, ...]) -> Iterator[SourceEvent]:
    for path in source_paths:
        path_hash = _path_hash(path)
        source = ledger.sources.get(path_hash)
        start_offset = _optional_int_value(source.get("end_offset")) if source is not None else 0
        if start_offset is None or start_offset < 0:
            start_offset = 0
        try:
            file_size = path.stat().st_size
        except OSError:
            continue
        if start_offset > file_size:
            ledger.drift_reports.append(
                {
                    "kind": "native_trace_source_rewound",
                    "source_path_hash": path_hash,
                    "previous_end_offset": start_offset,
                    "current_size": file_size,
                }
            )
            ledger.reset_sources.add(path_hash)
            if source is not None:
                reset_source = dict(source)
                reset_source.pop("instruction_hub_release_markers", None)
                reset_source.pop("provenance_only", None)
                ledger.sources[path_hash] = reset_source
            start_offset = 0
        if start_offset == file_size:
            continue
        try:
            with path.open("rb") as handle:
                handle.seek(start_offset)
                buffered_lines = bytearray()
                buffered_start = start_offset
                while handle.tell() < file_size:
                    record_start = handle.tell()
                    line = handle.readline()
                    if line == b"" or not line.endswith(b"\n"):
                        break
                    if len(line) > MAX_RECORD_BYTES:
                        if buffered_lines:
                            yield _range_event(path, path_hash, buffered_start, record_start, bytes(buffered_lines))
                            buffered_lines = bytearray()
                        yield _oversized_event(
                            path, path_hash, record_start, handle.tell(), line, reason="content_size"
                        )
                        buffered_start = handle.tell()
                        continue
                    if buffered_lines and len(buffered_lines) + len(line) > CHUNK_TARGET_BYTES:
                        yield _range_event(path, path_hash, buffered_start, record_start, bytes(buffered_lines))
                        buffered_lines = bytearray()
                        buffered_start = record_start
                    buffered_lines += line
                if buffered_lines:
                    yield _range_event(
                        path, path_hash, buffered_start, buffered_start + len(buffered_lines), bytes(buffered_lines)
                    )
        except OSError:
            # A source that vanished or lost read permission after the stat() above
            # must not abort the run: a persistently unreadable idle file would block
            # all later uploads forever. Ranges already yielded stay contiguous from
            # the watermark, which retries the remainder on the next collect.
            ledger.drift_reports.append({"kind": "native_trace_source_unreadable", "source_path_hash": path_hash})


def _range_event(path: Path, path_hash: str, start_offset: int, end_offset: int, content: bytes) -> SourceEvent:
    return SourceEvent(
        kind="jsonl_range",
        path=path,
        path_hash=path_hash,
        start_offset=start_offset,
        end_offset=end_offset,
        byte_count=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _oversized_event(
    path: Path,
    path_hash: str,
    start_offset: int,
    end_offset: int,
    content: bytes,
    *,
    reason: OversizedReason,
) -> SourceEvent:
    return SourceEvent(
        kind="oversized_record",
        path=path,
        path_hash=path_hash,
        start_offset=start_offset,
        end_offset=end_offset,
        byte_count=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
        oversized_reason=reason,
    )


def _event_transcript_paths(hook_context: HookTraceContext, lifecycle_event: LifecycleEvent) -> frozenset[Path]:
    """Return the transcript files described by the current lifecycle event.

    Idle-file sweeps piggyback on whatever hook fired; stamping the hook's
    lifecycle event onto chunks from other sessions' files would finalize those
    sessions spuriously on the worker.
    """

    if lifecycle_event == "subagent_stop":
        transcript_path = hook_context.agent_transcript_path or hook_context.transcript_path
    else:
        transcript_path = hook_context.transcript_path
    if transcript_path is None:
        return frozenset()
    try:
        resolved = transcript_path.expanduser().resolve(strict=False)
    except OSError:
        resolved = transcript_path.expanduser().absolute()
    return frozenset((resolved,))


def _chunk_payload(event: SourceEvent, lifecycle_event: LifecycleEvent | None) -> dict[str, JsonValue]:
    if event.content_sha256 is None:
        raise BootstrapError("source event missing content hash")
    payload: dict[str, JsonValue] = {
        "kind": event.kind,
        "source_path_hash": event.path_hash,
        "start_offset": event.start_offset,
        "end_offset": event.end_offset,
        "content_sha256": event.content_sha256,
    }
    if lifecycle_event is not None:
        payload["lifecycle_event"] = lifecycle_event
    if event.kind == "oversized_record":
        if event.oversized_reason is None:
            raise BootstrapError("oversized record event missing reason")
        payload["byte_count"] = event.byte_count
        payload["oversized_reason"] = event.oversized_reason
        return payload
    if event.content is None:
        raise BootstrapError("jsonl range event missing content")
    payload["line_count"] = event.content.count(b"\n")
    payload["content_encoding"] = "gzip"
    payload["content_base64"] = _gzip_base64(event.content)
    return payload


def _gzip_base64(content: bytes) -> str:
    return base64.b64encode(gzip.compress(content, mtime=0)).decode("ascii")


def _upload_batch(
    *,
    host: Host,
    metadata: RuntimeMetadata,
    policy: HostPolicy,
    lifecycle_event: LifecycleEvent,
    hook_context: HookTraceContext,
    events: tuple[SourceEvent, ...],
    chunks: tuple[dict[str, JsonValue], ...],
) -> UploadBatch:
    request: dict[str, JsonValue] = {
        "batch_id": _stable_upload_batch_id(host, policy, events),
        "source": host,
        "host": host,
        "policy_version": policy.policy_version,
        "collector_version": metadata.bootstrap_version,
        "plugin_version": metadata.plugin_version,
        "uploaded_at": _utc_now_iso(),
        "chunks": list(chunks),
    }
    request.update(_native_request_context(hook_context, lifecycle_event))
    return UploadBatch(request=request, events=events)


def _serialized_upload_batch_bytes(batch: UploadBatch) -> int:
    """Return the exact JSON body size used by the worker client."""

    return len(json.dumps(batch.request, sort_keys=True).encode())


def _native_request_context(hook_context: HookTraceContext, lifecycle_event: LifecycleEvent) -> dict[str, JsonValue]:
    session_id = hook_context.session_id
    parent_session_id = hook_context.parent_session_id
    agent_id = hook_context.agent_id
    agent_type = hook_context.agent_type
    if lifecycle_event == "subagent_stop":
        if parent_session_id is None:
            parent_session_id = hook_context.session_id
            session_id = None
        if agent_id is None and hook_context.agent_transcript_path is not None:
            agent_id = _path_hash(hook_context.agent_transcript_path)[:32]
        if agent_type is None and agent_id is not None:
            agent_type = "subagent"

    result: dict[str, JsonValue] = {}
    if session_id is not None:
        result["session_id"] = session_id
    if parent_session_id is not None and agent_id is not None:
        result["parent_session_id"] = parent_session_id
        result["agent_id"] = agent_id
        if agent_type is not None:
            result["agent_type"] = agent_type
    return result


def _stable_upload_batch_id(host: Host, policy: HostPolicy, events: tuple[SourceEvent, ...]) -> str:
    material = {
        "host": host,
        "policy_version": policy.policy_version,
        "ranges": [
            {
                "kind": event.kind,
                "source_path_hash": event.path_hash,
                "start_offset": event.start_offset,
                "end_offset": event.end_offset,
                "content_sha256": event.content_sha256,
            }
            for event in events
        ],
    }
    return "native-" + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:48]


def _post_upload_batch(
    upload_url: str,
    credential: HostCredential,
    policy: HostPolicy,
    batch: UploadBatch,
) -> dict[str, JsonValue]:
    response = _post_json_response(upload_url, credential.value, batch.request, label="trace batch response")
    _validate_upload_response(response, policy, batch)
    return response


def _validate_upload_response(response: dict[str, JsonValue], policy: HostPolicy, batch: UploadBatch) -> None:
    if response.get("accepted") is not True:
        raise BootstrapError("trace batch response was not accepted")
    if _string_value(response.get("batch_id")) != _string_value(batch.request.get("batch_id")):
        raise BootstrapError("trace batch response batch_id did not match upload request")
    if response.get("policy_version") != policy.policy_version:
        raise BootstrapError("trace batch response policy_version did not match applied policy")
    unparsed_record_count = _optional_int_value(response.get("unparsed_record_count"))
    if unparsed_record_count is None or unparsed_record_count < 0:
        raise BootstrapError("trace batch response unparsed_record_count must be a non-negative integer")
    expected_raw_count = sum(1 for event in batch.events if event.kind == "jsonl_range")
    if response.get("raw_artifact_count") != expected_raw_count:
        raise BootstrapError("trace batch response raw_artifact_count did not match upload request")
    expected_skipped_count = sum(1 for event in batch.events if event.kind == "oversized_record")
    if response.get("skipped_record_count") != expected_skipped_count:
        raise BootstrapError("trace batch response skipped_record_count did not match upload request")
    if response.get("acknowledged_ranges") != _expected_acknowledged_ranges(batch.events):
        raise BootstrapError("trace batch response acknowledged_ranges did not match upload request")


def _advance_ledger_from_response(
    ledger: SourceLedger,
    source_paths: tuple[Path, ...],
    response: dict[str, JsonValue],
) -> None:
    paths_by_hash = {_path_hash(path): path for path in source_paths}
    acknowledged_ranges = response.get("acknowledged_ranges")
    if not isinstance(acknowledged_ranges, list):
        raise BootstrapError("trace batch response acknowledged_ranges must be a list")
    for value in acknowledged_ranges:
        acknowledged = _json_mapping_or_empty(value)
        source_path_hash = _string_value(acknowledged.get("source_path_hash"))
        end_offset = _optional_int_value(acknowledged.get("end_offset"))
        if source_path_hash is None or end_offset is None:
            raise BootstrapError("trace batch response acknowledged range is malformed")
        path = paths_by_hash.get(source_path_hash)
        if path is None:
            raise BootstrapError("trace batch response acknowledged unknown source")
        _record_ledger_offset(ledger, path, end_offset)


def _expected_acknowledged_ranges(events: tuple[SourceEvent, ...]) -> list[dict[str, JsonValue]]:
    return [
        {
            "kind": event.kind,
            "source_path_hash": event.path_hash,
            "start_offset": event.start_offset,
            "end_offset": event.end_offset,
            "content_sha256": event.content_sha256,
        }
        for event in events
        if event.content_sha256 is not None
    ]


def _record_ledger_offset(ledger: SourceLedger, path: Path, end_offset: int) -> None:
    path_hash = _path_hash(path)
    existing = _json_mapping_or_empty(ledger.sources.get(path_hash))
    previous_offset = 0 if path_hash in ledger.reset_sources else (_optional_int_value(existing.get("end_offset")) or 0)
    updated_source = dict(existing)
    updated_source.update(
        {
            "path": str(path),
            "end_offset": max(previous_offset, end_offset),
            "updated_at": _utc_now_iso(),
        }
    )
    updated_source.pop("provenance_only", None)
    updated_source.pop("instruction_hub_release_markers", None)
    ledger.sources[path_hash] = updated_source


def _path_hash(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _collect_deadline() -> float:
    raw_value = _non_empty(os.environ.get(COLLECT_DEADLINE_ENV))
    if raw_value is None:
        seconds = COLLECT_DEADLINE_SECONDS
    else:
        try:
            seconds = max(0.0, float(raw_value))
        except ValueError:
            seconds = COLLECT_DEADLINE_SECONDS
    return time.monotonic() + seconds


def _ensure_collect_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise CollectDeadlineExceeded("native trace collection exceeded deadline")
