"""Worker HTTP transport, policy validation, and check-ins."""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.request

from .contracts import (
    BootstrapAuthError,
    BootstrapError,
    ConfigResult,
    HTTP_TIMEOUT_SECONDS,
    Host,
    HostCredential,
    HostPolicy,
    JsonValue,
    RuntimeMetadata,
    WorkerResponseError,
)
from .redaction import _redact_json
from .validation import _datetime_value, _decode_json_object, _int_value, _string_value

_MAX_WORKER_ERROR_RESPONSE_BYTES = 64 * 1024


def _worker_url(worker_base_url: str, path: str) -> str:
    return f"{worker_base_url}{path}"


def _get_json(url: str, token: str | None, *, label: str) -> dict[str, JsonValue]:
    headers = _auth_headers(token)
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise BootstrapAuthError(f"{label} request failed with HTTP {exc.code}") from exc
        raise _worker_response_error(exc, label) from exc
    return _decode_json_object(body, label)


def _post_check_in(
    url: str,
    credential: HostCredential,
    host: Host,
    metadata: RuntimeMetadata,
    policy: HostPolicy,
    result: ConfigResult,
) -> None:
    payload = _check_in_payload(host, metadata, policy, result)
    response_payload = _post_json_response(url, credential.value, payload, label="check-in response")
    if response_payload.get("accepted") is not True:
        raise BootstrapError("check-in response was not accepted")
    response_policy_version = response_payload.get("policy_version")
    if type(response_policy_version) is not int or response_policy_version != policy.policy_version:
        raise BootstrapError("check-in response policy version did not match the applied policy")


def _post_json_response(
    url: str,
    token: str | None,
    payload: dict[str, JsonValue],
    *,
    label: str,
) -> dict[str, JsonValue]:
    body = json.dumps(payload, sort_keys=True).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={**_auth_headers(token), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return _decode_json_object(response.read(), label)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise BootstrapAuthError(f"{label} request failed with HTTP {exc.code}") from exc
        raise _worker_response_error(exc, label) from exc


def _worker_response_error(error: urllib.error.HTTPError, label: str) -> WorkerResponseError:
    try:
        response_body = error.read(_MAX_WORKER_ERROR_RESPONSE_BYTES + 1)
    except OSError:
        response_body = b""
    if len(response_body) > _MAX_WORKER_ERROR_RESPONSE_BYTES:
        response_body = b""
    return WorkerResponseError(
        f"{label} request failed with HTTP {error.code}",
        status_code=error.code,
        response_body=response_body,
    )


def _auth_headers(token: str | None) -> dict[str, str]:
    if token is None:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _check_in_payload(
    host: Host,
    metadata: RuntimeMetadata,
    policy: HostPolicy,
    result: ConfigResult,
) -> dict[str, JsonValue]:
    return {
        "host": host,
        "plugin_version": metadata.plugin_version,
        "policy_version": policy.policy_version,
        "status": result.status,
        "needs_restart": result.needs_restart,
        "effective_config": result.effective_config,
        "drift_reports": [_redact_json(report) for report in result.drift_reports],
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "bootstrap_version": metadata.bootstrap_version,
    }


def _validate_signed_policy(payload: dict[str, JsonValue], host: Host) -> HostPolicy:
    policy_value = payload.get("policy")
    if not isinstance(policy_value, dict):
        raise BootstrapError("policy response missing policy object")
    signature = _string_value(payload.get("signature"))
    if signature is None or re.fullmatch(r"[A-Za-z0-9._-]+:.+", signature) is None:
        raise BootstrapError("policy response has invalid signature shape")
    # The dogfood endpoint relies on HTTPS transport authentication; this bootstrap only
    # shape-checks the hosted policy signature. Before broader customer rollout, hosted
    # policies should use asymmetric signatures verified here by the static native binary.
    schema_version = _int_value(policy_value.get("schema_version"), "policy.schema_version")
    if schema_version != 1:
        raise BootstrapError("policy.schema_version must be 1")
    expires_at = _datetime_value(policy_value.get("expires_at"), "policy.expires_at")
    if expires_at <= dt.datetime.now(dt.timezone.utc):
        raise BootstrapError("policy has expired")
    policy_version = _int_value(policy_value.get("policy_version"), "policy.policy_version")
    enabled_hosts = policy_value.get("enabled_hosts")
    if not isinstance(enabled_hosts, list) or host not in enabled_hosts:
        raise BootstrapError(f"policy does not enable {host}")
    # plugin_permissions is deliberately ignored: the runtime no longer writes user config
    # beyond deleting its own legacy managed blocks, so there is no capability to gate. A
    # policy carrying the retired field still validates while hosted/worker phase it out.
    required_bootstrap_version = _string_value(policy_value.get("required_bootstrap_version"))
    return HostPolicy(
        policy_version=policy_version,
        required_bootstrap_version=required_bootstrap_version,
    )
