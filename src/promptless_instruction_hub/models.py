"""Typed models for Instruction Hub source and generated manifests."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Harness = Literal["claude", "codex", "gemini", "cursor"]
AssetKind = Literal["skill", "rule", "agent", "command", "hook", "mcp"]
SupportMode = Literal["agent-skill", "native", "projected", "unsupported"]

SUPPORTED_HARNESSES: tuple[Harness, ...] = ("claude", "codex", "gemini", "cursor")
ASSET_KINDS: tuple[AssetKind, ...] = ("skill", "rule", "agent", "command", "hook", "mcp")
IDENTIFIER_PATTERN = r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"
IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def validate_identifier(value: str, field_name: str) -> str:
    """Validate a kebab-case identifier used in generated file paths."""

    if IDENTIFIER_RE.match(value) is None:
        msg = f"{field_name} must be a kebab-case identifier"
        raise ValueError(msg)
    return value


def validate_asset_ref(value: str) -> str:
    """Validate a package reference in `kind:id` form."""

    kind, separator, asset_id = value.partition(":")
    if separator != ":":
        msg = "package includes must use kind:id asset references"
        raise ValueError(msg)
    if kind not in ASSET_KINDS:
        msg = f"unknown asset kind in package reference: {kind}"
        raise ValueError(msg)
    validate_identifier(asset_id, "asset reference id")
    return value


class TargetSupport(BaseModel):
    """Declared delivery behavior for one asset on one harness."""

    model_config = ConfigDict(extra="forbid")

    mode: SupportMode
    reason: str | None = None

    @model_validator(mode="after")
    def require_reason_for_unsupported(self) -> "TargetSupport":
        """Require an explicit explanation when an asset is not distributed."""

        if self.mode == "unsupported" and not self.reason:
            msg = "unsupported target support requires a reason"
            raise ValueError(msg)
        return self


class HubConfig(BaseModel):
    """Root `hub.yaml` configuration."""

    model_config = ConfigDict(extra="forbid")

    org: str = Field(min_length=1)
    plugin_id: str
    plugin_name: str = Field(min_length=1)
    plugin_version: str
    stable_packages: list[str] = Field(default_factory=lambda: ["core"], min_length=1)
    targets: list[Harness] = Field(default_factory=lambda: list(SUPPORTED_HARNESSES), min_length=1)

    @field_validator("plugin_id")
    @classmethod
    def validate_plugin_id(cls, value: str) -> str:
        """Ensure plugin IDs are stable kebab-case identifiers."""

        return validate_identifier(value, "plugin_id")

    @field_validator("plugin_version")
    @classmethod
    def validate_plugin_version(cls, value: str) -> str:
        """Require SemVer for native plugin versions."""

        if SEMVER_RE.match(value) is None:
            msg = "plugin_version must be SemVer, for example 1.2.3"
            raise ValueError(msg)
        return value

    @field_validator("stable_packages")
    @classmethod
    def validate_stable_packages(cls, value: list[str]) -> list[str]:
        """Ensure stable package references are valid package IDs."""

        _validate_unique(value, "stable_packages")
        return [validate_identifier(package_id, "stable package id") for package_id in value]

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, value: list[Harness]) -> list[Harness]:
        """Ensure target lists do not contain duplicate harnesses."""

        _validate_unique(value, "targets")
        return value


class PackageDefinition(BaseModel):
    """Product-facing package grouping for governed assets."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = Field(min_length=1)
    owners: list[str] = Field(default_factory=list)
    includes: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        """Ensure package IDs are stable kebab-case identifiers."""

        return validate_identifier(value, "package id")

    @field_validator("includes")
    @classmethod
    def validate_includes(cls, value: list[str]) -> list[str]:
        """Ensure package includes are structured asset references."""

        _validate_unique(value, "package includes")
        return [validate_asset_ref(asset_ref) for asset_ref in value]


class AssetMetadata(BaseModel):
    """Optional per-asset metadata stored next to source content."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: AssetKind
    title: str | None = None
    source_path: str | None = None
    support: dict[Harness, TargetSupport] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        """Ensure asset IDs are safe generated path segments."""

        return validate_identifier(value, "asset id")


class LoadedAsset(BaseModel):
    """Resolved asset with source path, metadata, and content hash."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    type: AssetKind
    path: Path
    metadata: AssetMetadata
    content_hash: str

    @property
    def ref(self) -> str:
        """Return the package reference form for this asset."""

        return f"{self.type}:{self.id}"

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        """Ensure loaded asset IDs remain safe path segments."""

        return validate_identifier(value, "asset id")


@dataclass(frozen=True)
class StablePackage:
    """Resolved stable package and the assets to render into its plugin payload."""

    definition: PackageDefinition
    assets: tuple[LoadedAsset, ...]


def _validate_unique(values: Sequence[str], field_name: str) -> None:
    if len(set(values)) == len(values):
        return
    msg = f"{field_name} must not contain duplicates"
    raise ValueError(msg)
