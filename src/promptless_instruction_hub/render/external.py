"""Native marketplace entries for pinned upstream plugins."""

from __future__ import annotations

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue
from promptless_instruction_hub.models import (
    ExternalGitSource,
    ExternalPluginDefinition,
    ExternalPluginHarness,
    ExternalPluginTarget,
)


def external_marketplace_entry(plugin: ExternalPluginDefinition, target: ExternalPluginHarness) -> dict[str, JsonValue]:
    """Point the host at upstream files without overriding their publisher or version."""

    if not isinstance(plugin.source, ExternalGitSource):
        raise InstructionHubError(f"{plugin.id}: external source must be resolved before rendering")
    path = plugin.targets[target].path
    source: dict[str, JsonValue] = {
        "source": "url" if path == "." else "git-subdir",
        "url": plugin.source.url,
        "sha": plugin.source.ref,
    }
    if path != ".":
        source["path"] = path
    entry: dict[str, JsonValue] = {"name": plugin.id, "source": source}
    if target == "codex":
        entry["policy"] = {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}
        entry["category"] = "Productivity"
    return entry


def validate_external_marketplace_source(source: dict[str, JsonValue]) -> None:
    """Validate a generated external source before preserving it on the source branch."""

    kind = source.get("source")
    expected = {"source", "url", "sha", "path"} if kind == "git-subdir" else {"source", "url", "sha"}
    if kind not in {"url", "git-subdir"} or set(source) != expected:
        raise ValueError("Expected a pinned external Git marketplace source")
    ExternalGitSource.model_validate({"type": "git", "url": source["url"], "ref": source["sha"]})
    if kind == "git-subdir":
        target = ExternalPluginTarget.model_validate({"path": source["path"]})
        if target.path == ".":
            raise ValueError("Repository-root plugins must use a url marketplace source")
