#!/usr/bin/env python3
"""Exercise packaged and generated managed-runtime bundles at CI boundaries."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Sequence

RUNTIME_ASSET_ROOT = "promptless_instruction_hub/managed_runtime_assets"
HOST_ASSET_ROOT = f"{RUNTIME_ASSET_ROOT}/host_enrollment"
RUNTIME_EXECUTABLE = "promptless-host-runtime"
RUNTIME_PACKAGE = "promptless_host_runtime"
RUNTIME_MODULES = (
    "__init__.py",
    "cli.py",
    "contracts.py",
    "enrollment.py",
    "host_config.py",
    "metadata.py",
    "notices.py",
    "output.py",
    "redaction.py",
    "status.py",
    "storage.py",
    "traces.py",
    "validation.py",
    "worker.py",
)
EXPECTED_ARCHIVE_ASSETS = {
    f"{RUNTIME_ASSET_ROOT}/__init__.py",
    f"{HOST_ASSET_ROOT}/__init__.py",
    f"{HOST_ASSET_ROOT}/{RUNTIME_EXECUTABLE}",
    *(f"{HOST_ASSET_ROOT}/{RUNTIME_PACKAGE}/{module}" for module in RUNTIME_MODULES),
}
EXPECTED_GENERATED_BUNDLE = {
    RUNTIME_EXECUTABLE,
    *(f"{RUNTIME_PACKAGE}/{module}" for module in RUNTIME_MODULES),
}


def _run(
    command: Sequence[os.PathLike[str] | str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [os.fspath(part) for part in command],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        rendered = " ".join(os.fspath(part) for part in command)
        raise AssertionError(
            f"command failed ({result.returncode}): {rendered}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _json_output(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise AssertionError(f"expected a JSON object, got: {value!r}")
    return value


def _archive_names(artifact: Path) -> list[str]:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            return archive.namelist()
    with tarfile.open(artifact, mode="r:gz") as archive:
        return archive.getnames()


def _normalized_runtime_assets(names: Sequence[str]) -> set[str]:
    normalized: set[str] = set()
    marker = f"{RUNTIME_ASSET_ROOT}/"
    for name in names:
        marker_index = name.find(marker)
        if marker_index >= 0:
            normalized.add(name[marker_index:])
    return normalized


def _assert_artifact_contents(artifact: Path) -> None:
    names = _archive_names(artifact)
    runtime_assets = _normalized_runtime_assets(names)
    if runtime_assets != EXPECTED_ARCHIVE_ASSETS:
        missing = sorted(EXPECTED_ARCHIVE_ASSETS - runtime_assets)
        unexpected = sorted(runtime_assets - EXPECTED_ARCHIVE_ASSETS)
        raise AssertionError(
            f"managed-runtime contents differ in {artifact.name}; missing={missing}, unexpected={unexpected}"
        )
    cached = [name for name in names if "__pycache__" in name.split("/") or name.endswith(".pyc")]
    if cached:
        raise AssertionError(f"cached Python files were packaged in {artifact.name}: {cached}")


def _environment_executable(environment: Path, name: str) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / f"{name}.exe"
    return environment / "bin" / name


def _installed_version(python: Path, cwd: Path) -> str:
    result = _run(
        [
            python,
            "-c",
            "import importlib.metadata as m; print(m.version('promptless-instruction-hub'))",
        ],
        cwd=cwd,
    )
    return result.stdout.strip()


def _build_hub(toolchain: Sequence[os.PathLike[str] | str], workspace: Path) -> Path:
    hub_root = workspace / "hub"
    _run([*toolchain, "init", "--hub", hub_root, "--org", "Artifact Smoke"], cwd=workspace)
    _run([*toolchain, "build", "--hub", hub_root], cwd=workspace)
    plugin_root = hub_root / "dist" / "codex" / "core"
    if not plugin_root.is_dir():
        raise AssertionError(f"installed toolchain did not build the Codex plugin at {plugin_root}")
    return plugin_root


def _bundle_sha256(bin_root: Path) -> str:
    files = [bin_root / RUNTIME_EXECUTABLE]
    files.extend(
        path
        for path in (bin_root / RUNTIME_PACKAGE).rglob("*")
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


def _assert_generated_bundle(bin_root: Path) -> None:
    actual = {
        path.relative_to(bin_root).as_posix()
        for path in bin_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(bin_root).parts and path.suffix != ".pyc"
    }
    if actual != EXPECTED_GENERATED_BUNDLE:
        missing = sorted(EXPECTED_GENERATED_BUNDLE - actual)
        unexpected = sorted(actual - EXPECTED_GENERATED_BUNDLE)
        raise AssertionError(f"generated runtime bundle differs; missing={missing}, unexpected={unexpected}")


def _verify_generated_runtime(plugin_root: Path, runtime_python: Path, toolchain_version: str, workspace: Path) -> None:
    bin_root = plugin_root / "bin"
    runtime = bin_root / RUNTIME_EXECUTABLE
    _assert_generated_bundle(bin_root)

    manifest = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())
    records = manifest.get("managed_runtimes")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise AssertionError(f"unexpected managed-runtime manifest: {manifest!r}")
    record = records[0]
    digest = _bundle_sha256(bin_root)
    expected_record_values = {
        "id": "host-runtime",
        "status": "included",
        "target": "codex",
        "path": f"bin/{RUNTIME_EXECUTABLE}",
        "executable": RUNTIME_EXECUTABLE,
        "toolchain_version": toolchain_version,
        "sha256": digest,
    }
    for key, expected in expected_record_values.items():
        if record.get(key) != expected:
            raise AssertionError(f"manifest field {key!r}: expected {expected!r}, got {record.get(key)!r}")

    home = workspace / "home"
    home.mkdir()
    runtime_env = os.environ.copy()
    runtime_env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PLUGIN_ROOT": str(plugin_root),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        }
    )

    def runtime_json(*arguments: str) -> dict[str, Any]:
        result = _run([runtime_python, "-S", runtime, *arguments], cwd=workspace, env=runtime_env)
        return _json_output(result)

    version = runtime_json("version", "--json")
    expected_version = {
        "id": "host-runtime",
        "name": RUNTIME_EXECUTABLE,
        "version": record.get("version"),
        "channel": record.get("channel"),
        "sha256": digest,
    }
    if version != expected_version:
        raise AssertionError(f"unexpected version payload: {version!r}")

    status = runtime_json("status", "--host", "codex")
    if status.get("status") != "ok" or status.get("host") != "codex":
        raise AssertionError(f"unexpected status payload: {status!r}")
    if status.get("runtime") != expected_version:
        raise AssertionError(f"status reported different runtime metadata: {status.get('runtime')!r}")
    state = status.get("state")
    if not isinstance(state, dict) or state.get("exists") is not False:
        raise AssertionError(f"fresh isolated status unexpectedly had state: {state!r}")

    reset = runtime_json("reset", "--host", "codex", "--yes")
    expected_reset = {
        "status": "reset",
        "host": "codex",
        "credentials_removed": 0,
        "pending_enrollments_removed": 0,
    }
    if reset != expected_reset:
        raise AssertionError(f"unexpected reset payload: {reset!r}")
    status_after_reset = runtime_json("status", "--host", "codex")
    state_after_reset = status_after_reset.get("state")
    if not isinstance(state_after_reset, dict) or state_after_reset.get("exists") is not True:
        raise AssertionError(f"reset did not create valid isolated state: {state_after_reset!r}")

    cached_files = [path for path in bin_root.rglob("*") if "__pycache__" in path.parts or path.suffix == ".pyc"]
    if cached_files:
        raise AssertionError(f"Python 3.9 -S execution polluted the generated bundle: {cached_files}")


def _artifact_smoke(dist: Path, uv: Path, runtime_python: Path) -> None:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise AssertionError(f"expected exactly one wheel and one sdist in {dist}, got {wheels + sdists}")

    for artifact in (*wheels, *sdists):
        _assert_artifact_contents(artifact)
        with tempfile.TemporaryDirectory(prefix=f"runtime-{artifact.name}-") as temporary_directory:
            workspace = Path(temporary_directory)
            environment = workspace / "toolchain"
            _run([uv, "venv", "--python", sys.executable, environment], cwd=workspace)
            environment_python = _environment_executable(environment, "python")
            _run([uv, "pip", "install", "--python", environment_python, artifact.resolve()], cwd=workspace)
            toolchain = _environment_executable(environment, "pig")
            if not toolchain.is_file():
                raise AssertionError(f"artifact did not install the pig console entrypoint: {toolchain}")
            toolchain_version = _installed_version(environment_python, workspace)
            plugin_root = _build_hub([toolchain], workspace)
            _verify_generated_runtime(plugin_root, runtime_python, toolchain_version, workspace)
        print(f"validated release artifact: {artifact.name}")


def _wait_for_signal(path: Path, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"lock child exited before creating {path.name} ({process.returncode})\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for lock child to create {path}")
        time.sleep(0.02)


def _lock_child(bin_root: Path, state_path: Path, signal_a: Path, signal_b: Path, *, holder: bool) -> None:
    sys.path.insert(0, str(bin_root))
    storage = importlib.import_module(f"{RUNTIME_PACKAGE}.storage")
    state_file_lock = getattr(storage, "_state_file_lock")
    if holder:
        with state_file_lock(state_path):
            signal_a.write_text("ready\n")
            deadline = time.monotonic() + 15
            while not signal_b.exists():
                if time.monotonic() >= deadline:
                    raise AssertionError("lock holder timed out waiting for release")
                time.sleep(0.02)
        return

    started_at = time.monotonic()
    signal_a.write_text("attempting\n")
    with state_file_lock(state_path):
        elapsed = time.monotonic() - started_at
        signal_b.write_text(f"{elapsed:.6f}\n")


def _two_process_lock_smoke(bin_root: Path, runtime_python: Path, workspace: Path) -> None:
    state_path = workspace / "lock-state.json"
    holder_ready = workspace / "holder-ready"
    release_holder = workspace / "release-holder"
    waiter_attempting = workspace / "waiter-attempting"
    waiter_acquired = workspace / "waiter-acquired"
    script = Path(__file__).resolve()
    holder = subprocess.Popen(
        [
            str(runtime_python),
            "-S",
            str(script),
            "_lock-holder",
            str(bin_root),
            str(state_path),
            str(holder_ready),
            str(release_holder),
        ],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    waiter: subprocess.Popen[str] | None = None
    try:
        _wait_for_signal(holder_ready, holder, 10)
        waiter = subprocess.Popen(
            [
                str(runtime_python),
                "-S",
                str(script),
                "_lock-waiter",
                str(bin_root),
                str(state_path),
                str(waiter_attempting),
                str(waiter_acquired),
            ],
            cwd=workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_for_signal(waiter_attempting, waiter, 10)
        time.sleep(0.5)
        if waiter_acquired.exists() or waiter.poll() is not None:
            raise AssertionError("second process acquired the state lock while the first process still held it")
        release_holder.write_text("release\n")

        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        waiter_stdout, waiter_stderr = waiter.communicate(timeout=10)
        if holder.returncode != 0:
            raise AssertionError(
                f"lock holder failed ({holder.returncode})\nstdout:\n{holder_stdout}\nstderr:\n{holder_stderr}"
            )
        if waiter.returncode != 0:
            raise AssertionError(
                f"lock waiter failed ({waiter.returncode})\nstdout:\n{waiter_stdout}\nstderr:\n{waiter_stderr}"
            )
        if not waiter_acquired.exists():
            raise AssertionError("second process did not acquire the state lock after release")
        blocked_seconds = float(waiter_acquired.read_text().strip())
        if blocked_seconds < 0.35:
            raise AssertionError(
                f"second process was not observably blocked by the state lock ({blocked_seconds:.3f}s)"
            )
    finally:
        release_holder.touch()
        for process in (waiter, holder):
            if process is not None and process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)


def _platform_smoke(runtime_python: Path, *, require_windows: bool) -> None:
    if require_windows and os.name != "nt":
        raise AssertionError(f"Windows lock smoke ran on os.name={os.name!r}")
    with tempfile.TemporaryDirectory(prefix="runtime-platform-") as temporary_directory:
        workspace = Path(temporary_directory)
        toolchain_version = _installed_version(Path(sys.executable), workspace)
        plugin_root = _build_hub([sys.executable, "-m", "promptless_instruction_hub.cli"], workspace)
        _verify_generated_runtime(plugin_root, runtime_python, toolchain_version, workspace)
        _two_process_lock_smoke(plugin_root / "bin", runtime_python, workspace)
    print(f"validated generated runtime and two-process state lock on os.name={os.name}")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)

    artifacts = subcommands.add_parser("artifacts", help="test installed wheel and sdist release boundaries")
    artifacts.add_argument("--dist", type=Path, required=True)
    artifacts.add_argument("--uv", type=Path, required=True)
    artifacts.add_argument("--runtime-python", type=Path, required=True)

    platform = subcommands.add_parser("platform", help="test generated runtime startup and cross-process locking")
    platform.add_argument("--runtime-python", type=Path, required=True)
    platform.add_argument("--require-windows", action="store_true")

    for command in ("_lock-holder", "_lock-waiter"):
        child = subcommands.add_parser(command, help=argparse.SUPPRESS)
        child.add_argument("bin_root", type=Path)
        child.add_argument("state_path", type=Path)
        child.add_argument("signal_a", type=Path)
        child.add_argument("signal_b", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "artifacts":
        _artifact_smoke(args.dist.resolve(), args.uv.resolve(), args.runtime_python.resolve())
    elif args.command == "platform":
        _platform_smoke(args.runtime_python.resolve(), require_windows=args.require_windows)
    elif args.command == "_lock-holder":
        _lock_child(args.bin_root, args.state_path, args.signal_a, args.signal_b, holder=True)
    else:
        _lock_child(args.bin_root, args.state_path, args.signal_a, args.signal_b, holder=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
