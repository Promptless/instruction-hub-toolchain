"""Persist upstream verification alongside a published release's offline artifacts."""

from pathlib import Path
from typing import cast

from promptless_instruction_hub.fs import JsonValue, read_json_mapping, write_json
from promptless_instruction_hub.models import ExternalGitSource, ExternalPluginDefinition, SEMVER_RE
from promptless_instruction_hub.release.versions import read_release_manifest

EXTERNAL_VERIFICATION_PATH = "hub.external.json"


def write_external_verification(manifest_path: Path, verification_path: Path) -> None:
    """Bind complete verification results to the release that is about to be published."""

    _, basis = read_release_manifest(manifest_path)
    verification = read_json_mapping(verification_path)
    if set(verification) != {"verified_external_plugins"}:
        raise ValueError(f"{verification_path}: invalid external verification results")
    _validate_records(verification["verified_external_plugins"], basis)
    write_json(
        manifest_path.with_name(EXTERNAL_VERIFICATION_PATH),
        {
            "schema_version": 1,
            "release_hash": read_json_mapping(manifest_path)["release_hash"],
            **verification,
        },
    )


def read_external_versions(
    manifest_path: Path, basis: dict[str, JsonValue]
) -> dict[tuple[str, str], str | None] | None:
    """Read recorded versions, or return None for releases without verification records."""

    verification_path = manifest_path.with_name(EXTERNAL_VERIFICATION_PATH)
    if verification_path.is_symlink():
        raise ValueError(f"{verification_path}: external verification must be a regular file")
    if not verification_path.exists():
        return None
    verification = read_json_mapping(verification_path)
    if (
        set(verification) != {"schema_version", "release_hash", "verified_external_plugins"}
        or type(verification["schema_version"]) is not int
        or verification["schema_version"] != 1
        or verification["release_hash"] != read_json_mapping(manifest_path)["release_hash"]
    ):
        raise ValueError(f"{verification_path}: external verification must match the published release")
    return _validate_records(verification["verified_external_plugins"], basis)


def _validate_records(value: JsonValue, basis: dict[str, JsonValue]) -> dict[tuple[str, str], str | None]:
    expected = set()
    for item in cast(list[dict[str, JsonValue]], basis["plugins"]):
        if item.get("kind") == "external":
            plugin = ExternalPluginDefinition.model_validate(item)
            source = cast(ExternalGitSource, plugin.source)
            for target, location in plugin.targets.items():
                if target in cast(list[str], basis["targets"]):
                    expected.add((plugin.id, target, source.url, source.ref, location.path))

    if not isinstance(value, list):
        raise ValueError("verified_external_plugins must be a list")
    versions: dict[tuple[str, str], str | None] = {}
    for record in value:
        if not isinstance(record, dict) or set(record) != {"id", "target", "url", "sha", "path", "upstream_version"}:
            raise ValueError("invalid external verification record")
        identity = tuple(record[key] for key in ("id", "target", "url", "sha", "path"))
        if not all(isinstance(part, str) for part in identity) or identity not in expected:
            raise ValueError("external verification record must match a published plugin source")
        expected.remove(identity)
        version = record["upstream_version"]
        if version is not None and (not isinstance(version, str) or not SEMVER_RE.fullmatch(version)):
            raise ValueError("verified upstream version must be SemVer or null")
        versions[(cast(str, record["id"]), cast(str, record["target"]))] = version
    if expected:
        raise ValueError("external verification records must cover every published external target")
    return versions
