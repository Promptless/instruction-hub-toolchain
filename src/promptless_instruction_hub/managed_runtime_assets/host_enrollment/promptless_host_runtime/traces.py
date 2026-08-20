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
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from .contracts import (
    BootstrapAuthError,
    BootstrapError,
    CHUNK_TARGET_BYTES,
    COLLECT_DEADLINE_ENV,
    COLLECT_DEADLINE_SECONDS,
    CollectDeadlineExceeded,
    FIRST_CURRENT_BATCH_DEADLINE_SECONDS,
    HookTraceContext,
    Host,
    HostCredential,
    HostPolicy,
    HOST_VALUES,
    IDLE_SESSION_GRACE_SECONDS,
    InFlightSourceEvent,
    InFlightUploadBatch,
    InstalledInstructionHubRelease,
    JsonValue,
    LifecycleEvent,
    MAX_ANALYSIS_CONTEXT_SNAPSHOTS_PER_BATCH,
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
from .host_config import _claude_desktop_trace_roots, _native_trace_globs
from .metadata import (
    _dashboard_base_url,
    _load_installed_instruction_hub_release,
    _load_runtime_metadata,
    _plugin_root,
    _worker_base_url,
)
from .output import _emit
from .storage import _atomic_write_text, _ledger_path, _try_lock_state_file, _unlock_state_file
from .validation import (
    _decode_json_object,
    _is_kebab_case_identifier,
    _json_mapping_or_empty,
    _non_empty,
    _optional_int_value,
    _requires_newer_bootstrap,
    _string_value,
)
from .worker import _get_json, _post_json_response, _validate_signed_policy, _worker_url


_INSTRUCTION_HUB_RELEASE_MARKERS_KEY = "instruction_hub_release_markers"


@dataclass(frozen=True)
class _InstructionHubReleaseMarker:
    """Installed release known to govern bytes starting at one source offset."""

    start_offset: int
    session_id: str
    captured_at: str
    release: InstalledInstructionHubRelease


@dataclass(frozen=True)
class _InstructionHubReleaseSnapshotRange:
    """One proven source interval governed by an installed release marker."""

    source_path_hash: str
    session_id: str
    start_offset: int
    end_offset: int
    captured_at: str
    release: InstalledInstructionHubRelease


@dataclass(frozen=True)
class _SessionReleaseBoundary:
    """Resolved transcript and byte boundary captured by SessionStart."""

    path: Path
    size: int


def _run_collect(
    host: Host,
    *,
    lifecycle_event: LifecycleEvent,
    hook_context: HookTraceContext,
    baseline: bool,
    include_active: bool,
    quiet: bool,
    release_marker_captured: bool = False,
) -> int:
    first_current_batch_deadline = time.monotonic() + FIRST_CURRENT_BATCH_DEADLINE_SECONDS
    marker_deadline = _collect_deadline()
    plugin_root = _plugin_root()
    metadata = _load_runtime_metadata(plugin_root, host)
    installed_release = _load_installed_instruction_hub_release(plugin_root, metadata)
    ledger_path = _ledger_path()
    if not release_marker_captured:
        _persist_session_release_marker(
            ledger_path,
            lifecycle_event=lifecycle_event,
            hook_context=hook_context,
            installed_release=installed_release,
            deadline=marker_deadline,
        )
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

    baseline_pending_path = _baseline_pending_path(ledger_path, host)
    if baseline:
        try:
            _prepare_baseline(host)
        except OSError:
            _emit(
                {"status": "trace_upload_degraded", "reason": "baseline_pending_write_failed", "host": host},
                quiet=quiet,
            )
            return 1

    with _source_ledger_lock(ledger_path, wait_for_lock=True, deadline=_collect_deadline()) as lock_acquired:
        if not lock_acquired:
            _emit(
                {"status": "trace_upload_partial", "reason": "collection_deadline_exceeded", "host": host},
                quiet=quiet,
            )
            return 0

        ledger = _load_source_ledger(ledger_path)
        host_baselined = host in ledger.host_baselines or _ledger_has_host_source(ledger, host)
        if not baseline and _baseline_is_pending(baseline_pending_path) and not host_baselined:
            _emit({"status": "trace_upload_skipped", "reason": "baseline_required", "host": host}, quiet=quiet)
            return 0

        if include_active and host not in ledger.host_baselines:
            _emit({"status": "trace_upload_skipped", "reason": "baseline_required", "host": host}, quiet=quiet)
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
    if baseline:
        source_paths: tuple[Path, ...] = ()
        if not host_baselined:
            # Baseline discovery records offsets for every known source, including
            # files still inside the idle grace period. The unmetered scan runs
            # without the ledger lock so release-marker capture remains available.
            idle_source_paths, _ = _idle_root_scan_paths(host, deadline=float("inf"), include_active=True)
            source_paths = _ordered_unique_source_paths(current_transcript_paths, idle_source_paths)
        with _source_ledger_lock(ledger_path, wait_for_lock=True, deadline=_collect_deadline()) as lock_acquired:
            if not lock_acquired:
                _emit(
                    {"status": "trace_upload_partial", "reason": "collection_deadline_exceeded", "host": host},
                    quiet=quiet,
                )
                return 0
            ledger = _load_source_ledger(ledger_path)
            host_baselined = host in ledger.host_baselines or _ledger_has_host_source(ledger, host)
            if not host_baselined:
                _baseline_source_offsets(ledger, source_paths)
                ledger.host_baselines.add(host)
                _write_source_ledger(ledger)
                _clear_baseline_pending(baseline_pending_path)
                _emit(
                    {
                        "status": "trace_upload_baselined",
                        "host": host,
                        "source_count": len(source_paths),
                        "drift_report_count": len(ledger.drift_reports),
                    },
                    quiet=quiet,
                )
                return 0
            if host not in ledger.host_baselines:
                ledger.host_baselines.add(host)
                _write_source_ledger(ledger)
        _clear_baseline_pending(baseline_pending_path)

    # A new/quarantined ledger seen without --baseline (e.g. a terminal hook after a missed
    # SessionStart or a corrupt-ledger reset) must not baseline: that would skip the completed
    # transcript entirely. Fall through so unknown sources upload from offset 0.
    upload_url = _worker_url(worker_base_url, f"/v0/traces/batches?{urlencode({'target': host})}")
    uploaded_batch_count = 0
    uploaded_chunk_count = 0
    unparsed_record_count = 0
    unreadable_source_hashes: set[str] = set()

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
            deadline=first_current_batch_deadline,
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


def _ledger_has_host_source(ledger: SourceLedger, host: Host) -> bool:
    for source in ledger.sources.values():
        if source.get("provenance_only") is True:
            continue
        path_text = _string_value(source.get("path"))
        if path_text is None:
            continue
        if _native_trace_path_belongs_to_host(Path(path_text).expanduser(), host):
            return True
    return False


def _native_trace_path_belongs_to_host(path: Path, host: Host) -> bool:
    if host == "codex":
        root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        return _path_is_relative_to(path, root)
    if host == "claude":
        return _path_is_relative_to(path, Path.home() / ".claude/projects")
    return any(_path_is_relative_to(path, root) for root in _claude_desktop_trace_roots())


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve(strict=False).relative_to(root.expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


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


def _baseline_pending_path(ledger_path: Path, host: Host) -> Path:
    return ledger_path.with_name(f"{ledger_path.name}.{host}.baseline-pending")


def _prepare_baseline(host: Host) -> None:
    ledger_path = _ledger_path()
    pending_path = _baseline_pending_path(ledger_path, host)
    try:
        pending_path.stat()
    except FileNotFoundError:
        _mark_baseline_pending(pending_path, host)


def _mark_baseline_pending(path: Path, host: Host) -> None:
    payload = {"host": host, "created_at": _utc_now_iso()}
    _atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n")


def _baseline_is_pending(path: Path) -> bool:
    try:
        path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _clear_baseline_pending(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


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
        valid_sources[key] = source
    host_baselines_value = value.get("host_baselines")
    host_baselines: set[str] = set()
    if isinstance(host_baselines_value, list):
        for raw_host in host_baselines_value:
            host = _string_value(raw_host)
            if host in HOST_VALUES:
                host_baselines.add(host)
    return SourceLedger(
        path=path,
        is_new=False,
        sources=valid_sources,
        host_baselines=host_baselines,
        in_flight_batches=_load_in_flight_batches(value.get("in_flight_batches")),
    )


def _load_in_flight_batches(value: JsonValue | None) -> dict[Host, list[InFlightUploadBatch]]:
    batches_by_host: dict[Host, list[InFlightUploadBatch]] = {}
    for raw_host, raw_batches in _json_mapping_or_empty(value).items():
        host = _host_value(raw_host)
        if host is None or not isinstance(raw_batches, list):
            continue
        batches = [batch for raw_batch in raw_batches if (batch := _in_flight_batch_value(raw_batch)) is not None]
        if batches:
            batches_by_host[host] = batches
    return batches_by_host


def _in_flight_batch_value(value: JsonValue) -> InFlightUploadBatch | None:
    batch = _json_mapping_or_empty(value)
    lifecycle_event = _lifecycle_event_value(batch.get("lifecycle_event"))
    context = _json_mapping_or_empty(batch.get("hook_context"))
    raw_events = batch.get("events")
    if lifecycle_event is None or not isinstance(raw_events, list) or not raw_events:
        return None
    events = tuple(event for raw_event in raw_events if (event := _in_flight_event_value(raw_event)) is not None)
    if len(events) != len(raw_events) or len(events) > MAX_UPLOAD_CHUNKS_PER_BATCH:
        return None
    return InFlightUploadBatch(
        lifecycle_event=lifecycle_event,
        hook_context=HookTraceContext(
            transcript_path=_optional_path_value(context.get("transcript_path")),
            agent_transcript_path=_optional_path_value(context.get("agent_transcript_path")),
            session_id=_string_value(context.get("session_id")),
            parent_session_id=_string_value(context.get("parent_session_id")),
            agent_id=_string_value(context.get("agent_id")),
            agent_type=_string_value(context.get("agent_type")),
        ),
        events=events,
    )


def _in_flight_event_value(value: JsonValue) -> InFlightSourceEvent | None:
    event = _json_mapping_or_empty(value)
    kind_value = _string_value(event.get("kind"))
    if kind_value == "jsonl_range":
        kind = "jsonl_range"
    elif kind_value == "oversized_record":
        kind = "oversized_record"
    else:
        return None
    path_value = _non_empty(_string_value(event.get("path")))
    path_hash = _string_value(event.get("source_path_hash"))
    start_offset = _optional_int_value(event.get("start_offset"))
    end_offset = _optional_int_value(event.get("end_offset"))
    byte_count = _optional_int_value(event.get("byte_count"))
    content_sha256 = _string_value(event.get("content_sha256"))
    oversized_reason_value = _string_value(event.get("oversized_reason"))
    oversized_reason: OversizedReason | None = None
    if oversized_reason_value == "content_size":
        oversized_reason = "content_size"
    elif oversized_reason_value == "transport_size":
        oversized_reason = "transport_size"
    if (
        path_value is None
        or path_hash is None
        or len(path_hash) != 64
        or any(character not in "0123456789abcdef" for character in path_hash)
        or start_offset is None
        or start_offset < 0
        or end_offset is None
        or end_offset <= start_offset
        or byte_count is None
        or byte_count != end_offset - start_offset
        or content_sha256 is None
        or len(content_sha256) != 64
        or any(character not in "0123456789abcdef" for character in content_sha256)
        or (kind == "jsonl_range" and oversized_reason is not None)
        or (kind == "oversized_record" and oversized_reason is None)
    ):
        return None
    path = Path(path_value)
    if _path_hash(path) != path_hash:
        return None
    return InFlightSourceEvent(
        kind=kind,
        path=path,
        path_hash=path_hash,
        start_offset=start_offset,
        end_offset=end_offset,
        byte_count=byte_count,
        content_sha256=content_sha256,
        oversized_reason=oversized_reason,
    )


def _host_value(value: JsonValue | None) -> Host | None:
    text = _string_value(value)
    if text == "codex":
        return "codex"
    if text == "claude":
        return "claude"
    if text == "claude-desktop":
        return "claude-desktop"
    return None


def _lifecycle_event_value(value: JsonValue | None) -> LifecycleEvent | None:
    text = _string_value(value)
    if text == "session_start":
        return "session_start"
    if text == "stop":
        return "stop"
    if text == "session_end":
        return "session_end"
    if text == "subagent_stop":
        return "subagent_stop"
    return None


def _optional_path_value(value: JsonValue | None) -> Path | None:
    text = _string_value(value)
    return Path(text) if text is not None else None


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
        "host_baselines": sorted(ledger.host_baselines),
        "sources": ledger.sources,
    }
    if ledger.in_flight_batches:
        payload["in_flight_batches"] = {
            host: [_in_flight_batch_payload(batch) for batch in batches]
            for host, batches in ledger.in_flight_batches.items()
            if batches
        }
    _atomic_write_text(ledger.path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    try:
        ledger.path.chmod(0o600)
    except OSError:
        pass


def _in_flight_batch_payload(batch: InFlightUploadBatch) -> dict[str, JsonValue]:
    context = batch.hook_context
    hook_context: dict[str, JsonValue] = {}
    if context.transcript_path is not None:
        hook_context["transcript_path"] = str(context.transcript_path)
    if context.agent_transcript_path is not None:
        hook_context["agent_transcript_path"] = str(context.agent_transcript_path)
    if context.session_id is not None:
        hook_context["session_id"] = context.session_id
    if context.parent_session_id is not None:
        hook_context["parent_session_id"] = context.parent_session_id
    if context.agent_id is not None:
        hook_context["agent_id"] = context.agent_id
    if context.agent_type is not None:
        hook_context["agent_type"] = context.agent_type
    return {
        "lifecycle_event": batch.lifecycle_event,
        "hook_context": hook_context,
        "events": [_in_flight_event_payload(event) for event in batch.events],
    }


def _in_flight_event_payload(event: InFlightSourceEvent) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "kind": event.kind,
        "path": str(event.path),
        "source_path_hash": event.path_hash,
        "start_offset": event.start_offset,
        "end_offset": event.end_offset,
        "byte_count": event.byte_count,
        "content_sha256": event.content_sha256,
    }
    if event.oversized_reason is not None:
        payload["oversized_reason"] = event.oversized_reason
    return payload


def _baseline_source_offsets(ledger: SourceLedger, source_paths: tuple[Path, ...]) -> None:
    for path in source_paths:
        try:
            end_offset = path.stat().st_size
        except OSError:
            continue
        _record_ledger_offset(ledger, path, end_offset)


def _persist_session_release_marker(
    ledger_path: Path,
    *,
    lifecycle_event: LifecycleEvent,
    hook_context: HookTraceContext,
    installed_release: InstalledInstructionHubRelease | None,
    deadline: float,
) -> None:
    """Capture and durably record the exact SessionStart byte boundary."""

    boundary = _session_release_boundary(
        lifecycle_event=lifecycle_event,
        hook_context=hook_context,
        installed_release=installed_release,
    )
    if boundary is None:
        return
    with _source_ledger_lock(ledger_path, wait_for_lock=True, deadline=deadline) as lock_acquired:
        if not lock_acquired:
            raise CollectDeadlineExceeded("timed out persisting native trace release provenance")
        ledger = _load_source_ledger(ledger_path)
        if _record_session_release_marker(
            ledger,
            lifecycle_event=lifecycle_event,
            hook_context=hook_context,
            installed_release=installed_release,
            boundary=boundary,
        ):
            # Persist before credentials, policy fetches, uploads, or detached launch.
            # Retries then retain the same provenance and capture timestamp.
            _write_source_ledger(ledger)


def _session_release_boundary(
    *,
    lifecycle_event: LifecycleEvent,
    hook_context: HookTraceContext,
    installed_release: InstalledInstructionHubRelease | None,
) -> _SessionReleaseBoundary | None:
    if (
        lifecycle_event != "session_start"
        or installed_release is None
        or hook_context.session_id is None
        or len(hook_context.session_id) > 220
        or hook_context.transcript_path is None
    ):
        return None
    try:
        path = hook_context.transcript_path.expanduser().resolve(strict=False)
    except OSError:
        path = hook_context.transcript_path.expanduser().absolute()
    try:
        current_size = path.stat().st_size
    except OSError:
        return None
    return _SessionReleaseBoundary(path=path, size=current_size)


def _record_session_release_marker(
    ledger: SourceLedger,
    *,
    lifecycle_event: LifecycleEvent,
    hook_context: HookTraceContext,
    installed_release: InstalledInstructionHubRelease | None,
    boundary: _SessionReleaseBoundary | None = None,
) -> bool:
    """Persist release provenance only for an exact SessionStart source and session."""

    if (
        lifecycle_event != "session_start"
        or installed_release is None
        or hook_context.session_id is None
        or len(hook_context.session_id) > 220
        or hook_context.transcript_path is None
    ):
        return False
    if boundary is None:
        boundary = _session_release_boundary(
            lifecycle_event=lifecycle_event,
            hook_context=hook_context,
            installed_release=installed_release,
        )
    if boundary is None:
        return False
    path = boundary.path
    current_size = boundary.size

    path_hash = _path_hash(path)
    existing_source = ledger.sources.get(path_hash)
    source = dict(_json_mapping_or_empty(existing_source))
    start_offset = current_size
    previous_offset = _optional_int_value(source.get("end_offset")) or 0
    markers = list(_instruction_hub_release_markers(source))
    latest_marker_offset = markers[-1].start_offset if markers else 0
    if max(previous_offset, latest_marker_offset) > current_size:
        ledger.drift_reports.append(
            {
                "kind": "native_trace_source_rewound",
                "source_path_hash": path_hash,
                "previous_end_offset": previous_offset,
                "current_size": current_size,
            }
        )
        ledger.reset_sources.add(path_hash)
        source["end_offset"] = 0
        markers = []
        start_offset = 0

    if (
        markers
        and markers[-1].start_offset <= current_size
        and markers[-1].session_id == hook_context.session_id
        and markers[-1].release == installed_release
    ):
        return False

    marker = _InstructionHubReleaseMarker(
        start_offset=start_offset,
        session_id=hook_context.session_id,
        captured_at=_utc_now_iso(),
        release=installed_release,
    )
    for existing in markers:
        if existing.start_offset != start_offset:
            continue
        if existing.session_id == marker.session_id and existing.release == marker.release:
            return False
        markers = [candidate for candidate in markers if candidate.start_offset != start_offset]
        break
    markers.append(marker)
    markers.sort(key=lambda candidate: candidate.start_offset)
    source.update(
        {
            "path": str(path),
            "end_offset": _optional_int_value(source.get("end_offset")) or 0,
            "updated_at": _utc_now_iso(),
            _INSTRUCTION_HUB_RELEASE_MARKERS_KEY: [_release_marker_payload(value) for value in markers],
        }
    )
    if existing_source is None:
        source["provenance_only"] = True
    ledger.sources[path_hash] = source
    return True


def _instruction_hub_release_markers(
    source: dict[str, JsonValue],
) -> tuple[_InstructionHubReleaseMarker, ...]:
    raw_markers = source.get(_INSTRUCTION_HUB_RELEASE_MARKERS_KEY)
    if not isinstance(raw_markers, list):
        return ()
    markers: list[_InstructionHubReleaseMarker] = []
    seen_offsets: set[int] = set()
    conflicting_offsets: set[int] = set()
    for raw_marker in raw_markers:
        marker_value = _json_mapping_or_empty(raw_marker)
        start_offset = _optional_int_value(marker_value.get("start_offset"))
        session_id = _non_empty(_string_value(marker_value.get("session_id")))
        captured_at = _non_empty(_string_value(marker_value.get("captured_at")))
        release_value = _json_mapping_or_empty(marker_value.get("release"))
        plugin_id = _non_empty(_string_value(release_value.get("plugin_id")))
        plugin_name = _non_empty(_string_value(release_value.get("plugin_name")))
        plugin_version = _non_empty(_string_value(release_value.get("plugin_version")))
        release_id = _non_empty(_string_value(release_value.get("release_id")))
        if (
            start_offset is None
            or start_offset < 0
            or session_id is None
            or captured_at is None
            or not _timezone_aware_iso_datetime(captured_at)
            or len(session_id) > 220
            or plugin_id is None
            or len(plugin_id) > 120
            or not _is_kebab_case_identifier(plugin_id)
            or plugin_name is None
            or len(plugin_name) > 200
            or plugin_version is None
            or len(plugin_version) > 80
            or release_id is None
            or len(release_id) > 120
            or not _release_id_matches_plugin_version(release_id, plugin_version)
        ):
            continue
        if start_offset in seen_offsets:
            conflicting_offsets.add(start_offset)
            continue
        seen_offsets.add(start_offset)
        markers.append(
            _InstructionHubReleaseMarker(
                start_offset=start_offset,
                session_id=session_id,
                captured_at=captured_at,
                release=InstalledInstructionHubRelease(
                    plugin_id=plugin_id,
                    plugin_name=plugin_name,
                    plugin_version=plugin_version,
                    release_id=release_id,
                ),
            )
        )
    return tuple(
        sorted(
            (marker for marker in markers if marker.start_offset not in conflicting_offsets),
            key=lambda m: m.start_offset,
        )
    )


def _release_id_matches_plugin_version(release_id: str, plugin_version: str) -> bool:
    prefix = f"{plugin_version}+"
    suffix = release_id[len(prefix) :] if release_id.startswith(prefix) else ""
    return len(suffix) == 12 and all(character in "0123456789abcdef" for character in suffix)


def _timezone_aware_iso_datetime(value: str) -> bool:
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _release_marker_payload(marker: _InstructionHubReleaseMarker) -> dict[str, JsonValue]:
    return {
        "start_offset": marker.start_offset,
        "session_id": marker.session_id,
        "captured_at": marker.captured_at,
        "release": {
            "plugin_id": marker.release.plugin_id,
            "plugin_name": marker.release.plugin_name,
            "plugin_version": marker.release.plugin_version,
            "release_id": marker.release.release_id,
        },
    }


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
    pending_snapshot_count = 0
    pending_batch: UploadBatch | None = None
    for source_path in source_paths:
        _ensure_collect_deadline(deadline)
        for event in _iter_source_events(ledger, (source_path,)):
            _ensure_collect_deadline(deadline)
            chunk_lifecycle = lifecycle_event if event.path in event_transcript_paths else None
            event_snapshot_count = len(_analysis_context_snapshot_ranges(ledger, (event,)))
            if event_snapshot_count > MAX_ANALYSIS_CONTEXT_SNAPSHOTS_PER_BATCH:
                raise BootstrapError(
                    "one native trace JSONL range intersects more than "
                    f"{MAX_ANALYSIS_CONTEXT_SNAPSHOTS_PER_BATCH} instruction-hub release ranges"
                )
            payload = _chunk_payload(event, chunk_lifecycle)
            event_batch = _upload_batch(
                host=host,
                metadata=metadata,
                policy=policy,
                lifecycle_event=lifecycle_event,
                hook_context=hook_context,
                ledger=ledger,
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
                    ledger=ledger,
                    events=(event,),
                    chunks=(payload,),
                )
            if _serialized_upload_batch_bytes(event_batch) > MAX_TRANSPORT_BATCH_BYTES:
                raise BootstrapError("one native trace event exceeds the request transport limit")
            exceeds_structural_limit = (
                len(pending_payloads) >= MAX_UPLOAD_CHUNKS_PER_BATCH
                or pending_snapshot_count + event_snapshot_count > MAX_ANALYSIS_CONTEXT_SNAPSHOTS_PER_BATCH
                or pending_decoded_bytes + event.byte_count > MAX_TRACE_BATCH_BYTES
            )
            if pending_batch is not None and exceeds_structural_limit:
                _ensure_collect_deadline(deadline)
                yield pending_batch
                pending_events = []
                pending_payloads = []
                pending_decoded_bytes = 0
                pending_snapshot_count = 0
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
                    ledger=ledger,
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
                    pending_snapshot_count = 0
                    _ensure_collect_deadline(deadline)
            pending_events.append(event)
            pending_payloads.append(payload)
            pending_decoded_bytes += event.byte_count
            pending_snapshot_count += event_snapshot_count
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
            in_flight_batch = _next_in_flight_batch(ledger, host, source_paths)
            if in_flight_batch is None:
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
            else:
                batch = _restore_in_flight_upload_batch(
                    in_flight_batch,
                    host=host,
                    metadata=metadata,
                    policy=policy,
                    ledger=ledger,
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
            if in_flight_batch is None:
                in_flight_batch = _record_in_flight_batch(
                    ledger,
                    host=host,
                    lifecycle_event=lifecycle_event,
                    hook_context=hook_context,
                    batch=batch,
                )
                # The exact range boundaries must survive a worker commit whose
                # response is lost. Persist them before starting the request.
                _write_source_ledger(ledger)
            _ensure_collect_deadline(deadline)
            response = _post_upload_batch(upload_url, credential, policy, batch)
            _advance_ledger_from_response(ledger, source_paths, response)
            _clear_in_flight_batch(ledger, host, in_flight_batch)
            _write_source_ledger(ledger)
        uploaded_batch_count += 1
        uploaded_chunk_count += len(batch.events)
        unparsed_record_count += _optional_int_value(response.get("unparsed_record_count")) or 0
    return uploaded_batch_count, uploaded_chunk_count, unparsed_record_count, frozenset(unreadable_source_hashes)


def _next_in_flight_batch(
    ledger: SourceLedger,
    host: Host,
    source_paths: tuple[Path, ...],
) -> InFlightUploadBatch | None:
    paths_by_hash = {_path_hash(path): path for path in source_paths}
    for batch in ledger.in_flight_batches.get(host, []):
        if all(paths_by_hash.get(event.path_hash) == event.path for event in batch.events):
            return batch
    return None


def _record_in_flight_batch(
    ledger: SourceLedger,
    *,
    host: Host,
    lifecycle_event: LifecycleEvent,
    hook_context: HookTraceContext,
    batch: UploadBatch,
) -> InFlightUploadBatch:
    events: list[InFlightSourceEvent] = []
    for event in batch.events:
        if event.content_sha256 is None:
            raise BootstrapError("native trace event missing content hash")
        events.append(
            InFlightSourceEvent(
                kind=event.kind,
                path=event.path,
                path_hash=event.path_hash,
                start_offset=event.start_offset,
                end_offset=event.end_offset,
                byte_count=event.byte_count,
                content_sha256=event.content_sha256,
                oversized_reason=event.oversized_reason,
            )
        )
    in_flight_batch = InFlightUploadBatch(
        lifecycle_event=lifecycle_event,
        hook_context=hook_context,
        events=tuple(events),
    )
    ledger.in_flight_batches.setdefault(host, []).append(in_flight_batch)
    return in_flight_batch


def _restore_in_flight_upload_batch(
    in_flight_batch: InFlightUploadBatch,
    *,
    host: Host,
    metadata: RuntimeMetadata,
    policy: HostPolicy,
    ledger: SourceLedger,
) -> UploadBatch:
    events = tuple(_restore_in_flight_source_event(event) for event in in_flight_batch.events)
    event_transcript_paths = _event_transcript_paths(
        in_flight_batch.hook_context,
        in_flight_batch.lifecycle_event,
    )
    chunks = tuple(
        _chunk_payload(
            event,
            in_flight_batch.lifecycle_event if event.path in event_transcript_paths else None,
        )
        for event in events
    )
    batch = _upload_batch(
        host=host,
        metadata=metadata,
        policy=policy,
        lifecycle_event=in_flight_batch.lifecycle_event,
        hook_context=in_flight_batch.hook_context,
        ledger=ledger,
        events=events,
        chunks=chunks,
    )
    if _serialized_upload_batch_bytes(batch) > MAX_TRANSPORT_BATCH_BYTES:
        raise BootstrapError("persisted native trace batch exceeds the request transport limit")
    return batch


def _restore_in_flight_source_event(event: InFlightSourceEvent) -> SourceEvent:
    if event.kind == "oversized_record":
        return SourceEvent(
            kind=event.kind,
            path=event.path,
            path_hash=event.path_hash,
            start_offset=event.start_offset,
            end_offset=event.end_offset,
            byte_count=event.byte_count,
            content_sha256=event.content_sha256,
            oversized_reason=event.oversized_reason,
        )
    try:
        with event.path.open("rb") as handle:
            handle.seek(event.start_offset)
            content = handle.read(event.byte_count)
    except OSError as exc:
        raise BootstrapError("persisted native trace range is no longer readable") from exc
    if len(content) != event.byte_count or hashlib.sha256(content).hexdigest() != event.content_sha256:
        raise BootstrapError("persisted native trace range content changed before acknowledgement")
    return SourceEvent(
        kind=event.kind,
        path=event.path,
        path_hash=event.path_hash,
        start_offset=event.start_offset,
        end_offset=event.end_offset,
        byte_count=event.byte_count,
        content_sha256=event.content_sha256,
        content=content,
    )


def _clear_in_flight_batch(ledger: SourceLedger, host: Host, batch: InFlightUploadBatch) -> None:
    batches = ledger.in_flight_batches.get(host)
    if batches is None:
        raise BootstrapError("native trace acknowledgement has no persisted in-flight batch")
    try:
        batches.remove(batch)
    except ValueError as exc:
        raise BootstrapError("native trace acknowledgement does not match a persisted in-flight batch") from exc
    if not batches:
        ledger.in_flight_batches.pop(host)


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
                reset_source.pop(_INSTRUCTION_HUB_RELEASE_MARKERS_KEY, None)
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
    ledger: SourceLedger,
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
    snapshots = _analysis_context_snapshots(ledger, events)
    if snapshots:
        request["snapshots"] = snapshots
    request.update(_native_request_context(hook_context, lifecycle_event))
    return UploadBatch(request=request, events=events)


def _serialized_upload_batch_bytes(batch: UploadBatch) -> int:
    """Return the exact JSON body size used by the worker client."""

    return len(json.dumps(batch.request, sort_keys=True).encode())


def _analysis_context_snapshots(
    ledger: SourceLedger,
    events: tuple[SourceEvent, ...],
) -> list[dict[str, JsonValue]]:
    """Intersect JSONL chunks with durable markers without changing upload ranges."""

    ranges = _analysis_context_snapshot_ranges(ledger, events)
    if len(ranges) > MAX_ANALYSIS_CONTEXT_SNAPSHOTS_PER_BATCH:
        raise BootstrapError(
            "native trace batch intersects more than "
            f"{MAX_ANALYSIS_CONTEXT_SNAPSHOTS_PER_BATCH} instruction-hub release ranges"
        )
    return [_snapshot_range_payload(value) for value in ranges]


def _analysis_context_snapshot_ranges(
    ledger: SourceLedger,
    events: tuple[SourceEvent, ...],
) -> list[_InstructionHubReleaseSnapshotRange]:
    ranges: list[_InstructionHubReleaseSnapshotRange] = []
    for event in events:
        if event.kind != "jsonl_range":
            continue
        source = ledger.sources.get(event.path_hash)
        if source is None or _string_value(source.get("path")) != str(event.path):
            continue
        markers = _instruction_hub_release_markers(source)
        for index, marker in enumerate(markers):
            marker_end = markers[index + 1].start_offset if index + 1 < len(markers) else event.end_offset
            start_offset = max(event.start_offset, marker.start_offset)
            end_offset = min(event.end_offset, marker_end)
            if end_offset <= start_offset:
                continue
            candidate = _InstructionHubReleaseSnapshotRange(
                source_path_hash=event.path_hash,
                session_id=marker.session_id,
                start_offset=start_offset,
                end_offset=end_offset,
                captured_at=marker.captured_at,
                release=marker.release,
            )
            if ranges and _snapshot_ranges_are_coalescible(ranges[-1], candidate):
                previous = ranges[-1]
                ranges[-1] = _InstructionHubReleaseSnapshotRange(
                    source_path_hash=previous.source_path_hash,
                    session_id=previous.session_id,
                    start_offset=previous.start_offset,
                    end_offset=candidate.end_offset,
                    captured_at=previous.captured_at,
                    release=previous.release,
                )
            else:
                ranges.append(candidate)
    return ranges


def _snapshot_ranges_are_coalescible(
    first: _InstructionHubReleaseSnapshotRange,
    second: _InstructionHubReleaseSnapshotRange,
) -> bool:
    return (
        first.source_path_hash == second.source_path_hash
        and first.session_id == second.session_id
        and first.release == second.release
        and first.captured_at == second.captured_at
        and first.end_offset == second.start_offset
    )


def _snapshot_range_payload(value: _InstructionHubReleaseSnapshotRange) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "source_path_hash": value.source_path_hash,
        "session_id": value.session_id,
        "start_offset": value.start_offset,
        "end_offset": value.end_offset,
        "captured_at": value.captured_at,
        "installed_instruction_hub_release": {
            "plugin_id": value.release.plugin_id,
            "plugin_name": value.release.plugin_name,
            "plugin_version": value.release.plugin_version,
            "release_id": value.release.release_id,
        },
    }


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
