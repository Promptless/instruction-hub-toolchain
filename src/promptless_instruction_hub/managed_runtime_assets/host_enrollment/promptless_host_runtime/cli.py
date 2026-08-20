"""Command-line parsing and host-runtime orchestration."""

from __future__ import annotations

import argparse
import errno
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import urlencode

from .contracts import (
    BootstrapAuthError,
    BootstrapError,
    HookTraceContext,
    Host,
    MANAGED_RUNTIME_ID,
    RUNTIME_CHANNEL,
    RUNTIME_EXECUTABLE,
    RUNTIME_VERSION,
    RuntimeMetadata,
    _enrollment_host,
)
from .enrollment import (
    _credential_with_policy_identity,
    _enroll_host_credential,
    _enrollment_context,
    _forget_cached_host_credential,
    _obtain_host_credential,
    _store_internal_promptless_identity,
)
from .host_config import _blocked_result, _ensure_host_config, _has_native_trace_sources
from .metadata import (
    _dashboard_base_url,
    _load_runtime_metadata,
    _plugin_root,
    _resolve_host,
    _self_sha256,
    _worker_base_url,
)
from .notices import (
    _claim_deferred_first_enrollment_success_notice,
    _claim_first_enrollment_success_notice,
    _claim_internal_promptless_welcome,
    _defer_first_enrollment_success_notice,
    _pending_plugin_update,
    _record_plugin_version_seen,
)
from .output import (
    _emit,
    _emit_command_json,
    _flush_control_output,
    _record_collector_failure,
    _record_session_start_failure,
)
from .redaction import _redact_text
from .status import _reset_host_state, _status_payload
from .traces import (
    _hook_trace_context,
    _lifecycle_event,
    _read_hook_context,
    _read_hook_input,
    _run_collect,
)
from .validation import _requires_newer_bootstrap
from .worker import _get_json, _post_check_in, _validate_signed_policy, _worker_url

_WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
_WINDOWS_DETACHED_PROCESS = 0x00000008


def main(argv: list[str] | None = None) -> int:
    """Run the requested host-runtime command."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        return _run_version_command(json_output=args.json)
    host = _resolve_host(args.host)
    if args.command == "session-start":
        return _run_session_start_command(
            host,
            if_sources=args.if_sources,
            detach=args.detach,
            supervised=args.supervised,
        )
    if args.command == "ensure":
        return _run_ensure_command(
            host,
            quiet=args.quiet,
            if_sources=args.if_sources,
        )
    if args.command == "collect":
        collector_args = _collector_command_args(
            host,
            lifecycle=args.lifecycle,
            include_active=args.include_active,
            if_sources=args.if_sources,
            quiet=args.quiet,
        )
        if args.detach:
            return _launch_detached_collect(
                host,
                collector_args,
                if_sources=args.if_sources,
                quiet=args.quiet,
            )
        if args.supervised:
            return _supervise_collect(host, collector_args)
        return _run_collect_command(
            host,
            lifecycle=args.lifecycle,
            include_active=args.include_active,
            if_sources=args.if_sources,
            quiet=args.quiet,
        )
    if args.command == "status":
        return _run_status_command(host)
    if args.command == "enroll":
        return _run_enroll_command(host)
    if args.command == "reset":
        return _run_reset_command(host)
    parser.error(f"unknown command: {args.command}")
    return 2


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=RUNTIME_EXECUTABLE, description="Promptless host runtime")
    subcommands = parser.add_subparsers(dest="command", required=True)

    ensure_parser = subcommands.add_parser("ensure", help="Enroll if needed and ensure host telemetry config")
    _add_host_argument(ensure_parser)
    ensure_parser.add_argument("--quiet", action="store_true", help="Suppress hook status output")
    ensure_parser.add_argument(
        "--if-sources",
        action="store_true",
        help="Skip enrollment/config when the host has no native trace source files",
    )
    session_start_parser = subcommands.add_parser(
        "session-start",
        help="Reconcile the host and collect traces in a detached process",
    )
    _add_host_argument(session_start_parser)
    session_start_parser.add_argument(
        "--if-sources",
        action="store_true",
        help="Skip startup reconciliation when the host has no native trace source files",
    )
    session_start_execution = session_start_parser.add_mutually_exclusive_group(required=True)
    session_start_execution.add_argument("--detach", action="store_true", help="Detach startup reconciliation")
    session_start_execution.add_argument("--supervised", action="store_true", help=argparse.SUPPRESS)

    collect_parser = subcommands.add_parser(
        "collect",
        help="Upload native trace JSONL changes",
        allow_abbrev=False,
    )
    _add_host_argument(collect_parser)
    collect_parser.add_argument(
        "--lifecycle",
        choices=("session_start", "stop", "session_end", "subagent_stop"),
        default=None,
        help="Host lifecycle event that triggered collection",
    )
    collect_parser.add_argument(
        "--include-active",
        action="store_true",
        help="Include session files still inside the idle grace period",
    )
    collect_parser.add_argument(
        "--if-sources",
        action="store_true",
        help="Skip collection when the host has no native trace source files",
    )
    collect_parser.add_argument("--quiet", action="store_true", help="Suppress hook status output")
    collect_execution = collect_parser.add_mutually_exclusive_group()
    collect_execution.add_argument("--detach", action="store_true", help="Run collection in a detached process")
    collect_execution.add_argument("--supervised", action="store_true", help=argparse.SUPPRESS)

    status_parser = subcommands.add_parser("status", help="Print local host-runtime status as JSON")
    _add_host_argument(status_parser)

    enroll_parser = subcommands.add_parser("enroll", help="Enroll the host credential without editing host config")
    _add_host_argument(enroll_parser)

    reset_parser = subcommands.add_parser("reset", help="Clear cached host credentials and pending enrollment state")
    _add_host_argument(reset_parser)
    reset_parser.add_argument("--yes", action="store_true", required=True, help="Confirm local state reset")

    version_parser = subcommands.add_parser("version", help="Print host-runtime version")
    version_parser.add_argument("--json", action="store_true", help="Print version metadata as JSON")
    return parser


def _add_host_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", choices=("auto", "codex", "claude", "claude-desktop"), default="auto")


def _run_session_start_command(host: Host, *, if_sources: bool, detach: bool, supervised: bool) -> int:
    try:
        if detach:
            return _launch_detached_session_start(host, if_sources=if_sources)
        if supervised:
            return _supervise_session_start(host, if_sources=if_sources)
        raise ValueError("session-start requires --detach or --supervised")
    except (BootstrapError, OSError, ValueError, urllib.error.URLError) as exc:
        _emit({"status": "error", "host": host, "message": _redact_text(str(exc))})
        return 1
    finally:
        _flush_control_output()


def _run_ensure_command(
    host: Host,
    *,
    quiet: bool,
    if_sources: bool,
) -> int:
    try:
        return _run_ensure(
            host,
            quiet=quiet,
            if_sources=if_sources,
            claim_notices=True,
        )
    except (BootstrapError, OSError, ValueError, urllib.error.URLError) as exc:
        _emit(
            {"status": "error", "host": host, "message": _redact_text(str(exc))},
            quiet=quiet,
            internal_welcome_notice=_claim_internal_promptless_welcome(quiet=quiet),
        )
        return 0
    finally:
        _flush_control_output()


def _run_collect_command(
    host: Host,
    *,
    lifecycle: str | None,
    include_active: bool,
    if_sources: bool,
    quiet: bool,
) -> int:
    try:
        if if_sources and not _has_native_trace_sources(host):
            _emit({"status": "trace_upload_skipped", "reason": "no_sources", "host": host}, quiet=quiet)
            return 0
        event = _lifecycle_event(lifecycle)
        hook_context = _hook_trace_context(_read_hook_context())
        return _run_collect(
            host,
            lifecycle_event=event,
            hook_context=hook_context,
            include_active=include_active,
            quiet=quiet,
        )
    except (BootstrapError, OSError, ValueError, urllib.error.URLError) as exc:
        _emit({"status": "error", "host": host, "message": _redact_text(str(exc))}, quiet=quiet)
        return 0
    finally:
        _flush_control_output()


def _collector_command_args(
    host: Host,
    *,
    lifecycle: str | None,
    include_active: bool,
    if_sources: bool,
    quiet: bool,
) -> list[str]:
    command_args = ["collect", "--host", host]
    if lifecycle is not None:
        command_args.extend(("--lifecycle", lifecycle))
    if include_active:
        command_args.append("--include-active")
    if if_sources:
        command_args.append("--if-sources")
    if quiet:
        command_args.append("--quiet")
    return command_args


def _launch_detached_session_start(host: Host, *, if_sources: bool) -> int:
    hook_input = _read_hook_input()
    plugin_root = _plugin_root()
    metadata = _load_runtime_metadata(plugin_root, host)

    supervisor_args = [
        sys.executable,
        str(Path(sys.argv[0]).resolve()),
        "session-start",
        "--host",
        host,
    ]
    if if_sources:
        supervisor_args.append("--if-sources")
    supervisor_args.append("--supervised")
    with tempfile.TemporaryFile(mode="w+b") as preserved_stdin:
        binary_stdin = cast(BinaryIO, preserved_stdin)
        binary_stdin.write(hook_input)
        binary_stdin.seek(0)
        _spawn_detached(supervisor_args, stdin=binary_stdin)
    enrollment_target = _enrollment_host(host)
    enrollment_metadata = (
        metadata if enrollment_target == host else _load_runtime_metadata(plugin_root, enrollment_target)
    )
    _emit_pending_session_start_notices(enrollment_target, enrollment_metadata)
    return 0


def _emit_pending_session_start_notices(host: Host, metadata: RuntimeMetadata) -> None:
    pending_update = _pending_plugin_update(metadata)
    update_notice = pending_update.notice if pending_update is not None else None
    first_success = _claim_deferred_first_enrollment_success_notice(host)
    internal_welcome = _claim_internal_promptless_welcome(
        quiet=False,
        plugin_version=metadata.plugin_version,
        version_updated=update_notice is not None,
    )
    if update_notice is not None or first_success is not None or internal_welcome is not None:
        status = first_success.status if first_success is not None else "notice"
        _emit(
            {"status": status, "host": host, "needs_restart": status == "needs_restart"},
            update_notice=update_notice,
            first_success_notice=first_success.notice if first_success is not None else None,
            internal_welcome_notice=internal_welcome,
        )
    if pending_update is not None:
        _record_plugin_version_seen(pending_update)


def _supervise_session_start(host: Host, *, if_sources: bool) -> int:
    try:
        # The detached launcher discards these streams. Keeping ensure non-quiet
        # still persists its result as the host's latest support status.
        ensure_result = _run_ensure(
            host,
            quiet=False,
            if_sources=if_sources,
            claim_notices=False,
        )
    except (BootstrapError, OSError, ValueError, urllib.error.URLError) as exc:
        _record_session_start_failure(
            host,
            stage="ensure",
            exit_code=None,
            error_code=_exception_error_code(exc),
        )
        return 1
    if ensure_result != 0:
        _record_session_start_failure(
            host,
            stage="ensure",
            exit_code=ensure_result,
            error_code=None,
        )
        return ensure_result

    try:
        hook_context = _hook_trace_context(_read_hook_context())
    except (BootstrapError, OSError, ValueError) as exc:
        _record_collector_failure(host, exit_code=None, error_code=_exception_error_code(exc))
        return 1

    collect_result = _run_session_start_collect(
        host,
        hook_context=hook_context,
        if_sources=if_sources,
    )
    if host != "claude":
        return collect_result
    desktop_result = _run_session_start_collect(
        "claude-desktop",
        hook_context=_hook_trace_context({}),
        if_sources=True,
    )
    return collect_result or desktop_result


def _run_session_start_collect(
    host: Host,
    *,
    hook_context: HookTraceContext,
    if_sources: bool,
) -> int:
    try:
        if if_sources and not _has_native_trace_sources(host):
            return 0
        result = _run_collect(
            host,
            lifecycle_event="session_start",
            hook_context=hook_context,
            include_active=True,
            quiet=True,
        )
    except (BootstrapError, OSError, ValueError, urllib.error.URLError) as exc:
        _record_collector_failure(host, exit_code=None, error_code=_exception_error_code(exc))
        return 1
    if result != 0:
        _record_collector_failure(host, exit_code=result, error_code=None)
    return result


def _launch_detached_collect(
    host: Host,
    collector_args: list[str],
    *,
    if_sources: bool = False,
    quiet: bool = False,
) -> int:
    try:
        hook_input = _read_hook_input()
    except (BootstrapError, OSError, ValueError) as exc:
        _emit({"status": "error", "host": host, "message": _redact_text(str(exc))}, quiet=quiet)
        return 1
    if if_sources and not _has_native_trace_sources(host):
        _emit({"status": "trace_upload_skipped", "reason": "no_sources", "host": host}, quiet=quiet)
        return 0
    supervisor_args = [
        sys.executable,
        str(Path(sys.argv[0]).resolve()),
        *collector_args,
    ]
    supervisor_args.append("--supervised")
    try:
        with tempfile.TemporaryFile(mode="w+b") as preserved_stdin:
            binary_stdin = cast(BinaryIO, preserved_stdin)
            binary_stdin.write(hook_input)
            binary_stdin.seek(0)
            _spawn_detached(supervisor_args, stdin=binary_stdin)
    except OSError as exc:
        _record_collector_failure(host, exit_code=None, error_code=_os_error_code(exc))
        return 1
    return 0


def _supervise_collect(host: Host, collector_args: list[str]) -> int:
    process_args = [
        sys.executable,
        str(Path(sys.argv[0]).resolve()),
        *collector_args,
    ]
    try:
        result = subprocess.run(
            process_args,
            stdin=sys.stdin.buffer,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        _record_collector_failure(host, exit_code=None, error_code=_os_error_code(exc))
        return 1
    if result.returncode != 0:
        _record_collector_failure(host, exit_code=result.returncode, error_code=None)
    return result.returncode


def _spawn_detached(args: list[str], *, stdin: BinaryIO) -> None:
    if os.name == "nt":
        subprocess.Popen(
            args,
            stdin=stdin,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_WINDOWS_CREATE_NEW_PROCESS_GROUP | _WINDOWS_DETACHED_PROCESS,
        )
        return
    subprocess.Popen(
        args,
        stdin=stdin,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _os_error_code(exc: OSError) -> str:
    if exc.errno is None:
        return type(exc).__name__
    return errno.errorcode.get(exc.errno, str(exc.errno))


def _exception_error_code(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return _os_error_code(exc)
    return type(exc).__name__


def _run_status_command(host: Host) -> int:
    try:
        payload = _status_payload(host)
    except (BootstrapError, OSError, ValueError) as exc:
        _emit_command_json({"status": "error", "host": host, "message": _redact_text(str(exc))})
        return 1
    _emit_command_json(payload)
    return 0


def _run_enroll_command(host: Host) -> int:
    try:
        plugin_root = _plugin_root()
        enrollment_target = _enrollment_host(host)
        metadata = _load_runtime_metadata(plugin_root, enrollment_target)
        worker_base_url = _worker_base_url()
        dashboard_base_url = _dashboard_base_url()
        context = _enrollment_context(worker_base_url, dashboard_base_url, metadata)
        enrollment_attempt = _obtain_host_credential(context, quiet=True)
        if enrollment_attempt.credential is None:
            _emit_command_json(
                {
                    "status": "setup_pending",
                    "reason": enrollment_attempt.reason or "approval_pending",
                    "host": enrollment_target,
                }
            )
            return 0
        credential = enrollment_attempt.credential
        _emit_command_json(
            {
                "status": "enrolled",
                "host": enrollment_target,
                "credential_id": credential.credential_id,
                "deployment_instance_id": credential.deployment_instance_id or context.deployment_instance_id,
                "host_instance_id": context.host_instance_id,
            }
        )
        return 0
    except (BootstrapError, OSError, ValueError, urllib.error.URLError) as exc:
        _emit_command_json({"status": "error", "host": host, "message": _redact_text(str(exc))})
        return 1


def _run_reset_command(host: Host) -> int:
    try:
        credentials_removed, pending_removed = _reset_host_state(host)
    except (BootstrapError, OSError, ValueError) as exc:
        _emit_command_json({"status": "error", "host": host, "message": _redact_text(str(exc))})
        return 1
    _emit_command_json(
        {
            "status": "reset",
            "host": host,
            "credentials_removed": credentials_removed,
            "pending_enrollments_removed": pending_removed,
        }
    )
    return 0


def _run_version_command(*, json_output: bool) -> int:
    payload = {
        "id": MANAGED_RUNTIME_ID,
        "name": RUNTIME_EXECUTABLE,
        "version": RUNTIME_VERSION,
        "channel": RUNTIME_CHANNEL,
        "sha256": _self_sha256(),
    }
    if json_output:
        _emit_command_json(payload)
    else:
        sys.stdout.write(f"{RUNTIME_EXECUTABLE} {RUNTIME_VERSION}\n")
        sys.stdout.flush()
    return 0


def _run_ensure(
    host: Host,
    *,
    quiet: bool,
    if_sources: bool,
    claim_notices: bool,
) -> int:
    if if_sources and not _has_native_trace_sources(host):
        _emit({"status": "trace_upload_skipped", "reason": "no_sources", "host": host}, quiet=quiet)
        return 0
    # Compute any plugin-update notice before enrollment so every install learns when the
    # Instruction Hub plugin version changed. Both Claude and Codex render the SessionStart
    # `systemMessage`. The new version is recorded only after the hook output is emitted below,
    # so a later failure re-announces it on the next healthy session.
    plugin_root = _plugin_root()
    enrollment_target = _enrollment_host(host)
    metadata = _load_runtime_metadata(plugin_root, enrollment_target)
    pending_update = _pending_plugin_update(metadata)
    update_notice = pending_update.notice if pending_update is not None and claim_notices else None
    exit_code = _run_host_enrollment(
        enrollment_target,
        metadata,
        quiet=quiet,
        update_notice=update_notice,
        plugin_version_updated=update_notice is not None,
        claim_notices=claim_notices,
    )
    # Reached only when the host enrollment step returned without raising. Quiet detached
    # runs do not consume notices that no user could have seen.
    if pending_update is not None and not quiet and (claim_notices or pending_update.notice is None):
        _record_plugin_version_seen(pending_update)
    return exit_code


def _run_host_enrollment(
    host: Host,
    metadata: RuntimeMetadata,
    *,
    quiet: bool,
    update_notice: str | None,
    plugin_version_updated: bool,
    claim_notices: bool,
) -> int:
    notice_quiet = quiet or not claim_notices
    worker_base_url = _worker_base_url()
    dashboard_base_url = _dashboard_base_url()
    context = _enrollment_context(worker_base_url, dashboard_base_url, metadata)
    enrollment_attempt = _obtain_host_credential(context, quiet=quiet)
    if enrollment_attempt.credential is None:
        _emit(
            {
                "status": "setup_pending",
                "reason": enrollment_attempt.reason or "approval_pending",
                "host": host,
            },
            quiet=quiet,
            update_notice=update_notice,
            internal_welcome_notice=_claim_internal_promptless_welcome(
                quiet=notice_quiet,
                plugin_version=metadata.plugin_version,
                version_updated=plugin_version_updated,
            ),
        )
        return 0
    credential = enrollment_attempt.credential

    policy_url = _worker_url(worker_base_url, f"/v0/host-enrollment/policy?{urlencode({'target': host})}")
    check_in_url = _worker_url(worker_base_url, "/v0/host-enrollment/check-ins")
    try:
        signed_policy = _get_json(policy_url, credential.value, label="policy response")
    except BootstrapAuthError:
        _forget_cached_host_credential(context)
        enrollment_attempt = _enroll_host_credential(context, quiet=quiet)
        if enrollment_attempt.credential is None:
            _emit(
                {
                    "status": "setup_pending",
                    "reason": enrollment_attempt.reason or "approval_pending",
                    "host": host,
                },
                quiet=quiet,
                update_notice=update_notice,
                internal_welcome_notice=_claim_internal_promptless_welcome(
                    quiet=notice_quiet,
                    plugin_version=metadata.plugin_version,
                    version_updated=plugin_version_updated,
                ),
            )
            return 0
        credential = enrollment_attempt.credential
        signed_policy = _get_json(policy_url, credential.value, label="policy response")
    policy = _validate_signed_policy(signed_policy, host)
    credential = _credential_with_policy_identity(credential, signed_policy)
    _store_internal_promptless_identity(context, credential)
    trace_upload_endpoint = _worker_url(worker_base_url, "/v0/traces/batches")
    if _requires_newer_bootstrap(policy.required_bootstrap_version, RUNTIME_VERSION):
        result = _blocked_result(
            host,
            kind="bootstrap_upgrade_required",
            message="Worker policy requires a newer Promptless host runtime",
            details={
                "required_bootstrap_version": policy.required_bootstrap_version,
                "bootstrap_version": RUNTIME_VERSION,
            },
            trace_upload_endpoint=trace_upload_endpoint,
        )
        _post_check_in(check_in_url, credential, host, metadata, policy, result)
        _emit(
            {"status": "blocked", "reason": "bootstrap_upgrade_required", "host": host},
            quiet=quiet,
            update_notice=update_notice,
            internal_welcome_notice=_claim_internal_promptless_welcome(
                credential=credential,
                quiet=notice_quiet,
                plugin_version=metadata.plugin_version,
                version_updated=plugin_version_updated,
            ),
        )
        return 0

    result = _ensure_host_config(host, trace_upload_endpoint=trace_upload_endpoint)
    _post_check_in(check_in_url, credential, host, metadata, policy, result)
    first_success_notice = None
    if claim_notices:
        first_success_notice = _claim_first_enrollment_success_notice(
            host,
            status=result.status,
            quiet=quiet,
        )
    else:
        _defer_first_enrollment_success_notice(host, status=result.status)
    _emit(
        {"status": result.status, "host": host, "needs_restart": result.needs_restart},
        quiet=quiet,
        update_notice=update_notice,
        first_success_notice=first_success_notice,
        internal_welcome_notice=_claim_internal_promptless_welcome(
            credential=credential,
            quiet=notice_quiet,
            plugin_version=metadata.plugin_version,
            version_updated=plugin_version_updated,
        ),
    )
    return 0
