"""Convert catalog requests to immutable sources without network access."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from promptless_instruction_hub.config import EXTERNAL_LOCK_PATH
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import read_json_mapping
from promptless_instruction_hub.models import (
    ExternalPluginDefinition,
    HubPluginDefinition,
    ResolvedExternalPluginDefinition,
    ResolvedGitSource,
    ResolvedHubPluginDefinition,
    StablePlugin,
)
from promptless_instruction_hub.validate.hub import ValidationResult


class ExternalPluginLock(BaseModel):
    """Resolved default-branch commits, keyed by stable external plugin ID."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    plugins: dict[str, ResolvedGitSource] = Field(default_factory=dict)


def load_external_resolutions(
    hub_root: Path, validation: ValidationResult[HubPluginDefinition]
) -> ValidationResult[ResolvedHubPluginDefinition]:
    """Resolve fixed pins directly and floating requests from a matching lock."""

    lock = ExternalPluginLock()
    if any(
        isinstance(plugin.definition, ExternalPluginDefinition) and plugin.definition.source.ref == "latest"
        for plugin in validation.stable_plugins
    ):
        path = hub_root / EXTERNAL_LOCK_PATH
        if not path.is_file():
            raise InstructionHubError(f"missing {path}; run `pig resolve-external` in the Hub source directory")
        lock = ExternalPluginLock.model_validate(read_json_mapping(path))
    return apply_external_resolutions(validation, lock)


def apply_external_resolutions(
    validation: ValidationResult[HubPluginDefinition], lock: ExternalPluginLock
) -> ValidationResult[ResolvedHubPluginDefinition]:
    """Build resolved stable plugins while preserving the original catalog requests."""

    stable_plugins: list[StablePlugin[ResolvedHubPluginDefinition]] = []
    for plugin in validation.stable_plugins:
        definition = plugin.definition
        if isinstance(definition, ExternalPluginDefinition):
            if definition.source.ref == "latest":
                source = lock.plugins.get(definition.id)
                if source is None or source.url != definition.source.url:
                    raise InstructionHubError(
                        f"{definition.id}: missing or mismatched external resolution; run `pig resolve-external`"
                    )
            else:
                source = ResolvedGitSource(type="git", url=definition.source.url, sha=definition.source.ref)
            resolved = ResolvedExternalPluginDefinition.model_validate(
                {**definition.model_dump(exclude={"source"}), "source": source}
            )
            stable_plugins.append(StablePlugin(definition=resolved, assets=plugin.assets))
        else:
            stable_plugins.append(StablePlugin(definition=definition, assets=plugin.assets))
    return ValidationResult(
        config=validation.config,
        plugins=validation.plugins,
        assets=validation.assets,
        stable_plugins=tuple(stable_plugins),
        warnings=validation.warnings,
    )
