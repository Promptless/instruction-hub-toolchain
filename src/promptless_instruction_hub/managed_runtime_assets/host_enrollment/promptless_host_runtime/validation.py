"""JSON, URL, timestamp, identity, and version validation helpers."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from typing import cast
from urllib.parse import SplitResult, urlsplit

from .contracts import BootstrapError, INTERNAL_PROMPTLESS_EMAIL_DOMAIN, JsonValue, TEST_URL_OVERRIDE_ENV


def _is_kebab_case_identifier(value: str) -> bool:
    return re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", value) is not None


def _requires_newer_bootstrap(required: str | None, current: str) -> bool:
    if required is None:
        return False
    required_tuple = _semver_tuple(required)
    current_tuple = _semver_tuple(current)
    if required_tuple is None or current_tuple is None:
        return required != current
    return current_tuple < required_tuple


def _semver_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.match(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _validate_otlp_http_endpoint(value: JsonValue | None, field_name: str, expected_path: str) -> str:
    endpoint = _string_value(value)
    if endpoint is None:
        raise BootstrapError(f"{field_name} endpoint must be a string")
    parsed = _validate_http_url(endpoint, field_name)
    if parsed.path != expected_path:
        raise BootstrapError(f"{field_name} endpoint must use path {expected_path}")
    return endpoint


def _validate_http_url(value: str, field_name: str) -> SplitResult:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.netloc == "":
        raise BootstrapError(f"{field_name} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise BootstrapError(f"{field_name} must not include credentials, query, or fragment")
    if _url_origin_port(parsed) is None:
        raise BootstrapError(f"{field_name} must have a valid origin port")
    return parsed


def _validate_http_redirect_url(value: str, field_name: str) -> SplitResult:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.netloc == "":
        raise BootstrapError(f"{field_name} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise BootstrapError(f"{field_name} must not include credentials or fragment")
    if _url_origin_port(parsed) is None:
        raise BootstrapError(f"{field_name} must have a valid origin port")
    return parsed


def _same_url_origin(left: SplitResult, right: SplitResult) -> bool:
    return (
        left.scheme == right.scheme
        and left.hostname == right.hostname
        and _url_origin_port(left) == _url_origin_port(right)
    )


def _url_origin_port(parsed: SplitResult) -> int | None:
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None:
        return port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return None


def _datetime_value(value: JsonValue | None, field_name: str) -> dt.datetime:
    text = _string_value(value)
    if text is None:
        raise BootstrapError(f"{field_name} must be an ISO timestamp")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BootstrapError(f"{field_name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BootstrapError(f"{field_name} must include a timezone")
    return parsed


def _optional_datetime_value(value: JsonValue | None, field_name: str) -> dt.datetime | None:
    if value is None:
        return None
    return _datetime_value(value, field_name)


def _int_value(value: JsonValue | None, field_name: str) -> int:
    if type(value) is not int:
        raise BootstrapError(f"{field_name} must be an integer")
    return value


def _int_string_value(value: str | None, field_name: str) -> int:
    if value is None:
        raise BootstrapError(f"{field_name} must be an integer")
    try:
        return int(value)
    except ValueError as exc:
        raise BootstrapError(f"{field_name} must be an integer") from exc


def _optional_int_value(value: JsonValue | None) -> int | None:
    if type(value) is not int:
        return None
    return value


def _decode_json_object(body: bytes, label: str) -> dict[str, JsonValue]:
    try:
        value = json.loads(body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if isinstance(exc, UnicodeDecodeError):
            raise BootstrapError(f"{label} is not valid UTF-8") from exc
        raise BootstrapError(f"{label} is invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise BootstrapError(f"{label} must be a JSON object")
    return _json_object(value)


def _json_object(value: dict[object, object]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, child in value.items():
        if not isinstance(key, str):
            raise BootstrapError("JSON object contains a non-string key")
        result[key] = _json_value(child)
    return result


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(child) for child in value]
    if isinstance(value, dict):
        return _json_object(cast(dict[object, object], value))
    raise BootstrapError(f"unsupported JSON value: {type(value).__name__}")


def _json_mapping_or_empty(value: JsonValue | None) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        return {}
    return value


def _normalize_base_url(value: str, *, label: str) -> str:
    normalized = value.rstrip("/")
    parsed = _validate_http_url(normalized, label)
    _validate_worker_transport(parsed, label)
    if parsed.path not in {"", "/"}:
        raise BootstrapError(f"{label} must not include a path")
    return normalized


def _test_url_overrides_enabled() -> bool:
    return os.environ.get(TEST_URL_OVERRIDE_ENV) == "1"


def _is_loopback_parsed_url(parsed: SplitResult) -> bool:
    host = parsed.hostname
    return host in {"localhost", "127.0.0.1", "::1"}


def _validate_worker_transport(parsed: SplitResult, field_name: str) -> None:
    if parsed.scheme == "https":
        return
    if _test_url_overrides_enabled() and _is_loopback_parsed_url(parsed):
        return
    raise BootstrapError(f"{field_name} must use HTTPS unless {TEST_URL_OVERRIDE_ENV}=1 and the host is loopback")


def _string_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _identity_email(payload: dict[str, JsonValue]) -> str | None:
    for key in ("user_email", "email", "account_email"):
        email = _normalize_email(_string_value(payload.get(key)))
        if email is not None:
            return email
    for nested_key in ("user", "identity", "account"):
        nested = _json_mapping_or_empty(payload.get(nested_key))
        for email_key in ("email", "user_email"):
            email = _normalize_email(_string_value(nested.get(email_key)))
            if email is not None:
                return email
    return None


def _stored_credential_has_internal_promptless_identity(payload: dict[str, JsonValue]) -> bool:
    return payload.get("is_internal_promptless_user") is True


def _worker_response_has_internal_promptless_identity(payload: dict[str, JsonValue]) -> bool:
    return payload.get("is_internal_promptless_user") is True or _is_internal_promptless_email(_identity_email(payload))


def _policy_response_has_internal_promptless_identity(payload: dict[str, JsonValue]) -> bool:
    if _worker_response_has_internal_promptless_identity(payload):
        return True
    policy = _json_mapping_or_empty(payload.get("policy"))
    return bool(policy) and _worker_response_has_internal_promptless_identity(policy)


def _normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    email = value.strip().lower()
    if len(email) > 254 or "@" not in email or email.startswith("@") or email.endswith("@"):
        return None
    if any(char.isspace() for char in email):
        return None
    return email


def _is_internal_promptless_email(email: str | None) -> bool:
    normalized = _normalize_email(email)
    return normalized is not None and normalized.endswith(f"@{INTERNAL_PROMPTLESS_EMAIL_DOMAIN}")


def _non_empty(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    return value
