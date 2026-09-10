from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from .helpers import _git, _git_output

UPSTREAM_URL = "https://upstream.example.test/agent-tools.git"
PLUGIN_PATH = "plugins/doc-detective"


def external_definition(sha: str = "a" * 40, *, path: str = PLUGIN_PATH) -> dict[str, Any]:
    return {
        "kind": "external",
        "id": "doc-detective",
        "name": "Doc Detective",
        "source": {"type": "git", "url": UPSTREAM_URL, "sha": sha},
        "targets": {target: {"path": path} for target in ("claude", "codex")},
    }


def write_external(hub: Path, definition: dict[str, Any]) -> None:
    (hub / "plugins/doc-detective.yaml").write_text(yaml.safe_dump(definition))
    config = yaml.safe_load((hub / "hub.yaml").read_text())
    if "doc-detective" not in config["stable_plugins"]:
        config["stable_plugins"].append("doc-detective")
    (hub / "hub.yaml").write_text(yaml.safe_dump(config))


def make_upstream(root: Path, monkeypatch: pytest.MonkeyPatch, *, path: str = PLUGIN_PATH) -> str:
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Upstream Author")
    _git(root, "config", "user.email", "upstream@example.test")
    plugin = root / path
    for target in ("claude", "codex"):
        manifest = plugin / f".{target}-plugin/plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps(
                {
                    "name": "doc-detective",
                    "version": "1.2.3",
                    "author": {"name": "Upstream Author"},
                    "skills": "./skills/",
                    "mcpServers": "./.mcp.json",
                    "hooks": "./hooks/hooks.json",
                }
            )
        )
    (plugin / "skills/example").mkdir(parents=True)
    (plugin / "skills/example/SKILL.md").write_text("---\nname: example\ndescription: Example skill\n---\n# Example\n")
    (plugin / "hooks").mkdir()
    (plugin / "hooks/hooks.json").write_text('{"hooks": {}}')
    (plugin / ".mcp.json").write_text('{"mcpServers": {"upstream": {"url": "https://upstream.example.test/mcp"}}}')
    # Route a valid HTTPS declaration to a local Git fixture in this test only.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{root.as_posix()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", UPSTREAM_URL)
    return commit_upstream(root)


def commit_upstream(root: Path) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "upstream release")
    return _git_output(root, "rev-parse", "HEAD").strip()
