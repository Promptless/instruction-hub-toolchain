"""Host configuration inspection and legacy managed-config cleanup."""

from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from .contracts import (
    BootstrapError,
    CLAUDE_DESKTOP_TRACE_DIR_NAMES,
    CLAUDE_MANAGED_ENV_MARKER,
    CLAUDE_MANAGED_LEGACY_ENV_KEYS,
    ConfigResult,
    Host,
    JsonValue,
    MANAGED_BEGIN,
    MANAGED_END,
)
from .redaction import _redact_text
from .storage import _atomic_write_text, _ledger_path
from .validation import _non_empty, _string_value


def _native_trace_globs(host: Host) -> tuple[str, ...]:
    if host == "claude":
        return (str(Path.home() / ".claude/projects/**/*.jsonl"),)
    if host == "claude-desktop":
        return tuple(str(root / "**/audit.jsonl") for root in _claude_desktop_trace_roots())
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return (str(codex_home / "**/*.jsonl"),)


def _claude_desktop_trace_roots() -> tuple[Path, ...]:
    if os.name == "nt":
        appdata = _non_empty(os.environ.get("APPDATA"))
        base = Path(appdata).expanduser() if appdata is not None else Path.home() / "AppData/Roaming"
        claude_base = base / "Claude"
    elif sys.platform == "darwin":
        claude_base = Path.home() / "Library/Application Support/Claude"
    else:
        xdg_config_home = _non_empty(os.environ.get("XDG_CONFIG_HOME"))
        config_base = Path(xdg_config_home).expanduser() if xdg_config_home is not None else Path.home() / ".config"
        claude_base = config_base / "Claude"
    roots: list[Path] = []
    seen: set[str] = set()
    for directory_name in CLAUDE_DESKTOP_TRACE_DIR_NAMES:
        root = (claude_base / directory_name).expanduser()
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return tuple(roots)


def _has_native_trace_sources(host: Host) -> bool:
    for pattern in _native_trace_globs(host):
        for raw_path in glob.iglob(pattern, recursive=True):
            if Path(raw_path).is_file():
                return True
    return False


def _host_config_status(host: Host) -> dict[str, JsonValue]:
    if host == "codex":
        return _codex_config_status()
    if host == "claude":
        return _claude_config_status()
    return _claude_desktop_config_status()


def _codex_config_status() -> dict[str, JsonValue]:
    config_path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser() / "config.toml"
    result: dict[str, JsonValue] = {"path": str(config_path), "exists": config_path.exists()}
    if not config_path.exists():
        result["managed_config_detected"] = False
        return result
    try:
        existing = config_path.read_text()
    except OSError as exc:
        result["read_error"] = _redact_text(str(exc))
        result["managed_config_detected"] = False
        return result
    malformed = _has_malformed_managed_blocks(existing)
    result["malformed_managed_config"] = malformed
    result["managed_config_detected"] = not malformed and _managed_block_pattern().search(existing) is not None
    return result


def _claude_config_status() -> dict[str, JsonValue]:
    settings_path = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser() / "settings.json"
    result: dict[str, JsonValue] = {"path": str(settings_path), "exists": settings_path.exists()}
    if not settings_path.exists():
        result["managed_config_detected"] = False
        return result
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        result["read_error"] = _redact_text(str(exc))
        result["managed_config_detected"] = False
        return result
    if not isinstance(settings, dict):
        result["read_error"] = "settings file is not a JSON object"
        result["managed_config_detected"] = False
        return result
    env = settings.get("env")
    result["managed_config_detected"] = (
        isinstance(env, dict) and _string_value(env.get(CLAUDE_MANAGED_ENV_MARKER)) == "1"
    )
    return result


def _claude_desktop_config_status() -> dict[str, JsonValue]:
    roots = _claude_desktop_trace_roots()
    return {
        "path": "Claude Desktop local audit stores",
        "paths": [str(root) for root in roots],
        "exists": any(root.exists() for root in roots),
        "managed_config_detected": False,
    }


def _ensure_host_config(host: Host, *, trace_upload_endpoint: str) -> ConfigResult:
    """Remove telemetry config written by earlier managed bootstraps.

    Native trace collection needs no host-side telemetry config: the plugin
    ships the hooks and this runtime reads the ledgers directly. The only
    config work left is deleting what previous OTel-era bootstraps wrote so
    hosts stop exporting to retired worker endpoints.
    """

    if host == "codex":
        return _ensure_codex_config(trace_upload_endpoint=trace_upload_endpoint)
    if host == "claude":
        return _ensure_claude_config(trace_upload_endpoint=trace_upload_endpoint)
    return _ensure_claude_desktop_config(trace_upload_endpoint=trace_upload_endpoint)


def _ensure_codex_config(*, trace_upload_endpoint: str) -> ConfigResult:
    config_path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser() / "config.toml"
    existing = config_path.read_text() if config_path.exists() else ""
    if _has_malformed_managed_blocks(existing):
        return _blocked_result(
            "codex",
            kind="manual_config_required",
            message="Existing Codex managed telemetry markers are malformed",
            details={"path": str(config_path)},
            trace_upload_endpoint=trace_upload_endpoint,
        )
    updated = _without_managed_block(existing)
    changed = updated != existing
    if changed:
        _write_with_backup(config_path, updated)
    return ConfigResult(
        status="needs_restart" if changed else "configured",
        needs_restart=changed,
        effective_config=_effective_config(
            "codex",
            configured=True,
            managed_config_detected=changed,
            trace_upload_endpoint=trace_upload_endpoint,
        ),
        drift_reports=_removed_config_reports("codex", config_path, changed),
    )


def _without_managed_block(existing: str) -> str:
    return _managed_block_pattern().sub("", existing)


def _has_malformed_managed_blocks(existing: str) -> bool:
    begin_count = existing.count(MANAGED_BEGIN)
    end_count = existing.count(MANAGED_END)
    if begin_count != end_count:
        return begin_count > 0 or end_count > 0
    if begin_count == 0:
        return False
    return sum(1 for _ in _managed_block_pattern().finditer(existing)) != begin_count


def _managed_block_pattern() -> re.Pattern[str]:
    return re.compile(re.escape(MANAGED_BEGIN) + r".*?" + re.escape(MANAGED_END) + r"\n?", re.DOTALL)


def _ensure_claude_config(*, trace_upload_endpoint: str) -> ConfigResult:
    settings_path = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser() / "settings.json"
    settings = _read_settings(settings_path) if settings_path.exists() else {}
    env = settings.get("env")
    managed = isinstance(env, dict) and _string_value(env.get(CLAUDE_MANAGED_ENV_MARKER)) == "1"
    changed = False
    if managed and isinstance(env, dict):
        for key in _managed_claude_env_keys(env):
            env.pop(key, None)
            changed = True
        if not env:
            settings.pop("env", None)
        if changed:
            _write_with_backup(settings_path, json.dumps(settings, indent=2, sort_keys=True) + "\n")
    return ConfigResult(
        status="needs_restart" if changed else "configured",
        needs_restart=changed,
        effective_config=_effective_config(
            "claude",
            configured=True,
            managed_config_detected=changed,
            trace_upload_endpoint=trace_upload_endpoint,
        ),
        drift_reports=_removed_config_reports("claude", settings_path, changed),
    )


def _ensure_claude_desktop_config(*, trace_upload_endpoint: str) -> ConfigResult:
    return ConfigResult(
        status="configured",
        needs_restart=False,
        effective_config=_effective_config(
            "claude-desktop",
            configured=True,
            managed_config_detected=False,
            trace_upload_endpoint=trace_upload_endpoint,
        ),
        drift_reports=[],
    )


def _managed_claude_env_keys(env: dict[str, JsonValue]) -> list[str]:
    keys = {CLAUDE_MANAGED_ENV_MARKER}
    keys.update(CLAUDE_MANAGED_LEGACY_ENV_KEYS)
    keys.update(key for key in env if isinstance(key, str) and key.startswith("OTEL_"))
    return sorted(key for key in keys if key in env)


def _read_settings(settings_path: Path) -> dict[str, JsonValue]:
    if not settings_path.exists():
        return {}
    try:
        value = json.loads(settings_path.read_text())
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"Claude settings are invalid JSON at {settings_path}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise BootstrapError(f"Claude settings must be a JSON object at {settings_path}")
    return value


def _effective_config(
    host: Host,
    *,
    configured: bool,
    managed_config_detected: bool,
    trace_upload_endpoint: str,
) -> dict[str, JsonValue]:
    # Mirrors the worker's HostConfigState check-in contract.
    effective_config: dict[str, JsonValue] = {
        "host": host,
        "configured": configured,
        "trace_upload_endpoint": trace_upload_endpoint if configured else None,
        "native_root_count": len(_native_trace_globs(host)),
        "source_ledger_path": str(_ledger_path()),
        "managed_config_detected": managed_config_detected,
    }
    effective_config["config_hash"] = hashlib.sha256(json.dumps(effective_config, sort_keys=True).encode()).hexdigest()
    return effective_config


def _blocked_result(
    host: Host,
    *,
    kind: str,
    message: str,
    details: dict[str, JsonValue],
    trace_upload_endpoint: str,
) -> ConfigResult:
    return ConfigResult(
        status="blocked",
        needs_restart=False,
        effective_config=_effective_config(
            host,
            configured=False,
            managed_config_detected=False,
            trace_upload_endpoint=trace_upload_endpoint,
        ),
        drift_reports=[
            {
                "kind": kind,
                "message": message,
                "repaired": False,
                "details": details,
            }
        ],
    )


def _removed_config_reports(host: Host, path: Path, changed: bool) -> list[dict[str, JsonValue]]:
    if not changed:
        return []
    return [
        {
            "kind": "removed_managed_config",
            "message": f"Removed managed {host} telemetry config",
            "repaired": True,
            "details": {"path": str(path)},
        }
    ]


def _write_with_backup(path: Path, contents: str) -> None:
    if path.exists():
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")
        shutil.copy2(path, path.with_name(f"{path.name}.{timestamp}.bak"))
    _atomic_write_text(path, contents)
