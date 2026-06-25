from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from promptless_instruction_hub.compiler import build_hub, init_hub

BOOTSTRAP_BIN = "promptless-host-enrollment-bootstrap"


def test_build_injects_managed_bootstrap_runtime(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")

    build_hub(hub_root)

    for target in ("codex", "claude"):
        plugin_root = hub_root / "dist" / target / "core"
        bootstrap_path = plugin_root / "bin" / BOOTSTRAP_BIN
        assert bootstrap_path.exists()
        assert os.access(bootstrap_path, os.X_OK)
        hooks = json.loads((plugin_root / "hooks/hooks.json").read_text())
        hook_command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert f"--host {target}" in hook_command
        assert "--quiet" in hook_command
        metadata = json.loads((plugin_root / ".promptless/managed-runtimes.json").read_text())
        runtime = metadata["managed_runtimes"][0]
        assert runtime["id"] == "host-enrollment-bootstrap"
        assert runtime["status"] == "included"
        assert runtime["target"] == target
        assert runtime["version"] == "0.1.0"
        assert runtime["channel"] == "stable"
        assert runtime["path"] == f"bin/{BOOTSTRAP_BIN}"
        assert len(runtime["sha256"]) == 64

    for target in ("cursor", "gemini"):
        plugin_root = hub_root / "dist" / target / "core"
        assert not (plugin_root / "bin" / BOOTSTRAP_BIN).exists()
        metadata = json.loads((plugin_root / ".promptless/managed-runtimes.json").read_text())
        runtime = metadata["managed_runtimes"][0]
        assert runtime["status"] == "unsupported"
        assert runtime["target"] == target
        assert "reason" in runtime

    release_manifest = json.loads((hub_root / ".promptless/releases/current.json").read_text())
    assert len(release_manifest["managed_runtimes"]) == 4
    included = [runtime for runtime in release_manifest["managed_runtimes"] if runtime["status"] == "included"]
    assert {runtime["target"] for runtime in included} == {"codex", "claude"}


def test_bootstrap_missing_token_exits_zero_without_config_write(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    home = tmp_path / "home"

    result = subprocess.run(
        [str(hub_root / "dist/codex/core/bin" / BOOTSTRAP_BIN), "--host", "codex"],
        env={
            **os.environ,
            "HOME": str(home),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "setup_needed"
    assert not (home / ".codex/config.toml").exists()
    assert "plugin-token" not in result.stdout

    quiet_result = subprocess.run(
        [str(hub_root / "dist/codex/core/bin" / BOOTSTRAP_BIN), "--host", "codex", "--quiet"],
        env={
            **os.environ,
            "HOME": str(home),
            "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert quiet_result.returncode == 0
    assert quiet_result.stdout == ""


def test_bootstrap_configures_codex_and_claude_and_reports_metadata(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    server = _FakeWorkerServer()
    server.start()
    try:
        codex_home = tmp_path / "codex-home"
        _run_bootstrap(
            hub_root / "dist/codex/core",
            "codex",
            {
                "HOME": str(codex_home),
                "PLUGIN_ROOT": str(hub_root / "dist/codex/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )
        codex_config = (codex_home / ".codex/config.toml").read_text()
        assert "BEGIN PROMPTLESS MANAGED HOST ENROLLMENT" in codex_config
        assert 'endpoint = "http://127.0.0.1:4318/v1/logs"' in codex_config
        assert "plugin-token" not in codex_config

        claude_home = tmp_path / "claude-home"
        _run_bootstrap(
            hub_root / "dist/claude/core",
            "claude",
            {
                "HOME": str(claude_home),
                "PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "CLAUDE_PLUGIN_ROOT": str(hub_root / "dist/claude/core"),
                "PROMPTLESS_PLUGIN_ENROLLMENT_TOKEN": "plugin-token",
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            },
        )
        claude_settings = json.loads((claude_home / ".claude/settings.json").read_text())
        assert claude_settings["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"] == "http://127.0.0.1:4318/v1/logs"
        assert claude_settings["env"]["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer otlp-token"

        assert len(server.check_ins) == 2
        for check_in in server.check_ins:
            assert check_in["bootstrap_version"] == "0.1.0"
            assert check_in["bootstrap_channel"] == "stable"
            assert len(check_in["bootstrap_sha256"]) == 64
            assert check_in["toolchain_version"]
            assert check_in["plugin_id"] == "promptless-instruction-hub-core"
            assert check_in["package_id"] == "core"
            assert check_in["status"] == "needs_restart"
            assert check_in["needs_restart"] is True
            assert check_in["effective_config"]["configured"] is True
    finally:
        server.stop()


def _run_bootstrap(plugin_root: Path, host: str, env: dict[str, str]) -> None:
    result = subprocess.run(
        [str(plugin_root / "bin" / BOOTSTRAP_BIN), "--host", host],
        env={**os.environ, **env},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "plugin-token" not in result.stdout
    assert json.loads(result.stdout)["status"] == "needs_restart"


class _FakeWorkerServer:
    def __init__(self) -> None:
        self.check_ins: list[dict[str, object]] = []
        _FakeWorkerHandler.check_ins = self.check_ins
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
    check_ins: ClassVar[list[dict[str, object]]] = []

    def do_GET(self) -> None:
        if self.path != "/v0/host-enrollment/policy" or self.headers.get("Authorization") != "Bearer plugin-token":
            self.send_response(401)
            self.end_headers()
            return
        self._write_json(_signed_policy())

    def do_POST(self) -> None:
        if self.path != "/v0/host-enrollment/check-ins" or self.headers.get("Authorization") != "Bearer plugin-token":
            self.send_response(401)
            self.end_headers()
            return
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        self.check_ins.append(payload)
        self._write_json({"accepted": True, "policy_version": 1})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _signed_policy() -> dict[str, object]:
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
            "enabled_hosts": ["codex", "claude"],
            "capture_policy": {
                "user_prompts": "full_local_default",
                "tool_inputs": "full_local_default",
                "tool_outputs": "full_local_default",
                "raw_api_bodies": "disabled",
            },
            "plugin_permissions": {
                "write_user_config": True,
                "repair_user_config": True,
            },
            "required_bootstrap_version": "0.1.0",
        },
        "signature": "hmac-sha256-v1:test",
        "signed_at": now.isoformat(),
    }
