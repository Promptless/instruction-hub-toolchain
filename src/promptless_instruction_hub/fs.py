"""Filesystem helpers for deterministic Instruction Hub output."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path

import yaml

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def validate_json_value(value: object, path: Path | str, key_path: tuple[str, ...] = ()) -> JsonValue:
    """Return a JSON-compatible value or raise for YAML-native data."""

    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            msg = f"{path} contains a non-finite float at {_format_key_path(key_path)}"
            raise ValueError(msg)
        return value
    if isinstance(value, list):
        return [validate_json_value(child, path, (*key_path, str(index))) for index, child in enumerate(value)]
    if isinstance(value, dict):
        validated: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                msg = f"{path} contains a non-string mapping key at {_format_key_path(key_path)}"
                raise ValueError(msg)
            validated[key] = validate_json_value(child, path, (*key_path, key))
        return validated
    msg = f"{path} contains a non-JSON value at {_format_key_path(key_path)}: {type(value).__name__}"
    raise ValueError(msg)


def read_yaml_mapping(path: Path) -> dict[str, JsonValue]:
    """Read a YAML file and require a top-level mapping."""

    raw_data = yaml.safe_load(path.read_text()) if path.exists() else {}
    validated = validate_json_value(raw_data, path)
    if validated is None:
        return {}
    if not isinstance(validated, dict):
        msg = f"{path} must contain a YAML mapping"
        raise ValueError(msg)
    return validated


def read_json_mapping(path: Path) -> dict[str, JsonValue]:
    """Read a JSON file and require a top-level object."""

    raw_data = json.loads(path.read_text())
    validated = validate_json_value(raw_data, path)
    if not isinstance(validated, dict):
        msg = f"{path} must contain a JSON object"
        raise ValueError(msg)
    return validated


def write_yaml(path: Path, data: JsonValue) -> None:
    """Write YAML with stable key ordering disabled for human readability."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def write_json(path: Path, data: JsonValue) -> None:
    """Write stable, pretty JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def copy_tree(source: Path, destination: Path, *, skip_names: set[str] | None = None) -> None:
    """Copy a directory tree while skipping generated metadata files."""

    skipped = skip_names or set()
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for source_path in sorted(source.rglob("*")):
        relative_path = source_path.relative_to(source)
        if any(part in skipped for part in relative_path.parts):
            continue
        target_path = destination / relative_path
        if source_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)


def directory_hash(path: Path, *, skip_names: set[str] | None = None) -> str:
    """Return a sha256 hash for file names and bytes under a directory."""

    skipped = skip_names or set()
    digest = hashlib.sha256()
    if not path.exists():
        return digest.hexdigest()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative_path = child.relative_to(path)
        if any(part in skipped for part in relative_path.parts):
            continue
        digest.update(str(relative_path).encode())
        digest.update(b"\0")
        digest.update(child.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def file_hash(path: Path) -> str:
    """Return the sha256 hash for one file."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_tree(source: Path, destination: Path) -> None:
    """Replace one generated directory tree with another."""

    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)


def trees_equal(left: Path, right: Path) -> bool:
    """Compare two directory trees by relative file names and bytes."""

    return _tree_fingerprint(left) == _tree_fingerprint(right)


def _tree_fingerprint(path: Path) -> dict[str, str]:
    fingerprint: dict[str, str] = {}
    if not path.exists():
        return fingerprint
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        fingerprint[str(child.relative_to(path))] = file_hash(child)
    return fingerprint


def _format_key_path(key_path: tuple[str, ...]) -> str:
    if not key_path:
        return "<root>"
    return ".".join(key_path)
