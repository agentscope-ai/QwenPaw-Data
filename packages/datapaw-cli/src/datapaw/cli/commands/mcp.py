"""MCP configuration commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agentscope.mcp import MCPClient

from datapaw.host.core.paths import Paths, resolve_datapaw_home


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("mcp", help="Manage Host workspace MCP config")
    mcp_subparsers = parser.add_subparsers(dest="mcp_command", required=True)

    import_parser = mcp_subparsers.add_parser(
        "import",
        help="Replace the Host workspace .mcp file",
    )
    import_parser.add_argument("file", type=Path, help="AgentScope .mcp JSON file")
    import_parser.set_defaults(handler=handle_import)


def _load_mcp_file(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in MCP config {path}") from exc

    if not isinstance(raw, list):
        raise ValueError("MCP config must be a JSON array")

    clients: list[dict[str, Any]] = []
    for idx, item in enumerate(raw):
        try:
            client = MCPClient.model_validate(item)
        except Exception as exc:
            raise ValueError(f"invalid MCP entry at index {idx}: {exc}") from exc
        clients.append(client.model_dump(mode="json"))
    return clients


def _default_mcp_path() -> Path:
    return Paths(resolve_datapaw_home()).mcp_config_file


def handle_import(args: argparse.Namespace) -> int:
    clients = _load_mcp_file(args.file)
    target = _default_mcp_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(clients, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Imported {len(clients)} MCP config(s) to {target}")
    return 0
