"""Command-line interface for Promptless Instruction Hub."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from promptless_instruction_hub.compiler import build_hub, init_hub, validate_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.mcp_status import run_status_mcp
from promptless_instruction_hub.scan.hub import scan_hub
from promptless_instruction_hub.status import summarize_release_manifest


def main(argv: list[str] | None = None) -> int:
    """Run the `promptless-instruction-hub` / `pi` command."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (FileNotFoundError, InstructionHubError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi", description="Promptless Instruction Hub")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser("init", help="initialize an empty Instruction Hub")
    _add_hub_arg(init_parser)
    init_parser.add_argument("--org", default="Promptless")
    init_parser.add_argument("--plugin-id")
    init_parser.add_argument("--plugin-name")
    init_parser.add_argument("--plugin-version", default="0.1.0")

    scan_parser = subcommands.add_parser("scan", help="import reusable assets and inventory repo context")
    _add_hub_arg(scan_parser)
    scan_parser.add_argument("--source", type=Path, default=Path.cwd())

    validate_parser = subcommands.add_parser("validate", help="validate hub source files")
    _add_hub_arg(validate_parser)

    build_parser = subcommands.add_parser("build", help="generate target distribution artifacts")
    _add_hub_arg(build_parser)
    build_parser.add_argument("--check", action="store_true", help="fail if generated artifacts are stale")

    status_parser = subcommands.add_parser("status", help="print local release metadata")
    status_parser.add_argument("--manifest", type=Path, default=Path(".promptless/releases/current.json"))

    mcp_parser = subcommands.add_parser("mcp-status", help=argparse.SUPPRESS)
    mcp_parser.add_argument("--manifest", type=Path, required=True)

    return parser


def _add_hub_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hub", type=Path, default=Path.cwd(), help="Instruction Hub repository root")


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "init":
        root = init_hub(
            args.hub,
            org=args.org,
            plugin_id=args.plugin_id,
            plugin_name=args.plugin_name,
            plugin_version=args.plugin_version,
        )
        print(f"initialized Instruction Hub at {root}")
        return 0
    if args.command == "scan":
        result = scan_hub(args.hub, args.source)
        print(
            f"imported {len(result.imported_skills)} skill(s), {len(result.imported_mcps)} MCP config(s); "
            f"inventoried {len(result.inventoried_context_files)} context file(s)"
        )
        return 0
    if args.command == "validate":
        result = validate_hub(args.hub)
        print(f"valid Instruction Hub: {len(result.stable_assets)} stable asset(s)")
        return 0
    if args.command == "build":
        result = build_hub(args.hub, check=args.check)
        verb = "checked" if result.checked else "built"
        print(f"{verb} release {result.release_id} ({result.release_hash[:12]})")
        return 0
    if args.command == "status":
        print(json.dumps(summarize_release_manifest(args.manifest), indent=2, sort_keys=True))
        return 0
    if args.command == "mcp-status":
        run_status_mcp(args.manifest)
        return 0
    msg = f"unknown command: {args.command}"
    raise InstructionHubError(msg)


if __name__ == "__main__":
    raise SystemExit(main())
