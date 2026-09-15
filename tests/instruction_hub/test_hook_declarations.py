from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub, validate_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import read_yaml_mapping, write_yaml
from promptless_instruction_hub.hook_runtime import normalize_event
from tests.config_helpers import enable_trace_ingestion


def declaration(root: Path) -> Path:
    init_hub(root)
    bundle = root / "assets/hooks/example"
    bundle.mkdir(parents=True)
    metadata = {
        "support": {host: {"mode": "native"} for host in ("claude", "codex", "cursor")},
        "hook": {
            "entrypoint": "handler's $example.py",
            "timeout": 75,
            "status_message": "Example hook",
            "bindings": {
                "claude": [{"event": "PostToolUseFailure", "matcher": "Bash"}],
                "codex": [{"event": "PostToolUse", "matcher": "Bash"}],
                "cursor": [{"event": "postToolUseFailure", "matcher": "Shell"}],
            },
        },
    }
    write_yaml(bundle / "asset.yaml", metadata)
    (bundle / "handler's $example.py").write_text(
        "import json, sys\nfrom helper import echo\n"
        "assert len(sys.argv) == 1\nprint(json.dumps({'context': echo(json.load(sys.stdin))}))\n"
    )
    (bundle / "helper.py").write_text("import json\ndef echo(event): return json.dumps(event)\n")
    (root / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes: [hook:example]\n")
    return bundle / "asset.yaml"


def event(host: str) -> dict:
    data = {"cwd": "/workspace/nested", "tool_input": {"command": "python helper.py"}}
    if host == "claude":
        data.update(hook_event_name="PostToolUseFailure", tool_name="Bash", error="Exit code 1\nerror")
    elif host == "codex":
        # Actual Codex 0.153 envelope: raw shell output without an exit-code field.
        data.update(hook_event_name="PostToolUse", tool_name="Bash", tool_response="error")
    else:
        data.update(
            hook_event_name="postToolUseFailure", tool_name="Shell", error_message="error", failure_type="error"
        )
    return data


@pytest.mark.parametrize(
    "host,root_var", [("claude", "CLAUDE_PLUGIN_ROOT"), ("codex", "PLUGIN_ROOT"), ("cursor", "CURSOR_PLUGIN_ROOT")]
)
def test_generated_launcher_executes_packaged_adapter_and_script(tmp_path: Path, host: str, root_var: str) -> None:
    root = tmp_path / "hub with $spaces"
    declaration(root)
    enable_trace_ingestion(root)
    build_hub(root)
    plugin = root / "dist" / host / "pig"
    hooks = json.loads((plugin / "hooks/hooks.json").read_text())
    native_event = event(host)
    handler = hooks["hooks"][native_event["hook_event_name"]][0]
    action = handler if host == "cursor" else handler["hooks"][0]
    assert action["timeout"] == 75
    assert (plugin / "hooks/_instruction_hub_hook.py").is_file()
    assert not (plugin / "hooks/example/asset.yaml").exists()
    if host != "cursor":
        assert "SessionStart" in hooks["hooks"]  # Managed hooks coexist with the adapter.
    assert not (root / "dist/gemini/pig/hooks/_instruction_hub_hook.py").exists()
    result = subprocess.run(
        ["/bin/sh", "-c", action["command"]],
        input=json.dumps(native_event),
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, root_var: str(plugin), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    response = json.loads(result.stdout)
    if host == "cursor":
        normalized = json.loads(response["additional_context"])
    else:
        assert response["hookSpecificOutput"]["hookEventName"] == native_event["hook_event_name"]
        normalized = json.loads(response["hookSpecificOutput"]["additionalContext"])
    assert normalized["cwd"] == "/workspace/nested"
    assert normalized["shell"]["command"] == "python helper.py"
    assert normalized["shell"]["output"].endswith("error")
    assert normalized["shell"]["status"] == ("unknown" if host == "codex" else "failed")
    # Missing Python reports a diagnostic without attempting installation.
    result = subprocess.run(
        ["/bin/sh", "-c", action["command"]],
        input="{}",
        capture_output=True,
        text=True,
        env={root_var: str(plugin), "PATH": str(tmp_path / "missing-bin")},
    )
    assert result.returncode == 0 and "require Python 3.9" in result.stderr


@pytest.mark.parametrize(
    "field,value",
    [
        ("entrypoint", "../outside.py"),
        ("entrypoint", "/outside.py"),
        ("entrypoint", "missing.py"),
        ("timeout", 0),
        ("timeout", True),
        ("timeout", "75"),
        ("bindings", {"codex": [{"event": "PostToolUse"}]}),
        ("bindings", {"claude": [], "codex": [], "cursor": []}),
        ("bindings", {"claude": [{"event": "Bogus"}], "codex": [{"event": "Bogus"}], "cursor": [{"event": "Bogus"}]}),
        ("typo", "unknown setting"),
    ],
)
def test_invalid_declarations_fail_validation(tmp_path: Path, field: str, value: object) -> None:
    path = declaration(tmp_path)
    data = read_yaml_mapping(path)
    assert isinstance(data["hook"], dict)
    data["hook"][field] = value
    write_yaml(path, data)
    with pytest.raises((InstructionHubError, ValueError), match="hook|entrypoint|binding|timeout|adapter|typo"):
        validate_hub(tmp_path)


def test_native_json_is_an_alternative_not_an_ambiguous_override(tmp_path: Path) -> None:
    path = declaration(tmp_path)
    (path.parent / "hooks.json").write_text('{"hooks": {}}')
    with pytest.raises(InstructionHubError, match="not both"):
        validate_hub(tmp_path)


def test_declaration_changes_affect_asset_hash_and_release(tmp_path: Path) -> None:
    path = declaration(tmp_path)
    first = validate_hub(tmp_path).assets["hook:example"].content_hash
    release = build_hub(tmp_path).release_hash
    data = read_yaml_mapping(path)
    assert isinstance(data["hook"], dict)
    data["hook"]["timeout"] = 90
    write_yaml(path, data)
    assert validate_hub(tmp_path).assets["hook:example"].content_hash != first
    assert build_hub(tmp_path).release_hash != release


@pytest.mark.parametrize("host", ["claude", "codex", "cursor"])
def test_normalized_statuses_preserve_process_outcomes(host: str) -> None:
    raw = event(host)
    raw.pop("error", None)
    raw.pop("error_message", None)
    raw["hook_event_name"] = "postToolUse" if host == "cursor" else "PostToolUse"
    for code, status in [(0, "success"), (1, "failed")]:
        result = (
            {"exitCode": code, "stdout": "text from a file"}
            if host == "cursor"
            else {"exit_code": code, "output": "text from a file"}
        )
        raw["tool_response"] = json.dumps(result) if host == "cursor" else result
        normalized = normalize_event(host, raw)["shell"]
        assert normalized["status"] == status and normalized["exit_code"] == code
    raw["is_interrupt"] = True
    assert normalize_event(host, raw)["shell"]["status"] == "cancelled"
    raw["tool_name"] = "mcp__remote__execute"
    assert normalize_event(host, raw)["shell"] is None


def test_cursor_cwd_fallback_and_codex_running_result() -> None:
    raw = {"hook_event_name": "sessionStart", "workspace_roots": ["/first", "/second"]}
    assert normalize_event("cursor", raw)["cwd"] == "/first"
    raw["cwd"] = "/reported"
    assert normalize_event("cursor", raw)["cwd"] == "/reported"
    codex = event("codex")
    codex["tool_response"] = {"session_id": 123, "output": "still running"}
    assert normalize_event("codex", codex)["shell"]["status"] == "running"
    codex["tool_response"] = "Process exit code: 0\nerror example"
    assert normalize_event("codex", codex)["shell"]["status"] == "success"
    codex["tool_response"] = "Process exited with code 255\nerror"
    assert normalize_event("codex", codex)["shell"]["exit_code"] == 255


def test_runner_rejects_bad_output_and_preserves_failure_and_silence(tmp_path: Path) -> None:
    from promptless_instruction_hub import hook_runtime

    script = tmp_path / "run.py"
    command = [sys.executable, str(Path(hook_runtime.__file__)), "--host", "codex", "--entrypoint", str(script)]
    for source, code, diagnostic in [
        ("raise SystemExit(7)", 7, ""),
        ("print('unexpected stdout')", 1, "could not run"),
        ("pass", 0, ""),
    ]:
        script.write_text(source)
        result = subprocess.run(command, input=json.dumps(event("codex")), text=True, capture_output=True)
        assert result.returncode == code
        assert not result.stdout
        assert diagnostic in result.stderr


def test_runner_delivers_cancellation_directly_to_hook(tmp_path: Path) -> None:
    import signal
    import time
    from promptless_instruction_hub import hook_runtime

    ready = tmp_path / "ready"
    script = tmp_path / "cancel.py"
    script.write_text(
        "import json, os, signal, time\nfrom pathlib import Path\n"
        "def cancel(*args):\n    print(json.dumps({'context': 'cancelled'}))\n    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, cancel)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "while True: time.sleep(0.05)\n"
    )
    payload = tmp_path / "input.json"
    payload.write_text(json.dumps(event("codex")))
    with payload.open() as stdin:
        process = subprocess.Popen(
            [sys.executable, str(Path(hook_runtime.__file__)), "--host", "codex", "--entrypoint", str(script)],
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert int(ready.read_text()) == process.pid
        process.send_signal(signal.SIGTERM)
        output, error = process.communicate(timeout=5)
        assert process.returncode == 0 and not error
        assert json.loads(output)["hookSpecificOutput"]["additionalContext"] == "cancelled"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
