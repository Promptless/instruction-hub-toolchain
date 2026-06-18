"""Deterministic hashing for generated Instruction Hub manifests."""

from __future__ import annotations

import hashlib
import json

from promptless_instruction_hub.fs import JsonValue


def stable_hash(data: JsonValue) -> str:
    """Return a stable sha256 hash for JSON-compatible data."""

    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()
