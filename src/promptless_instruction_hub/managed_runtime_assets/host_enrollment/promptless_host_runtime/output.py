"""Hook control output, local diagnostics, and user-facing messages."""

from __future__ import annotations

import datetime as dt
import json
import sys
from dataclasses import dataclass

from .contracts import Host, JsonValue, MAX_DIAGNOSTIC_LOG_BYTES
from .redaction import _redact_json
from .storage import _atomic_write_text, _diagnostic_log_path, _last_status_path, _state_file_lock
from .validation import _string_value


_PENDING_CONTROL_OUTPUT: dict[str, JsonValue] | None = None


_PENDING_CONTROL_OUTPUT_PRIORITY = -1


_CONTROL_OUTPUT_STATUS_PRIORITY = {
    "browser_enrollment_starting": 10,
    "configured": 20,
    "needs_restart": 80,
    "setup_pending": 90,
    "blocked": 100,
    "error": 110,
}


def _emit_command_json(payload: dict[str, JsonValue]) -> None:
    sys.stdout.write(json.dumps(_redact_json(payload), sort_keys=True) + "\n")
    sys.stdout.flush()


def _emit(
    payload: dict[str, JsonValue],
    *,
    quiet: bool = False,
    update_notice: str | None = None,
    first_success_notice: str | None = None,
    internal_welcome_notice: str | None = None,
) -> None:
    # SessionStart hook output contract. Codex validates the hook's *stdout* against a strict schema
    # (serde deny_unknown_fields): only continue/stopReason/systemMessage/suppressOutput/
    # hookSpecificOutput are accepted, and any extra key makes Codex reject the whole object with
    # "hook returned invalid session start JSON output". Claude Code ignores unrecognized fields, but
    # Codex does not. So Codex stdout carries only the user-facing systemMessage — and nothing at
    # all when there is no message, which Codex treats as success. Claude stdout may also carry
    # terminalSequence for a terminal-level notification. The full diagnostic status object
    # (status/host/needs_restart/reason) still goes to stderr, which neither host parses as hook
    # control output.
    # https://developers.openai.com/codex/hooks  https://code.claude.com/docs/en/hooks
    message = _system_message(
        update_notice, internal_welcome_notice, first_success_notice, _enrollment_user_message(payload)
    )
    terminal_sequence = _terminal_sequence(payload, message)
    diagnostic = dict(payload)
    if message:
        diagnostic["systemMessage"] = message
    if terminal_sequence is not None:
        diagnostic["terminalSequence"] = terminal_sequence
    _write_diagnostic_log(diagnostic)
    if quiet:
        return
    _write_last_status(diagnostic)
    sys.stderr.write(json.dumps(_redact_json(diagnostic), sort_keys=True) + "\n")
    sys.stderr.flush()
    if message or terminal_sequence is not None:
        control: dict[str, JsonValue] = {}
        if message:
            control["systemMessage"] = message
        if terminal_sequence is not None:
            control["terminalSequence"] = terminal_sequence
        # Keep stdout to one hook-control object, but let terminal outcomes replace the
        # preliminary browser-start banner before the process exits.
        _queue_control_output(payload, control)


def _system_message(*parts: str | None) -> str:
    messages = [part for part in parts if part is not None]
    if not messages:
        return ""
    separator = "\n\n" if any("\n" in message for message in messages) else " "
    return separator.join(messages)


def _queue_control_output(payload: dict[str, JsonValue], control: dict[str, JsonValue]) -> None:
    global _PENDING_CONTROL_OUTPUT, _PENDING_CONTROL_OUTPUT_PRIORITY

    if _PENDING_CONTROL_OUTPUT == control:
        return
    priority = _control_output_priority(payload)
    if _PENDING_CONTROL_OUTPUT is None or priority >= _PENDING_CONTROL_OUTPUT_PRIORITY:
        _PENDING_CONTROL_OUTPUT = dict(control)
        _PENDING_CONTROL_OUTPUT_PRIORITY = priority


def _flush_control_output() -> None:
    global _PENDING_CONTROL_OUTPUT, _PENDING_CONTROL_OUTPUT_PRIORITY

    if _PENDING_CONTROL_OUTPUT is None:
        return
    sys.stdout.write(json.dumps(_redact_json(_PENDING_CONTROL_OUTPUT), sort_keys=True) + "\n")
    sys.stdout.flush()
    _PENDING_CONTROL_OUTPUT = None
    _PENDING_CONTROL_OUTPUT_PRIORITY = -1


def _control_output_priority(payload: dict[str, JsonValue]) -> int:
    status = _string_value(payload.get("status"))
    if status is None:
        return 0
    return _CONTROL_OUTPUT_STATUS_PRIORITY.get(status, 0)


def _write_last_status(diagnostic: dict[str, JsonValue]) -> None:
    payload = dict(diagnostic)
    payload["emitted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        path = _last_status_path()
        _atomic_write_text(path, json.dumps(_redact_json(payload), indent=2, sort_keys=True) + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        return


def _write_diagnostic_log(diagnostic: dict[str, JsonValue]) -> None:
    payload = dict(diagnostic)
    payload["emitted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        path = _diagnostic_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_redact_json(payload), sort_keys=True) + "\n"
        with _state_file_lock(path):
            if path.exists() and path.stat().st_size + len(line.encode("utf-8")) > MAX_DIAGNOSTIC_LOG_BYTES:
                rotated_path = path.with_name(f"{path.name}.1")
                try:
                    rotated_path.unlink()
                except FileNotFoundError:
                    pass
                path.replace(rotated_path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            try:
                path.chmod(0o600)
            except OSError:
                pass
    except OSError:
        return


def _record_collector_failure(host: Host, *, exit_code: int | None, error_code: str | None) -> None:
    diagnostic: dict[str, JsonValue] = {
        "status": "error",
        "reason": "collector_process_failed",
        "host": host,
    }
    if exit_code is not None:
        diagnostic["exit_code"] = exit_code
    if error_code is not None:
        diagnostic["error_code"] = error_code
    _write_diagnostic_log(diagnostic)
    _write_last_status(diagnostic)


@dataclass(frozen=True)
class HostDisplay:
    """Human-facing host name and user config path used in SessionStart messages."""

    name: str
    config_path: str


HOST_DISPLAY_NAMES: dict[str, HostDisplay] = {
    "claude": HostDisplay(name="Claude Code", config_path="~/.claude/settings.json"),
    "claude-desktop": HostDisplay(name="Claude Desktop", config_path="Claude Desktop local audit store"),
    "codex": HostDisplay(name="Codex", config_path="~/.codex/config.toml"),
}


def _enrollment_user_message(payload: dict[str, JsonValue]) -> str | None:
    """Return a user-facing enrollment-status message for the host, or None to stay silent.

    Both Claude and Codex surface a message, and only for outcomes worth showing on a hook that
    runs every startup/resume: legacy-config removal, a pending browser approval, a block, or a
    failure that needs user action. The steady "already configured" state stays silent here; the
    one-time first-success confirmation is emitted separately (see _claim_first_enrollment_success_notice).
    """

    status = payload.get("status")
    host_label = HOST_DISPLAY_NAMES.get(_string_value(payload.get("host")) or "")
    if status == "error":
        host_name = host_label.name if host_label is not None else "the agent host"
        details = _string_value(payload.get("message"))
        if details:
            return f"Promptless host enrollment failed for {host_name}: {details}"
        return f"Promptless host enrollment failed for {host_name}. Check the hook diagnostic output."
    if host_label is None:
        return None
    if status == "browser_enrollment_starting":
        return (
            "Promptless Instruction Governance telemetry is starting browser-based enrollment. "
            "Approve the Promptless browser tab to continue."
        )
    if status == "needs_restart":
        return (
            f"Promptless removed legacy managed telemetry config from {host_label.name}. "
            f"Restart {host_label.name} to apply the cleanup."
        )
    if status == "setup_pending":
        reason = payload.get("reason")
        if reason == "browser_launch_failed":
            return (
                f"Promptless host enrollment could not open a browser for {host_label.name}. "
                "Restart from a desktop session with browser access, then approve the Promptless enrollment prompt. "
                "Details were saved to ~/.promptless/instruction-hub/last-bootstrap-status.json."
            )
        if reason == "browser_approval_timeout":
            return (
                f"Promptless host enrollment did not receive browser approval before timing out. "
                f"Restart {host_label.name} and approve the Promptless enrollment prompt."
            )
        if reason == "enrollment_in_progress":
            return (
                "Promptless host enrollment is already running in another installed Promptless plugin. "
                f"Finish that approval, then restart {host_label.name}."
            )
        if reason == "approval_expired":
            return (
                f"Promptless host enrollment approval expired. Restart {host_label.name} to request a fresh approval."
            )
        return (
            "Promptless host enrollment is awaiting browser approval. "
            f"Approve in the browser tab that opened, then restart {host_label.name}."
        )
    if status == "blocked":
        if payload.get("reason") == "bootstrap_upgrade_required":
            return "Promptless host enrollment is blocked: a newer Promptless host runtime is required."
        return (
            "Promptless host enrollment is blocked: existing telemetry settings were left unchanged. "
            f"Review {host_label.config_path}."
        )
    return None


def _terminal_sequence(payload: dict[str, JsonValue], message: str) -> str | None:
    if not message or payload.get("host") != "claude":
        return None
    status = payload.get("status")
    if status not in {"blocked", "browser_enrollment_starting", "error", "needs_restart", "setup_pending"}:
        return None
    body = _terminal_notification_body(payload)
    return f"\033]777;notify;Promptless;{body}\007"


def _terminal_notification_body(payload: dict[str, JsonValue]) -> str:
    status = payload.get("status")
    if status == "error":
        return "Host enrollment failed"
    if status == "blocked":
        return "Host enrollment needs review"
    if status == "needs_restart":
        return "Telemetry configured; restart Claude Code"
    return "Host enrollment needs approval"
