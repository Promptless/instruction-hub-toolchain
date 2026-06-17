"""Asset discovery and validation for Instruction Hub source trees."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import (
    JsonValue,
    directory_hash,
    file_hash,
    read_json_mapping,
    read_yaml_mapping,
    validate_json_value,
)
from promptless_instruction_hub.models import (
    ASSET_KINDS,
    AssetKind,
    AssetMetadata,
    Harness,
    LoadedAsset,
    SUPPORTED_HARNESSES,
    TargetSupport,
)

ASSETS_DIR = Path("assets")
METADATA_FILE = "asset.yaml"
SECRET_KEY_FRAGMENTS = ("token", "secret", "password", "api_key", "apikey", "private_key")
SUPPORTED_FILE_SUFFIXES = (".md", ".yaml", ".yml", ".json")
SIDECAR_METADATA_SUFFIX = ".asset.yaml"
DEFAULT_TITLES = {
    "repo-mcp": "Repository MCP Servers",
    "cursor-mcp": "Cursor MCP Servers",
}


def load_assets(hub_root: Path) -> dict[str, LoadedAsset]:
    """Load all supported assets under the hub's `assets/` directory."""

    assets: dict[str, LoadedAsset] = {}
    for asset in _iter_assets(hub_root):
        if asset.ref in assets:
            msg = f"duplicate asset reference: {asset.ref}"
            raise InstructionHubError(msg)
        assets[asset.ref] = asset
    return assets


def validate_no_literal_secrets(hub_root: Path) -> None:
    """Reject secret-looking literal values in structured JSON/YAML assets."""

    asset_root = hub_root / ASSETS_DIR
    source_paths = sorted(
        path for path in asset_root.rglob("*") if path.is_file() and path.suffix in {".json", ".yaml", ".yml"}
    )
    for source_path in source_paths:
        data = _read_structured_file(source_path)
        if data is None:
            continue
        _validate_secret_values(source_path, data, ())


def default_skill_support() -> dict[Harness, TargetSupport]:
    """Return target support for portable Agent Skills."""

    return {target: TargetSupport(mode="agent-skill") for target in SUPPORTED_HARNESSES}


def default_mcp_support() -> dict[Harness, TargetSupport]:
    """Return target support for portable MCP definitions."""

    return {target: TargetSupport(mode="native") for target in SUPPORTED_HARNESSES}


def default_asset_support(asset_kind: AssetKind) -> dict[Harness, TargetSupport]:
    """Return convention-based support for assets that omit metadata."""

    if asset_kind == "skill":
        return default_skill_support()
    if asset_kind == "mcp":
        return default_mcp_support()
    return unsupported_support("Non-portable assets require explicit target support in v1.")


def default_asset_title(path: Path, asset_kind: AssetKind) -> str:
    """Infer a human-readable asset title from source content or file naming."""

    if asset_kind == "skill" and path.is_dir():
        skill_file = _find_skill_file(path)
        if skill_file is not None:
            extracted_title = _extract_markdown_title(skill_file)
            if extracted_title:
                return extracted_title
    asset_id = path.name if path.is_dir() else path.stem
    return DEFAULT_TITLES.get(asset_id, _titleize(asset_id))


def unsupported_support(reason: str) -> dict[Harness, TargetSupport]:
    """Return unsupported target support for every v1 harness."""

    return {target: TargetSupport(mode="unsupported", reason=reason) for target in SUPPORTED_HARNESSES}


def _iter_assets(hub_root: Path) -> Iterable[LoadedAsset]:
    assets_root = hub_root / ASSETS_DIR
    if not assets_root.exists():
        return

    skills_dir = assets_root / "skills"
    if skills_dir.exists():
        for skill_path in sorted(skills_dir.iterdir()):
            if skill_path.name.startswith("."):
                continue
            if not skill_path.is_dir():
                msg = f"unexpected file in skills asset directory: {skill_path}"
                raise InstructionHubError(msg)
            if not _find_skill_file(skill_path):
                msg = f"skill asset directory must contain SKILL.md: {skill_path}"
                raise InstructionHubError(msg)
            yield _load_directory_asset(skill_path, "skill", default_asset_support("skill"))

    for asset_kind in ASSET_KINDS:
        if asset_kind == "skill":
            continue
        kind_dir = assets_root / f"{asset_kind}s"
        if not kind_dir.exists():
            continue
        yield from _iter_non_skill_assets(kind_dir, asset_kind)


def _iter_non_skill_assets(kind_dir: Path, asset_kind: AssetKind) -> Iterable[LoadedAsset]:
    default_support = default_asset_support(asset_kind)
    for asset_path in sorted(kind_dir.iterdir()):
        if asset_path.name.startswith("."):
            continue
        if asset_path.is_dir():
            yield _load_directory_asset(asset_path, asset_kind, default_support)
            continue
        if asset_path.name.endswith(SIDECAR_METADATA_SUFFIX):
            _validate_sidecar_has_asset_file(kind_dir, asset_path)
            continue
        if asset_path.name == METADATA_FILE:
            msg = f"unexpected metadata file in asset directory: {asset_path}"
            raise InstructionHubError(msg)
        if asset_path.suffix not in SUPPORTED_FILE_SUFFIXES:
            msg = f"unsupported asset file extension for {asset_path}; expected one of {SUPPORTED_FILE_SUFFIXES}"
            raise InstructionHubError(msg)
        yield _load_file_asset(asset_path, asset_kind, default_support)


def _load_directory_asset(
    path: Path, asset_kind: AssetKind, default_support: dict[Harness, TargetSupport]
) -> LoadedAsset:
    metadata = _load_metadata(
        path / METADATA_FILE,
        path.name,
        asset_kind,
        default_asset_title(path, asset_kind),
        default_asset_source_path(path),
        default_support,
    )
    return LoadedAsset(
        id=metadata.id,
        type=asset_kind,
        path=path,
        metadata=metadata,
        content_hash=directory_hash(path, skip_names={METADATA_FILE}),
    )


def _load_file_asset(path: Path, asset_kind: AssetKind, default_support: dict[Harness, TargetSupport]) -> LoadedAsset:
    metadata = _load_metadata(
        path.with_suffix(".asset.yaml"),
        path.stem,
        asset_kind,
        default_asset_title(path, asset_kind),
        default_asset_source_path(path),
        default_support,
    )
    return LoadedAsset(
        id=metadata.id,
        type=asset_kind,
        path=path,
        metadata=metadata,
        content_hash=file_hash(path),
    )


def _load_metadata(
    metadata_path: Path,
    default_id: str,
    asset_kind: AssetKind,
    default_title: str,
    default_source_path: str,
    default_support: dict[Harness, TargetSupport],
) -> AssetMetadata:
    if metadata_path.exists():
        raw_metadata = read_yaml_mapping(metadata_path)
        raw_metadata.setdefault("id", default_id)
        raw_metadata.setdefault("type", asset_kind)
        raw_metadata.setdefault("title", default_title)
        raw_metadata.setdefault("source_path", default_source_path)
        _merge_default_support(raw_metadata, default_support)
        return _validate_metadata_type(metadata_path, AssetMetadata.model_validate(raw_metadata), asset_kind)
    return AssetMetadata(
        id=default_id,
        type=asset_kind,
        title=default_title,
        source_path=default_source_path,
        support=default_support,
    )


def _merge_default_support(raw_metadata: dict[str, JsonValue], default_support: dict[Harness, TargetSupport]) -> None:
    default_payload = {target: support.model_dump(exclude_none=True) for target, support in default_support.items()}
    raw_support = raw_metadata.get("support")
    if raw_support is None:
        raw_metadata["support"] = default_payload
        return
    if isinstance(raw_support, dict):
        raw_metadata["support"] = {**default_payload, **raw_support}


def _validate_secret_values(path: Path, value: object, key_path: tuple[str, ...]) -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            _validate_secret_values(path, child, (*key_path, key))
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_secret_values(path, child, (*key_path, str(index)))
        return
    if not isinstance(value, str):
        return
    joined_key = ".".join(key_path).lower()
    if not any(fragment in joined_key for fragment in SECRET_KEY_FRAGMENTS):
        return
    if value.startswith("${") and value.endswith("}"):
        return
    if value.startswith("env:"):
        return
    msg = f"{path} contains a literal secret-looking value at {'.'.join(key_path)}; use an env placeholder"
    raise InstructionHubError(msg)


def _read_structured_file(path: Path) -> object:
    if path.suffix == ".json":
        return read_json_mapping(path)
    return validate_json_value(yaml.safe_load(path.read_text()), path)


def _find_skill_file(skill_dir: Path) -> Path | None:
    for child in sorted(skill_dir.iterdir()):
        if child.is_file() and child.name.lower() == "skill.md":
            return child
    return None


def _extract_markdown_title(markdown_path: Path) -> str | None:
    for line in markdown_path.read_text().splitlines():
        if line.startswith("# "):
            return line.removeprefix("# ").strip()
    return None


def default_asset_source_path(path: Path) -> str:
    """Infer the asset's source path within the customer hub."""

    for index in range(len(path.parts) - 1, -1, -1):
        if path.parts[index] == ASSETS_DIR.name:
            return Path(*path.parts[index:]).as_posix()
    return path.as_posix()


def _titleize(value: str) -> str:
    words: list[str] = []
    for part in value.replace("_", "-").split("-"):
        if not part:
            continue
        words.append(part.upper() if part.lower() == "mcp" else part.capitalize())
    return " ".join(words)


def _validate_metadata_type(metadata_path: Path, metadata: AssetMetadata, asset_kind: AssetKind) -> AssetMetadata:
    if metadata.type == asset_kind:
        return metadata
    msg = f"{metadata_path} declares type {metadata.type!r}, but its asset directory is for {asset_kind!r}"
    raise InstructionHubError(msg)


def _validate_sidecar_has_asset_file(kind_dir: Path, metadata_path: Path) -> None:
    asset_id = metadata_path.name.removesuffix(SIDECAR_METADATA_SUFFIX)
    for suffix in SUPPORTED_FILE_SUFFIXES:
        if (kind_dir / f"{asset_id}{suffix}").exists():
            return
    msg = f"sidecar metadata has no matching asset file: {metadata_path}"
    raise InstructionHubError(msg)
