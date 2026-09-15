"""Command compilation contracts at validation, build, and release boundaries."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import pytest
import yaml

from promptless_instruction_hub.compiler import build_hub, init_hub, verify_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.models import SUPPORTED_HARNESSES, Harness
from promptless_instruction_hub.release.versions import resolve_publish_version
from promptless_instruction_hub.validate.hub import validate_hub

from .helpers import _assert_codex_plugin_ingestion_contract, _snapshot_tree

NAME = "rebase-pr"
BODY = '\n# Rebase\n\nResolve conflicts, then report.\n\nQuotes: """; path: C:\\repo; Unicode: é 🚀; DEL: \x7f\n'


def _write_command(
    hub: Path,
    *,
    name: str = NAME,
    metadata: dict[str, object] | None = None,
    support: dict[str, object] | None = None,
    body: str = BODY,
) -> Path:
    init_hub(hub)
    source = hub / "assets/commands" / f"{name}.md"
    frontmatter = metadata if metadata is not None else {"description": "Rebase the current PR."}
    source.write_text("---\n" + yaml.safe_dump(frontmatter) + "---\n" + body, encoding="utf-8")
    if support is not None:
        source.with_suffix(".asset.yaml").write_text(yaml.safe_dump({"support": support}))
    (hub / "plugins/pig.yaml").write_text(f"id: pig\nname: PIG\nincludes: [command:{name}]\n")
    return source


@pytest.mark.parametrize("explicit_support", [False, True])
def test_build_preserves_name_body_manual_invocation_and_provenance(tmp_path: Path, explicit_support: bool) -> None:
    """Legacy native declarations and new commands produce one native entry per target."""

    support = {target: {"mode": "native"} for target in SUPPORTED_HARNESSES} if explicit_support else None
    _write_command(tmp_path, support=support)
    first = build_hub(tmp_path)
    for target in ("claude", "codex", "cursor"):
        root = tmp_path / f"dist/{target}/pig"
        generated = (root / f"skills/{NAME}/SKILL.md").read_text()
        metadata = yaml.safe_load(generated.split("---\n", 2)[1])
        expected: dict[str, str | bool] = {"name": NAME, "description": "Rebase the current PR."}
        if target == "codex":
            _assert_codex_plugin_ingestion_contract(root)
            policy = yaml.safe_load((root / f"skills/{NAME}/agents/openai.yaml").read_text())
            assert policy == {"policy": {"allow_implicit_invocation": False}}
        else:
            expected["disable-model-invocation"] = True
        assert metadata == expected
        assert generated.endswith(BODY)
        assert not (root / "commands").exists()
        manifest = json.loads((root / f".{target}-plugin/plugin.json").read_text())
        assert manifest["skills"] == "./skills/"
        assert "commands" not in manifest
    gemini = tmp_path / "dist/gemini/pig"
    assert tomllib.loads((gemini / f"commands/{NAME}.toml").read_text()) == {
        "description": "Rebase the current PR.",
        "prompt": BODY,
    }
    assert not (gemini / f"skills/{NAME}").exists()
    release = json.loads((tmp_path / "hub.release.json").read_text())
    assert release["assets"][0]["ref"] == f"command:{NAME}"
    assert release["assets"][0]["support"] == {target: {"mode": "native"} for target in SUPPORTED_HARNESSES}
    assert not list((tmp_path / "dist").rglob("source-command-*"))
    before = _snapshot_tree(tmp_path)
    assert verify_hub(tmp_path).release_hash == first.release_hash
    assert _snapshot_tree(tmp_path) == before
    assert build_hub(tmp_path, check=True).release_hash == first.release_hash


def test_explicit_support_table_preserves_undeclared_and_excluded_targets(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        support={"claude": {"mode": "native"}, "gemini": {"mode": "unsupported", "reason": "Not enabled."}},
        metadata={"description": "Prune old worktrees.", "argument-hint": "[stale-days]"},
        body="\nUse `$ARGUMENTS` days or 30; first argument: $0; indexed: $ARGUMENTS[0].\n",
    )
    build_hub(tmp_path)
    for target in ("codex", "cursor", "gemini"):
        assert not (tmp_path / f"dist/{target}/pig/skills/{NAME}").exists()
        assert not (tmp_path / f"dist/{target}/pig/commands").exists()
    generated = (tmp_path / f"dist/claude/pig/skills/{NAME}/SKILL.md").read_text()
    assert yaml.safe_load(generated.split("---\n", 2)[1])["argument-hint"] == "[stale-days]"
    assert "$ARGUMENTS[0]" in generated


@pytest.mark.parametrize(
    ("metadata", "error"),
    [
        ({}, "description"),
        ({"description": " "}, "description"),
        ({"description": 10}, "description"),
        ({"description": "x" * 1025}, "1024"),
        ({"description": "Use <command>"}, "angle-bracket"),
        ({"description": "Rebase.", "name": "other"}, "must match"),
        ({"description": "Rebase.", "allowed-tools": "Bash"}, "allowed-tools"),
        ({"description": "Rebase.", "context": "fork"}, "context"),
        ({"description": "Rebase.", "model": "opus"}, "model"),
        ({"description": "Rebase.", "disable-model-invocation": False}, "disable-model-invocation"),
        ({"description": "Rebase.", "user-invocable": False}, "user-invocable"),
    ],
)
def test_untranslatable_frontmatter_fails_before_output(
    tmp_path: Path, metadata: dict[str, object], error: str
) -> None:
    _write_command(tmp_path, metadata=metadata)
    with pytest.raises(InstructionHubError, match=error):
        build_hub(tmp_path)
    assert list((tmp_path / "dist").iterdir()) == []


@pytest.mark.parametrize(
    ("contents", "error"),
    [
        ("# Rebase\n", "starting with"),
        ("---\ndescription: Rebase\n", "must end"),
        ("---\ndescription: [\n---\nRebase\n", "malformed YAML"),
        ("---\n- description\n---\nRebase\n", "YAML mapping"),
        ("---\ndescription: Rebase\n---\n\n", "procedure must not be empty"),
        ("---\ndescription: Rebase\ndescription: Other\n---\nBody\n", "duplicate"),
        ("---\ndescription: Rebase\n<<: {allowed-tools: Bash}\n---\nBody\n", "merge keys"),
    ],
)
def test_malformed_source_is_rejected(tmp_path: Path, contents: str, error: str) -> None:
    _write_command(tmp_path).write_text(contents)
    with pytest.raises(InstructionHubError, match=error):
        validate_hub(tmp_path)


@pytest.mark.parametrize("target", SUPPORTED_HARNESSES)
@pytest.mark.parametrize("body", ["!`git status`", "!{git status}", "@{file.txt}", "{{args}}", "${CLAUDE_SKILL_DIR}"])
def test_host_dynamic_syntax_requires_explicit_native_override(tmp_path: Path, target: Harness, body: str) -> None:
    _write_command(tmp_path, support={target: {"mode": "native"}}, body=body)
    with pytest.raises(InstructionHubError, match="host-specific dynamic syntax"):
        validate_hub(tmp_path)


@pytest.mark.parametrize("target", ["codex", "cursor", "gemini"])
@pytest.mark.parametrize("body", ["Use $ARGUMENTS", "Use $ARGUMENTS[1]", "Use $1"])
def test_argument_substitutions_are_not_silently_lost(tmp_path: Path, target: str, body: str) -> None:
    _write_command(tmp_path, support={target: {"mode": "native"}}, body=body)
    with pytest.raises(InstructionHubError, match="argument substitution.*only for Claude"):
        validate_hub(tmp_path)


@pytest.mark.parametrize("target", ["codex", "cursor", "gemini"])
def test_argument_hints_are_not_silently_dropped(tmp_path: Path, target: str) -> None:
    _write_command(
        tmp_path,
        support={target: {"mode": "native"}},
        metadata={"description": "Rebase.", "argument-hint": "[branch]"},
    )
    with pytest.raises(InstructionHubError, match="argument-hint.*only for Claude"):
        validate_hub(tmp_path)


@pytest.mark.parametrize("target", SUPPORTED_HARNESSES)
@pytest.mark.parametrize("managed", [False, True])
def test_commands_cannot_shadow_authored_or_managed_skills(tmp_path: Path, target: Harness, managed: bool) -> None:
    name = "update-instruction-hub" if managed else NAME
    _write_command(tmp_path, name=name, support={target: {"mode": "native"}})
    if not managed:
        skill = tmp_path / "assets/skills" / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Authored skill\n---\nBody\n")
        (tmp_path / "plugins/pig.yaml").write_text(f"id: pig\nname: PIG\nincludes: [command:{name}, skill:{name}]\n")
    with pytest.raises(InstructionHubError, match="conflicts"):
        build_hub(tmp_path)
    assert list((tmp_path / "dist").iterdir()) == []


@pytest.mark.parametrize(
    ("target", "mode"),
    [("claude", "native"), ("codex", "native"), ("cursor", "native"), ("claude", "verbatim"), ("cursor", "verbatim")],
)
def test_command_cannot_shadow_external_plugin_managed_skill(tmp_path: Path, target: str, mode: str) -> None:
    _write_command(tmp_path, name="add-external-plugin", support={target: {"mode": mode}})
    with pytest.raises(InstructionHubError, match="compiler-managed skill"):
        build_hub(tmp_path)
    assert list((tmp_path / "dist").iterdir()) == []


def test_external_plugin_skill_name_is_available_for_gemini_commands(tmp_path: Path) -> None:
    _write_command(tmp_path, name="add-external-plugin", support={"gemini": {"mode": "native"}})
    build_hub(tmp_path)
    command = tmp_path / "dist/gemini/pig/commands/add-external-plugin.toml"
    assert tomllib.loads(command.read_text())["prompt"] == BODY


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_authored_skill_alias_cannot_shadow_external_plugin_managed_skill(tmp_path: Path, target: str) -> None:
    _write_command(tmp_path, support={target: {"mode": "native"}})
    hub_config = yaml.safe_load((tmp_path / "hub.yaml").read_text())
    hub_config["targets"] = [target]
    (tmp_path / "hub.yaml").write_text(yaml.safe_dump(hub_config))
    skill = tmp_path / "assets/skills/aliased"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: add-external-plugin\ndescription: Alias\n---\nBody\n")
    (tmp_path / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes: [skill:aliased]\n")
    with pytest.raises(InstructionHubError, match="invocation 'add-external-plugin' conflicts"):
        build_hub(tmp_path)
    assert list((tmp_path / "dist").iterdir()) == []


def test_command_cannot_overwrite_converted_agent(tmp_path: Path) -> None:
    _write_command(tmp_path)
    source = tmp_path / f"assets/agents/{NAME}.md"
    source.write_text("---\ndescription: Research\n---\nRead records.\n")
    source.with_suffix(".asset.yaml").write_text("support:\n  codex:\n    mode: agent-skill\n")
    (tmp_path / "plugins/pig.yaml").write_text(f"id: pig\nname: PIG\nincludes: [command:{NAME}, agent:{NAME}]\n")
    with pytest.raises(InstructionHubError, match="conflicts"):
        validate_hub(tmp_path)


@pytest.mark.parametrize("target", ["claude", "cursor", "gemini"])
def test_verbatim_preserves_native_semantics_and_release_mode(tmp_path: Path, target: str) -> None:
    source = _write_command(
        tmp_path,
        support={target: {"mode": "verbatim"}},
        metadata={"allowed-tools": "Bash", "context": "fork"},
        body="!`git status`\n$ARGUMENTS\n",
    )
    if target == "gemini":
        source = source.rename(source.with_suffix(".toml"))
        source.write_text('description = "Search"\nprompt = "Search {{args}} with !{git status}"\n')
    build_hub(tmp_path)
    assert (tmp_path / f"dist/{target}/pig/commands" / source.name).read_bytes() == source.read_bytes()
    assert not (tmp_path / f"dist/{target}/pig/skills/{NAME}").exists()
    release = json.loads((tmp_path / "hub.release.json").read_text())
    assert release["assets"][0]["support"][target] == {"mode": "verbatim"}
    assert build_hub(tmp_path, check=True).checked


def test_verbatim_codex_cannot_reintroduce_importer_names(tmp_path: Path) -> None:
    _write_command(tmp_path, support={"codex": {"mode": "verbatim"}})
    with pytest.raises(InstructionHubError, match="no native command format"):
        validate_hub(tmp_path)


@pytest.mark.parametrize("contents", ['prompt = "unterminated', 'description = "Missing prompt"', "prompt = 42"])
def test_invalid_native_gemini_commands_are_rejected(tmp_path: Path, contents: str) -> None:
    source = _write_command(tmp_path, support={"gemini": {"mode": "verbatim"}})
    source.rename(source.with_suffix(".toml")).write_text(contents)
    with pytest.raises(InstructionHubError, match="TOML|prompt"):
        validate_hub(tmp_path)


@pytest.mark.parametrize("directory", [False, True])
def test_portable_conversion_requires_markdown_file(tmp_path: Path, directory: bool) -> None:
    source = _write_command(tmp_path)
    if directory:
        destination = source.with_suffix("")
        destination.mkdir()
        source.rename(destination / "COMMAND.md")
    else:
        source.rename(source.with_suffix(".mdc"))
    with pytest.raises(InstructionHubError, match="requires a Markdown .md file"):
        validate_hub(tmp_path)


def test_codex_combined_plugin_command_name_limit(tmp_path: Path) -> None:
    _write_command(tmp_path, name="a" * 61)
    with pytest.raises(InstructionHubError, match="combined Codex.*exceeds 64"):
        validate_hub(tmp_path)


def test_body_without_final_newline_survives_conversion(tmp_path: Path) -> None:
    _write_command(tmp_path, body="Body without newline")
    build_hub(tmp_path)
    assert (tmp_path / f"dist/codex/pig/skills/{NAME}/SKILL.md").read_text().endswith("Body without newline")
    assert (
        tomllib.loads((tmp_path / f"dist/gemini/pig/commands/{NAME}.toml").read_text())["prompt"]
        == "Body without newline"
    )


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_skill_frontmatter_alias_cannot_duplicate_command_invocation(tmp_path: Path, target: str) -> None:
    _write_command(tmp_path, support={target: {"mode": "native"}})
    skill = tmp_path / "assets/skills/aliased"
    skill.mkdir()
    (skill / "SKILL.md").write_text(f"---\nname: {NAME}\ndescription: Alias\n---\nBody\n")
    (tmp_path / "plugins/pig.yaml").write_text(f"id: pig\nname: PIG\nincludes: [command:{NAME}, skill:aliased]\n")
    with pytest.raises(InstructionHubError, match=f"invocation '{NAME}' conflicts"):
        validate_hub(tmp_path)


def test_native_skill_destination_collision_is_case_insensitive(tmp_path: Path) -> None:
    _write_command(tmp_path, support={"claude": {"mode": "native"}})
    skill = tmp_path / "assets/skills/Rebase-Pr"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: other\ndescription: Alias\n---\nBody\n")
    (skill / "asset.yaml").write_text("id: other\nsupport:\n  claude:\n    mode: native\n")
    (tmp_path / "plugins/pig.yaml").write_text(f"id: pig\nname: PIG\nincludes: [command:{NAME}, skill:other]\n")
    with pytest.raises(InstructionHubError, match="skills/Rebase-Pr conflicts"):
        validate_hub(tmp_path)


def test_verbatim_command_cannot_shadow_authored_skill(tmp_path: Path) -> None:
    _write_command(tmp_path, support={"claude": {"mode": "verbatim"}})
    skill = tmp_path / f"assets/skills/{NAME}"
    skill.mkdir()
    (skill / "SKILL.md").write_text(f"---\nname: {NAME}\ndescription: Skill\n---\nBody\n")
    (tmp_path / "plugins/pig.yaml").write_text(f"id: pig\nname: PIG\nincludes: [command:{NAME}, skill:{NAME}]\n")
    with pytest.raises(InstructionHubError, match=f"invocation '{NAME}' conflicts"):
        validate_hub(tmp_path)


def test_switching_from_verbatim_to_conversion_removes_old_command_and_bumps_release(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    source = _write_command(hub, support={"claude": {"mode": "verbatim"}})
    build_hub(hub)
    previous = tmp_path / "previous"
    shutil.copytree(hub, previous)
    # The authoritative release reader understands the new support mode.
    assert resolve_publish_version(hub, previous_release_root=previous) == "0.1.0"
    source.with_suffix(".asset.yaml").write_text("support:\n  claude:\n    mode: native\n")
    assert resolve_publish_version(hub, previous_release_root=previous) == "0.1.1"
    build_hub(hub)
    assert not (hub / "dist/claude/pig/commands").exists()
    assert (hub / f"dist/claude/pig/skills/{NAME}/SKILL.md").exists()


def test_only_enabled_hub_targets_validate_command_semantics(tmp_path: Path) -> None:
    _write_command(tmp_path, body="Use $ARGUMENTS.")
    config_path = tmp_path / "hub.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["targets"] = ["claude"]
    config_path.write_text(yaml.safe_dump(config))
    assert build_hub(tmp_path).target_count == 1


def test_verbatim_is_not_a_general_asset_passthrough_mode(tmp_path: Path) -> None:
    init_hub(tmp_path)
    source = tmp_path / "assets/agents/research.md"
    source.write_text("Research.")
    source.with_suffix(".asset.yaml").write_text("support:\n  claude:\n    mode: verbatim\n")
    with pytest.raises(InstructionHubError, match="declares unsupported mode 'verbatim'"):
        validate_hub(tmp_path)


def test_invalid_source_does_not_replace_previous_build(tmp_path: Path) -> None:
    source = _write_command(tmp_path)
    build_hub(tmp_path)
    before = _snapshot_tree(tmp_path / "dist")
    source.write_text("---\ndescription: Rebase.\nallowed-tools: Bash\n---\nRebase\n")
    with pytest.raises(InstructionHubError, match="allowed-tools"):
        build_hub(tmp_path)
    assert _snapshot_tree(tmp_path / "dist") == before
