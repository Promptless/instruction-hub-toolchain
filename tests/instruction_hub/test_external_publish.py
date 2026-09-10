from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.external_plugins import verify_external_plugins

from .external_helpers import (
    PLUGIN_PATH,
    UPSTREAM_URL,
    commit_upstream,
    external_definition,
    make_upstream,
    write_external,
)
from .helpers import _git, _git_output, _init_action_repo, _release_branch_path_exists, _run_action


@pytest.mark.parametrize(
    ("server", "repository", "hub_path", "plugin_path"),
    [
        ("https://github.com", "acme/hub", ".", PLUGIN_PATH),
        ("https://gitlab.example.test", "acme/team/hub", "instructions/hub", "."),
    ],
)
def test_publish_preserves_external_sources_and_bumps_pin_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: str, repository: str, hub_path: str, plugin_path: str
) -> None:
    upstream = tmp_path / "upstream"
    sha = make_upstream(upstream, monkeypatch, path=plugin_path)
    repo = _init_action_repo(
        tmp_path / "publisher", targets=("claude", "codex", "cursor", "gemini"), hub_root_name=hub_path
    )
    definition = external_definition(sha, path=plugin_path)
    write_external(repo / hub_path, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "include external plugin")
    _git(repo, "push")
    extra_env = {"GITHUB_SERVER_URL": server, "GITHUB_REPOSITORY": repository}
    result = _run_action(repo, tmp_path / "output", hub_root=hub_path, extra_env=extra_env)
    assert result.returncode == 0, result.stdout + result.stderr
    _git(repo, "fetch", "origin")
    prefix = "" if hub_path == "." else hub_path + "/"
    sources = {}
    for target, marketplace in (
        ("claude", ".claude-plugin/marketplace.json"),
        ("codex", ".agents/plugins/marketplace.json"),
    ):
        source_entries = json.loads(_git_output(repo, "show", f"origin/main:{prefix}{marketplace}"))["plugins"]
        release_entries = json.loads(_git_output(repo, "show", f"origin/release/stable:{prefix}{marketplace}"))[
            "plugins"
        ]
        assert source_entries[1] == release_entries[1]
        sources[target] = source_entries[1]["source"]
        assert sources[target] == {
            "source": "url" if plugin_path == "." else "git-subdir",
            "url": UPSTREAM_URL,
            "sha": sha,
            **({"path": plugin_path} if plugin_path != "." else {}),
        }
        assert "version" not in source_entries[1] and "author" not in source_entries[1]
        assert source_entries[0]["source"] == {
            "source": "git-subdir",
            "url": f"{server}/{repository}.git",
            "path": f"{prefix}dist/{target}/pig",
            "ref": "release/stable",
        }
    for target in ("claude", "codex", "cursor", "gemini"):
        assert not _release_branch_path_exists(repo, f"{prefix}dist/{target}/doc-detective")
    before = _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable")
    rerun = _run_action(repo, tmp_path / "output", hub_root=hub_path, extra_env=extra_env)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable") == before

    for target in ("claude", "codex"):
        manifest_path = upstream / plugin_path / f".{target}-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = "1.2.4"
        manifest_path.write_text(json.dumps(manifest))
    definition["source"]["sha"] = commit_upstream(upstream)
    write_external(repo / hub_path, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "update upstream pin")
    _git(repo, "push")
    update = _run_action(repo, tmp_path / "output", hub_root=hub_path, extra_env=extra_env)
    assert update.returncode == 0, update.stdout + update.stderr
    _git(repo, "fetch", "origin")
    release = json.loads(_git_output(repo, "show", f"origin/release/stable:{prefix}hub.release.json"))
    assert release["version"] == "0.1.1"
    assert release["version_basis"]["plugins"][1] == definition
    assert _git_output(repo, "status", "--short") == ""


@pytest.mark.parametrize("failure", ["missing-commit", "same-version", "missing-manifest"])
def test_failed_external_verification_preserves_published_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    upstream = tmp_path / "upstream"
    sha = make_upstream(upstream, monkeypatch)
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude", "codex"))
    definition = external_definition(sha)
    write_external(repo, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "include external plugin")
    _git(repo, "push")
    initial = _run_action(repo, tmp_path / "output")
    assert initial.returncode == 0, initial.stdout + initial.stderr

    if failure == "missing-commit":
        definition["source"]["sha"] = "0" * 40
    else:
        if failure == "same-version":
            (upstream / PLUGIN_PATH / "skills/example/SKILL.md").write_text("# Changed skill\n")
        else:
            (upstream / PLUGIN_PATH / ".claude-plugin/plugin.json").unlink()
        definition["source"]["sha"] = commit_upstream(upstream)
    write_external(repo, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "propose invalid upstream pin")
    _git(repo, "push")
    before = _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable")
    result = _run_action(repo, tmp_path / "output")
    assert result.returncode != 0
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable") == before
    assert _git_output(repo, "status", "--short") == ""
    if failure == "same-version":
        assert "retains upstream version 1.2.3" in result.stderr


@pytest.mark.parametrize("mode", ["build", "check"])
def test_ci_modes_fail_for_unavailable_external_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    make_upstream(tmp_path / "upstream", monkeypatch)
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude",))
    write_external(repo, external_definition("0" * 40))
    build_hub(repo)
    result = _run_action(repo, tmp_path / "output", extra_env={"INPUT_MODE": mode})
    assert result.returncode != 0
    assert "cannot fetch external plugin revision" in result.stderr


@pytest.mark.parametrize("change", ["path", "versionless", "codex-only"])
def test_previous_release_comparison_respects_host_version_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    upstream, hub, previous = tmp_path / "upstream", tmp_path / "hub", tmp_path / "previous"
    sha = make_upstream(upstream, monkeypatch)
    init_hub(hub)
    definition = external_definition(sha)
    if change == "codex-only":
        del definition["targets"]["claude"]
    write_external(hub, definition)
    build_hub(hub)
    shutil.copytree(hub, previous)
    if change == "path":
        shutil.copytree(upstream / PLUGIN_PATH, upstream / "new-plugin")
        definition["targets"]["claude"]["path"] = "new-plugin"
    elif change == "versionless":
        for target in ("claude", "codex"):
            manifest_path = upstream / PLUGIN_PATH / f".{target}-plugin/plugin.json"
            manifest = json.loads(manifest_path.read_text())
            del manifest["version"]
            manifest_path.write_text(json.dumps(manifest))
    else:
        (upstream / PLUGIN_PATH / "skills/example/SKILL.md").write_text("# Updated\n")
    definition["source"]["sha"] = commit_upstream(upstream)
    write_external(hub, definition)
    if change == "path":
        with pytest.raises(InstructionHubError, match="retains upstream version"):
            verify_external_plugins(hub, previous_release_root=previous)
    else:
        assert verify_external_plugins(hub, previous_release_root=previous)


def test_authored_to_external_migration_cannot_reuse_claude_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, hub, previous = tmp_path / "upstream", tmp_path / "hub", tmp_path / "previous"
    sha = make_upstream(upstream, monkeypatch)
    init_hub(hub, version="1.2.3")
    write_external(hub, {"id": "doc-detective", "name": "Vendored Doc Detective", "includes": []})
    build_hub(hub)
    shutil.copytree(hub, previous)
    write_external(hub, external_definition(sha))
    # A Hub version bump cannot override the upstream version used by Claude.
    config = yaml.safe_load((hub / "hub.yaml").read_text())
    config["version"] = "1.2.4"
    (hub / "hub.yaml").write_text(yaml.safe_dump(config))
    with pytest.raises(InstructionHubError, match="retains upstream version 1.2.3"):
        verify_external_plugins(hub, previous_release_root=previous)
