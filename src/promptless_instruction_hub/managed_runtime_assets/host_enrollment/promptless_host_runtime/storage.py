"""Host-runtime paths, atomic writes, and cross-process file locks."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .contracts import (
    BootstrapError,
    DIAGNOSTIC_LOG_FILE_NAME,
    JsonValue,
    LAST_STATUS_FILE_NAME,
    LEDGER_FILE_NAME,
    STATE_FILE_NAME,
)
from .validation import _json_object, _non_empty


def _ledger_path() -> Path:
    override = _non_empty(os.environ.get("PROMPTLESS_HOST_RUNTIME_LEDGER"))
    if override is not None:
        return Path(override).expanduser()
    return _state_path().with_name(LEDGER_FILE_NAME)


def _state_path() -> Path:
    # Host enrollment is a per-host concept: one approved credential and one OTel policy cover
    # every Promptless plugin this user installs from the hub. Persist the state at a single
    # host-global path so multiple plugins coordinate through one file (and one credential)
    # instead of each enrolling -- and opening its own browser window -- independently. The
    # per-plugin CLAUDE_PLUGIN_DATA/PLUGIN_DATA directories are intentionally not used here: the
    # agent host sets a different one per plugin, which would re-fragment the shared host state.
    return Path.home() / ".promptless/instruction-hub" / STATE_FILE_NAME


def _last_status_path() -> Path:
    return _state_path().with_name(LAST_STATUS_FILE_NAME)


def _diagnostic_log_path() -> Path:
    return _state_path().with_name(DIAGNOSTIC_LOG_FILE_NAME)


def _load_state(path: Path) -> dict[str, JsonValue]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"host enrollment state is invalid JSON at {path}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise BootstrapError(f"host enrollment state must be a JSON object at {path}")
    return _json_object(value)


def _write_state(path: Path, state: dict[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(state, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, body)
    try:
        path.chmod(0o600)
    except OSError:
        pass


@contextmanager
def _state_file_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        _lock_state_file(lock_file)
        try:
            yield
        finally:
            _unlock_state_file(lock_file)


def _lock_state_file(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        _ensure_windows_lock_byte(lock_file)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)  # ty: ignore[unresolved-attribute]
        return
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _unlock_state_file(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)  # ty: ignore[unresolved-attribute]
        return
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _ensure_windows_lock_byte(lock_file: BinaryIO) -> None:
    # Do not read byte 0 here: a different process may hold an exclusive Windows
    # range lock on it, and accessing the locked byte fails before LK_LOCK can wait.
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)


def _try_lock_state_file(lock_file: BinaryIO) -> bool:
    """Attempt a non-blocking exclusive lock, returning True when acquired and False when held elsewhere."""
    if os.name == "nt":
        _ensure_windows_lock_byte(lock_file)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)  # ty: ignore[unresolved-attribute]
        except OSError:
            return False
        return True
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    directory_descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
