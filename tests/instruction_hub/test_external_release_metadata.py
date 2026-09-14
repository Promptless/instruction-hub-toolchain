from __future__ import annotations

import json
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.release.external import read_external_versions, write_external_verification
from promptless_instruction_hub.release.versions import read_release_manifest

from .external_helpers import PLUGIN_PATH, UPSTREAM_URL, external_definition, write_external


@pytest.fixture
def recorded_release(tmp_path: Path) -> Path:
    init_hub(tmp_path)
    write_external(tmp_path, external_definition())
    build_hub(tmp_path)
    verification_path = tmp_path / "verification.json"
    verification_path.write_text(
        json.dumps(
            {
                "verified_external_plugins": [
                    {
                        "id": "doc-detective",
                        "target": target,
                        "url": UPSTREAM_URL,
                        "sha": "a" * 40,
                        "path": PLUGIN_PATH,
                        "upstream_version": "1.2.3" if target == "claude" else None,
                    }
                    for target in ("claude", "codex", "cursor")
                ]
            }
        )
    )
    manifest_path = tmp_path / "hub.release.json"
    write_external_verification(manifest_path, verification_path)
    return manifest_path


def test_recorded_versions_distinguish_versionless_targets_from_legacy_releases(recorded_release: Path) -> None:
    _, basis = read_release_manifest(recorded_release)
    assert read_external_versions(recorded_release, basis) == {
        ("doc-detective", "claude"): "1.2.3",
        ("doc-detective", "codex"): None,
        ("doc-detective", "cursor"): None,
    }
    recorded_release.with_name("hub.external.json").unlink()
    assert read_external_versions(recorded_release, basis) is None


@pytest.mark.parametrize(
    "mutation", ["release-hash", "schema", "source", "missing", "duplicate", "version", "record-shape", "extra"]
)
def test_invalid_external_records_cannot_bypass_version_checks(recorded_release: Path, mutation: str) -> None:
    metadata_path = recorded_release.with_name("hub.external.json")
    metadata = json.loads(metadata_path.read_text())
    records = metadata["verified_external_plugins"]
    if mutation == "release-hash":
        metadata["release_hash"] = "0" * 64
    elif mutation == "schema":
        metadata["schema_version"] = True
    elif mutation == "source":
        records[0]["sha"] = "b" * 40
    elif mutation == "missing":
        records.pop()
    elif mutation == "duplicate":
        records.append(records[0])
    elif mutation == "version":
        records[0]["upstream_version"] = "latest"
    elif mutation == "record-shape":
        records[0]["id"] = []
    else:
        metadata["unexpected"] = True
    metadata_path.write_text(json.dumps(metadata))
    _, basis = read_release_manifest(recorded_release)
    with pytest.raises(ValueError):
        read_external_versions(recorded_release, basis)


def test_recording_rejects_verification_for_different_sources(recorded_release: Path) -> None:
    verification_path = recorded_release.with_name("verification.json")
    verification = json.loads(verification_path.read_text())
    verification["verified_external_plugins"][0]["url"] = "https://different.example.test/plugins.git"
    verification_path.write_text(json.dumps(verification))
    metadata_path = recorded_release.with_name("hub.external.json")
    before = metadata_path.read_bytes()
    with pytest.raises(ValueError, match="must match a published plugin source"):
        write_external_verification(recorded_release, verification_path)
    assert metadata_path.read_bytes() == before
