"""Standalone Python 3.9+ hook adapter, copied into generated plugins.

Scripts receive normalized JSON on stdin and emit {"context": "..."} or nothing.
They run in this process so cancellation signals reach their handlers directly.
Keep this module standard-library-only, without package-relative imports.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
import sys

EVENTS = {
    "claude": {"SessionStart": "session_start", "PostToolUse": "tool_result", "PostToolUseFailure": "tool_result"},
    "codex": {"SessionStart": "session_start", "PostToolUse": "tool_result"},
    "cursor": {"sessionStart": "session_start", "postToolUse": "tool_result", "postToolUseFailure": "tool_result"},
}
SHELL_TOOLS = {"claude": {"Bash"}, "codex": {"Bash", "exec_command", "shell_command", "shell"}, "cursor": {"Shell"}}


def normalize_event(host: str, event: dict) -> dict:
    """Normalize known envelopes without inferring missing process exit statuses."""
    name = event.get("hook_event_name")
    if name not in EVENTS[host]:
        raise ValueError("unsupported native hook event")
    cwd = event.get("cwd")
    if not cwd and host == "cursor":
        roots = event.get("workspace_roots", [])
        cwd = roots[0] if isinstance(roots, list) and roots else None
    result = {"event": EVENTS[host][name], "cwd": cwd if isinstance(cwd, str) else None, "shell": None}
    if result["event"] == "tool_result" and event.get("tool_name") in SHELL_TOOLS[host]:
        result["shell"] = _shell_result(host, event)
    return result


def _shell_result(host: str, event: dict) -> dict:
    tool_input = event.get("tool_input")
    command = tool_input.get("command", tool_input.get("cmd")) if isinstance(tool_input, dict) else None
    response = event.get("tool_response")
    if response is None:
        response = event.get("tool_output")
    # Cursor documents tool_output as a serialized response envelope. Codex's
    # string is ordinary command output, even when it happens to contain JSON.
    if host == "cursor" and isinstance(response, str):
        try:
            envelope = json.loads(response)
        except ValueError:
            pass
        else:
            if isinstance(envelope, dict) and any(
                key in envelope for key in ("exit_code", "exitCode", "exitCodeNumber", "stdout", "stderr", "output")
            ):
                response = envelope
    error = event.get("error", event.get("error_message"))
    code = None
    status = "unknown" if host == "codex" else "success"
    output = ""
    if isinstance(response, dict):
        code = response.get("exit_code", response.get("exitCode", response.get("exitCodeNumber")))
        output = "\n".join(
            value for key in ("stdout", "stderr", "output") if isinstance(value := response.get(key), str)
        )
    elif isinstance(response, str):
        output = response
    if not isinstance(code, int) or isinstance(code, bool):
        code = None
    if code is not None:
        status = "success" if code == 0 else "failed"
    if isinstance(error, str):
        status, output = "failed", error
    if event.get("hook_event_name") in ("PostToolUseFailure", "postToolUseFailure"):
        status = "failed"
    if isinstance(response, dict) and response.get("session_id") is not None and code is None:
        status = "running"
    if event.get("is_interrupt") or isinstance(response, dict) and response.get("interrupted"):
        status = "cancelled"
    if event.get("failure_type") in ("timeout", "permission_denied", "cancelled"):
        status = event["failure_type"]
    return {
        "command": command if isinstance(command, str) else None,
        "output": output,
        "exit_code": code,
        "status": status,
    }


def feedback(host: str, event: dict, context: str) -> dict:
    if host == "cursor":
        return {"additional_context": context}
    return {"hookSpecificOutput": {"hookEventName": event["hook_event_name"], "additionalContext": context}}


def execute(entrypoint: Path, event: dict) -> str | None:
    """Run an ordinary script in-process with a host-independent stdin/stdout contract."""
    original_stdin, original_argv = sys.stdin, sys.argv
    original_path = sys.path[:]
    output = io.StringIO()
    try:
        sys.stdin = io.StringIO(json.dumps(event))
        sys.argv = [str(entrypoint)]
        sys.path.insert(0, str(entrypoint.parent))
        with redirect_stdout(output):
            try:
                runpy.run_path(str(entrypoint), run_name="__main__")
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    raise
    finally:
        sys.stdin, sys.argv = original_stdin, original_argv
        sys.path[:] = original_path
    if not output.getvalue().strip():
        return None
    response = json.loads(output.getvalue())
    if not isinstance(response, dict) or set(response) - {"context"}:
        raise ValueError("hook output must be an object containing only context")
    context = response.get("context")
    if context is not None and not isinstance(context, str):
        raise ValueError("hook context must be a string")
    return context


def main() -> int:
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", choices=tuple(EVENTS), required=True)
    parser.add_argument("--entrypoint", type=Path, required=True)
    args = parser.parse_args()
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("hook input must be an object")
        context = execute(args.entrypoint, normalize_event(args.host, event))
        if context:
            print(json.dumps(feedback(args.host, event, context)))
        return 0
    except (OSError, ValueError) as exc:
        print(f"Instruction Hub hook could not run: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
