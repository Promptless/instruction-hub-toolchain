from __future__ import annotations

import socket
import urllib.error
import urllib.request
from typing import NoReturn

import pytest

from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.contracts import (
    HTTP_TIMEOUT_SECONDS,
    BootstrapError,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.output import (
    _enrollment_user_message,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.worker import (
    _get_json,
    _post_json_response,
)


def test_get_json_reports_actionable_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_read_timeout(_request: urllib.request.Request, *, timeout: int) -> NoReturn:
        assert timeout == HTTP_TIMEOUT_SECONDS
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(urllib.request, "urlopen", raise_read_timeout)

    with pytest.raises(
        BootstrapError,
        match=(
            rf"Promptless worker did not respond within {HTTP_TIMEOUT_SECONDS} seconds while waiting for the "
            r"policy response\. Retry shortly; if it persists, check the worker's health and workload\."
        ),
    ):
        _get_json("https://pig.example.test/v0/host-enrollment/policy", "credential", label="policy response")


def test_post_json_reports_actionable_wrapped_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_wrapped_timeout(_request: urllib.request.Request, *, timeout: int) -> NoReturn:
        assert timeout == HTTP_TIMEOUT_SECONDS
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", raise_wrapped_timeout)

    with pytest.raises(
        BootstrapError,
        match=(
            rf"Promptless worker did not respond within {HTTP_TIMEOUT_SECONDS} seconds while waiting for the "
            r"check-in response\. Retry shortly; if it persists, check the worker's health and workload\."
        ),
    ):
        _post_json_response(
            "https://pig.example.test/v0/host-enrollment/check-ins",
            "credential",
            {"status": "configured"},
            label="check-in response",
        )


def test_timeout_message_identifies_host_operation_and_next_step() -> None:
    error_message = (
        f"Promptless worker did not respond within {HTTP_TIMEOUT_SECONDS} seconds while waiting for the policy response. "
        "Retry shortly; if it persists, check the worker's health and workload."
    )

    message = _enrollment_user_message(
        {
            "status": "error",
            "host": "claude",
            "message": error_message,
        }
    )

    assert message == f"Promptless host enrollment failed for Claude Code: {error_message}"
