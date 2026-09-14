from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.config import write_hub_version
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
        ("https://github.com", "acme/hub", "Customer Hub", PLUGIN_PATH),
        ("https://github.com", "acme/hub", "Customer Hub", "plugins/Doc Detective"),
        ("https://github.com", "acme/hub", ".", "."),
        ("https://gitlab.example.test", "acme/team/hub", "instructions/hub", PLUGIN_PATH),
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
        ("cursor", ".cursor-plugin/marketplace.json"),
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
        if target == "cursor" and server == "https://github.com":
            assert source_entries[0]["source"] == {
                "type": "github",
                "owner": "acme",
                "repo": "hub",
                "path": f"{prefix}dist/cursor/pig",
                "ref": "release/stable",
            }
        else:
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

    for target in ("claude", "codex", "cursor"):
        manifest_path = upstream / plugin_path / f".{target}-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = "1.2.4"
        manifest_path.write_text(json.dumps(manifest))
    definition["source"]["ref"] = commit_upstream(upstream)
    write_external(repo / hub_path, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "update upstream pin")
    _git(repo, "push")
    update = _run_action(repo, tmp_path / "output", hub_root=hub_path, extra_env=extra_env)
    assert update.returncode == 0, update.stdout + update.stderr
    _git(repo, "fetch", "origin")
    release = json.loads(_git_output(repo, "show", f"origin/release/stable:{prefix}hub.release.json"))
    assert release["version"] == "0.1.1"
    assert release["version_basis"]["plugins"][1] == {
        **definition,
        "source": {"type": "git", "url": UPSTREAM_URL, "sha": definition["source"]["ref"]},
    }
    for branch in ("main", "release/stable"):
        cursor = json.loads(_git_output(repo, "show", f"origin/{branch}:{prefix}.cursor-plugin/marketplace.json"))
        assert cursor["plugins"][1]["source"] == {**sources["cursor"], "sha": definition["source"]["ref"]}
    assert _git_output(repo, "status", "--short") == ""

    definition["source"]["ref"] = sha
    write_external(repo / hub_path, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "roll back upstream pin")
    _git(repo, "push")
    rollback = _run_action(repo, tmp_path / "output", hub_root=hub_path, extra_env=extra_env)
    assert rollback.returncode == 0, rollback.stdout + rollback.stderr
    _git(repo, "fetch", "origin")
    for branch in ("main", "release/stable"):
        cursor = json.loads(_git_output(repo, "show", f"origin/{branch}:{prefix}.cursor-plugin/marketplace.json"))
        assert cursor["plugins"][1]["source"] == sources["cursor"]
    release = json.loads(_git_output(repo, "show", f"origin/release/stable:{prefix}hub.release.json"))
    assert release["version"] == "0.1.2"


@pytest.mark.parametrize("failure", ["missing-commit", "same-version", "missing-manifest", "missing-cursor-manifest"])
def test_failed_external_verification_preserves_published_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    upstream = tmp_path / "upstream"
    sha = make_upstream(upstream, monkeypatch)
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude", "codex", "cursor"))
    definition = external_definition(sha)
    write_external(repo, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "include external plugin")
    _git(repo, "push")
    initial = _run_action(repo, tmp_path / "output")
    assert initial.returncode == 0, initial.stdout + initial.stderr

    if failure == "missing-commit":
        definition["source"]["ref"] = "0" * 40
    else:
        if failure == "same-version":
            (upstream / PLUGIN_PATH / "skills/example/SKILL.md").write_text("# Changed skill\n")
        else:
            target = "cursor" if failure == "missing-cursor-manifest" else "claude"
            (upstream / PLUGIN_PATH / f".{target}-plugin/plugin.json").unlink()
            if target == "cursor":
                claude_path = upstream / PLUGIN_PATH / ".claude-plugin/plugin.json"
                claude = json.loads(claude_path.read_text())
                claude["version"] = "1.2.4"
                claude_path.write_text(json.dumps(claude))
        definition["source"]["ref"] = commit_upstream(upstream)
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
    elif failure == "missing-cursor-manifest":
        assert "missing upstream manifest plugins/doc-detective/.cursor-plugin/plugin.json" in result.stderr


@pytest.mark.parametrize("mode", ["build", "check"])
def test_ci_modes_fail_for_unavailable_external_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    make_upstream(tmp_path / "upstream", monkeypatch)
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude",))
    write_external(repo, external_definition("0" * 40))
    build_hub(repo)
    result = _run_action(repo, tmp_path / "output", extra_env={"INPUT_MODE": mode})
    assert result.returncode != 0
    assert "cannot fetch external plugin revision" in result.stderr


@pytest.mark.parametrize("change", ["path", "versionless", "codex-only", "cursor-only"])
def test_previous_release_comparison_respects_host_version_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    upstream, hub, previous = tmp_path / "upstream", tmp_path / "hub", tmp_path / "previous"
    sha = make_upstream(upstream, monkeypatch)
    init_hub(hub)
    definition = external_definition(sha)
    if change.endswith("-only"):
        definition["targets"] = {change.removesuffix("-only"): {"path": PLUGIN_PATH}}
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
    definition["source"]["ref"] = commit_upstream(upstream)
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


def test_external_to_authored_publish_rejects_version_collision_until_hub_version_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = make_upstream(tmp_path / "upstream", monkeypatch)
    hub_path = "Customer Hub"
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude", "codex"), hub_root_name=hub_path)
    hub = repo / hub_path
    write_hub_version(hub, "1.2.2")
    write_external(hub, external_definition(sha))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "include external plugin")
    _git(repo, "push")
    initial = _run_action(repo, tmp_path / "output", hub_root=hub_path)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    shutil.rmtree(tmp_path / "upstream")

    write_external(hub, {"id": "doc-detective", "name": "Authored Doc Detective", "includes": []})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "replace external plugin with authored plugin")
    _git(repo, "push")
    before = _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable")
    collision = _run_action(repo, tmp_path / "output", hub_root=hub_path)
    assert collision.returncode != 0
    assert "authored replacement retains upstream version 1.2.3" in collision.stderr
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable") == before
    assert _git_output(repo, "status", "--short") == ""

    write_hub_version(hub, "1.2.4")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "avoid installed upstream version")
    _git(repo, "push")
    publish = _run_action(repo, tmp_path / "output", hub_root=hub_path)
    assert publish.returncode == 0, publish.stdout + publish.stderr
    _git(repo, "fetch", "origin")
    for target in ("claude", "codex"):
        manifest = json.loads(
            _git_output(
                repo,
                "show",
                f"origin/release/stable:{hub_path}/dist/{target}/doc-detective/.{target}-plugin/plugin.json",
            )
        )
        assert manifest["version"] == "1.2.4"
    before_rerun = _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable")
    rerun = _run_action(repo, tmp_path / "output", hub_root=hub_path)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable") == before_rerun


@pytest.mark.parametrize("latest", [False, True])
def test_external_source_can_be_replaced_after_old_repository_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, latest: bool
) -> None:
    upstream = tmp_path / "upstream"
    sha = make_upstream(upstream, monkeypatch)
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude", "codex", "cursor"))
    definition = external_definition(sha)
    if latest:
        definition["source"] = {"type": "git", "url": UPSTREAM_URL, "ref": "latest"}
    write_external(repo, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "include external plugin")
    _git(repo, "push")
    initial = _run_action(repo, tmp_path / "output")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    shutil.rmtree(upstream)

    replacement = tmp_path / "replacement"
    replacement_sha = make_upstream(replacement, monkeypatch)
    if not latest:
        definition["source"]["ref"] = replacement_sha
    definition["source"]["url"] = "https://replacement.example.test/plugins.git"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{upstream.as_posix()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", f"url.{replacement.as_posix()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", definition["source"]["url"])
    write_external(repo, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "replace unavailable upstream repository")
    _git(repo, "push")
    before = _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable")
    collision = _run_action(repo, tmp_path / "output")
    assert collision.returncode != 0
    assert "retains upstream version 1.2.3" in collision.stderr
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable") == before

    for target in ("claude", "codex", "cursor"):
        manifest_path = replacement / PLUGIN_PATH / f".{target}-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = "1.2.4"
        manifest_path.write_text(json.dumps(manifest))
    replacement_sha = commit_upstream(replacement)
    if not latest:
        definition["source"]["ref"] = replacement_sha
        write_external(repo, definition)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "avoid the previously installed version")
        _git(repo, "push")
    publish = _run_action(repo, tmp_path / "output")
    assert publish.returncode == 0, publish.stdout + publish.stderr
    _git(repo, "fetch", "origin")
    release = json.loads(_git_output(repo, "show", "origin/release/stable:hub.release.json"))
    metadata = json.loads(_git_output(repo, "show", "origin/release/stable:hub.external.json"))
    assert metadata["release_hash"] == release["release_hash"]
    assert {record["upstream_version"] for record in metadata["verified_external_plugins"]} == {"1.2.4"}
    assert {record["url"] for record in metadata["verified_external_plugins"]} == {definition["source"]["url"]}
    assert {record["sha"] for record in metadata["verified_external_plugins"]} == {replacement_sha}
    assert not (repo / "hub.external.json").exists()
    assert _git_output(repo, "status", "--short") == ""


@pytest.mark.parametrize(
    "change",
    ["auto-bump", "versionless", "codex-only", "previous-claude-disabled", "current-claude-disabled", "unselected"],
)
def test_safe_external_to_authored_migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    upstream, hub, previous = tmp_path / "upstream", tmp_path / "hub", tmp_path / "previous"
    sha = make_upstream(upstream, monkeypatch)
    if change == "versionless":
        for target in ("claude", "codex"):
            manifest_path = upstream / PLUGIN_PATH / f".{target}-plugin/plugin.json"
            manifest = json.loads(manifest_path.read_text())
            del manifest["version"]
            manifest_path.write_text(json.dumps(manifest))
        sha = commit_upstream(upstream)
    init_hub(hub, version="1.2.3" if change == "auto-bump" else "1.2.2")
    definition = external_definition(sha)
    if change == "codex-only":
        del definition["targets"]["claude"]
    write_external(hub, definition)
    config = yaml.safe_load((hub / "hub.yaml").read_text())
    config["targets"] = ["codex"] if change == "previous-claude-disabled" else ["claude", "codex"]
    (hub / "hub.yaml").write_text(yaml.safe_dump(config))
    build_hub(hub)
    shutil.copytree(hub, previous)

    write_external(hub, {"id": "doc-detective", "name": "Authored Doc Detective", "includes": []})
    config["targets"] = ["codex"] if change == "current-claude-disabled" else ["claude", "codex"]
    if change == "unselected":
        config["stable_plugins"].remove("doc-detective")
    (hub / "hub.yaml").write_text(yaml.safe_dump(config))
    if change not in {"auto-bump", "versionless"}:
        monkeypatch.setattr(
            "promptless_instruction_hub.external_plugins._fetch_revision",
            lambda *args: pytest.fail("migration without a Claude replacement fetched an upstream revision"),
        )
    assert verify_external_plugins(hub, previous_release_root=previous) == []
