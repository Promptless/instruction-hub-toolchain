from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.release.versions import resolve_publish_plugin_version

from .helpers import (
    _configure_split_package_hub,
    _write_release_manifest_with_fresh_identity,
)


def test_publish_version_bumps_when_package_name_changes(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    _configure_split_package_hub(hub_root, targets=("claude", "codex"))
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)

    (hub_root / "packages/dev.yaml").write_text(
        "id: dev\nname: Developer Tools\nincludes:\n  - skill:authoring-tools\n"
    )

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.1.1"


def test_publish_version_bumps_when_package_membership_changes(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    _configure_split_package_hub(hub_root, targets=("claude", "codex"))
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)

    (hub_root / "packages/dev.yaml").write_text("id: dev\nname: Dev\nincludes:\n  - skill:runbooks\n")
    (hub_root / "packages/ops.yaml").write_text("id: ops\nname: Ops\nincludes:\n  - skill:authoring-tools\n")

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.1.1"


def test_publish_version_prefers_manual_semver_promotion(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, plugin_version="1.0.0-alpha.1")
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    (hub_root / "hub.yaml").write_text(
        (hub_root / "hub.yaml").read_text().replace("plugin_version: 1.0.0-alpha.1", "plugin_version: 1.0.0")
    )

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "1.0.0"


def test_publish_version_prefers_higher_configured_version_floor(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, plugin_version="0.1.1")
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    (hub_root / "hub.yaml").write_text(
        (hub_root / "hub.yaml").read_text().replace("plugin_version: 0.1.1", "plugin_version: 0.2.0")
    )

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.2.0"


def test_publish_version_accepts_legacy_managed_runtime_id_in_previous_release(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    for runtime in manifest["managed_runtimes"]:
        runtime["id"] = "host-enrollment-bootstrap"
    for runtime in manifest["version_basis"]["managed_runtimes"]:
        runtime["id"] = "host-enrollment-bootstrap"
    _write_release_manifest_with_fresh_identity(manifest_path, manifest)

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.1.1"


def test_publish_version_rejects_invalid_authoritative_release_manifest(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_text(
        json.dumps({"plugin": {"id": "acme", "name": "Acme", "version": "not-semver"}, "version_basis": {}})
    )

    with pytest.raises(ValueError, match=r"hub\.release\.json: plugin\.version must be SemVer"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_rejects_malformed_authoritative_version_basis(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_text(
        json.dumps({"plugin": {"id": "acme", "name": "Acme", "version": "0.1.0"}, "version_basis": {}})
    )

    with pytest.raises(ValueError, match=r"hub\.release\.json: version_basis must contain exactly"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


@pytest.mark.parametrize("field", ["stable_packages", "targets"])
def test_publish_version_rejects_empty_authoritative_version_basis_required_lists(
    tmp_path: Path,
    field: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version_basis"][field] = []
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=rf"hub\.release\.json: version_basis\.{field} must not be empty"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_reports_nested_authoritative_version_basis_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version_basis"]["plugin"]["id"] = None
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=r"hub\.release\.json: version_basis\.plugin\.id must be a string"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("schema_version", r"hub\.release\.json: schema_version must be 1"),
        ("assets_object", r"hub\.release\.json: assets must be a list"),
        ("assets_empty", r"hub\.release\.json: assets refs must match version_basis package assets"),
        (
            "target_hash_missing",
            r"hub\.release\.json: version_basis\.target_hashes keys must match version_basis\.targets",
        ),
        (
            "managed_runtime_bad_sha",
            r"hub\.release\.json: version_basis\.managed_runtimes\[0\]\.sha256 must be a sha256 hex digest",
        ),
        ("release_id_empty", r"hub\.release\.json: release_id must not be empty"),
        ("release_hash_mismatch", r"hub\.release\.json: release_hash must match manifest content"),
    ],
)
def test_publish_version_rejects_authoritative_release_manifest_tampering(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    _configure_split_package_hub(hub_root, targets=("claude", "codex"))
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "schema_version":
        manifest["schema_version"] = 999
    elif mutation == "assets_object":
        manifest["assets"] = {}
    elif mutation == "assets_empty":
        manifest["assets"] = []
    elif mutation == "target_hash_missing":
        del manifest["version_basis"]["target_hashes"]["claude"]
    elif mutation == "managed_runtime_bad_sha":
        manifest["version_basis"]["managed_runtimes"][0]["sha256"] = "bad"
    elif mutation == "release_id_empty":
        manifest["release_id"] = ""
    elif mutation == "release_hash_mismatch":
        manifest["release_hash"] = "0" * 64
    else:
        raise AssertionError(f"unhandled mutation: {mutation}")
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=message):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_rejects_authoritative_release_manifest_unexpected_root_key(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["unexpected"] = True
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=r"hub\.release\.json: release manifest must contain exactly"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_uses_config_when_previous_release_has_no_version_metadata(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, plugin_version="0.2.0")
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "README.md").write_text("# Previous release\n")

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.2.0"


def test_publish_version_ignores_repo_context_inventory(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    _configure_split_package_hub(hub_root, targets=("claude", "codex"))
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    (hub_root / "hub.repo-context.json").write_text(
        json.dumps({"schema_version": 1, "files": [{"path": "AGENTS.md", "imported": False}]})
    )

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.1.0"


def test_publish_version_rejects_release_manifest_without_version_basis(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_text(
        json.dumps({"plugin": {"id": "acme", "name": "Acme", "version": "0.1.0"}})
    )

    with pytest.raises(ValueError, match=r"hub\.release\.json: version_basis is missing"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_reports_malformed_previous_release_json_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_text("{")

    with pytest.raises(ValueError, match=r"hub\.release\.json contains malformed JSON"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_reports_malformed_previous_release_json_encoding_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_bytes(b"\xff")

    with pytest.raises(ValueError, match=r"hub\.release\.json contains malformed JSON"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_rejects_missing_previous_hub_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "repo/hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()

    with pytest.raises(ValueError, match="previous release is missing hub path: hub"):
        resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root, hub_relative_path="hub")


def test_publish_version_uses_config_floor_when_previous_release_has_no_manifest(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, plugin_version="0.3.0")
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "dist").mkdir()

    assert resolve_publish_plugin_version(hub_root, previous_release_root=previous_release_root) == "0.3.0"
