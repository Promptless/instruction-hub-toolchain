from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from promptless_instruction_hub.fs import JsonValue, validate_json_value

HOST_RUNTIME_BIN = "promptless-host-runtime"
HOST_RUNTIME_PACKAGE = "promptless_host_runtime"
HOST_STATE_REL_PATH = Path(".promptless/instruction-hub/host-enrollment-state.json")
LAST_STATUS_REL_PATH = Path(".promptless/instruction-hub/last-bootstrap-status.json")
DIAGNOSTIC_LOG_REL_PATH = Path(".promptless/instruction-hub/host-runtime-diagnostics.jsonl")
INTERNAL_WELCOME_SHOWN_AT_KEY = "internal_promptless_welcome_shown_at"
INTERNAL_WELCOME_SHOWN_BY_VERSION_KEY = "internal_promptless_welcome_shown_at_by_version"
FIRST_SUCCESS_SHOWN_KEY = "first_enrollment_success_shown_at_by_target"
FIRST_SUCCESS_ACTIVE_FRAGMENT = "telemetry is now active for"
FIRST_SUCCESS_NO_RESTART_FRAGMENT = "No restart or plugin reload is needed."
BROWSER_ENROLLMENT_MESSAGE = (
    "Promptless Instruction Governance telemetry is starting browser-based enrollment. "
    "Approve the Promptless browser tab to continue."
)
BUNDLE_LOAD_ERROR = (
    "Promptless Instruction Hub could not load its managed runtime bundle. Reinstall the Promptless plugin.\n"
)


def _host_state_path(home: Path) -> Path:
    """Return the host-global enrollment state file shared by pig installations."""
    return home / HOST_STATE_REL_PATH


def _last_status_path(home: Path) -> Path:
    """Return the last host-global bootstrap status file for debugging failed hook runs."""
    return home / LAST_STATUS_REL_PATH


def _diagnostic_log_path(home: Path) -> Path:
    """Return the redacted host-global hook diagnostic log."""
    return home / DIAGNOSTIC_LOG_REL_PATH


def _runtime_bundle_sha256(bin_root: Path) -> str:
    package_root = bin_root / HOST_RUNTIME_PACKAGE
    files = [bin_root / HOST_RUNTIME_BIN]
    files.extend(
        path
        for path in package_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(bin_root).parts and path.suffix != ".pyc"
    )

    digest = hashlib.sha256()
    for path in sorted(files, key=lambda candidate: candidate.relative_to(bin_root).as_posix()):
        relative_path = path.relative_to(bin_root).as_posix()
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _assert_no_promptless_directory(root: Path) -> None:
    assert list(root.rglob(".promptless")) == []


def _assert_hook_output(result: subprocess.CompletedProcess[str], expected: object) -> None:
    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == expected


def _assert_hook_system_message(result: subprocess.CompletedProcess[str], message: str) -> None:
    _assert_hook_output(result, {"systemMessage": message})


CODEX_SAFE_STDOUT_KEYS = frozenset({"systemMessage"})
CLAUDE_SAFE_STDOUT_KEYS = frozenset({"systemMessage", "terminalSequence"})


def _bootstrap_diagnostics(stderr: str) -> list[dict[str, JsonValue]]:
    return [
        _json_mapping(validate_json_value(json.loads(line), "bootstrap diagnostic"), "bootstrap diagnostic")
        for line in stderr.splitlines()
        if line.strip()
    ]


def _diagnostic_log_entries(home: Path) -> list[dict[str, JsonValue]]:
    return [
        _json_mapping(validate_json_value(json.loads(line), "runtime diagnostic log entry"), "runtime diagnostic log")
        for line in _diagnostic_log_path(home).read_text().splitlines()
        if line.strip()
    ]


def _parse_session_start_streams(stdout: str, stderr: str) -> dict[str, JsonValue]:
    """Assert the SessionStart hook stream split and return the final stderr diagnostic object.

    stderr carries full diagnostics (status/host/...) as JSONL; stdout carries the selected
    schema-safe systemMessage/terminalSequence object and stays empty when there is no user-facing
    message.
    """

    diagnostics = _bootstrap_diagnostics(stderr)
    assert diagnostics, "bootstrap emitted no diagnostic status"
    diagnostic = diagnostics[-1]
    stdout_text = stdout.strip()
    if stdout_text:
        control = _json_mapping(validate_json_value(json.loads(stdout_text), "bootstrap stdout"), "bootstrap stdout")
        control_source = next(
            (emitted for emitted in diagnostics if all(emitted.get(key) == value for key, value in control.items())),
            None,
        )
        assert control_source is not None, "stdout control output did not match any diagnostic"
        allowed_keys = CLAUDE_SAFE_STDOUT_KEYS if control_source.get("host") == "claude" else CODEX_SAFE_STDOUT_KEYS
        assert set(control) <= allowed_keys, f"stdout leaks non-schema keys: {sorted(set(control))}"
    else:
        for emitted in diagnostics:
            assert "systemMessage" not in emitted
            assert "terminalSequence" not in emitted
    return diagnostic


def _assert_session_start_streams(stdout: str, stderr: str, expected_status: str) -> dict[str, JsonValue]:
    """Validate the stream split and pin the diagnostic status."""

    diagnostic = _parse_session_start_streams(stdout, stderr)
    assert diagnostic["status"] == expected_status
    return diagnostic


def _run_bootstrap(
    plugin_root: Path,
    host: str,
    env: dict[str, str],
    *,
    expected_status: str = "configured",
    prepare_baseline: bool = False,
) -> tuple[dict[str, JsonValue], subprocess.CompletedProcess[str]]:
    args = [str(plugin_root / "runtime" / HOST_RUNTIME_BIN), "ensure", "--host", host]
    if prepare_baseline:
        args.append("--prepare-baseline")
    result = subprocess.run(
        args,
        env=_clean_env(**env),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "plihost_localcredential" not in result.stdout
    assert "plihost_localcredential" not in result.stderr
    assert "plihenroll_devicecode" not in result.stdout
    assert "plihenroll_devicecode" not in result.stderr
    payload = _assert_session_start_streams(result.stdout, result.stderr, expected_status)
    return payload, result


def _run_runtime_json(
    plugin_root: Path,
    args: list[str],
    env: dict[str, str],
    *,
    expected_returncode: int = 0,
) -> tuple[dict[str, JsonValue], subprocess.CompletedProcess[str]]:
    result = subprocess.run(
        [str(plugin_root / "runtime" / HOST_RUNTIME_BIN), *args],
        env=_clean_env(**env),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected_returncode
    assert "plihost_localcredential" not in result.stdout
    assert "plihost_localcredential" not in result.stderr
    assert "plihenroll_devicecode" not in result.stdout
    assert "plihenroll_devicecode" not in result.stderr
    assert result.stderr == ""
    payload = validate_json_value(json.loads(result.stdout), "runtime command stdout")
    return _json_mapping(payload, "runtime command stdout"), result


def _run_collect(
    plugin_root: Path,
    args: list[str],
    env: dict[str, str],
    stdin_payload: dict[str, JsonValue],
    *,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(plugin_root / "runtime" / HOST_RUNTIME_BIN), *args],
        env=_clean_env(**env),
        input=json.dumps(stdin_payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
    assert result.returncode == 0
    assert "plihost_localcredential" not in result.stdout
    assert "plihost_localcredential" not in result.stderr
    assert "plihenroll_devicecode" not in result.stdout
    assert "plihenroll_devicecode" not in result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    return result


def _start_bootstrap(plugin_root: Path, host: str, env: dict[str, str]) -> subprocess.Popen[str]:
    process_env = _clean_env()
    process_env.update(env)
    if "PROMPTLESS_WORKER_BASE_URL" in process_env and "PROMPTLESS_DASHBOARD_BASE_URL" not in process_env:
        process_env["PROMPTLESS_DASHBOARD_BASE_URL"] = process_env["PROMPTLESS_WORKER_BASE_URL"]
    return subprocess.Popen(
        [str(plugin_root / "runtime" / HOST_RUNTIME_BIN), "ensure", "--host", host],
        env=process_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _read_bootstrap_process(
    process: subprocess.Popen[str],
    *,
    expected_status: str = "configured",
) -> dict[str, JsonValue]:
    payload = _read_any_bootstrap_status(process)
    assert payload["status"] == expected_status
    return payload


def _read_any_bootstrap_status(process: subprocess.Popen[str]) -> dict[str, JsonValue]:
    """Drain a background bootstrap and return its emitted payload without pinning the status.

    Used for concurrent runs where which process leads enrollment (and so its terminal status)
    depends on scheduling.
    """
    try:
        stdout, stderr = process.communicate(timeout=80)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        pytest.fail(f"bootstrap timed out with stdout={stdout!r} stderr={stderr!r}")
    assert process.returncode == 0
    assert "plihost_localcredential" not in stdout
    assert "plihost_localcredential" not in stderr
    assert "plihenroll_devicecode" not in stdout
    assert "plihenroll_devicecode" not in stderr
    return _parse_session_start_streams(stdout, stderr)


def _clone_plugin_with_identity(source_plugin: Path, destination: Path, *, plugin_id: str, package_id: str) -> Path:
    """Copy a built plugin and rewrite its managed-runtime identity to simulate a second hub plugin."""
    shutil.copytree(source_plugin, destination)
    manifest_path = destination / "hub.managed-runtimes.json"
    manifest = _json_mapping(validate_json_value(json.loads(manifest_path.read_text()), "manifest"), "manifest")
    runtimes = _json_list(manifest["managed_runtimes"], "managed_runtimes")
    runtime = _json_mapping(runtimes[0], "managed_runtimes[0]")
    runtime["plugin_id"] = plugin_id
    runtime["package_id"] = package_id
    manifest_path.write_text(json.dumps(manifest))
    return destination


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES": "1",
        "PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER": "0",
    }
    env.update(overrides)
    if "PROMPTLESS_WORKER_BASE_URL" in env and "PROMPTLESS_DASHBOARD_BASE_URL" not in env:
        env["PROMPTLESS_DASHBOARD_BASE_URL"] = env["PROMPTLESS_WORKER_BASE_URL"]
    return env


def _claude_desktop_audit_path(home: Path, store_name: str, session_name: str) -> Path:
    if os.name == "nt":
        claude_base = home / "AppData/Roaming/Claude"
    elif sys.platform == "darwin":
        claude_base = home / "Library/Application Support/Claude"
    else:
        claude_base = home / ".config/Claude"
    return claude_base / store_name / session_name / "audit.jsonl"


def _write_shell_script(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _write_python_forwarder(path: Path) -> None:
    _write_shell_script(path, f'exec {shlex.quote(sys.executable)} "$@"')


def _credential_cache_key(*, worker_base_url: str, target: str) -> str:
    cache_material = {
        "deployment_instance_id": "worker-local-1",
        "target": target,
        "worker_base_url": worker_base_url,
    }
    return hashlib.sha256(json.dumps(cache_material, sort_keys=True).encode()).hexdigest()


def _async_urlopen_browser_command(path: Path) -> str:
    path.write_text(
        """
import subprocess
import sys

target_url = sys.argv[1]
subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import sys, urllib.error, urllib.request\\n"
        "try:\\n"
        "    urllib.request.urlopen(sys.argv[1], timeout=10).read()\\n"
        "except urllib.error.HTTPError:\\n"
        "    pass\\n",
        target_url,
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
""".lstrip()
    )
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))} %s"


def _json_mapping(value: JsonValue, field_path: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{field_path} must be a JSON object"
    return value


def _json_list(value: JsonValue, field_path: str) -> list[JsonValue]:
    assert isinstance(value, list), f"{field_path} must be a JSON array"
    return value


def _json_string(value: JsonValue, field_path: str) -> str:
    assert isinstance(value, str), f"{field_path} must be a JSON string"
    return value


def _json_int(value: JsonValue, field_path: str) -> int:
    assert isinstance(value, int) and not isinstance(value, bool), f"{field_path} must be a JSON integer"
    return value


def _callback_state(callback_url_value: JsonValue, field_path: str) -> str:
    callback_url = _json_string(callback_url_value, field_path)
    state_values = parse_qs(urlsplit(callback_url).query).get("state")
    assert state_values is not None and len(state_values) == 1 and state_values[0] != ""
    return state_values[0]


def _url_with_query_params(url: str, params: dict[str, JsonValue]) -> str:
    parsed = urlsplit(url)
    query_pairs: list[tuple[str, str]] = []
    for key, values in parse_qs(parsed.query, keep_blank_values=False).items():
        query_pairs.extend((key, value) for value in values)
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, str):
            query_pairs.append((key, value))
        elif isinstance(value, (int, float, bool)):
            query_pairs.append((key, str(value)))
        else:
            raise AssertionError(f"{key} must be a query scalar")
    return parsed._replace(query=urlencode(query_pairs)).geturl()


def _callback_url_with_state(callback_url: str, state: str) -> str:
    parsed = urlsplit(callback_url)
    query_pairs: list[tuple[str, str]] = []
    for key, values in parse_qs(parsed.query, keep_blank_values=False).items():
        if key == "state":
            continue
        query_pairs.extend((key, value) for value in values)
    query_pairs.append(("state", state))
    return parsed._replace(query=urlencode(query_pairs)).geturl()


def _write_native_hook_asset(
    hub_root: Path,
    hooks: dict[str, JsonValue],
    *,
    package_id: str = "pig",
) -> None:
    hooks_path = hub_root / "assets/hooks/hooks.json"
    hooks_path.write_text(json.dumps(hooks))
    (hub_root / "assets/hooks/hooks.asset.yaml").write_text(
        "\n".join(
            [
                "id: hooks",
                "type: hook",
                "support:",
                "  codex:",
                "    mode: native",
                "  claude:",
                "    mode: native",
                "  cursor:",
                "    mode: unsupported",
                "    reason: hooks are only native for Codex and Claude",
                "  gemini:",
                "    mode: unsupported",
                "    reason: hooks are only native for Codex and Claude",
                "",
            ]
        )
    )
    package_name = "PIG" if package_id == "pig" else package_id.replace("-", " ").title()
    (hub_root / f"packages/{package_id}.yaml").write_text(
        f"id: {package_id}\nname: {package_name}\nincludes:\n  - hook:hooks\n"
    )
    if package_id != "pig":
        config_path = hub_root / "hub.yaml"
        config_path.write_text(
            config_path.read_text().replace("stable_packages:\n- pig\n", f"stable_packages:\n- pig\n- {package_id}\n")
        )


def _policy_with(**policy_updates: JsonValue) -> dict[str, JsonValue]:
    payload = _json_mapping(
        validate_json_value(json.loads(json.dumps(_signed_policy())), "signed policy fixture"),
        "signed policy fixture",
    )
    policy = _json_mapping(payload["policy"], "policy")
    policy.update(policy_updates)
    return payload


def _invalid_policy(case: str) -> dict[str, JsonValue]:
    now = dt.datetime.now(dt.timezone.utc)
    payload = _policy_with()
    policy = _json_mapping(payload["policy"], "policy")

    if case == "expired":
        policy["expires_at"] = (now - dt.timedelta(minutes=1)).isoformat()
    else:
        raise AssertionError(f"unhandled invalid policy case: {case}")
    return payload


def _session_response() -> dict[str, JsonValue]:
    return {
        "session_id": "11111111-1111-4111-8111-111111111111",
        "deployment_instance_id": "worker-local-1",
        "device_code": "plihenroll_devicecode",
        "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
        "poll_interval_seconds": 1,
    }


def _approved_poll_response(**updates: JsonValue) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "status": "approved",
        "host_credential": "plihost_localcredential",
        "credential_id": "22222222-2222-4222-8222-222222222222",
        "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
    }
    payload.update(updates)
    return payload


class _FakeWorkerServer:
    def __init__(
        self,
        *,
        policy: dict[str, JsonValue] | None = None,
        poll_response: dict[str, JsonValue] | None = None,
        post_response: dict[str, JsonValue] | None = None,
        session_response: dict[str, JsonValue] | None = None,
        session_barrier_count: int = 0,
        callback_state_override: str | None = None,
        pending_approval_url_override: str | None = None,
        pending_approval_path: str = "/instruction-hub/enroll",
        unparsed_record_count: int = 0,
    ) -> None:
        self.check_ins: list[dict[str, JsonValue]] = []
        self.policy_requests: list[str] = []
        self.poll_requests: list[dict[str, JsonValue]] = []
        self.session_requests: list[dict[str, JsonValue]] = []
        self.trace_batches: list[dict[str, JsonValue]] = []
        self._session_condition = threading.Condition()
        _FakeWorkerHandler.check_ins = self.check_ins
        _FakeWorkerHandler.policy_requests = self.policy_requests
        _FakeWorkerHandler.poll_requests = self.poll_requests
        _FakeWorkerHandler.session_requests = self.session_requests
        _FakeWorkerHandler.trace_batches = self.trace_batches
        _FakeWorkerHandler.policy_response = policy or _signed_policy()
        _FakeWorkerHandler.poll_response = poll_response
        _FakeWorkerHandler.post_response = post_response
        _FakeWorkerHandler.session_response = session_response
        _FakeWorkerHandler.session_barrier_count = session_barrier_count
        _FakeWorkerHandler.session_condition = self._session_condition
        _FakeWorkerHandler.callback_state_override = callback_state_override
        _FakeWorkerHandler.pending_approval_url_override = pending_approval_url_override
        _FakeWorkerHandler.pending_approval_path = pending_approval_path
        _FakeWorkerHandler.unparsed_record_count = unparsed_record_count
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWorkerHandler)
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _FakeWorkerHandler(BaseHTTPRequestHandler):
    check_ins: ClassVar[list[dict[str, JsonValue]]] = []
    policy_requests: ClassVar[list[str]] = []
    poll_requests: ClassVar[list[dict[str, JsonValue]]] = []
    trace_batches: ClassVar[list[dict[str, JsonValue]]] = []
    policy_response: ClassVar[dict[str, JsonValue]]
    poll_response: ClassVar[dict[str, JsonValue] | None]
    post_response: ClassVar[dict[str, JsonValue] | None]
    session_response: ClassVar[dict[str, JsonValue] | None]
    session_barrier_count: ClassVar[int] = 0
    session_condition: ClassVar[threading.Condition | None] = None
    session_requests: ClassVar[list[dict[str, JsonValue]]] = []
    callback_state_override: ClassVar[str | None] = None
    pending_approval_url_override: ClassVar[str | None] = None
    pending_approval_path: ClassVar[str] = "/instruction-hub/enroll"
    unparsed_record_count: ClassVar[int] = 0

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            self._write_json(
                {
                    "status": "ok",
                    "deployment_instance_id": "worker-local-1",
                    "worker_version": "0.1.0-test",
                }
            )
            return
        if parsed.path == "/instruction-hub/enroll/start":
            payload = self._single_value_query_payload(parsed.query)
            callback_url = _json_string(payload.get("callback_url"), "callback_url")
            if callback_url is None:
                self.send_response(400)
                self.end_headers()
                return
            self._record_session_request(payload)
            session_response = self._session_response_payload()
            approval_params = {"callback_url": callback_url, **session_response}
            hosted_approval_url = self.pending_approval_url_override or (
                f"{self._base_url()}{self.pending_approval_path}?{urlencode(approval_params)}"
            )
            if payload.get("pending_callback") == "1":
                pending_params = {
                    "status": "pending",
                    "approval_url": hosted_approval_url,
                    **session_response,
                }
                self._redirect(_url_with_query_params(callback_url, pending_params))
                return
            self._redirect(hosted_approval_url)
            return
        if parsed.path == "/instruction-hub/enroll":
            payload = self._single_value_query_payload(parsed.query)
            callback_url = _json_string(payload.pop("callback_url", None), "callback_url")
            if callback_url is None:
                self.send_response(400)
                self.end_headers()
                return
            if self.callback_state_override is not None:
                callback_url = _callback_url_with_state(callback_url, self.callback_state_override)
            self._redirect(_url_with_query_params(callback_url, {"status": "approved", **payload}))
            return
        target = parse_qs(parsed.query).get("target")
        if (
            parsed.path != "/v0/host-enrollment/policy"
            or target not in (["codex"], ["claude"], ["claude-desktop"])
            or self.headers.get("Authorization") != "Bearer plihost_localcredential"
        ):
            self.send_response(401)
            self.end_headers()
            return
        self.policy_requests.append(self.path)
        self._write_json(self.policy_response)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if self.path == "/v1/instruction-hub/host-enrollments/sessions/11111111-1111-4111-8111-111111111111/poll":
            payload = self._read_json_request("session poll request")
            if payload.get("device_code") != "plihenroll_devicecode":
                self.send_response(401)
                self.end_headers()
                return
            self.poll_requests.append(payload)
            self._write_json(dict(self.poll_response or _approved_poll_response()))
            return
        if parsed.path == "/v0/traces/batches":
            target = parse_qs(parsed.query).get("target")
            if (
                target not in (["codex"], ["claude"], ["claude-desktop"])
                or self.headers.get("Authorization") != "Bearer plihost_localcredential"
            ):
                self.send_response(401)
                self.end_headers()
                return
            payload = self._read_json_request("trace batch request")
            self.trace_batches.append(payload)
            chunks = _json_list(payload["chunks"], "trace batch chunks")
            acknowledged_ranges: list[dict[str, JsonValue]] = []
            raw_artifact_count = 0
            skipped_record_count = 0
            for chunk_value in chunks:
                chunk = _json_mapping(chunk_value, "trace batch chunk")
                if chunk["kind"] == "jsonl_range":
                    raw_artifact_count += 1
                elif chunk["kind"] == "oversized_record":
                    skipped_record_count += 1
                acknowledged_ranges.append(
                    {
                        "kind": chunk["kind"],
                        "source_path_hash": chunk["source_path_hash"],
                        "start_offset": chunk["start_offset"],
                        "end_offset": chunk["end_offset"],
                        "content_sha256": chunk["content_sha256"],
                    }
                )
            self._write_json(
                {
                    "accepted": True,
                    "batch_id": payload["batch_id"],
                    "policy_version": payload["policy_version"],
                    "raw_artifact_count": raw_artifact_count,
                    "skipped_record_count": skipped_record_count,
                    "acknowledged_ranges": acknowledged_ranges,
                    "trace_count": raw_artifact_count,
                    "event_count": raw_artifact_count,
                    "unparsed_record_count": self.unparsed_record_count,
                }
            )
            return
        if (
            self.path != "/v0/host-enrollment/check-ins"
            or self.headers.get("Authorization") != "Bearer plihost_localcredential"
        ):
            self.send_response(401)
            self.end_headers()
            return
        payload = self._read_json_request("check-in request")
        self.check_ins.append(payload)
        self._write_json(self.post_response or {"accepted": True, "policy_version": 1})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write_json(self, payload: dict[str, JsonValue], *, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def _session_response_payload(self) -> dict[str, JsonValue]:
        payload = dict(self.session_response or _session_response())
        payload.setdefault(
            "poll_url",
            f"{self._base_url()}/v1/instruction-hub/host-enrollments/sessions/11111111-1111-4111-8111-111111111111/poll",
        )
        return payload

    def _single_value_query_payload(self, query: str) -> dict[str, JsonValue]:
        parsed_query = parse_qs(query, keep_blank_values=False)
        payload: dict[str, JsonValue] = {}
        for key, values in parsed_query.items():
            if len(values) == 1:
                payload[key] = values[0]
        return payload

    def _read_json_request(self, label: str) -> dict[str, JsonValue]:
        length = int(self.headers["Content-Length"])
        return _json_mapping(
            validate_json_value(json.loads(self.rfile.read(length)), label),
            label,
        )

    def _record_session_request(self, payload: dict[str, JsonValue]) -> None:
        condition = self.session_condition
        if condition is None or self.session_barrier_count <= 1:
            self.session_requests.append(payload)
            return
        with condition:
            self.session_requests.append(payload)
            if len(self.session_requests) >= self.session_barrier_count:
                condition.notify_all()
                return
            condition.wait_for(lambda: len(self.session_requests) >= self.session_barrier_count, timeout=10)


def _signed_policy(*, enabled_hosts: list[str] | None = None) -> dict[str, JsonValue]:
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "policy": {
            "schema_version": 1,
            "org_id": "org_test",
            "deployment_id": "worker-local-1",
            "policy_version": 1,
            "issued_at": now.isoformat(),
            "expires_at": (now + dt.timedelta(days=7)).isoformat(),
            "collector": {
                "otlp_http_logs_endpoint": "http://127.0.0.1:4318/v1/logs",
                "otlp_http_traces_endpoint": "http://127.0.0.1:4318/v1/traces",
                "otlp_http_metrics_endpoint": "http://127.0.0.1:4318/v1/metrics",
                "otlp_grpc_endpoint": "http://127.0.0.1:4317",
                "headers": {"Authorization": "Bearer otlp-token"},
                "tls": None,
            },
            "enabled_hosts": enabled_hosts or ["codex", "claude"],
            "required_bootstrap_version": "0.2.0",
        },
        "signature": "hmac-sha256-v1:test",
        "signed_at": now.isoformat(),
    }
