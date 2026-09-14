from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from promptless_instruction_hub.cli import main
from promptless_instruction_hub.compiler import build_hub, init_hub, verify_hub
from promptless_instruction_hub.config import EXTERNAL_LOCK_PATH
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.external_lock import ExternalPluginLock, apply_external_resolutions
from promptless_instruction_hub.external_plugins import resolve_external_plugins, verify_external_plugins
from promptless_instruction_hub.models import (
    ExternalPluginDefinition,
    ResolvedExternalPluginDefinition,
    ResolvedGitSource,
)
from promptless_instruction_hub.release.versions import read_release_manifest, resolve_publish_version
from promptless_instruction_hub.render.external import external_marketplace_entry, validate_external_marketplace_source
from promptless_instruction_hub.validate.hub import validate_hub

from .external_helpers import (
    PLUGIN_PATH,
    UPSTREAM_URL,
    commit_upstream,
    external_definition,
    make_upstream,
    write_external,
)
from .helpers import (
    _git,
    _git_output,
    _init_action_repo,
    _run_action,
    _snapshot_tree,
    _write_release_manifest_with_fresh_identity,
)


def latest_definition(*, path: str = PLUGIN_PATH) -> dict[str, Any]:
    definition = external_definition(path=path)
    definition["source"] = {"type": "git", "url": UPSTREAM_URL, "ref": "latest"}
    return definition


def advance_upstream(upstream: Path, *, version: str = "1.2.4", path: str = PLUGIN_PATH) -> str:
    for target in ("claude", "codex", "cursor"):
        manifest = upstream / path / f".{target}-plugin/plugin.json"
        data = json.loads(manifest.read_text())
        data["version"] = version
        manifest.write_text(json.dumps(data))
    return commit_upstream(upstream)


@pytest.mark.parametrize("path", [".", PLUGIN_PATH])
def test_latest_resolves_once_and_builds_the_verified_pin_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    upstream, hub = tmp_path / "upstream", tmp_path / "hub"
    sha = make_upstream(upstream, monkeypatch, path=path)
    _git(upstream, "branch", "-m", "trunk")
    init_hub(hub)
    definition = latest_definition(path=path)
    write_external(hub, definition)
    validate_hub(hub)
    for operation in (build_hub, verify_hub, verify_external_plugins, resolve_publish_version):
        with pytest.raises(InstructionHubError, match="pig resolve-external"):
            operation(hub)

    # All three targets share the same fetch and verified Git objects.
    run = subprocess.run
    fetches = []

    def record_fetch(*args, **kwargs):
        if "fetch" in args[0]:
            fetches.append(args[0])
        return run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", record_fetch)
    assert main(["resolve-external", "--hub", str(hub)]) == 0
    records = json.loads(capsys.readouterr().out)["verified_external_plugins"]
    assert {record["sha"] for record in records} == {sha}
    assert {record["target"] for record in records} == {"claude", "codex", "cursor"}
    assert len(fetches) == 1 and fetches[0][-1] == "HEAD"
    assert yaml.safe_load((hub / "plugins/doc-detective.yaml").read_text()) == definition
    assert json.loads((hub / EXTERNAL_LOCK_PATH).read_text()) == {
        "schema_version": 1,
        "plugins": {"doc-detective": {"type": "git", "url": UPSTREAM_URL, "sha": sha}},
    }

    advance_upstream(upstream, path=path)
    assert {record["sha"] for record in verify_external_plugins(hub)} == {sha}
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("offline build fetched upstream"))
    result = build_hub(hub)
    before = _snapshot_tree(hub)
    assert verify_hub(hub).release_hash == result.release_hash
    assert build_hub(hub, check=True).checked
    assert _snapshot_tree(hub) == before
    for marketplace in (".claude-plugin", ".agents/plugins", ".cursor-plugin"):
        source = json.loads((hub / marketplace / "marketplace.json").read_text())["plugins"][1]["source"]
        assert source["sha"] == sha and "ref" not in source
    _, basis = read_release_manifest(hub / "hub.release.json")
    assert basis["plugins"][1]["source"] == {"type": "git", "url": UPSTREAM_URL, "sha": sha}


@pytest.mark.parametrize(
    "mutation", ["wrong-url", "missing-plugin", "bad-sha", "floating", "ref-only", "both-fields", "bad-schema"]
)
def test_offline_build_rejects_stale_or_invalid_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    make_upstream(tmp_path / "upstream", monkeypatch)
    hub = init_hub(tmp_path / "hub")
    write_external(hub, latest_definition())
    resolve_external_plugins(hub)
    lock = json.loads((hub / EXTERNAL_LOCK_PATH).read_text())
    source = lock["plugins"]["doc-detective"]
    if mutation == "wrong-url":
        source["url"] = "https://different.example.test/repo.git"
    elif mutation == "missing-plugin":
        lock["plugins"] = {}
    elif mutation == "bad-sha":
        source["sha"] = "main"
    elif mutation == "floating":
        source["sha"] = "latest"
    elif mutation == "ref-only":
        source["ref"] = source.pop("sha")
    elif mutation == "both-fields":
        source["ref"] = source["sha"]
    else:
        lock["schema_version"] = 2
    (hub / EXTERNAL_LOCK_PATH).write_text(json.dumps(lock))
    with pytest.raises((InstructionHubError, ValueError)):
        build_hub(hub)
    assert not (hub / "hub.release.json").exists()


@pytest.mark.parametrize("ref", ["latest", "a" * 40])
def test_requested_sources_cannot_escape_into_marketplaces_or_release_provenance(tmp_path: Path, ref: str) -> None:
    definition = external_definition(ref)
    with pytest.raises(InstructionHubError, match="resolved before rendering"):
        external_marketplace_entry(ExternalPluginDefinition.model_validate(definition), "cursor")
    with pytest.raises(ValueError, match="pinned external"):
        validate_external_marketplace_source({"source": "url", "url": UPSTREAM_URL, "ref": ref})
    init_hub(tmp_path)
    write_external(tmp_path, external_definition())
    build_hub(tmp_path)
    path = tmp_path / "hub.release.json"
    manifest = json.loads(path.read_text())
    manifest["version_basis"]["plugins"][1]["source"] = definition["source"]
    _write_release_manifest_with_fresh_identity(path, manifest)
    with pytest.raises(ValueError, match="invalid version_basis"):
        read_release_manifest(path)


@pytest.mark.parametrize("ref", ["latest", "a" * 40])
def test_resolution_preserves_catalog_requests_and_resolves_only_stable_plugins(tmp_path: Path, ref: str) -> None:
    init_hub(tmp_path)
    definition = external_definition(ref)
    write_external(tmp_path, definition)
    # An unselected latest plugin does not need a lock or enter the build.
    unselected = {**latest_definition(), "id": "unselected"}
    (tmp_path / "plugins/unselected.yaml").write_text(yaml.safe_dump(unselected))
    validation = validate_hub(tmp_path)
    lock = ExternalPluginLock(plugins={"doc-detective": ResolvedGitSource(type="git", url=UPSTREAM_URL, sha="b" * 40)})
    resolved = apply_external_resolutions(validation, lock)

    requested = validation.stable_plugins[1].definition
    assert isinstance(requested, ExternalPluginDefinition)
    assert requested.source.ref == ref
    assert resolved.plugins == validation.plugins
    assert resolved.plugins["doc-detective"] is requested
    plugin = resolved.stable_plugins[1].definition
    assert isinstance(plugin, ResolvedExternalPluginDefinition)
    assert plugin.source.sha == ("b" * 40 if ref == "latest" else ref)
    assert [item.definition.id for item in resolved.stable_plugins] == ["pig", "doc-detective"]
    assert yaml.safe_load((tmp_path / "plugins/doc-detective.yaml").read_text()) == definition


def test_switching_latest_to_the_same_fixed_commit_preserves_release_identity(tmp_path: Path) -> None:
    init_hub(tmp_path)
    write_external(tmp_path, latest_definition())
    lock = ExternalPluginLock(plugins={"doc-detective": ResolvedGitSource(type="git", url=UPSTREAM_URL, sha="a" * 40)})
    (tmp_path / EXTERNAL_LOCK_PATH).write_text(lock.model_dump_json())
    latest = build_hub(tmp_path)
    write_external(tmp_path, external_definition())
    (tmp_path / EXTERNAL_LOCK_PATH).unlink()
    assert build_hub(tmp_path).release_hash == latest.release_hash


@pytest.mark.parametrize("failure", ["same-version", "missing-manifest", "unavailable"])
def test_failed_latest_refresh_preserves_lock_and_published_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    upstream = tmp_path / "upstream"
    make_upstream(upstream, monkeypatch)
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude", "codex", "cursor"))
    write_external(repo, latest_definition())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "follow upstream")
    _git(repo, "push")
    initial = _run_action(repo, tmp_path / "output")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    before = _snapshot_tree(repo / ".cursor-plugin")
    lock = (repo / EXTERNAL_LOCK_PATH).read_bytes()
    branches = _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable")
    if failure == "unavailable":
        shutil.rmtree(upstream)
    elif failure == "same-version":
        (upstream / PLUGIN_PATH / "skills/example/SKILL.md").write_text("# New behavior\n")
        commit_upstream(upstream)
    else:
        advance_upstream(upstream)
        (upstream / PLUGIN_PATH / ".cursor-plugin/plugin.json").unlink()
        commit_upstream(upstream)
    failed = _run_action(repo, tmp_path / "output")
    assert failed.returncode != 0
    expected = {
        "unavailable": "cannot fetch external plugin revision HEAD",
        "same-version": "retains upstream version 1.2.3",
        "missing-manifest": "missing upstream manifest",
    }
    assert expected[failure] in failed.stderr
    assert (repo / EXTERNAL_LOCK_PATH).read_bytes() == lock
    assert _snapshot_tree(repo / ".cursor-plugin") == before
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable") == branches
    assert _git_output(repo, "status", "--short") == ""


@pytest.mark.parametrize("mode", ["build", "check"])
def test_ci_build_refreshes_latest_but_check_keeps_the_committed_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    upstream = tmp_path / "upstream"
    sha = make_upstream(upstream, monkeypatch)
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude", "codex", "cursor"))
    write_external(repo, latest_definition())
    resolve_external_plugins(repo)
    build_hub(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "lock and build upstream")
    lock = (repo / EXTERNAL_LOCK_PATH).read_bytes()
    new_sha = advance_upstream(upstream)

    result = _run_action(repo, tmp_path / "output", extra_env={"INPUT_MODE": mode})
    assert result.returncode == 0, result.stdout + result.stderr
    expected_sha = new_sha if mode == "build" else sha
    assert f'"sha": "{expected_sha}"' in result.stdout
    assert (repo / EXTERNAL_LOCK_PATH).read_bytes() == lock
    assert _git_output(repo, "status", "--short") == ""


@pytest.mark.parametrize("hub_path", [".", "Customer Hub"])
def test_publish_refreshes_latest_without_catalog_edits_and_can_roll_back_to_a_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hub_path: str
) -> None:
    upstream = tmp_path / "upstream"
    sha = make_upstream(upstream, monkeypatch)
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude", "codex", "cursor"), hub_root_name=hub_path)
    hub = repo / hub_path
    definition = latest_definition()
    write_external(hub, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "follow upstream")
    _git(repo, "push")
    prefix = "" if hub_path == "." else hub_path + "/"
    extra_env = {"GITHUB_SERVER_URL": "https://gitlab.example.test", "GITHUB_REPOSITORY": "acme/team/hub"}

    def publish(expected_sha: str, version: str, *, locked: bool = True) -> None:
        result = _run_action(repo, tmp_path / "output", hub_root=hub_path, extra_env=extra_env)
        assert result.returncode == 0, result.stdout + result.stderr
        _git(repo, "fetch", "origin")
        for branch in ("main", "release/stable"):
            for marketplace in (".claude-plugin", ".agents/plugins", ".cursor-plugin"):
                entries = json.loads(
                    _git_output(repo, "show", f"origin/{branch}:{prefix}{marketplace}/marketplace.json")
                )
                source = entries["plugins"][1]["source"]
                assert source["sha"] == expected_sha and "ref" not in source
            if locked:
                lock = json.loads(_git_output(repo, "show", f"origin/{branch}:{prefix}{EXTERNAL_LOCK_PATH}"))
                assert lock["plugins"]["doc-detective"]["sha"] == expected_sha
            else:
                assert EXTERNAL_LOCK_PATH.name not in _git_output(repo, "ls-tree", "-r", f"origin/{branch}")
        release = json.loads(_git_output(repo, "show", f"origin/release/stable:{prefix}hub.release.json"))
        assert release["version"] == version
        assert release["version_basis"]["plugins"][1]["source"]["sha"] == expected_sha
        assert yaml.safe_load((hub / "plugins/doc-detective.yaml").read_text()) == definition
        assert _git_output(repo, "status", "--short") == ""

    publish(sha, "0.1.0")
    branches = _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable")
    publish(sha, "0.1.0")
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable") == branches
    new_sha = advance_upstream(upstream)
    publish(new_sha, "0.1.1")

    definition["source"] = {"type": "git", "url": UPSTREAM_URL, "ref": sha}
    write_external(hub, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "roll back and stop following latest")
    _git(repo, "push")
    publish(sha, "0.1.2", locked=False)
