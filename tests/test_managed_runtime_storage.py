from __future__ import annotations

import io

from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.storage import (
    _ensure_windows_lock_byte,
)


class _UnreadableLockFile(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        raise AssertionError("Windows lock-byte initialization must not read a potentially locked byte")


def test_windows_lock_byte_initialization_does_not_read_lock_region() -> None:
    for initial_contents in (b"", b"\0"):
        lock_file = _UnreadableLockFile(initial_contents)

        _ensure_windows_lock_byte(lock_file)

        assert lock_file.getvalue() == b"\0"
        assert lock_file.tell() == 0
