"""Command-line parsing and host-runtime orchestration."""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode

from .contracts import (
    BootstrapAuthError,
    BootstrapError,
    Host,
    MANAGED_RUNTIME_ID,
    RUNTIME_CHANNEL,
    RUNTIME_EXECUTABLE,
    RUNTIME_VERSION,
    RuntimeMetadata,
)
from .enrollment import (
    _credential_with_policy_identity,
    _enroll_host_credential,
    _enrollment_context,
    _forget_cached_host_credential,
    _host_disabled_by_cached_policy,
    _obtain_host_credential,
    _store_internal_promptless_identity,
    _store_policy_observation,
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
    _claim_first_enrollment_success_notice,
    _claim_internal_promptless_welcome,
    _pending_plugin_update,
    _record_plugin_version_seen,
)
from .output import _emit, _emit_command_json, _flush_control_output
from .redaction import _redact_text
from .status import _reset_host_state, _status_payload
from .traces import _hook_trace_context, _lifecycle_event, _read_hook_context, _run_collect
from .validation import _requires_newer_bootstrap
from .worker import _get_json, _post_check_in, _validate_signed_policy, _worker_url


def main(argv: list[str] | None = None) -> int:
    """Run the requested host-runtime command."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        return _run_version_command(json_output=args.json)
    host = _resolve_host(args.host)
    if args.command == "ensure":
        return _run_ensure_command(host, quiet=args.quiet, if_sources=args.if_sources)
    if args.command == "collect":
        return _run_collect_command(
            host,
            lifecycle=args.lifecycle,
            baseline=args.baseline,
            include_active=args.include_active,
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

    collect_parser = subcommands.add_parser("collect", help="Upload native trace JSONL changes")
    _add_host_argument(collect_parser)
    collect_parser.add_argument(
        "--lifecycle",
        choices=("session_start", "stop", "session_end", "subagent_stop"),
        default=None,
        help="Host lifecycle event that triggered collection",
    )
    collect_parser.add_argument(
        "--baseline",
        action="store_true",
        help="On first run, record current source offsets without uploading historical ranges",
    )
    collect_parser.add_argument(
        "--include-active",
        action="store_true",
        help="Include session files still inside the idle grace period after an established baseline",
    )
    collect_parser.add_argument("--quiet", action="store_true", help="Suppress hook status output")

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


def _run_ensure_command(host: Host, *, quiet: bool, if_sources: bool) -> int:
    try:
        return _run_ensure(host, quiet=quiet, if_sources=if_sources)
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
    baseline: bool,
    include_active: bool,
    quiet: bool,
) -> int:
    try:
        event = _lifecycle_event(lifecycle)
        hook_context = _hook_trace_context(_read_hook_context())
        return _run_collect(
            host,
            lifecycle_event=event,
            hook_context=hook_context,
            baseline=baseline,
            include_active=include_active,
            quiet=quiet,
        )
    except (BootstrapError, OSError, ValueError, urllib.error.URLError) as exc:
        _emit({"status": "error", "host": host, "message": _redact_text(str(exc))}, quiet=quiet)
        return 0
    finally:
        _flush_control_output()


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
        metadata = _load_runtime_metadata(plugin_root, host)
        worker_base_url = _worker_base_url()
        dashboard_base_url = _dashboard_base_url()
        context = _enrollment_context(worker_base_url, dashboard_base_url, metadata)
        enrollment_attempt = _obtain_host_credential(context, quiet=True)
        if enrollment_attempt.credential is None:
            _emit_command_json(
                {
                    "status": "setup_pending",
                    "reason": enrollment_attempt.reason or "approval_pending",
                    "host": host,
                }
            )
            return 0
        credential = enrollment_attempt.credential
        _emit_command_json(
            {
                "status": "enrolled",
                "host": host,
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


def _run_ensure(host: Host, *, quiet: bool, if_sources: bool) -> int:
    if if_sources and _host_disabled_by_cached_policy(_worker_base_url(), host):
        _emit({"status": "trace_upload_skipped", "reason": "policy_disabled", "host": host}, quiet=quiet)
        return 0
    if if_sources and not _has_native_trace_sources(host):
        _emit({"status": "trace_upload_skipped", "reason": "no_sources", "host": host}, quiet=quiet)
        return 0
    # Compute any plugin-update notice before enrollment so every install learns when the
    # Instruction Hub plugin version changed. Both Claude and Codex render the SessionStart
    # `systemMessage`. The new version is recorded only after the hook output is emitted below,
    # so a later failure re-announces it on the next healthy session.
    plugin_root = _plugin_root()
    metadata = _load_runtime_metadata(plugin_root, host)
    pending_update = _pending_plugin_update(metadata)
    update_notice = pending_update.notice if pending_update is not None else None
    exit_code = _run_host_enrollment(
        host,
        metadata,
        quiet=quiet,
        update_notice=update_notice,
        plugin_version_updated=update_notice is not None,
    )
    # Reached only when the host enrollment step returned without raising, i.e. the SessionStart
    # output (including any update notice) was emitted. A quiet run suppresses that output, so it
    # does not consume the one-time notice.
    if pending_update is not None and not quiet:
        _record_plugin_version_seen(pending_update)
    return exit_code


def _run_host_enrollment(
    host: Host,
    metadata: RuntimeMetadata,
    *,
    quiet: bool,
    update_notice: str | None,
    plugin_version_updated: bool,
) -> int:
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
                quiet=quiet,
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
                    quiet=quiet,
                    plugin_version=metadata.plugin_version,
                    version_updated=plugin_version_updated,
                ),
            )
            return 0
        credential = enrollment_attempt.credential
        signed_policy = _get_json(policy_url, credential.value, label="policy response")
    policy = _validate_signed_policy(signed_policy, host)
    _store_policy_observation(worker_base_url, policy)
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
                quiet=quiet,
                plugin_version=metadata.plugin_version,
                version_updated=plugin_version_updated,
            ),
        )
        return 0

    result = _ensure_host_config(host, trace_upload_endpoint=trace_upload_endpoint)
    _post_check_in(check_in_url, credential, host, metadata, policy, result)
    _emit(
        {"status": result.status, "host": host, "needs_restart": result.needs_restart},
        quiet=quiet,
        update_notice=update_notice,
        first_success_notice=_claim_first_enrollment_success_notice(host, status=result.status, quiet=quiet),
        internal_welcome_notice=_claim_internal_promptless_welcome(
            credential=credential,
            quiet=quiet,
            plugin_version=metadata.plugin_version,
            version_updated=plugin_version_updated,
        ),
    )
    return 0
