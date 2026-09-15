"""Compile explicitly invoked commands into each harness's supported format."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.models import Harness, LoadedAsset

# These constructs are interpreted by a host before the model reads the body.
# Never silently turn them into inert prose or activate a different host's macro.
_DYNAMIC_SYNTAX = re.compile(r"!`|!\{|@\{|\{\{.*?\}\}|\$\{?(?:CLAUDE|CODEX)_[A-Z_]+", re.DOTALL)
_ARGUMENT_SYNTAX = re.compile(r"\$ARGUMENTS\b|\$\d+\b")


class _CommandFrontmatter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str | None = Field(default=None, min_length=1)
    description: str = Field(min_length=1, max_length=1024)
    argument_hint: str | None = Field(default=None, alias="argument-hint", min_length=1)
    disable_model_invocation: Literal[True] = Field(default=True, alias="disable-model-invocation")
    user_invocable: Literal[True] = Field(default=True, alias="user-invocable")

    @field_validator("description", "argument_hint", mode="before")
    @classmethod
    def trim_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("description")
    @classmethod
    def portable_description(cls, value: str) -> str:
        if "<" in value or ">" in value:
            raise ValueError("use a portable description without angle-bracket invocation syntax")
        return value


@dataclass(frozen=True)
class Command:
    """Validated command metadata and its complete Markdown procedure."""

    name: str
    description: str
    body: str
    argument_hint: str | None


def _error(asset: LoadedAsset, target: Harness, message: str) -> InstructionHubError:
    return InstructionHubError(
        f"{asset.ref} ({target}): {message}. Use portable Markdown, exclude this target, "
        "or use mode: verbatim for a host-native command (Codex requires a skill asset)."
    )


def read_command(asset: LoadedAsset, target: Harness) -> Command:
    """Reject unsupported semantics before output is generated."""

    if not asset.path.is_file() or asset.path.suffix != ".md":
        raise _error(asset, target, "command conversion requires a Markdown .md file")
    if len(asset.id) > 64:
        raise _error(asset, target, "command name exceeds 64 characters")
    lines = asset.path.read_text(encoding="utf-8").splitlines(keepends=True)
    if not lines or lines[0] != "---\n":
        raise _error(asset, target, "command conversion requires YAML frontmatter starting with ---")
    closing = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\n") == "---"), None)
    if closing is None:
        raise _error(asset, target, "YAML frontmatter must end with ---")
    frontmatter = "".join(lines[1:closing])
    try:
        node = yaml.compose(frontmatter, Loader=yaml.SafeLoader)
        if not isinstance(node, yaml.MappingNode):
            raise _error(asset, target, "command frontmatter must be a YAML mapping")
        keys: set[str] = set()
        for key, _ in node.value:
            if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                raise _error(asset, target, "frontmatter keys must be strings; YAML merge keys are unsupported")
            if key.value in keys:
                raise _error(asset, target, f"duplicate command frontmatter field {key.value!r}")
            keys.add(key.value)
        metadata = _CommandFrontmatter.model_validate(yaml.safe_load(frontmatter))
    except yaml.YAMLError as exc:
        raise _error(asset, target, "malformed YAML frontmatter") from exc
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'frontmatter'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise _error(asset, target, f"cannot convert command frontmatter: {details}") from exc
    if metadata.name is not None and metadata.name != asset.id:
        raise _error(asset, target, "frontmatter name must match the command asset id")
    body = "".join(lines[closing + 1 :])
    if not body.strip():
        raise _error(asset, target, "command procedure must not be empty")
    dynamic = _DYNAMIC_SYNTAX.search(body)
    if dynamic is not None:
        raise _error(asset, target, f"host-specific dynamic syntax {dynamic.group()!r} cannot be converted")
    if target != "claude":
        if metadata.argument_hint is not None:
            raise _error(asset, target, "argument-hint is supported only for Claude conversion")
        argument = _ARGUMENT_SYNTAX.search(body)
        if argument is not None:
            raise _error(asset, target, f"argument substitution {argument.group()!r} is supported only for Claude")
    return Command(asset.id, metadata.description, body, metadata.argument_hint)


def validate_verbatim_command(asset: LoadedAsset, target: Harness) -> None:
    """Check the native file format without rewriting host-specific semantics."""

    if target == "codex":
        raise InstructionHubError(
            f"{asset.ref} (codex): no native command format; use a skill asset instead of verbatim"
        )
    suffix = ".toml" if target == "gemini" else ".md"
    if not asset.path.is_file() or asset.path.suffix != suffix:
        raise InstructionHubError(f"{asset.ref} ({target}): verbatim commands require a {suffix} file")
    if target == "gemini":
        try:
            data = tomllib.loads(asset.path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise InstructionHubError(f"{asset.ref} (gemini): malformed command TOML") from exc
        if not isinstance(data.get("prompt"), str) or not data["prompt"].strip():
            raise InstructionHubError(f"{asset.ref} (gemini): native command requires a nonempty prompt string")


def render_command(target_root: Path, target: Harness, command: Command) -> None:
    """Write one manual entry with the authored command name."""

    if target == "gemini":
        path = target_root / "commands" / f"{command.name}.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"description = {_toml_string(command.description)}\nprompt = {_toml_string(command.body)}\n",
            encoding="utf-8",
        )
        return
    destination = target_root / "skills" / command.name
    destination.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, str | bool] = {"name": command.name, "description": command.description}
    if target == "codex":
        policy_path = destination / "agents/openai.yaml"
        policy_path.parent.mkdir()
        policy_path.write_text("policy:\n  allow_implicit_invocation: false\n", encoding="utf-8")
    else:
        metadata["disable-model-invocation"] = True
        if command.argument_hint is not None:
            metadata["argument-hint"] = command.argument_hint
    (destination / "SKILL.md").write_text(
        "---\n" + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True) + "---\n" + command.body,
        encoding="utf-8",
    )


def _toml_string(value: str) -> str:
    # JSON escaping works for TOML basic strings except literal DEL and Unicode
    # surrogate pairs. Keep non-ASCII characters literal and escape DEL.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
