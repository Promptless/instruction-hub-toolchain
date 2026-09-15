from __future__ import annotations

import json
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.errors import InstructionHubError
from tests.config_helpers import enable_trace_ingestion


def bundle(root: Path, name: str, configs: dict[str, object]) -> Path:
    path = root / "assets/hooks" / name
    path.mkdir(parents=True)
    (path / "asset.yaml").write_text(
        "support:\n  claude:\n    mode: native\n  codex:\n    mode: native\n  cursor:\n    mode: native\n"
    )
    for filename, config in configs.items():
        (path / filename).write_text(json.dumps(config))
    (path / "run.py").write_text("print('hook')\n")
    return path


def config(event: str, command: str) -> dict[str, object]:
    return {"hooks": {event: [{"hooks": [{"type": "command", "command": command}]}]}}


def test_native_bundles_select_merge_and_preserve_managed_hooks(tmp_path: Path) -> None:
    init_hub(tmp_path)
    enable_trace_ingestion(tmp_path)
    bundle(
        tmp_path,
        "a",
        {
            "hooks.json": config("SessionStart", "shared-a"),
            "hooks.cursor.json": {"version": 1, "hooks": {"sessionStart": [{"command": "cursor-a"}]}},
        },
    )
    bundle(
        tmp_path,
        "b",
        {
            "hooks.json": config("SessionStart", "shared-b"),
            "hooks.cursor.json": {"version": 1, "hooks": {"sessionStart": [{"command": "cursor-b"}]}},
        },
    )
    (tmp_path / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes: [hook:b, hook:a]\n")
    build_hub(tmp_path)
    build_hub(tmp_path, check=True)
    for target in ("claude", "codex"):
        root = tmp_path / "dist" / target / "pig"
        hooks = json.loads((root / "hooks/hooks.json").read_text())["hooks"]
        assert [h["hooks"][0]["command"] for h in hooks["SessionStart"][:2]] == ["shared-a", "shared-b"]
        assert len(hooks["SessionStart"]) == 3
        assert {"Stop", "SessionEnd", "SubagentStop"} <= hooks.keys()
        assert (root / "hooks/a/run.py").read_text() == "print('hook')\n"
        assert not (root / "hooks/a/asset.yaml").exists()
    cursor = json.loads((tmp_path / "dist/cursor/pig/hooks/hooks.json").read_text())
    assert cursor == {"version": 1, "hooks": {"sessionStart": [{"command": "cursor-a"}, {"command": "cursor-b"}]}}
    assert not (tmp_path / "dist/gemini/pig/hooks").exists()


def test_legacy_file_and_bundle_coexist(tmp_path: Path) -> None:
    init_hub(tmp_path)
    bundle(tmp_path, "b", {"hooks.json": config("Stop", "bundle")})
    (tmp_path / "assets/hooks/hooks.json").write_text(json.dumps(config("Stop", "legacy")))
    (tmp_path / "assets/hooks/hooks.asset.yaml").write_text("support:\n  codex:\n    mode: native\n")
    (tmp_path / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes: [hook:hooks, hook:b]\n")
    build_hub(tmp_path)
    result = json.loads((tmp_path / "dist/codex/pig/hooks/hooks.json").read_text())
    assert [h["hooks"][0]["command"] for h in result["hooks"]["Stop"]] == ["bundle", "legacy"]


def test_legacy_non_json_file_is_copied_without_inventing_configuration(tmp_path: Path) -> None:
    init_hub(tmp_path)
    source = tmp_path / "assets/hooks/legacy.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# Legacy hook instructions\n")
    source.chmod(0o755)
    (source.parent / "legacy.asset.yaml").write_text("support:\n  codex:\n    mode: native\n")
    (tmp_path / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes: [hook:legacy]\n")
    build_hub(tmp_path)
    output = tmp_path / "dist/codex/pig/hooks"
    assert (output / "legacy.md").read_bytes() == source.read_bytes()
    assert (output / "legacy.md").stat().st_mode & 0o111
    assert not (output / "hooks.json").exists()


@pytest.mark.parametrize("value", [[], {}, {"hooks": []}, {"hooks": {"Stop": ["bad"]}}, {"hooks": {"Stop": {}}}])
def test_invalid_hook_configuration_fails(tmp_path: Path, value: object) -> None:
    init_hub(tmp_path)
    bundle(tmp_path, "bad", {"hooks.json": value})
    (tmp_path / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes: [hook:bad]\n")
    with pytest.raises(InstructionHubError, match="hook"):
        build_hub(tmp_path)


def test_missing_target_config_fails_instead_of_silently_shipping(tmp_path: Path) -> None:
    init_hub(tmp_path)
    bundle(tmp_path, "bad", {"hooks.codex.json": config("Stop", "codex")})
    (tmp_path / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes: [hook:bad]\n")
    with pytest.raises(InstructionHubError, match="native claude bundle requires"):
        build_hub(tmp_path)


def test_conflicting_top_level_fields_fail(tmp_path: Path) -> None:
    init_hub(tmp_path)
    for name, version in (("a", 1), ("b", 2)):
        bundle(tmp_path, name, {"hooks.json": {"version": version, **config("Stop", name)}})
    (tmp_path / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes: [hook:a, hook:b]\n")
    with pytest.raises(InstructionHubError, match="conflicting hook configuration field 'version'"):
        build_hub(tmp_path)
