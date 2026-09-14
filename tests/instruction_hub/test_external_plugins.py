from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from promptless_instruction_hub.cli import main
from promptless_instruction_hub.compiler import build_hub, init_hub, verify_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.external_plugins import verify_external_plugins
from promptless_instruction_hub.release.versions import read_release_manifest, resolve_publish_version
from promptless_instruction_hub.validate.hub import validate_hub

from .external_helpers import (
    PLUGIN_PATH,
    UPSTREAM_URL,
    commit_upstream,
    external_definition,
    make_upstream,
    write_external,
)
from .helpers import _git, _git_output, _snapshot_tree, _write_release_manifest_with_fresh_identity


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "vendored"),
        ("id", "pig"),
        ("includes", ["skill:example"]),
        ("targets", {}),
        ("targets", {"gemini": {"path": "."}}),
        ("source", {"type": "git", "url": UPSTREAM_URL, "sha": "main"}),
        ("source", {"type": "git", "url": UPSTREAM_URL, "sha": "a" * 40, "ref": "main"}),
    ],
)
def test_invalid_external_definitions_fail_offline(tmp_path: Path, field: str, value: Any) -> None:
    init_hub(tmp_path)
    definition = external_definition()
    definition[field] = value
    write_external(tmp_path, definition)
    with pytest.raises(InstructionHubError):
        validate_hub(tmp_path)


@pytest.mark.parametrize(
    "path", ["../plugin", "/plugin", "x/../plugin", "x//plugin", "./plugin", "C:/plugin", "x\\y", ""]
)
def test_external_plugin_paths_cannot_escape_repo(tmp_path: Path, path: str) -> None:
    init_hub(tmp_path)
    write_external(tmp_path, external_definition(path=path))
    with pytest.raises(InstructionHubError, match="relative POSIX"):
        validate_hub(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/repo",
        "git@github.com:org/repo",
        "http://example.test/repo",
        "https://token@example.test/repo",
        "https://example.test/repo?token=secret",
        "https://example.test/repo#main",
    ],
)
def test_external_plugin_urls_are_portable_and_credential_free(tmp_path: Path, url: str) -> None:
    init_hub(tmp_path)
    definition = external_definition()
    definition["source"]["url"] = url
    write_external(tmp_path, definition)
    with pytest.raises(InstructionHubError, match="HTTPS repository URL"):
        validate_hub(tmp_path)


@pytest.mark.parametrize("cursor_path", [".", PLUGIN_PATH])
def test_mixed_marketplaces_build_offline_without_external_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cursor_path: str
) -> None:
    init_hub(tmp_path)
    definition = external_definition()
    definition["targets"]["codex"]["path"] = "."
    definition["targets"]["cursor"]["path"] = cursor_path
    write_external(tmp_path, definition)
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: pytest.fail("offline compilation attempted a process")
    )

    validate_hub(tmp_path)
    result = build_hub(tmp_path)
    assert verify_hub(tmp_path).release_hash == result.release_hash
    assert build_hub(tmp_path, check=True).checked
    claude = json.loads((tmp_path / ".claude-plugin/marketplace.json").read_text())["plugins"]
    codex = json.loads((tmp_path / ".agents/plugins/marketplace.json").read_text())["plugins"]
    assert claude[0]["source"] == "./dist/claude/pig"
    assert claude[1] == {
        "name": "doc-detective",
        "source": {"source": "git-subdir", "url": UPSTREAM_URL, "sha": "a" * 40, "path": PLUGIN_PATH},
    }
    assert codex[1]["source"] == {"source": "url", "url": UPSTREAM_URL, "sha": "a" * 40}
    assert "version" not in codex[1] and "author" not in codex[1]
    cursor = json.loads((tmp_path / ".cursor-plugin/marketplace.json").read_text())["plugins"]
    assert cursor[0]["source"] == "dist/cursor/pig"
    assert cursor[1] == {
        "name": "doc-detective",
        "source": {
            "source": "url" if cursor_path == "." else "git-subdir",
            "url": UPSTREAM_URL,
            "sha": "a" * 40,
            **({"path": cursor_path} if cursor_path != "." else {}),
        },
    }
    assert not list((tmp_path / "dist").glob("*/doc-detective"))
    manifest = json.loads((tmp_path / "hub.release.json").read_text())
    assert manifest["schema_version"] == 3
    assert json.loads((tmp_path / "hub.stable.json").read_text())["schema_version"] == 3
    assert manifest["version_basis"]["plugins"][1] == definition
    assert all(runtime["plugin_id"] != "doc-detective" for runtime in manifest["managed_runtimes"])


def test_only_declared_enabled_targets_are_emitted(tmp_path: Path) -> None:
    init_hub(tmp_path)
    definition = external_definition()
    definition["targets"] = {"claude": {"path": PLUGIN_PATH}}
    write_external(tmp_path, definition)
    build_hub(tmp_path)
    codex = json.loads((tmp_path / ".agents/plugins/marketplace.json").read_text())
    assert [plugin["name"] for plugin in codex["plugins"]] == ["pig"]
    cursor = json.loads((tmp_path / ".cursor-plugin/marketplace.json").read_text())
    assert [plugin["name"] for plugin in cursor["plugins"]] == ["pig"]
    config = yaml.safe_load((tmp_path / "hub.yaml").read_text())
    config["targets"] = ["cursor", "gemini"]
    (tmp_path / "hub.yaml").write_text(yaml.safe_dump(config))
    with pytest.raises(InstructionHubError, match="no enabled Hub target"):
        validate_hub(tmp_path)


def test_external_pins_participate_in_versioning_across_schema_migration(tmp_path: Path) -> None:
    hub, previous = tmp_path / "hub", tmp_path / "previous"
    init_hub(hub)
    build_hub(hub)
    shutil.copytree(hub, previous)
    assert read_release_manifest(previous / "hub.release.json")[0] == "0.1.0"
    definition = external_definition()
    write_external(hub, definition)
    assert resolve_publish_version(hub, previous_release_root=previous) == "0.1.1"
    build_hub(hub)
    shutil.copytree(hub, previous, dirs_exist_ok=True)
    assert resolve_publish_version(hub, previous_release_root=previous) == "0.1.0"
    old = json.loads((hub / "hub.release.json").read_text())
    definition["source"]["sha"] = "b" * 40
    write_external(hub, definition)
    assert resolve_publish_version(hub, previous_release_root=previous) == "0.1.1"
    build_hub(hub)
    new = json.loads((hub / "hub.release.json").read_text())
    assert new["release_hash"] != old["release_hash"]
    assert new["target_hashes"] == old["target_hashes"]
    (hub / "plugins/doc-detective.yaml").unlink()
    config = yaml.safe_load((hub / "hub.yaml").read_text())
    config["stable_plugins"].remove("doc-detective")
    (hub / "hub.yaml").write_text(yaml.safe_dump(config))
    assert resolve_publish_version(hub, previous_release_root=previous) == "0.1.1"
    build_hub(hub)
    assert json.loads((hub / "hub.release.json").read_text())["schema_version"] == 2


@pytest.mark.parametrize("mutation", ["legacy-schema", "bad-pin", "extra-field", "asset-injection"])
def test_release_reader_rejects_invalid_external_provenance(tmp_path: Path, mutation: str) -> None:
    init_hub(tmp_path)
    write_external(tmp_path, external_definition())
    build_hub(tmp_path)
    manifest_path = tmp_path / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    plugin = manifest["version_basis"]["plugins"][1]
    if mutation == "legacy-schema":
        manifest["schema_version"] = 2
    elif mutation == "bad-pin":
        plugin["source"]["sha"] = "main"
    elif mutation == "extra-field":
        plugin["source"]["ref"] = "main"
    else:
        plugin["assets"] = []
    _write_release_manifest_with_fresh_identity(manifest_path, manifest)
    with pytest.raises(ValueError):
        read_release_manifest(manifest_path)


@pytest.mark.parametrize("path", [".", PLUGIN_PATH])
def test_verifier_reads_pinned_upstream_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    upstream, hub = tmp_path / "upstream", tmp_path / "hub"
    sha = make_upstream(upstream, monkeypatch, path=path)
    init_hub(hub)
    write_external(hub, external_definition(sha, path=path))
    # A changed working tree is deliberately ignored: the pin is authoritative.
    (upstream / path / ".claude-plugin/plugin.json").write_text("invalid JSON")
    before = _snapshot_tree(hub)
    assert main(["verify-external", "--hub", str(hub)]) == 0
    records = json.loads(capsys.readouterr().out)["verified_external_plugins"]
    assert {(record["target"], record["sha"], record["upstream_version"]) for record in records} == {
        ("claude", sha, "1.2.3"),
        ("codex", sha, "1.2.3"),
        ("cursor", sha, "1.2.3"),
    }
    assert _snapshot_tree(hub) == before


@pytest.mark.parametrize("path_kind", ["absolute", "parent", "symlink", "manifest-symlink"])
def test_verifier_rejects_previous_hub_paths_outside_release(tmp_path: Path, path_kind: str) -> None:
    if os.name == "nt" and "symlink" in path_kind:
        pytest.skip("Windows symlink creation requires privileges")
    hub, previous = tmp_path / "hub", tmp_path / "previous"
    init_hub(hub)
    write_external(hub, external_definition())
    build_hub(hub)
    previous.mkdir()
    if path_kind == "absolute":
        hub_relative_path = str(previous)
    elif path_kind == "parent":
        hub_relative_path = "../hub"
    elif path_kind == "symlink":
        (previous / "linked-hub").symlink_to(hub, target_is_directory=True)
        hub_relative_path = "linked-hub"
    else:
        (previous / "hub.release.json").symlink_to(hub / "hub.release.json")
        hub_relative_path = "."

    with pytest.raises(
        InstructionHubError, match="Hub path must be relative and stay inside the previous release root"
    ):
        verify_external_plugins(hub, previous_release_root=previous, hub_relative_path=hub_relative_path)


@pytest.mark.parametrize("target", ["claude", "codex", "cursor"])
@pytest.mark.parametrize(
    "failure",
    [
        "missing-commit",
        "missing-manifest",
        "wrong-name",
        "bad-json",
        "bad-version",
        "escape",
        "missing-component",
        "symlink",
        "submodule",
    ],
)
def test_upstream_verification_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, target: str
) -> None:
    upstream, hub = tmp_path / "upstream", tmp_path / "hub"
    sha = make_upstream(upstream, monkeypatch)
    manifest_path = upstream / PLUGIN_PATH / f".{target}-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    if failure == "missing-commit":
        sha = "0" * 40
    elif failure == "missing-manifest":
        manifest_path.unlink()
    elif failure == "bad-json":
        manifest_path.write_text("not JSON")
    elif failure == "symlink":
        if os.name == "nt":
            pytest.skip("Windows symlink creation requires privileges")
        (upstream / PLUGIN_PATH / "escape").symlink_to("../../secret")
    elif failure == "submodule":
        _git(upstream, "update-index", "--add", "--cacheinfo", f"160000,{sha},{PLUGIN_PATH}/nested")
        _git(upstream, "commit", "-m", "add gitlink")
        sha = _git_output(upstream, "rev-parse", "HEAD").strip()
    else:
        key, value = {
            "wrong-name": ("name", "different-plugin"),
            "bad-version": ("version", "latest"),
            "escape": ("skills", "./../../outside"),
            "missing-component": ("skills", "./absent"),
        }[failure]
        manifest[key] = value
        manifest_path.write_text(json.dumps(manifest))
    if failure not in {"missing-commit", "submodule"}:
        sha = commit_upstream(upstream)
    init_hub(hub)
    write_external(hub, external_definition(sha))
    with pytest.raises(InstructionHubError):
        verify_external_plugins(hub)


@pytest.mark.parametrize("target", ["claude", "codex", "cursor"])
@pytest.mark.parametrize("field", ["hooks", "mcpServers", "lspServers"])
@pytest.mark.parametrize("path", ["./hooks", "./", "./config.json", "./hooks/hooks.json/"])
def test_configuration_paths_must_reference_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, field: str, path: str
) -> None:
    upstream, hub = tmp_path / "upstream", tmp_path / "hub"
    make_upstream(upstream, monkeypatch)
    plugin = upstream / PLUGIN_PATH
    (plugin / "config.json").mkdir()
    (plugin / "config.json/nested.json").write_text("{}")
    manifest_path = plugin / f".{target}-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field] = path
    manifest_path.write_text(json.dumps(manifest))
    sha = commit_upstream(upstream)
    init_hub(hub)
    write_external(hub, external_definition(sha))

    with pytest.raises(InstructionHubError, match=rf"\({target}\): {field} path must reference an upstream file"):
        verify_external_plugins(hub)


@pytest.mark.parametrize("target", ["claude", "codex", "cursor"])
def test_configuration_paths_accept_files_and_inline_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    upstream, hub = tmp_path / "upstream", tmp_path / "hub"
    make_upstream(upstream, monkeypatch)
    manifest_path = upstream / PLUGIN_PATH / f".{target}-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["mcpServers"] = ["./.mcp.json"]
    manifest["lspServers"] = {}
    manifest_path.write_text(json.dumps(manifest))
    sha = commit_upstream(upstream)
    init_hub(hub)
    write_external(hub, external_definition(sha))

    assert len(verify_external_plugins(hub)) == 3


def test_cursor_only_hub_verifies_only_enabled_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upstream, hub = tmp_path / "upstream", tmp_path / "hub"
    make_upstream(upstream, monkeypatch)
    (upstream / PLUGIN_PATH / ".claude-plugin/plugin.json").unlink()
    (upstream / PLUGIN_PATH / ".codex-plugin/plugin.json").unlink()
    sha = commit_upstream(upstream)
    init_hub(hub)
    config = yaml.safe_load((hub / "hub.yaml").read_text())
    config["targets"] = ["cursor"]
    (hub / "hub.yaml").write_text(yaml.safe_dump(config))
    write_external(hub, external_definition(sha))
    assert [record["target"] for record in verify_external_plugins(hub)] == ["cursor"]
    build_hub(hub)
    assert read_release_manifest(hub / "hub.release.json")[1]["targets"] == ["cursor"]
    cursor = json.loads((hub / ".cursor-plugin/marketplace.json").read_text())
    assert [plugin["name"] for plugin in cursor["plugins"]] == ["pig", "doc-detective"]


@pytest.mark.parametrize("rules", ["./../../outside", "./absent"])
def test_cursor_rules_must_exist_inside_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rules: str) -> None:
    upstream, hub = tmp_path / "upstream", tmp_path / "hub"
    make_upstream(upstream, monkeypatch)
    manifest_path = upstream / PLUGIN_PATH / ".cursor-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["rules"] = rules
    manifest_path.write_text(json.dumps(manifest))
    sha = commit_upstream(upstream)
    init_hub(hub)
    write_external(hub, external_definition(sha))
    with pytest.raises(InstructionHubError, match=r"\(cursor\): .*rules"):
        verify_external_plugins(hub)


def test_git_failure_does_not_print_credential_helper_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    init_hub(tmp_path)
    write_external(tmp_path, external_definition())
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "secret-token")
    )
    assert main(["verify-external", "--hub", str(tmp_path)]) == 1
    assert "secret-token" not in capsys.readouterr().err


def test_authored_only_verification_never_fetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_hub(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected fetch"))
    assert verify_external_plugins(tmp_path) == []


def test_git_timeout_fails_cli_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    init_hub(tmp_path)
    write_external(tmp_path, external_definition())

    def timeout(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("git", 60)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert main(["verify-external", "--hub", str(tmp_path)]) == 1
    assert "could not complete" in capsys.readouterr().err


def test_manifest_can_reference_plugin_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upstream, hub = tmp_path / "upstream", tmp_path / "hub"
    make_upstream(upstream, monkeypatch)
    manifest_path = upstream / PLUGIN_PATH / ".claude-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["skills"] = "./"
    manifest_path.write_text(json.dumps(manifest))
    sha = commit_upstream(upstream)
    init_hub(hub)
    write_external(hub, external_definition(sha))
    assert verify_external_plugins(hub)
