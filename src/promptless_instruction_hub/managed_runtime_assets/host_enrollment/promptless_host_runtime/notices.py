"""Persistent one-time update, enrollment, and internal-user notices."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .contracts import (
    BootstrapError,
    ConfigStatus,
    FIRST_ENROLLMENT_SUCCESS_SHOWN_KEY,
    Host,
    HostCredential,
    INTERNAL_PROMPTLESS_WELCOME_MESSAGE_LINES,
    INTERNAL_PROMPTLESS_WELCOME_SHOWN_AT_KEY,
    INTERNAL_PROMPTLESS_WELCOME_SHOWN_VERSIONS_KEY,
    PENDING_FIRST_ENROLLMENT_SUCCESS_KEY,
    RuntimeMetadata,
)
from .output import HOST_DISPLAY_NAMES
from .storage import _load_state, _state_file_lock, _state_path, _write_state
from .validation import (
    _json_mapping_or_empty,
    _non_empty,
    _stored_credential_has_internal_promptless_identity,
    _string_value,
)


@dataclass(frozen=True)
class PendingPluginUpdate:
    """The installed plugin version to record as seen for a host, with any one-time update notice."""

    target: str
    version: str
    notice: str | None


@dataclass(frozen=True)
class FirstEnrollmentSuccessNotice:
    """A claimed deferred enrollment result and its user-facing notice."""

    status: ConfigStatus
    notice: str


def _pending_plugin_update(metadata: RuntimeMetadata) -> PendingPluginUpdate | None:
    """Return the plugin version to record plus any one-time update notice, or None when there is nothing to do.

    The installed version comes from the plugin's managed-runtime manifest; the last-seen version is
    read from the host data dir (which survives plugin updates), keyed by host so a data dir shared
    between Claude and Codex tracks each separately. This is read-only and best-effort: a missing,
    unwritable, or corrupt data dir must never turn an otherwise healthy SessionStart into an error,
    so state I/O and parse failures yield no notice. The notice is None on a first install (nothing
    seen yet); the version is still returned so the caller can record it.
    """

    current_version = metadata.plugin_version
    if current_version == "unknown":
        return None
    try:
        state_path = _state_path()
        with _state_file_lock(state_path):
            seen_versions = _json_mapping_or_empty(_load_state(state_path).get("last_seen_plugin_versions"))
        last_seen_version = _string_value(seen_versions.get(metadata.target))
    except (OSError, BootstrapError):
        return None
    if last_seen_version == current_version:
        return None
    notice = (
        None
        if last_seen_version is None
        else f"Promptless Instruction Hub updated to v{current_version} (was v{last_seen_version})."
    )
    return PendingPluginUpdate(target=metadata.target, version=current_version, notice=notice)


def _record_plugin_version_seen(pending: PendingPluginUpdate) -> None:
    """Persist the installed plugin version as seen for the host, preserving other state.

    Called only after the SessionStart output (including any update notice) has been emitted, so a
    failure before output leaves the prior version and re-announces next time. Best-effort: state I/O
    failures are swallowed so recording never breaks a session. Re-reads state under the lock so a
    credential written by the enrollment step in the same run is preserved.
    """

    try:
        state_path = _state_path()
        with _state_file_lock(state_path):
            state = _load_state(state_path)
            seen_versions = _json_mapping_or_empty(state.get("last_seen_plugin_versions"))
            seen_versions[pending.target] = pending.version
            state["last_seen_plugin_versions"] = seen_versions
            _write_state(state_path, state)
    except (OSError, BootstrapError):
        return


def _claim_internal_promptless_welcome(
    credential: HostCredential | None = None,
    *,
    quiet: bool,
    plugin_version: str | None = None,
    version_updated: bool = False,
) -> str | None:
    """Return the internal dogfood welcome once per installed marketplace version."""

    welcome_version = _non_empty(plugin_version)
    if (
        quiet
        or welcome_version is None
        or welcome_version == "unknown"
        or not _has_internal_promptless_identity(credential)
    ):
        return None
    try:
        state_path = _state_path()
        with _state_file_lock(state_path):
            state = _load_state(state_path)
            shown_versions = _json_mapping_or_empty(state.get(INTERNAL_PROMPTLESS_WELCOME_SHOWN_VERSIONS_KEY))
            if _string_value(shown_versions.get(welcome_version)) is not None:
                return None
            shown_at = dt.datetime.now(dt.timezone.utc).isoformat()
            shown_versions[welcome_version] = shown_at
            state[INTERNAL_PROMPTLESS_WELCOME_SHOWN_VERSIONS_KEY] = shown_versions
            state[INTERNAL_PROMPTLESS_WELCOME_SHOWN_AT_KEY] = shown_at
            _write_state(state_path, state)
    except (OSError, BootstrapError):
        return None
    return _internal_promptless_welcome_message(welcome_version, version_updated=version_updated)


def _internal_promptless_welcome_message(plugin_version: str, *, version_updated: bool) -> str:
    version_label = "version updated" if version_updated else "version"
    return "\n".join(
        [
            *INTERNAL_PROMPTLESS_WELCOME_MESSAGE_LINES,
            f"{version_label}: v{plugin_version}",
        ]
    )


def _claim_first_enrollment_success_notice(host: Host, *, status: ConfigStatus, quiet: bool) -> str | None:
    """Return a one-time confirmation the first time enrollment succeeds for a host.

    The SessionStart hook runs on every startup/resume and stays silent on the steady
    "already configured" state, so a fresh install never gets a "you're enrolled" signal.
    This surfaces one -- and only one -- confirmation the first time enrollment reaches a
    healthy result (configured or needs_restart) for each host, then latches so later sessions
    stay quiet. A blocked result (e.g. malformed legacy managed markers) is not a success: it
    returns None without claiming the latch, so the confirmation still fires after manual
    repair. Enrollment writes nothing the running host must reload (the credential lives in
    the host-global state file and traces upload out-of-process from the hooks), so the message
    is a plain confirmation rather than a restart prompt; the needs_restart branch adds its own
    restart instruction, so this stays generic there to avoid contradicting it. A quiet baseline
    run returns None without claiming the latch, leaving the notice for the next visible session.
    """

    if quiet:
        return None
    claimed = _claim_first_enrollment_success(host, status=status)
    return claimed.notice if claimed is not None else None


def _defer_first_enrollment_success_notice(host: Host, *, status: ConfigStatus) -> None:
    """Persist a healthy detached enrollment result for the next visible SessionStart."""

    if _first_enrollment_success_message(host, status=status) is None:
        return
    try:
        state_path = _state_path()
        with _state_file_lock(state_path):
            state = _load_state(state_path)
            shown_targets = _json_mapping_or_empty(state.get(FIRST_ENROLLMENT_SUCCESS_SHOWN_KEY))
            if _string_value(shown_targets.get(host)) is not None:
                return
            pending_targets = _json_mapping_or_empty(state.get(PENDING_FIRST_ENROLLMENT_SUCCESS_KEY))
            pending_targets[host] = status
            state[PENDING_FIRST_ENROLLMENT_SUCCESS_KEY] = pending_targets
            _write_state(state_path, state)
    except (OSError, BootstrapError):
        return


def _claim_deferred_first_enrollment_success_notice(host: Host) -> FirstEnrollmentSuccessNotice | None:
    """Claim and return the enrollment success left by a detached ensure run."""

    return _claim_first_enrollment_success(host, status=None)


def _claim_first_enrollment_success(
    host: Host,
    *,
    status: ConfigStatus | None,
) -> FirstEnrollmentSuccessNotice | None:
    try:
        state_path = _state_path()
        with _state_file_lock(state_path):
            state = _load_state(state_path)
            pending_targets = _json_mapping_or_empty(state.get(PENDING_FIRST_ENROLLMENT_SUCCESS_KEY))
            if status is None:
                status = _healthy_config_status(pending_targets.get(host))
                if status is None:
                    return None
            message = _first_enrollment_success_message(host, status=status)
            if message is None:
                return None

            shown_targets = _json_mapping_or_empty(state.get(FIRST_ENROLLMENT_SUCCESS_SHOWN_KEY))
            if _string_value(shown_targets.get(host)) is not None:
                return None
            shown_targets[host] = dt.datetime.now(dt.timezone.utc).isoformat()
            pending_targets.pop(host, None)
            state[FIRST_ENROLLMENT_SUCCESS_SHOWN_KEY] = shown_targets
            state[PENDING_FIRST_ENROLLMENT_SUCCESS_KEY] = pending_targets
            _write_state(state_path, state)
    except (OSError, BootstrapError):
        return None
    return FirstEnrollmentSuccessNotice(status=status, notice=message)


def _healthy_config_status(value: object) -> ConfigStatus | None:
    if value == "configured":
        return "configured"
    if value == "needs_restart":
        return "needs_restart"
    return None


def _first_enrollment_success_message(host: Host, *, status: ConfigStatus) -> str | None:
    if status == "blocked":
        return None
    host_label = HOST_DISPLAY_NAMES.get(host)
    if host_label is None:
        return None
    if status == "needs_restart":
        # The needs_restart status message already tells the user to restart to drop the legacy
        # config it removed, so keep this to a bare confirmation to avoid contradicting it.
        return f"Promptless Instruction Governance telemetry is now enrolled for {host_label.name}."
    return (
        f"Promptless Instruction Governance telemetry is now active for {host_label.name}. "
        "No restart or plugin reload is needed."
    )


def _has_internal_promptless_identity(credential: HostCredential | None) -> bool:
    if credential is not None and credential.is_internal_promptless_user:
        return True
    return _has_stored_internal_promptless_identity()


def _has_stored_internal_promptless_identity() -> bool:
    try:
        state_path = _state_path()
        with _state_file_lock(state_path):
            state = _load_state(state_path)
            credentials = _json_mapping_or_empty(state.get("credentials"))
            return any(
                _stored_credential_has_internal_promptless_identity(_json_mapping_or_empty(value))
                for value in credentials.values()
            )
    except (OSError, BootstrapError):
        return False
