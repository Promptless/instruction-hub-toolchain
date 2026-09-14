"""Agent conversion contracts across validation, rendering, and the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from promptless_instruction_hub.agent_skills import AgentSkillWarning, read_agent_skill
from promptless_instruction_hub.cli import main
from promptless_instruction_hub.compiler import build_hub, init_hub, verify_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.validate.hub import validate_hub

from .helpers import _assert_codex_plugin_ingestion_contract, _snapshot_tree

ROLE = "researcher"
BODY = "\n# Research\n\nRead the supplied records.\n\nReturn the conflicting values.\n"


def _write_agent(
    hub: Path,
    metadata: dict[str, object] | None = None,
    *,
    role: str = ROLE,
    plugin: str = "pig",
) -> Path:
    init_hub(hub)
    source = hub / "assets/agents" / f"{role}.md"
    frontmatter = metadata if metadata is not None else {"name": role, "description": "Use when records disagree."}
    source.write_text("---\n" + yaml.safe_dump(frontmatter) + "---\n" + BODY)
    source.with_suffix(".asset.yaml").write_text(
        yaml.safe_dump({"support": {"codex": {"mode": "agent-skill"}, "claude": {"mode": "native"}}})
    )
    (hub / "plugins" / f"{plugin}.yaml").write_text(
        yaml.safe_dump({"id": plugin, "name": plugin.title(), "includes": [f"agent:{role}"]})
    )
    if plugin != "pig":
        config_path = hub / "hub.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["stable_plugins"] = ["pig", plugin]
        config_path.write_text(yaml.safe_dump(config))
    return source


def test_build_converts_agents_and_preserves_native_output_and_provenance(tmp_path: Path) -> None:
    """The generated skill is loadable while Claude receives the native definition."""

    source = _write_agent(
        tmp_path,
        {"name": "source-name", "description": "Use when records disagree.", "model": "opus", "color": "green"},
    )
    validation = validate_hub(tmp_path)
    parsed = read_agent_skill(validation.assets[f"agent:{ROLE}"])

    result = build_hub(tmp_path)

    codex_root = tmp_path / "dist/codex/pig"
    _assert_codex_plugin_ingestion_contract(codex_root)
    generated = (codex_root / "skills" / ROLE / "SKILL.md").read_text()
    metadata = yaml.safe_load(generated.split("---\n", 2)[1])
    assert metadata == {"name": ROLE, "description": parsed.description}
    # Compare the complete authored body, not particular instruction phrases.
    assert generated[-len(parsed.body) :] == parsed.body == BODY
    assert (tmp_path / "dist/claude/pig/agents" / source.name).read_bytes() == source.read_bytes()
    assert not (codex_root / "agents").exists()
    assert not (tmp_path / "dist/gemini/pig/skills" / ROLE).exists()
    assert not (tmp_path / "dist/cursor/pig/skills" / ROLE).exists()
    manifest = json.loads((codex_root / ".codex-plugin/plugin.json").read_text())
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["capabilities"] == ["Skills"]
    release = json.loads((tmp_path / "hub.release.json").read_text())
    assert release["assets"][0]["ref"] == f"agent:{ROLE}"
    assert release["assets"][0]["support"]["codex"] == {"mode": "agent-skill"}
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        ("Read, Bash(git:*)", ("Read", "Bash(git:*)")),
        ([" Read ", "mcp__repo__lookup"], ("Read", "mcp__repo__lookup")),
        ([], ()),
    ],
)
def test_tool_declarations_accept_strings_and_lists(
    tmp_path: Path, tools: str | list[str], expected: tuple[str, ...]
) -> None:
    """Both source tool fields keep their names without provider mapping."""

    _write_agent(tmp_path, {"description": "Read records.", "tools": tools, "disallowedTools": tools})

    validation = validate_hub(tmp_path)
    skill = read_agent_skill(validation.assets[f"agent:{ROLE}"])

    assert skill.tools == expected
    assert skill.disallowed_tools == expected
    assert validation.warnings == (AgentSkillWarning(f"agent:{ROLE}", ("tools", "disallowedTools")),)


def test_missing_tools_differs_from_explicit_empty_allowlist(tmp_path: Path) -> None:
    """An omitted declaration inherits tools; an explicit empty list allows none."""

    _write_agent(tmp_path)
    validation = validate_hub(tmp_path)
    skill = read_agent_skill(validation.assets[f"agent:{ROLE}"])
    assert skill.tools is None
    assert skill.disallowed_tools is None
    assert skill.warning is None

    _write_agent(tmp_path, {"description": "Read records.", "tools": []})
    validation = validate_hub(tmp_path)
    assert read_agent_skill(validation.assets[f"agent:{ROLE}"]).tools == ()
    assert validation.warnings == (AgentSkillWarning(f"agent:{ROLE}", ("tools",)),)


@pytest.mark.parametrize(
    ("metadata", "error"),
    [
        ({}, "description"),
        ({"description": " "}, "description"),
        ({"description": 12}, "description"),
        ({"description": "a" * 1025}, "1024"),
        ({"description": "Use <Task>"}, "angle-bracket"),
        ({"description": "Read records.", "hooks": {}}, "hooks"),
        ({"description": "Read records.", "permissionMode": "bypassPermissions"}, "permissionMode"),
        ({"description": "Read records.", "tools": "Read,,Bash"}, "tool name"),
        ({"description": "Read records.", "disallowedTools": ["Write", 4]}, "tool name"),
        ({"description": "Read records.", "tools": None}, "comma-separated"),
        ({"description": "Read records.", "tools": {}}, "comma-separated"),
    ],
)
def test_invalid_agent_metadata_is_rejected(tmp_path: Path, metadata: dict[str, object], error: str) -> None:
    """Invalid or untranslatable metadata fails before any output is generated."""

    _write_agent(tmp_path, metadata)

    with pytest.raises(InstructionHubError, match=error):
        build_hub(tmp_path)

    assert list((tmp_path / "dist").iterdir()) == []


@pytest.mark.parametrize(
    ("contents", "error"),
    [
        ("# No frontmatter\n", "starting with"),
        ("---\ndescription: Research\n", "must end"),
        ("---\ndescription: [\n---\nBody\n", "malformed YAML"),
        ("---\n- description\n---\nBody\n", "frontmatter"),
        ("---\ndescription: Research\n---\n\n", "procedure must not be empty"),
    ],
)
def test_invalid_markdown_is_rejected(tmp_path: Path, contents: str, error: str) -> None:
    """Missing delimiters, malformed YAML, and an empty procedure are actionable errors."""

    source = _write_agent(tmp_path)
    source.write_text(contents)

    with pytest.raises(InstructionHubError, match=error):
        validate_hub(tmp_path)


def test_description_limit_and_body_without_final_newline(tmp_path: Path) -> None:
    """The boundary length is accepted and the complete body survives conversion."""

    source = _write_agent(tmp_path, {"description": "a" * 1024})
    source.write_text(source.read_text().rstrip())
    build_hub(tmp_path)
    generated = (tmp_path / f"dist/codex/pig/skills/{ROLE}/SKILL.md").read_text()
    assert yaml.safe_load(generated.split("---\n", 2)[1])["description"] == "a" * 1024
    assert generated[-len(BODY.rstrip()) :] == BODY.rstrip()


@pytest.mark.parametrize(
    ("frontmatter", "error"),
    [
        ("description: Read.\ndisallowedTools: Write\ndisallowedTools: []\n", "duplicate.*disallowedTools"),
        ("description: Read.\ntools: []\ntools: Read\n", "duplicate.*tools"),
        ("description: Read.\n<<: {tools: Read}\n", "merge keys are unsupported"),
    ],
)
def test_duplicate_and_merged_fields_cannot_discard_restrictions(tmp_path: Path, frontmatter: str, error: str) -> None:
    """YAML cannot replace an explicit declaration before strict validation sees it."""

    source = _write_agent(tmp_path)
    source.write_text("---\n" + frontmatter + "---\n" + BODY)

    with pytest.raises(InstructionHubError, match=error):
        validate_hub(tmp_path)


@pytest.mark.parametrize("directory", [False, True])
def test_conversion_requires_markdown_file(tmp_path: Path, directory: bool) -> None:
    """Other asset layouts cannot accidentally be copied as a skill."""

    source = _write_agent(tmp_path)
    if directory:
        destination = source.with_suffix("")
        destination.mkdir()
        source.rename(destination / "AGENT.md")
        source.with_suffix(".asset.yaml").rename(destination / "asset.yaml")
    else:
        source.rename(source.with_suffix(".mdc"))

    with pytest.raises(InstructionHubError, match="requires a Markdown .md file"):
        validate_hub(tmp_path)


@pytest.mark.parametrize("target", ["claude", "cursor", "gemini"])
def test_agent_conversion_is_codex_only(tmp_path: Path, target: str) -> None:
    """Other targets need their own explicit conversion implementation."""

    source = _write_agent(tmp_path)
    source.with_suffix(".asset.yaml").write_text(yaml.safe_dump({"support": {target: {"mode": "agent-skill"}}}))

    with pytest.raises(InstructionHubError, match=f"not {target}"):
        validate_hub(tmp_path)


@pytest.mark.parametrize("mode", ["native", "agent-skill"])
def test_converted_agent_cannot_overwrite_authored_skill(tmp_path: Path, mode: str) -> None:
    """Colliding authored destinations are rejected by validation."""

    _write_agent(tmp_path)
    skill = tmp_path / "assets/skills" / ROLE
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: researcher\ndescription: Read.\n---\nRead records.\n")
    (skill / "asset.yaml").write_text(yaml.safe_dump({"support": {"codex": {"mode": mode}}}))
    (tmp_path / "plugins/pig.yaml").write_text(f"id: pig\nname: PIG\nincludes: [agent:{ROLE}, skill:{ROLE}]\n")

    with pytest.raises(InstructionHubError, match=f"skills/{ROLE} conflicts"):
        validate_hub(tmp_path)


def test_converted_agent_cannot_overwrite_managed_skill(tmp_path: Path) -> None:
    """Managed PIG skill names are reserved before rendering begins."""

    _write_agent(tmp_path, role="update-instruction-hub")

    with pytest.raises(InstructionHubError, match="compiler-managed skill"):
        validate_hub(tmp_path)


def test_native_skill_path_collision_is_case_insensitive(tmp_path: Path) -> None:
    """An aliased native skill cannot replace the generated role on macOS or Windows."""

    _write_agent(tmp_path)
    native = tmp_path / "assets/skills/Researcher"
    native.mkdir()
    (native / "SKILL.md").write_text("---\nname: authored\ndescription: Read.\n---\nRead records.\n")
    (native / "asset.yaml").write_text("id: authored\nsupport:\n  codex:\n    mode: native\n")
    (tmp_path / "plugins/pig.yaml").write_text(f"id: pig\nname: PIG\nincludes: [agent:{ROLE}, skill:authored]\n")

    with pytest.raises(InstructionHubError, match="skills/Researcher conflicts"):
        validate_hub(tmp_path)


def test_generated_skill_identity_length_is_checked_per_plugin(tmp_path: Path) -> None:
    """Codex's combined plugin and skill name limit is checked before publishing."""

    _write_agent(tmp_path, plugin="long-" + "a" * 60)

    with pytest.raises(InstructionHubError, match="exceeds 64 characters"):
        validate_hub(tmp_path)


def test_warnings_are_deduplicated_across_plugins_and_do_not_change_hashes(tmp_path: Path) -> None:
    """Warnings follow stable source assets, independent of how often they ship."""

    _write_agent(tmp_path, {"description": "Read records.", "disallowedTools": "Write"}, plugin="gtm")
    (tmp_path / "plugins/pig.yaml").write_text(f"id: pig\nname: PIG\nincludes: [agent:{ROLE}]\n")
    expected = (AgentSkillWarning(f"agent:{ROLE}", ("disallowedTools",)),)
    validation = validate_hub(tmp_path)
    before = _snapshot_tree(tmp_path)
    verified = verify_hub(tmp_path)
    assert _snapshot_tree(tmp_path) == before
    first = build_hub(tmp_path)
    second = build_hub(tmp_path, check=True)
    versioned = build_hub(tmp_path, version="1.0.0")

    assert (
        validation.warnings == verified.warnings == first.warnings == second.warnings == versioned.warnings == expected
    )
    assert first.release_hash == second.release_hash == verified.release_hash
    release = json.loads((tmp_path / "hub.release.json").read_text())
    assert "warnings" not in release


def test_unshipped_agent_does_not_emit_conversion_warning(tmp_path: Path) -> None:
    """Warnings describe the selected target output, not unused source assets."""

    _write_agent(tmp_path, {"description": "Read records.", "tools": "Read"})
    (tmp_path / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes: []\n")

    assert validate_hub(tmp_path).warnings == ()


@pytest.mark.parametrize("command", ["validate", "verify", "build"])
def test_cli_emits_one_warning_on_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str) -> None:
    """The CLI exposes conversion warnings without changing its stdout contract."""

    _write_agent(tmp_path, {"description": "Read records.", "tools": "Read", "disallowedTools": "Write"})
    warning = AgentSkillWarning(f"agent:{ROLE}", ("tools", "disallowedTools"))

    assert main([command, "--hub", str(tmp_path)]) == 0

    output = capsys.readouterr()
    assert output.err.splitlines() == [f"warning: {warning.message}"]
    assert len(output.out.splitlines()) == 1


def test_agent_without_conversion_keeps_native_metadata(tmp_path: Path) -> None:
    """Unconverted agent frontmatter is not subject to Codex conversion rules."""

    source = _write_agent(tmp_path, {"description": "a" * 2000, "hooks": {"Stop": []}})
    source.with_suffix(".asset.yaml").write_text("support:\n  claude:\n    mode: native\n")

    result = build_hub(tmp_path)

    assert result.warnings == ()
    assert (tmp_path / "dist/claude/pig/agents" / source.name).read_bytes() == source.read_bytes()
    assert not (tmp_path / f"dist/codex/pig/skills/{ROLE}").exists()
