from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from promptless_instruction_hub.compiler import init_hub
from promptless_instruction_hub.release.hashing import stable_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests/fixtures"
SCHEMAS = REPO_ROOT / "schemas"
WORKFLOWS = REPO_ROOT / ".github/workflows"


def _write_release_manifest_with_fresh_identity(manifest_path: Path, manifest: dict[str, Any]) -> None:
    plugin = manifest.get("plugin")
    assert isinstance(plugin, dict)
    plugin_version = plugin.get("version")
    assert isinstance(plugin_version, str)
    manifest.pop("release_id", None)
    manifest.pop("release_hash", None)
    content_hash = stable_hash(manifest)
    manifest["release_id"] = f"{plugin_version}+{content_hash[:12]}"
    manifest["release_hash"] = stable_hash(manifest)
    manifest_path.write_text(json.dumps(manifest))


def _assert_no_promptless_directory(root: Path) -> None:
    assert list(root.rglob(".promptless")) == []


def _snapshot_tree(root: Path) -> list[tuple[str, str, bytes]]:
    return [
        (
            str(path.relative_to(root)),
            "directory" if path.is_dir() else "file",
            b"" if path.is_dir() else path.read_bytes(),
        )
        for path in sorted(root.rglob("*"))
    ]


def _assert_codex_plugin_ingestion_contract(plugin_root: Path) -> None:
    manifest_path = plugin_root / ".codex-plugin/plugin.json"
    manifest_data: object = json.loads(manifest_path.read_text())
    assert isinstance(manifest_data, dict)

    for field in ("name", "version", "description"):
        _assert_non_empty_string(manifest_data.get(field), f"plugin.json {field}")
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", manifest_data["version"])

    author_data = manifest_data.get("author")
    assert isinstance(author_data, dict)
    _assert_non_empty_string(author_data.get("name"), "plugin.json author.name")

    interface_data = manifest_data.get("interface")
    assert isinstance(interface_data, dict)
    for field in ("displayName", "shortDescription", "longDescription", "developerName", "category"):
        _assert_non_empty_string(interface_data.get(field), f"plugin.json interface.{field}")

    capabilities_data = interface_data.get("capabilities")
    assert isinstance(capabilities_data, list)
    assert all(isinstance(capability, str) and capability for capability in capabilities_data)
    assert "defaultPrompt" in interface_data or "default_prompt" in interface_data

    if manifest_data.get("skills") is not None:
        assert manifest_data["skills"] == "./skills/"
        skill_files = sorted((plugin_root / "skills").glob("*/SKILL.md"))
        assert skill_files
        for skill_file in skill_files:
            skill_contents = skill_file.read_text()
            assert skill_contents.startswith("---\n")
            assert any(line == "---" for line in skill_contents.splitlines()[1:])

    if "mcpServers" not in manifest_data:
        return
    assert manifest_data["mcpServers"] == "./.mcp.json"
    mcp_data: object = json.loads((plugin_root / ".mcp.json").read_text())
    assert isinstance(mcp_data, dict)
    assert set(mcp_data) == {"mcpServers"}
    servers_data = mcp_data["mcpServers"]
    assert isinstance(servers_data, dict)
    assert all(isinstance(server_name, str) and server_name for server_name in servers_data)
    assert all(isinstance(server_config, dict) for server_config in servers_data.values())


def _assert_non_empty_string(value: object, field_path: str) -> None:
    assert isinstance(value, str), f"{field_path} must be a string"
    assert value, f"{field_path} must not be empty"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _git_output(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout


def _remote_branch_exists(cwd: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise AssertionError(result.stdout + result.stderr)


def _release_branch_path_exists(repo: Path, path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"origin/release/stable:{path}"],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _release_branch_plugin_versions(
    repo: Path,
    *,
    packages: tuple[str, ...] = ("pig",),
    targets: tuple[str, ...] = ("claude", "codex", "cursor", "gemini"),
) -> set[str]:
    manifest_names = {
        "claude": ".claude-plugin/plugin.json",
        "codex": ".codex-plugin/plugin.json",
        "cursor": ".cursor-plugin/plugin.json",
        "gemini": "gemini-extension.json",
    }
    manifest_paths = [f"dist/{target}/{package}/{manifest_names[target]}" for target in targets for package in packages]
    return {
        json.loads(_git_output(repo, "show", f"origin/release/stable:{manifest_path}"))["version"]
        for manifest_path in manifest_paths
    }


def _init_action_repo(root: Path, *, targets: tuple[str, ...], hub_root_name: str = ".") -> Path:
    remote = root / "remote.git"
    repo = root / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "--bare", str(remote))
    repo.mkdir()
    hub_root = repo if hub_root_name == "." else repo / hub_root_name
    init_hub(hub_root, org="Acme")
    _write_hub_config(hub_root, targets)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "instruction-hub@example.com")
    _git(repo, "config", "user.name", "Instruction Hub Test")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial hub")
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _write_hub_config(hub_root: Path, targets: tuple[str, ...]) -> None:
    target_lines = "\n".join(f"  - {target}" for target in targets)
    (hub_root / "hub.yaml").write_text(
        "\n".join(
            [
                "org: Acme",
                "plugin_id: acme-instruction-hub",
                "plugin_name: Acme Instruction Hub",
                "plugin_version: 0.1.0",
                "stable_packages:",
                "  - pig",
                "targets:",
                target_lines,
                "",
            ]
        )
    )


def _configure_split_package_hub(hub_root: Path, targets: tuple[str, ...]) -> None:
    target_lines = "\n".join(f"  - {target}" for target in targets)
    (hub_root / "hub.yaml").write_text(
        "\n".join(
            [
                "org: Acme",
                "plugin_id: acme-instruction-hub",
                "plugin_name: Acme Instruction Hub",
                "plugin_version: 0.1.0",
                "stable_packages:",
                "  - dev",
                "  - ops",
                "  - pig",
                "targets:",
                target_lines,
                "",
            ]
        )
    )
    (hub_root / "packages/dev.yaml").write_text("id: dev\nname: Dev\nincludes:\n  - skill:authoring-tools\n")
    (hub_root / "packages/ops.yaml").write_text("id: ops\nname: Ops\nincludes:\n  - skill:runbooks\n")
    (hub_root / "assets/skills/authoring-tools").mkdir(parents=True, exist_ok=True)
    (hub_root / "assets/skills/authoring-tools/SKILL.md").write_text("# Authoring Tools\n")
    (hub_root / "assets/skills/runbooks").mkdir(parents=True, exist_ok=True)
    (hub_root / "assets/skills/runbooks/SKILL.md").write_text("# Runbooks\n")


def _run_action(
    repo: Path,
    output_path: Path,
    *,
    hub_root: str = ".",
    release_branch: str = "release/stable",
    source_branch: str = "main",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GITHUB_ACTION_PATH": str(REPO_ROOT),
        "GITHUB_WORKSPACE": str(repo),
        "GITHUB_REPOSITORY": "Promptless/instruction-hub-test",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REF_NAME": source_branch,
        "GITHUB_REF_TYPE": "branch",
        "GITHUB_OUTPUT": str(output_path),
        "INPUT_MODE": "publish",
        "INPUT_HUB_ROOT": hub_root,
        "INPUT_RELEASE_BRANCH": release_branch,
        "INPUT_SOURCE_BRANCH": source_branch,
        "INPUT_UPDATE_CLAUDE_POINTER": "true",
    }
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/run.sh")],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
