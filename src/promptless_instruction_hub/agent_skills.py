"""Convert Markdown agent definitions into skills that delegate their procedure."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.models import LoadedAsset

ToolRestrictionField = Literal["tools", "disallowedTools"]


@dataclass(frozen=True)
class AgentSkillWarning:
    """Tool restriction declarations that become advisory in a Codex skill."""

    asset_ref: str
    fields: tuple[ToolRestrictionField, ...]

    @property
    def message(self) -> str:
        """Describe the conversion limitation for CLI users."""

        return (
            f"{self.asset_ref} (codex): {', '.join(self.fields)} become advisory instructions; "
            "Codex does not enforce agent tool restrictions through skill metadata."
        )


@dataclass(frozen=True)
class AgentSkill:
    """Validated source content for a generated Codex skill.

    A missing tools declaration inherits available tools; an empty tuple declares
    that the specialist must not use tools. The body retains the source Markdown.
    """

    name: str
    description: str
    body: str
    tools: tuple[str, ...] | None
    disallowed_tools: tuple[str, ...] | None

    @property
    def warning(self) -> AgentSkillWarning | None:
        """Return the advisory restriction diagnostic, if any fields declare it."""

        fields: list[ToolRestrictionField] = []
        if self.tools is not None:
            fields.append("tools")
        if self.disallowed_tools is not None:
            fields.append("disallowedTools")
        if not fields:
            return None
        return AgentSkillWarning(asset_ref=f"agent:{self.name}", fields=tuple(fields))


class _AgentFrontmatter(BaseModel):
    """The agent fields whose conversion semantics the compiler supports."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str | None = Field(default=None, min_length=1)
    description: str = Field(min_length=1, max_length=1024)
    tools: tuple[str, ...] | None = None
    disallowedTools: tuple[str, ...] | None = None
    model: str | None = None
    color: str | None = None

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        """Trim descriptions without shortening or rewriting their content."""

        if isinstance(value, str):
            if "<" in value or ">" in value:
                raise ValueError("use a portable description without angle-bracket invocation syntax")
            return value.strip()
        return value

    @field_validator("tools", "disallowedTools", mode="before")
    @classmethod
    def parse_tools(cls, value: object) -> tuple[str, ...]:
        """Read comma-separated names or YAML lists, rejecting empty names."""

        if isinstance(value, str):
            values = value.split(",")
        elif isinstance(value, list):
            values = value
        else:
            raise ValueError("expected a comma-separated string or a list of tool names")
        names: list[str] = []
        for item in values:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("each tool name must be a nonempty string")
            names.append(item.strip())
        return tuple(names)


def read_agent_skill(asset: LoadedAsset) -> AgentSkill:
    """Validate a Markdown agent for Codex conversion, or raise InstructionHubError."""

    if not asset.path.is_file() or asset.path.suffix != ".md":
        raise InstructionHubError(f"{asset.ref}: Codex agent-skill conversion requires a Markdown .md file")
    lines = asset.path.read_text(encoding="utf-8").splitlines(keepends=True)
    if not lines or lines[0] != "---\n":
        raise InstructionHubError(f"{asset.ref}: agent-skill conversion requires YAML frontmatter starting with ---")
    closing = next((index for index, line in enumerate(lines[1:], 1) if line.rstrip("\n") == "---"), None)
    if closing is None:
        raise InstructionHubError(f"{asset.ref}: YAML frontmatter must end with ---")
    try:
        frontmatter = "".join(lines[1:closing])
        _validate_frontmatter_keys(frontmatter, asset.ref)
        metadata = _AgentFrontmatter.model_validate(yaml.safe_load(frontmatter))
    except yaml.YAMLError as exc:
        raise InstructionHubError(f"{asset.ref}: malformed YAML frontmatter") from exc
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'frontmatter'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise InstructionHubError(f"{asset.ref}: cannot convert agent frontmatter for Codex: {details}") from exc
    body = "".join(lines[closing + 1 :])
    if not body.strip():
        raise InstructionHubError(f"{asset.ref}: agent procedure must not be empty")
    return AgentSkill(
        name=asset.id,
        description=metadata.description,
        body=body,
        tools=metadata.tools,
        disallowed_tools=metadata.disallowedTools,
    )


def _validate_frontmatter_keys(frontmatter: str, asset_ref: str) -> None:
    node = yaml.compose(frontmatter, Loader=yaml.SafeLoader)
    if not isinstance(node, yaml.MappingNode):
        raise InstructionHubError(f"{asset_ref}: agent frontmatter must be a YAML mapping")
    keys: set[str] = set()
    for key, _ in node.value:
        if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str":
            raise InstructionHubError(
                f"{asset_ref}: agent frontmatter keys must be strings; YAML merge keys are unsupported"
            )
        if key.value in keys:
            raise InstructionHubError(f"{asset_ref}: duplicate agent frontmatter field {key.value!r}")
        keys.add(key.value)


def render_agent_skill(skill: AgentSkill) -> str:
    """Render portable frontmatter, delegation instructions, and the full source body."""

    frontmatter = f"---\nname: {json.dumps(skill.name)}\ndescription: {json.dumps(skill.description)}\n---"
    delegation = f"""## Delegation

This skill runs the `{skill.name}` role in a subagent.

If you were explicitly assigned this role by a parent agent, perform the procedure
below directly. Do not delegate this same role again. Follow the procedure's
instructions for any work assigned to other roles.

Otherwise:

1. Use the available subagent tools to launch one child for this role. If subagent
   execution is unavailable, stop and explain; do not perform the procedure yourself.
2. Tell the child it is the specialist assigned to `{skill.name}`. Pass the task,
   relevant context, and this skill's location so it can read the full procedure.
   Do not set a model or reasoning override.
3. Relay the child's clarification requests to the user and send the answers back.
   Required answers must arrive before the child proceeds with dependent work.
4. Return the child's result to the user.

As the assigned specialist, send clarification requests to your parent and wait for
required answers. Return your completed result to the parent.
"""
    restrictions: list[str] = []
    if skill.tools is not None:
        if skill.tools:
            restrictions.append(
                "Use only these declared tools: " + ", ".join(f"`{name}`" for name in skill.tools) + "."
            )
        else:
            restrictions.append("Do not use tools while performing the procedure.")
    if skill.disallowed_tools:
        restrictions.append(
            "Do not use these declared tools: " + ", ".join(f"`{name}`" for name in skill.disallowed_tools) + "."
        )
    if restrictions:
        delegation += (
            "\n## Advisory tool restrictions\n\n"
            "These are instructions for the specialist. Codex does not enforce them\n"
            "through skill metadata. Tool names are copied from the source definition;\n"
            "the compiler does not map names between hosts.\n\n"
            + "\n".join(f"- {restriction}" for restriction in restrictions)
            + "\n"
        )
    return f"{frontmatter}\n\n{delegation}\n## Procedure\n{skill.body}"
