"""Scanner for importing reusable agent assets and inventorying repo context."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from promptless_instruction_hub.assets import unsupported_support
from promptless_instruction_hub.config import PACKAGE_DIR
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import (
    JsonValue,
    file_hash,
    read_json_mapping,
    read_yaml_mapping,
    write_json,
    write_yaml,
)
from promptless_instruction_hub.models import Harness, PackageDefinition, TargetSupport

SKILL_SOURCE_DIR = Path(".agents/skills")
ROOT_MCP_CONFIG_CANDIDATES = (Path(".mcp.json"), Path("mcp.json"), Path("mcp.yaml"), Path("mcp.yml"))
CURSOR_MCP_CONFIG = Path(".cursor/mcp.json")
REPO_CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
MCP_SERVER_CONFIG_KEYS = {"command", "url", "type", "args", "env", "headers", "transport"}


@dataclass(frozen=True)
class ScanResult:
    """Summary of imported reusable assets and inventoried repo context."""

    imported_skills: tuple[str, ...]
    imported_mcps: tuple[str, ...]
    inventoried_context_files: tuple[str, ...]


def scan_hub(hub_root: Path, source_root: Path) -> ScanResult:
    """Import reusable assets and inventory repo context from a source repo."""

    root = hub_root.resolve()
    source = source_root.resolve()
    imported_skills = _import_skills(root, source)
    imported_mcps = _import_mcp_configs(root, source)
    imported_asset_refs = [f"skill:{skill_id}" for skill_id in imported_skills]
    imported_asset_refs.extend(f"mcp:{mcp_id}" for mcp_id in imported_mcps)
    _update_core_package(root, imported_asset_refs)
    context_files = _inventory_repo_context(root, source)
    return ScanResult(
        imported_skills=tuple(imported_skills),
        imported_mcps=tuple(imported_mcps),
        inventoried_context_files=tuple(context_files),
    )


def _import_skills(hub_root: Path, source_root: Path) -> list[str]:
    skills_root = source_root / SKILL_SOURCE_DIR
    if not skills_root.exists():
        return []
    imported: list[str] = []
    seen_asset_ids: dict[str, Path] = {}
    for source_skill in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        skill_file = _find_skill_file(source_skill)
        if skill_file is None:
            msg = f"source skill directory must contain SKILL.md: {source_skill}"
            raise InstructionHubError(msg)
        asset_id = _slugify(source_skill.name)
        previous_source = seen_asset_ids.get(asset_id)
        if previous_source is not None:
            msg = f"source skill directories {previous_source} and {source_skill} both map to asset id {asset_id!r}"
            raise InstructionHubError(msg)
        seen_asset_ids[asset_id] = source_skill
        destination = hub_root / "assets/skills" / asset_id
        _copy_skill_tree(source_skill, destination, skill_file)
        imported.append(asset_id)
    return imported


def _import_mcp_configs(hub_root: Path, source_root: Path) -> list[str]:
    imported: list[str] = []
    root_mcp_path = _first_existing_source_path(source_root, ROOT_MCP_CONFIG_CANDIDATES)
    root_servers: dict[str, JsonValue] = {}
    if root_mcp_path is not None:
        root_servers = _read_mcp_servers(root_mcp_path)
        _copy_mcp_config(
            hub_root=hub_root,
            source_root=source_root,
            source_path=root_mcp_path,
            asset_id="repo-mcp",
        )
        imported.append("repo-mcp")

    cursor_mcp_path = source_root / CURSOR_MCP_CONFIG
    if not cursor_mcp_path.is_file():
        return imported

    cursor_servers = _read_mcp_servers(cursor_mcp_path)
    if root_servers and _mcp_servers_subset(cursor_servers, root_servers):
        return imported

    _copy_mcp_config(
        hub_root=hub_root,
        source_root=source_root,
        source_path=cursor_mcp_path,
        asset_id="cursor-mcp",
        title="Cursor MCP Servers",
        support=_cursor_only_mcp_support(),
    )
    imported.append("cursor-mcp")
    return imported


def _update_core_package(hub_root: Path, imported_asset_refs: list[str]) -> None:
    package_path = hub_root / PACKAGE_DIR / "core.yaml"
    raw_package = read_yaml_mapping(package_path) if package_path.exists() else {"id": "core", "name": "Core"}
    package = PackageDefinition.model_validate(raw_package)
    includes = set(package.includes)
    includes.update(imported_asset_refs)
    write_yaml(
        package_path,
        {
            "id": package.id,
            "name": package.name,
            "owners": package.owners,
            "includes": sorted(includes),
        },
    )


def _inventory_repo_context(hub_root: Path, source_root: Path) -> list[str]:
    files: list[dict[str, JsonValue]] = []
    for file_name in REPO_CONTEXT_FILES:
        source_path = source_root / file_name
        if not source_path.exists():
            continue
        files.append(
            {
                "path": file_name,
                "sha256": file_hash(source_path),
                "bytes": source_path.stat().st_size,
                "imported": False,
                "reason": "repo-specific context is inventoried but not converted into org-wide assets",
            }
        )
    write_json(
        hub_root / ".promptless/inventory/repo-context.json",
        {
            "schema_version": 1,
            "files": files,
        },
    )
    return [str(file["path"]) for file in files]


def _copy_skill_tree(source_skill: Path, destination: Path, skill_file: Path) -> None:
    skip_names = {".pytest_cache", ".ruff_cache", "__pycache__"}
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for source_path in sorted(source_skill.rglob("*")):
        relative_path = source_path.relative_to(source_skill)
        if any(part in skip_names for part in relative_path.parts):
            continue
        if source_path.is_symlink():
            msg = f"source skill contains a symlink that cannot be imported: {source_path}"
            raise InstructionHubError(msg)
        target_relative_path = Path("SKILL.md") if source_path == skill_file else relative_path
        target_path = destination / target_relative_path
        if source_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)


def _copy_mcp_config(
    *,
    hub_root: Path,
    source_root: Path,
    source_path: Path,
    asset_id: str,
    title: str | None = None,
    support: dict[Harness, TargetSupport] | None = None,
) -> None:
    destination = hub_root / "assets/mcps" / f"{asset_id}{source_path.suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination)
    metadata_path = destination.with_suffix(".asset.yaml")
    if title is None and support is None:
        metadata_path.unlink(missing_ok=True)
        return
    metadata: dict[str, JsonValue] = {
        "source_path": str(source_path.relative_to(source_root)),
    }
    if title is not None:
        metadata["title"] = title
    if support is not None:
        metadata["support"] = {target: details.model_dump(exclude_none=True) for target, details in support.items()}
    write_yaml(
        metadata_path,
        metadata,
    )


def _find_skill_file(skill_dir: Path) -> Path | None:
    for child in sorted(skill_dir.iterdir()):
        if child.is_file() and child.name.lower() == "skill.md":
            return child
    return None


def _first_existing_source_path(source_root: Path, candidates: tuple[Path, ...]) -> Path | None:
    for relative_path in candidates:
        source_path = source_root / relative_path
        if source_path.is_file():
            return source_path
    return None


def _read_mcp_servers(path: Path) -> dict[str, JsonValue]:
    raw_data = read_json_mapping(path) if path.suffix == ".json" else read_yaml_mapping(path)
    mcp_servers = raw_data.get("mcpServers")
    if isinstance(mcp_servers, dict):
        return {str(key): value for key, value in mcp_servers.items()}
    servers = raw_data.get("servers")
    if isinstance(servers, dict):
        return {str(key): value for key, value in servers.items()}
    if _looks_like_mcp_server_config(raw_data):
        return {path.stem.removeprefix("."): raw_data}
    return {str(key): value for key, value in raw_data.items()}


def _mcp_servers_subset(candidate_servers: dict[str, JsonValue], source_servers: dict[str, JsonValue]) -> bool:
    return all(source_servers.get(name) == server_config for name, server_config in candidate_servers.items())


def _cursor_only_mcp_support() -> dict[Harness, TargetSupport]:
    support = unsupported_support("Cursor-specific MCP config is only distributed to Cursor.")
    support["cursor"] = TargetSupport(mode="native")
    return support


def _looks_like_mcp_server_config(value: dict[str, JsonValue]) -> bool:
    return any(key in value for key in MCP_SERVER_CONFIG_KEYS)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "skill"
