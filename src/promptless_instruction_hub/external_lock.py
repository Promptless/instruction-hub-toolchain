"""Offline use of the exact Git commits selected for floating external sources."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from promptless_instruction_hub.config import EXTERNAL_LOCK_PATH
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import read_json_mapping
from promptless_instruction_hub.models import ExternalGitSource, ExternalPluginDefinition, LatestExternalGitSource
from promptless_instruction_hub.validate.hub import ValidationResult


class ExternalPluginLock(BaseModel):
    """Resolved default-branch commits, keyed by stable external plugin ID."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    plugins: dict[str, ExternalGitSource] = Field(default_factory=dict)


def load_external_resolutions(hub_root: Path, validation: ValidationResult) -> ValidationResult:
    """Compile floating sources only from a matching lock, without network access."""

    if not any(
        isinstance(plugin.definition, ExternalPluginDefinition)
        and isinstance(plugin.definition.source, LatestExternalGitSource)
        for plugin in validation.stable_plugins
    ):
        return validation
    path = hub_root / EXTERNAL_LOCK_PATH
    if not path.is_file():
        raise InstructionHubError(f"missing {path}; run `pig resolve-external` in the Hub source directory")
    lock = ExternalPluginLock.model_validate(read_json_mapping(path))
    return apply_external_resolutions(validation, lock)


def apply_external_resolutions(validation: ValidationResult, lock: ExternalPluginLock) -> ValidationResult:
    """Replace selected floating declarations with their locked immutable sources."""

    plugins = dict(validation.plugins)
    stable_plugins = []
    for plugin in validation.stable_plugins:
        definition = plugin.definition
        if isinstance(definition, ExternalPluginDefinition) and isinstance(definition.source, LatestExternalGitSource):
            source = lock.plugins.get(definition.id)
            if source is None or source.url != definition.source.url:
                raise InstructionHubError(
                    f"{definition.id}: missing or mismatched external resolution; run `pig resolve-external`"
                )
            definition = definition.model_copy(update={"source": source})
            plugins[definition.id] = definition
            plugin = replace(plugin, definition=definition)
        stable_plugins.append(plugin)
    return replace(validation, plugins=plugins, stable_plugins=tuple(stable_plugins))
