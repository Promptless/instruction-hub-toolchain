from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime import (
    cli as host_runtime_cli,
)

from .helpers import (
    BUNDLE_LOAD_ERROR,
    HOST_RUNTIME_BIN,
    HOST_RUNTIME_PACKAGE,
    _FakeWorkerServer,
    _clean_env,
    _diagnostic_log_entries,
    _diagnostic_log_path,
    _host_state_path,
    _json_mapping,
    _json_string,
    _last_status_path,
    _run_bootstrap,
    _run_runtime_json,
    _runtime_bundle_sha256,
)


def test_host_runtime_requires_subcommand_and_reports_version(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/pig"
    runtime_path = plugin_root / "runtime" / HOST_RUNTIME_BIN
    home = tmp_path / "home"

    missing_command = subprocess.run(
        [str(runtime_path)],
        env=_clean_env(HOME=str(home), PLUGIN_ROOT=str(plugin_root)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing_command.returncode == 2
    assert "usage:" in missing_command.stderr

    for abbreviated_flag in ("--det", "--super"):
        abbreviated_collect = subprocess.run(
            [str(runtime_path), "collect", "--host", "codex", abbreviated_flag],
            env=_clean_env(HOME=str(home), PLUGIN_ROOT=str(plugin_root)),
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
        assert abbreviated_collect.returncode == 2

    payload, _ = _run_runtime_json(
        plugin_root,
        ["version", "--json"],
        {"HOME": str(home), "PLUGIN_ROOT": str(plugin_root)},
    )
    assert payload["id"] == "host-runtime"
    assert payload["name"] == HOST_RUNTIME_BIN
    assert payload["version"] == "0.2.8"
    assert payload["channel"] == "stable"
    manifest = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())
    bundle_sha256 = _runtime_bundle_sha256(plugin_root / "runtime")
    assert payload["sha256"] == manifest["managed_runtimes"][0]["sha256"] == bundle_sha256

    isolated = subprocess.run(
        [sys.executable, "-S", str(runtime_path), "version", "--json"],
        cwd=tmp_path,
        env=_clean_env(
            HOME=str(home),
            PLUGIN_ROOT=str(plugin_root),
            PYTHONPATH="",
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert isolated.returncode == 0
    assert json.loads(isolated.stdout)["sha256"] == bundle_sha256
    assert isolated.stderr == ""
    assert list((plugin_root / "runtime" / HOST_RUNTIME_PACKAGE).rglob("__pycache__")) == []
    assert list((plugin_root / "runtime" / HOST_RUNTIME_PACKAGE).rglob("*.pyc")) == []

    text_version = subprocess.run(
        [str(runtime_path), "version"],
        env=_clean_env(HOME=str(home), PLUGIN_ROOT=str(plugin_root)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert text_version.returncode == 0
    assert text_version.stdout == f"{HOST_RUNTIME_BIN} 0.2.8\n"
    assert text_version.stderr == ""

    poison_root = tmp_path / "poison-pythonpath"
    poison_package = poison_root / HOST_RUNTIME_PACKAGE
    poison_marker = tmp_path / "poison-loaded"
    poison_package.mkdir(parents=True)
    (poison_package / "__init__.py").write_text("")
    (poison_package / "cli.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['PROMPTLESS_POISON_MARKER']).write_text('loaded')\n"
        "def main():\n"
        "    return 99\n"
    )
    poisoned_pythonpath = subprocess.run(
        [str(runtime_path), "version", "--json"],
        env=_clean_env(
            HOME=str(home),
            PLUGIN_ROOT=str(plugin_root),
            PYTHONPATH=str(poison_root),
            PROMPTLESS_POISON_MARKER=str(poison_marker),
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert poisoned_pythonpath.returncode == 0
    assert json.loads(poisoned_pythonpath.stdout)["version"] == "0.2.8"
    assert poisoned_pythonpath.stderr == ""
    assert not poison_marker.exists()

    incomplete_bin = tmp_path / "incomplete-bin"
    incomplete_bin.mkdir()
    incomplete_runtime = incomplete_bin / HOST_RUNTIME_BIN
    shutil.copy2(runtime_path, incomplete_runtime)
    incomplete_bundle = subprocess.run(
        [str(incomplete_runtime), "version"],
        env=_clean_env(
            HOME=str(home),
            PLUGIN_ROOT=str(plugin_root),
            PYTHONPATH=str(poison_root),
            PROMPTLESS_POISON_MARKER=str(poison_marker),
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert incomplete_bundle.returncode == 1
    assert incomplete_bundle.stdout == ""
    assert incomplete_bundle.stderr == BUNDLE_LOAD_ERROR
    assert not poison_marker.exists()

    missing_module_bin = tmp_path / "missing-module-bin"
    shutil.copytree(plugin_root / "runtime", missing_module_bin)
    (missing_module_bin / HOST_RUNTIME_PACKAGE / "contracts.py").unlink()
    missing_module = subprocess.run(
        [str(missing_module_bin / HOST_RUNTIME_BIN), "version"],
        env=_clean_env(
            HOME=str(home),
            PLUGIN_ROOT=str(plugin_root),
            PYTHONPATH=str(poison_root),
            PROMPTLESS_POISON_MARKER=str(poison_marker),
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing_module.returncode == 1
    assert missing_module.stdout == ""
    assert missing_module.stderr == BUNDLE_LOAD_ERROR
    assert not poison_marker.exists()

    internal_import_bin = tmp_path / "internal-import-bin"
    shutil.copytree(plugin_root / "runtime", internal_import_bin)
    internal_cli = internal_import_bin / HOST_RUNTIME_PACKAGE / "cli.py"
    internal_cli.write_text(
        internal_cli.read_text().replace(
            "from __future__ import annotations\n",
            "from __future__ import annotations\n\nraise ImportError('sentinel internal import')\n",
            1,
        )
    )
    internal_import = subprocess.run(
        [str(internal_import_bin / HOST_RUNTIME_BIN), "version"],
        env=_clean_env(HOME=str(home), PLUGIN_ROOT=str(plugin_root)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert internal_import.returncode == 1
    assert internal_import.stdout == ""
    assert "ImportError: sentinel internal import" in internal_import.stderr
    assert BUNDLE_LOAD_ERROR.strip() not in internal_import.stderr


@pytest.mark.parametrize("execution_mode", ("detached", "supervised"))
def test_collector_process_creation_failure_is_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution_mode: str,
) -> None:
    home = tmp_path / "home"
    hook_input_path = tmp_path / "hook-input.json"
    hook_input_path.write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(host_runtime_cli.sys, "executable", str(tmp_path / "missing-python"))
    collector_args = ["collect", "--host", "codex", "--lifecycle", "stop", "--quiet"]

    with hook_input_path.open() as hook_input:
        monkeypatch.setattr(host_runtime_cli.sys, "stdin", hook_input)
        if execution_mode == "detached":
            return_code = host_runtime_cli._launch_detached_collect(
                "codex",
                collector_args,
                lifecycle="stop",
            )
        else:
            return_code = host_runtime_cli._supervise_collect("codex", collector_args)

    assert return_code == 1
    diagnostic = _diagnostic_log_entries(home)
    assert len(diagnostic) == 1
    assert diagnostic[0]["status"] == "error"
    assert diagnostic[0]["reason"] == "collector_process_failed"
    assert diagnostic[0]["host"] == "codex"
    assert diagnostic[0]["error_code"] == "ENOENT"
    last_status = json.loads(_last_status_path(home).read_text())
    assert last_status["reason"] == "collector_process_failed"
    assert last_status["host"] == "codex"
    assert last_status["error_code"] == "ENOENT"
    assert _diagnostic_log_path(home).stat().st_mode & 0o777 == 0o600
    assert _last_status_path(home).stat().st_mode & 0o777 == 0o600


def test_detached_session_start_persists_boundary_before_spawn_and_preserves_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root, plugin_version="1.2.3")
    plugin_root = hub_root / "dist/codex/pig"
    transcript_path = tmp_path / "session.jsonl"
    before_launch = b'{"kind":"existing"}\n'
    after_launch = b'{"kind":"appended-after-launch"}\n'
    transcript_path.write_bytes(before_launch)
    ledger_path = tmp_path / "ledger.json"
    hook_input_path = tmp_path / "hook-input.json"
    hook_input_path.write_text(json.dumps({"session_id": "session-1", "transcript_path": str(transcript_path)}))
    raw_hook_input = hook_input_path.read_bytes()
    spawned_args: list[str] = []
    spawned_stdin = b""

    def spawn_detached(args: list[str], *, stdin: BinaryIO) -> None:
        nonlocal spawned_stdin
        spawned_args.extend(args)
        spawned_stdin = stdin.read()
        transcript_path.write_bytes(before_launch + after_launch)

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("PROMPTLESS_HOST_RUNTIME_LEDGER", str(ledger_path))
    monkeypatch.setattr(host_runtime_cli, "_spawn_detached", spawn_detached)
    collector_args = ["collect", "--host", "codex", "--lifecycle", "session_start", "--quiet"]

    with hook_input_path.open() as hook_input:
        monkeypatch.setattr(host_runtime_cli.sys, "stdin", hook_input)
        return_code = host_runtime_cli._launch_detached_collect(
            "codex",
            collector_args,
            lifecycle="session_start",
        )

    assert return_code == 0
    assert spawned_stdin == raw_hook_input
    assert spawned_args.count("--release-marker-captured") == 1
    ledger = json.loads(ledger_path.read_text())
    source = next(iter(ledger["sources"].values()))
    marker = source["instruction_hub_release_markers"][0]
    assert marker["start_offset"] == len(before_launch)
    assert marker["session_id"] == "session-1"


def test_host_runtime_enroll_status_and_reset_commands(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/pig"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        enroll_payload, _ = _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        assert enroll_payload["status"] == "enrolled"
        assert enroll_payload["host"] == "codex"
        assert enroll_payload["credential_id"] == "22222222-2222-4222-8222-222222222222"
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert len(server.poll_requests) == 1
        assert server.policy_requests == []
        assert server.check_ins == []

        status_payload, _ = _run_runtime_json(
            plugin_root,
            ["status", "--host", "codex"],
            env,
        )
        assert status_payload["status"] == "ok"
        status_state = _json_mapping(status_payload["state"], "status.state")
        status_config = _json_mapping(status_payload["config"], "status.config")
        assert status_state["credential_count"] == 1
        assert status_state["pending_enrollment_count"] == 0
        assert status_config["managed_config_detected"] is False
        assert len(server.session_requests) == 1
        assert len(server.poll_requests) == 1
        assert server.policy_requests == []
        assert server.check_ins == []

        _run_bootstrap(plugin_root, "codex", env)
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert server.policy_requests == ["/v0/host-enrollment/policy?target=codex"]
        assert len(server.check_ins) == 1

        configured_status, _ = _run_runtime_json(
            plugin_root,
            ["status", "--host", "codex"],
            env,
        )
        configured_state = _json_mapping(configured_status["state"], "configured.state")
        configured_config = _json_mapping(configured_status["config"], "configured.config")
        host_instance_id = _json_string(configured_state["host_instance_id"], "host_instance_id")
        assert configured_state["credential_count"] == 1
        assert configured_state["last_seen_plugin_version"] == "0.1.0"
        assert configured_config["managed_config_detected"] is False
        assert len(server.session_requests) == 1
        assert len(server.check_ins) == 1

        reset_payload, _ = _run_runtime_json(
            plugin_root,
            ["reset", "--host", "codex", "--yes"],
            env,
        )
        assert reset_payload == {
            "credentials_removed": 1,
            "host": "codex",
            "pending_enrollments_removed": 0,
            "status": "reset",
        }
        state_after_reset = json.loads(_host_state_path(home).read_text())
        assert state_after_reset["host_instance_id"] == host_instance_id
        assert state_after_reset["last_seen_plugin_versions"] == {"codex": "0.1.0"}
        assert state_after_reset["credentials"] == {}
        assert state_after_reset["pending_enrollments"] == {}
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert len(server.check_ins) == 1

        reset_status, _ = _run_runtime_json(
            plugin_root,
            ["status", "--host", "codex"],
            env,
        )
        reset_state = _json_mapping(reset_status["state"], "reset.state")
        reset_config = _json_mapping(reset_status["config"], "reset.config")
        assert reset_state["credential_count"] == 0
        assert reset_state["last_seen_plugin_version"] == "0.1.0"
        assert reset_config["managed_config_detected"] is False
    finally:
        server.stop()
