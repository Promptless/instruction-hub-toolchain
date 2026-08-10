"""Browser enrollment and host-credential persistence."""

from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import json
import os
import platform
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .contracts import (
    BootstrapError,
    CALLBACK_STATE_PARAM,
    ENROLLMENT_CALLBACK_DEADLINE_SECONDS,
    ENROLLMENT_POLL_DEADLINE_SECONDS,
    EnrollmentAttempt,
    EnrollmentContext,
    EnrollmentSession,
    EnrollmentSessionAttempt,
    HOSTED_ENROLLMENT_APPROVAL_PATH,
    HOSTED_ENROLLMENT_START_PATH,
    HTTP_TIMEOUT_SECONDS,
    Host,
    HostCredential,
    HostPolicy,
    HostedEnrollmentRoutes,
    JsonValue,
    OPEN_BROWSER_ENV,
    RuntimeMetadata,
)
from .output import _emit
from .storage import _load_state, _state_file_lock, _state_path, _try_lock_state_file, _unlock_state_file, _write_state
from .validation import (
    _datetime_value,
    _int_string_value,
    _is_loopback_parsed_url,
    _json_mapping_or_empty,
    _non_empty,
    _optional_datetime_value,
    _optional_int_value,
    _policy_response_has_internal_promptless_identity,
    _same_url_origin,
    _stored_credential_has_internal_promptless_identity,
    _string_value,
    _test_url_overrides_enabled,
    _validate_http_redirect_url,
    _validate_http_url,
    _validate_worker_transport,
    _worker_response_has_internal_promptless_identity,
)
from .worker import _get_json, _post_json_response, _worker_url


def _enrollment_context(worker_base_url: str, dashboard_base_url: str, metadata: RuntimeMetadata) -> EnrollmentContext:
    deployment_instance_id = _worker_deployment_instance_id(worker_base_url)
    state_path = _state_path()
    with _state_file_lock(state_path):
        state = _load_state(state_path)
        host_instance_id = _host_instance_id(state)
        if state.get("host_instance_id") != host_instance_id:
            state["host_instance_id"] = host_instance_id
            _write_state(state_path, state)
    return EnrollmentContext(
        worker_base_url=worker_base_url,
        dashboard_base_url=dashboard_base_url,
        deployment_instance_id=deployment_instance_id,
        metadata=metadata,
        host_instance_id=host_instance_id,
        host_label=_host_label(),
        host_platform=_host_platform(),
    )


def _worker_deployment_instance_id(worker_base_url: str) -> str:
    response = _get_json(_worker_url(worker_base_url, "/healthz"), None, label="worker health response")
    deployment_instance_id = _non_empty(_string_value(response.get("deployment_instance_id")))
    if deployment_instance_id is None:
        raise BootstrapError("worker health response missing deployment_instance_id")
    return deployment_instance_id


@contextmanager
def _enrollment_leader_lock(context: EnrollmentContext, state_path: Path) -> Iterator[bool]:
    """Serialize browser-approval enrollment across the plugins that share this host credential.

    Yields ``True`` to the single process that wins the lock (the leader, which opens the one
    browser approval) and ``False`` to any concurrent process for the same credential (a
    follower, which must not open a second browser). The lock is scoped to the credential cache
    key so independent agent hosts still enroll in parallel, and it is
    non-blocking so a follower returns immediately and defers to a later session instead of
    stacking up behind the leader's approval wait.
    """

    lock_path = state_path.with_name(f"host-enrollment-{_credential_cache_key(context)}.enroll.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        acquired = _try_lock_state_file(lock_file)
        try:
            yield acquired
        finally:
            if acquired:
                _unlock_state_file(lock_file)


def _host_instance_id(state: dict[str, JsonValue]) -> str:
    existing = _non_empty(_string_value(state.get("host_instance_id")))
    if existing is not None:
        return existing
    return f"host-{uuid.uuid4().hex}"


def _host_label() -> str:
    username = _non_empty(getpass.getuser()) or "unknown-user"
    node = _non_empty(platform.node()) or "unknown-host"
    return _truncate(f"{username}@{node}", 240)


def _host_platform() -> str:
    return _truncate(platform.platform(), 120)


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[:max_length]


def _obtain_host_credential(context: EnrollmentContext, *, quiet: bool) -> EnrollmentAttempt:
    credential = _cached_host_credential(context)
    if credential is not None:
        return EnrollmentAttempt(credential=credential)
    return _enroll_host_credential(context, quiet=quiet)


def _cached_host_credential(context: EnrollmentContext) -> HostCredential | None:
    return _host_credential_from_state(context, _load_state(_state_path()))


def _host_credential_from_state(
    context: EnrollmentContext,
    state: dict[str, JsonValue],
) -> HostCredential | None:
    credentials = _json_mapping_or_empty(state.get("credentials"))
    cached_credential = _json_mapping_or_empty(credentials.get(_credential_cache_key(context)))
    credential_value = _string_value(cached_credential.get("value"))
    credential_id = _string_value(cached_credential.get("credential_id"))
    deployment_instance_id = _string_value(cached_credential.get("deployment_instance_id"))
    credential = (
        HostCredential(
            value=credential_value,
            credential_id=credential_id,
            deployment_instance_id=deployment_instance_id,
            is_internal_promptless_user=_stored_credential_has_internal_promptless_identity(cached_credential),
        )
        if credential_value
        else None
    )
    return credential


def _credential_with_policy_identity(credential: HostCredential, payload: dict[str, JsonValue]) -> HostCredential:
    if credential.is_internal_promptless_user or not _policy_response_has_internal_promptless_identity(payload):
        return credential
    return HostCredential(
        value=credential.value,
        credential_id=credential.credential_id,
        deployment_instance_id=credential.deployment_instance_id,
        is_internal_promptless_user=True,
    )


def _policy_observation_enrollment_host(host: Host) -> Host:
    """Return the enrollment host whose policy observation governs optional work for this host."""
    return "claude" if host == "claude-desktop" else host


def _host_disabled_by_cached_policy(
    context: EnrollmentContext,
    host: Host,
) -> bool:
    """Return whether an unexpired validated policy observation omits the host.

    Cached policy can suppress work for a disabled host. It never authorizes work
    for an enabled host; that still requires a fresh Worker policy response. The
    observation must match the credential currently enrolled for this policy host.
    """

    state = _load_state(_state_path())
    credential = _host_credential_from_state(context, state)
    if credential is None:
        return False
    observations = _json_mapping_or_empty(state.get("policy_observations"))
    observation = _json_mapping_or_empty(observations.get(_policy_observation_cache_key(context, credential)))
    if _string_value(observation.get("worker_base_url")) != context.worker_base_url:
        return False
    expires_at_text = _string_value(observation.get("expires_at"))
    if expires_at_text is None:
        return False
    try:
        expires_at = dt.datetime.fromisoformat(expires_at_text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        return False
    if expires_at <= dt.datetime.now(dt.timezone.utc):
        return False
    enabled_hosts = observation.get("enabled_hosts")
    if not isinstance(enabled_hosts, list):
        return False
    return host not in enabled_hosts


def _store_policy_observation(
    context: EnrollmentContext,
    credential: HostCredential,
    policy: HostPolicy,
) -> None:
    """Best-effort persist a negative policy observation without blocking work on storage failures."""
    try:
        state_path = _state_path()
        with _state_file_lock(state_path):
            state = _load_state(state_path)
            observations = _json_mapping_or_empty(state.get("policy_observations"))
            cache_key = _policy_observation_cache_key(context, credential)
            observation: dict[str, JsonValue] = {
                "worker_base_url": context.worker_base_url,
                "enabled_hosts": list(policy.enabled_hosts),
                "expires_at": policy.expires_at.isoformat(),
            }
            if observations.get(cache_key) == observation:
                return
            observations[cache_key] = observation
            state["policy_observations"] = observations
            _write_state(state_path, state)
    except (OSError, BootstrapError) as exc:
        _emit(
            {
                "status": "policy_observation_not_stored",
                "reason": "host_state_unavailable",
                "message": str(exc),
            },
            quiet=True,
        )
        return


def _policy_observation_cache_key(context: EnrollmentContext, credential: HostCredential) -> str:
    cache_material = {
        "credential_cache_key": _credential_cache_key(context),
        "credential_id": credential.credential_id,
        "credential_value_sha256": hashlib.sha256(credential.value.encode()).hexdigest(),
    }
    return hashlib.sha256(json.dumps(cache_material, sort_keys=True).encode()).hexdigest()


def _store_internal_promptless_identity(context: EnrollmentContext, credential: HostCredential) -> None:
    if not credential.is_internal_promptless_user:
        return
    try:
        state_path = _state_path()
        with _state_file_lock(state_path):
            state = _load_state(state_path)
            credentials = _json_mapping_or_empty(state.get("credentials"))
            key = _credential_cache_key(context)
            cached_credential = _json_mapping_or_empty(credentials.get(key))
            if not cached_credential or cached_credential.get("is_internal_promptless_user") is True:
                return
            cached_credential["is_internal_promptless_user"] = True
            credentials[key] = cached_credential
            state["credentials"] = credentials
            _write_state(state_path, state)
    except (OSError, BootstrapError):
        return


def _forget_cached_host_credential(context: EnrollmentContext) -> None:
    state_path = _state_path()
    with _state_file_lock(state_path):
        state = _load_state(state_path)
        credentials = _json_mapping_or_empty(state.get("credentials"))
        key = _credential_cache_key(context)
        if key in credentials:
            del credentials[key]
            state["credentials"] = credentials
            _write_state(state_path, state)


def _enroll_host_credential(context: EnrollmentContext, *, quiet: bool) -> EnrollmentAttempt:
    state_path = _state_path()
    session = _load_pending_enrollment_session(context, state_path)
    if session is not None:
        # A pending session already exists (e.g. left by an earlier session start); polling it
        # opens no browser, so no cross-plugin coordination is needed here.
        return _complete_host_enrollment(context, state_path, session)
    # A fresh enrollment opens a browser. Serialize that across every plugin on this host so only
    # the leader drives the single browser approval; concurrent followers reuse the resulting
    # credential or defer rather than opening their own browser window.
    with _enrollment_leader_lock(context, state_path) as is_enrollment_leader:
        if not is_enrollment_leader:
            return EnrollmentAttempt(credential=_cached_host_credential(context), reason="enrollment_in_progress")
        # Re-check under the lock: a previous leader may have just finished enrolling this host.
        credential = _cached_host_credential(context)
        if credential is not None:
            return EnrollmentAttempt(credential=credential)
        session = _load_pending_enrollment_session(context, state_path)
        if session is None:
            _emit({"status": "browser_enrollment_starting", "host": context.metadata.target}, quiet=quiet)
            created_session = _create_enrollment_session(context)
            if created_session.session is None:
                return EnrollmentAttempt(credential=None, reason=created_session.reason or "approval_pending")
            session, stored_new_session = _store_pending_enrollment(context, state_path, created_session.session)
            if not stored_new_session:
                session = _load_pending_enrollment_session(context, state_path) or session
        return _complete_host_enrollment(context, state_path, session)


def _complete_host_enrollment(
    context: EnrollmentContext,
    state_path: Path,
    session: EnrollmentSession,
) -> EnrollmentAttempt:
    enrollment_attempt = _poll_enrollment_session(context, session)
    if enrollment_attempt.credential is None:
        return enrollment_attempt
    credential = enrollment_attempt.credential
    _store_host_credential(context, state_path, credential)
    return EnrollmentAttempt(credential=credential)


def _load_pending_enrollment_session(context: EnrollmentContext, state_path: Path) -> EnrollmentSession | None:
    with _state_file_lock(state_path):
        return _pending_enrollment_session(context, _load_state(state_path))


def _pending_enrollment_session(context: EnrollmentContext, state: dict[str, JsonValue]) -> EnrollmentSession | None:
    pending_enrollments = _json_mapping_or_empty(state.get("pending_enrollments"))
    session_value = _json_mapping_or_empty(pending_enrollments.get(_credential_cache_key(context)))
    if not session_value:
        return None
    expires_at = _optional_datetime_value(session_value.get("expires_at"), "pending enrollment expires_at")
    if expires_at is None or expires_at <= dt.datetime.now(dt.timezone.utc):
        return None
    session_id = _string_value(session_value.get("session_id"))
    device_code = _string_value(session_value.get("device_code"))
    poll_url = _string_value(session_value.get("poll_url"))
    poll_interval_seconds = _optional_int_value(session_value.get("poll_interval_seconds"))
    deployment_instance_id = _string_value(session_value.get("deployment_instance_id"))
    if (
        session_id is None
        or deployment_instance_id is None
        or device_code is None
        or poll_url is None
        or poll_interval_seconds is None
    ):
        return None
    if deployment_instance_id != context.deployment_instance_id:
        return None
    poll_url_parts = _validate_http_url(poll_url, "pending host enrollment poll URL")
    _validate_worker_transport(poll_url_parts, "pending host enrollment poll URL")
    return EnrollmentSession(
        session_id=session_id,
        deployment_instance_id=deployment_instance_id,
        device_code=device_code,
        poll_url=poll_url,
        expires_at=expires_at,
        poll_interval_seconds=poll_interval_seconds,
    )


def _create_enrollment_session(context: EnrollmentContext) -> EnrollmentSessionAttempt:
    routes = _hosted_enrollment_routes(context.dashboard_base_url)
    with _EnrollmentCallbackServer(routes) as callback_server:
        start_url = _hosted_enrollment_start_url(context, routes, callback_server.callback_url)
        if not _open_hosted_enrollment_url(start_url):
            return EnrollmentSessionAttempt(session=None, reason="browser_launch_failed")
        callback_payload = callback_server.wait(ENROLLMENT_CALLBACK_DEADLINE_SECONDS)
    if callback_payload is None:
        return EnrollmentSessionAttempt(session=None, reason="browser_approval_timeout")
    return EnrollmentSessionAttempt(session=_enrollment_session_from_callback(context, callback_payload))


def _hosted_enrollment_routes(dashboard_base_url: str) -> HostedEnrollmentRoutes:
    return HostedEnrollmentRoutes(
        dashboard_base_url=dashboard_base_url,
        start_path=HOSTED_ENROLLMENT_START_PATH,
        approval_path=HOSTED_ENROLLMENT_APPROVAL_PATH,
    )


def _hosted_enrollment_start_url(
    context: EnrollmentContext,
    routes: HostedEnrollmentRoutes,
    callback_url: str,
) -> str:
    payload = {
        "callback_url": callback_url,
        "deployment_instance_id": context.deployment_instance_id,
        "target": context.metadata.target,
        "plugin_id": context.metadata.plugin_id,
        "plugin_version": context.metadata.plugin_version,
        "package_id": context.metadata.package_id,
        "bootstrap_version": context.metadata.bootstrap_version,
        "toolchain_version": context.metadata.toolchain_version,
        "host_instance_id": context.host_instance_id,
        "host_label": context.host_label,
        "host_platform": context.host_platform,
        "pending_callback": "1",
    }
    return f"{routes.dashboard_base_url}{routes.start_path}?{urlencode(payload)}"


class _EnrollmentCallbackServer:
    """Small loopback server that receives the hosted approval result."""

    def __init__(self, routes: HostedEnrollmentRoutes) -> None:
        self._routes = routes
        self._event = threading.Event()
        self._payload: dict[str, str] | None = None
        self._state = secrets.token_urlsafe(32)
        handler = self._handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        host, port = self._server.server_address
        callback_query = urlencode({CALLBACK_STATE_PARAM: self._state})
        self.callback_url = f"http://{host}:{port}/callback?{callback_query}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _EnrollmentCallbackServer:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def wait(self, timeout_seconds: float) -> dict[str, str] | None:
        if not self._event.wait(timeout_seconds):
            return None
        return self._payload

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        callback_server = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                query = parse_qs(parsed.query, keep_blank_values=False)
                if query.pop(CALLBACK_STATE_PARAM, []) != [callback_server._state]:
                    body = b"Promptless enrollment callback rejected."
                    self.send_response(403)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                payload: dict[str, str] = {}
                for key, values in query.items():
                    if len(values) == 1:
                        payload[key] = values[0]
                if payload.get("status") == "pending":
                    try:
                        approval_url = _validate_pending_callback_approval_url(payload, callback_server._routes)
                    except BootstrapError as exc:
                        callback_server._payload = {"status": "error", "message": str(exc)}
                        callback_server._event.set()
                        body = f"Promptless enrollment callback rejected approval URL: {exc}".encode()
                        self.send_response(400)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    callback_server._payload = payload
                    callback_server._event.set()
                    self.send_response(302)
                    self.send_header("Location", approval_url)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                callback_server._payload = payload
                callback_server._event.set()
                body = b"Promptless enrollment received. You can return to your agent."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        return CallbackHandler


def _enrollment_session_from_callback(context: EnrollmentContext, payload: dict[str, str]) -> EnrollmentSession:
    status = payload.get("status")
    if status == "error":
        message = _non_empty(payload.get("message")) or "host enrollment callback failed"
        raise BootstrapError(message)
    if status not in {"pending", "approved"}:
        raise BootstrapError("host enrollment callback had invalid status")
    session_id = _non_empty(payload.get("session_id"))
    deployment_instance_id = _non_empty(payload.get("deployment_instance_id"))
    device_code = _non_empty(payload.get("device_code"))
    poll_url = _non_empty(payload.get("poll_url"))
    expires_at = _datetime_value(payload.get("expires_at"), "host enrollment callback expires_at")
    poll_interval_seconds = _int_string_value(payload.get("poll_interval_seconds"), "host enrollment poll interval")
    if session_id is None or deployment_instance_id is None or device_code is None or poll_url is None:
        raise BootstrapError("host enrollment callback missing required fields")
    if deployment_instance_id != context.deployment_instance_id:
        raise BootstrapError("host enrollment callback deployment_instance_id did not match worker identity")
    poll_url_parts = _validate_http_url(poll_url, "host enrollment poll URL")
    _validate_worker_transport(poll_url_parts, "host enrollment poll URL")
    if poll_interval_seconds < 1 or poll_interval_seconds > 30:
        raise BootstrapError("host enrollment poll interval must be between 1 and 30 seconds")
    return EnrollmentSession(
        session_id=session_id,
        deployment_instance_id=deployment_instance_id,
        device_code=device_code,
        poll_url=poll_url,
        expires_at=expires_at,
        poll_interval_seconds=poll_interval_seconds,
    )


def _validate_pending_callback_approval_url(payload: dict[str, str], routes: HostedEnrollmentRoutes) -> str:
    approval_url = _non_empty(payload.get("approval_url"))
    if approval_url is None:
        raise BootstrapError("host enrollment pending callback missing approval URL")
    parsed = _validate_http_redirect_url(approval_url, "host enrollment approval URL")
    dashboard_origin = urlsplit(routes.dashboard_base_url)
    if parsed.path != routes.approval_path or not _same_url_origin(parsed, dashboard_origin):
        raise BootstrapError("host enrollment approval URL did not match dashboard enrollment route")
    return approval_url


def _store_pending_enrollment(
    context: EnrollmentContext,
    state_path: Path,
    session: EnrollmentSession,
) -> tuple[EnrollmentSession, bool]:
    with _state_file_lock(state_path):
        state = _load_state(state_path)
        existing_session = _pending_enrollment_session(context, state)
        if existing_session is not None:
            return existing_session, False
        pending_enrollments = _json_mapping_or_empty(state.get("pending_enrollments"))
        pending_enrollments[_credential_cache_key(context)] = {
            "session_id": session.session_id,
            "deployment_instance_id": session.deployment_instance_id,
            "device_code": session.device_code,
            "poll_url": session.poll_url,
            "expires_at": session.expires_at.isoformat(),
            "poll_interval_seconds": session.poll_interval_seconds,
            "target": context.metadata.target,
            "worker_base_url": context.worker_base_url,
        }
        state["pending_enrollments"] = pending_enrollments
        _write_state(state_path, state)
        return session, True


def _poll_enrollment_session(context: EnrollmentContext, session: EnrollmentSession) -> EnrollmentAttempt:
    deadline = time.monotonic() + ENROLLMENT_POLL_DEADLINE_SECONDS
    while True:
        response = _post_json_response(
            session.poll_url,
            None,
            {"device_code": session.device_code},
            label="host enrollment poll response",
        )
        status = _string_value(response.get("status"))
        if status == "approved":
            host_credential = _string_value(response.get("host_credential"))
            if host_credential is None:
                raise BootstrapError("approved host enrollment poll response missing host_credential")
            return EnrollmentAttempt(
                credential=HostCredential(
                    value=host_credential,
                    credential_id=_string_value(response.get("credential_id")),
                    deployment_instance_id=session.deployment_instance_id,
                    is_internal_promptless_user=_worker_response_has_internal_promptless_identity(response),
                )
            )
        if status in {"expired", "consumed"}:
            _forget_pending_enrollment(context)
            return EnrollmentAttempt(credential=None, reason="approval_expired")
        if status != "pending":
            raise BootstrapError("host enrollment poll response has invalid status")
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return EnrollmentAttempt(credential=None, reason="approval_pending")
        time.sleep(min(float(session.poll_interval_seconds), remaining_seconds))


def _store_host_credential(
    context: EnrollmentContext,
    state_path: Path,
    credential: HostCredential,
) -> None:
    with _state_file_lock(state_path):
        state = _load_state(state_path)
        credentials = _json_mapping_or_empty(state.get("credentials"))
        credentials[_credential_cache_key(context)] = {
            "value": credential.value,
            "credential_id": credential.credential_id,
            "worker_base_url": context.worker_base_url,
            "deployment_instance_id": credential.deployment_instance_id,
            "target": context.metadata.target,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        if credential.is_internal_promptless_user:
            credentials[_credential_cache_key(context)]["is_internal_promptless_user"] = True
        state["credentials"] = credentials
        pending_enrollments = _json_mapping_or_empty(state.get("pending_enrollments"))
        pending_enrollments.pop(_credential_cache_key(context), None)
        state["pending_enrollments"] = pending_enrollments
        _write_state(state_path, state)


def _forget_pending_enrollment(context: EnrollmentContext) -> None:
    state_path = _state_path()
    with _state_file_lock(state_path):
        state = _load_state(state_path)
        pending_enrollments = _json_mapping_or_empty(state.get("pending_enrollments"))
        if pending_enrollments.pop(_credential_cache_key(context), None) is not None:
            state["pending_enrollments"] = pending_enrollments
            _write_state(state_path, state)


def _credential_cache_key(context: EnrollmentContext) -> str:
    # Key the credential and pending enrollment on host-level identity only: the worker
    # deployment and the agent host. Every plugin from the hub on this machine maps to the same
    # key, so one browser approval enrolls the host once and all of its plugins reuse the
    # resulting credential. Plugin- and package-scoped fields are deliberately excluded.
    cache_material = {
        "deployment_instance_id": context.deployment_instance_id,
        "target": context.metadata.target,
        "worker_base_url": context.worker_base_url,
    }
    return hashlib.sha256(json.dumps(cache_material, sort_keys=True).encode()).hexdigest()


def _open_hosted_enrollment_url(enrollment_url: str) -> bool:
    if os.environ.get(OPEN_BROWSER_ENV) == "0":
        if _test_url_overrides_enabled() and _is_loopback_parsed_url(urlsplit(enrollment_url)):
            _open_loopback_url_for_test(enrollment_url)
            return True
        return False
    try:
        return webbrowser.open(enrollment_url, new=2, autoraise=True)
    except webbrowser.Error:
        return False


def _open_loopback_url_for_test(enrollment_url: str) -> None:
    request = urllib.request.Request(enrollment_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise BootstrapError(f"hosted enrollment start request failed with HTTP {exc.code}") from exc
