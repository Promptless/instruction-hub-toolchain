"""Local runtime status and host-state reset operations."""

from __future__ import annotations

from .contracts import (
    FIRST_ENROLLMENT_SUCCESS_SHOWN_KEY,
    Host,
    JsonValue,
    MANAGED_RUNTIME_ID,
    RUNTIME_CHANNEL,
    RUNTIME_EXECUTABLE,
    RUNTIME_VERSION,
    _enrollment_host,
)
from .host_config import _host_config_status
from .metadata import _self_sha256
from .storage import _load_state, _state_file_lock, _state_path, _write_state
from .validation import _json_mapping_or_empty, _string_value


def _status_payload(host: Host) -> dict[str, JsonValue]:
    state_path = _state_path()
    state = _load_state(state_path)
    enrollment_target = _enrollment_host(host)
    credentials = _credential_statuses_for_host(state, enrollment_target)
    pending_counts = _pending_enrollment_counts(state, enrollment_target)
    seen_versions = _json_mapping_or_empty(state.get("last_seen_plugin_versions"))
    return {
        "status": "ok",
        "host": host,
        "runtime": {
            "id": MANAGED_RUNTIME_ID,
            "name": RUNTIME_EXECUTABLE,
            "version": RUNTIME_VERSION,
            "channel": RUNTIME_CHANNEL,
            "sha256": _self_sha256(),
        },
        "state": {
            "path": str(state_path),
            "exists": state_path.exists(),
            "host_instance_id": _string_value(state.get("host_instance_id")),
            "credential_count": len(credentials),
            "credentials": credentials,
            "pending_enrollment_count": pending_counts[0],
            "legacy_pending_enrollment_count": pending_counts[1],
            "last_seen_plugin_version": _string_value(seen_versions.get(enrollment_target)),
        },
        "config": _host_config_status(host),
    }


def _credential_statuses_for_host(state: dict[str, JsonValue], host: Host) -> list[dict[str, JsonValue]]:
    statuses: list[dict[str, JsonValue]] = []
    credentials = _json_mapping_or_empty(state.get("credentials"))
    for credential_value in credentials.values():
        credential = _json_mapping_or_empty(credential_value)
        if _string_value(credential.get("target")) != host:
            continue
        if _string_value(credential.get("value")) is None:
            continue
        statuses.append(
            {
                "credential_id": _string_value(credential.get("credential_id")),
                "deployment_instance_id": _string_value(credential.get("deployment_instance_id")),
                "worker_base_url": _string_value(credential.get("worker_base_url")),
                "updated_at": _string_value(credential.get("updated_at")),
            }
        )
    return sorted(statuses, key=lambda item: _string_value(item.get("updated_at")) or "")


def _pending_enrollment_counts(state: dict[str, JsonValue], host: Host) -> tuple[int, int]:
    pending_enrollments = _json_mapping_or_empty(state.get("pending_enrollments"))
    host_pending = 0
    legacy_pending = 0
    for pending_value in pending_enrollments.values():
        pending = _json_mapping_or_empty(pending_value)
        target = _string_value(pending.get("target"))
        if target == host:
            host_pending += 1
        elif target is None:
            legacy_pending += 1
    return host_pending, legacy_pending


def _reset_host_state(host: Host) -> tuple[int, int]:
    enrollment_target = _enrollment_host(host)
    reset_targets = {enrollment_target}
    if enrollment_target == "claude":
        reset_targets.add("claude-desktop")
    state_path = _state_path()
    with _state_file_lock(state_path):
        state = _load_state(state_path)
        credentials = _json_mapping_or_empty(state.get("credentials"))
        kept_credentials: dict[str, JsonValue] = {}
        credentials_removed = 0
        for key, credential_value in credentials.items():
            credential = _json_mapping_or_empty(credential_value)
            if _string_value(credential.get("target")) in reset_targets:
                credentials_removed += 1
                continue
            kept_credentials[key] = credential_value
        state["credentials"] = kept_credentials

        pending_enrollments = _json_mapping_or_empty(state.get("pending_enrollments"))
        kept_pending: dict[str, JsonValue] = {}
        pending_removed = 0
        for key, pending_value in pending_enrollments.items():
            pending = _json_mapping_or_empty(pending_value)
            target = _string_value(pending.get("target"))
            if target in reset_targets or target is None:
                pending_removed += 1
                continue
            kept_pending[key] = pending_value
        state["pending_enrollments"] = kept_pending

        # Re-arm the one-time first-success confirmation: a reset makes the next enrollment a
        # genuine first success for this host, so it should confirm again.
        shown_targets = _json_mapping_or_empty(state.get(FIRST_ENROLLMENT_SUCCESS_SHOWN_KEY))
        reset_latch = False
        for target in reset_targets:
            if target in shown_targets:
                del shown_targets[target]
                reset_latch = True
        if reset_latch:
            state[FIRST_ENROLLMENT_SUCCESS_SHOWN_KEY] = shown_targets

        _write_state(state_path, state)
    return credentials_removed, pending_removed
