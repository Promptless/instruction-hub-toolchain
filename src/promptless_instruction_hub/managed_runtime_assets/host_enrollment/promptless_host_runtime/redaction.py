"""Credential-safe diagnostic redaction."""

from __future__ import annotations

import re

from .contracts import JsonValue


def _redact_json(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_json(child) for child in value]
    if isinstance(value, dict):
        redacted: dict[str, JsonValue] = {}
        for key, child in value.items():
            if key.lower() in {"authorization", "device_code", "host_credential", "token"}:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_json(child)
        return redacted
    return value


def _redact_text(value: str) -> str:
    redacted = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", value)
    redacted = re.sub(r"plihost_[A-Za-z0-9._~+/=-]+", "plihost_<redacted>", redacted)
    return re.sub(r"plihenroll_[A-Za-z0-9._~+/=-]+", "plihenroll_<redacted>", redacted)
