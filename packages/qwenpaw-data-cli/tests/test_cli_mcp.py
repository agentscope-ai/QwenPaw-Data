from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest


def _valid_mcp_config() -> list[dict]:
    return [
        {
            "name": "databridge",
            "is_stateful": False,
            "mcp_config": {
                "type": "http_mcp",
                "url": "http://127.0.0.1:8000/mcp",
                "headers": {},
                "timeout": 2400.0,
            },
            "enable_tools": None,
            "disable_tools": None,
            "execution_timeout": None,
        },
    ]


@pytest.fixture()
def mcp_module() -> object:
    from qwenpaw_data.cli.commands import mcp

    yield mcp


def test_mcp_import_replaces_host_workspace_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qwenpaw_data.cli.main import main

    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path))
    source = tmp_path / "databridge.mcp"
    source.write_text(json.dumps(_valid_mcp_config()), encoding="utf-8")

    assert main(["mcp", "import", str(source)]) == 0

    target = tmp_path / "host" / "workspace" / ".mcp"
    assert json.loads(target.read_text(encoding="utf-8")) == _valid_mcp_config()


def test_mcp_command_is_hidden_but_registered_in_cli(mcp_module: object) -> None:
    from qwenpaw_data.cli.main import build_parser

    public_parser = build_parser()
    help_text = public_parser.format_help()
    assert "mcp" not in help_text

    internal_parser = build_parser(include_internal=True)
    args = internal_parser.parse_args(["mcp", "import", "unused.mcp"])
    assert args.command == "mcp"
    assert args.mcp_command == "import"
    assert args.handler is mcp_module.handle_import


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"name": "databridge"}),
        json.dumps([{"name": "bad name"}]),
    ],
)
def test_mcp_import_rejects_invalid_config_without_overwriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    mcp_module: object,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path))
    target = tmp_path / "host" / "workspace" / ".mcp"
    target.parent.mkdir(parents=True)
    target.write_text("old config", encoding="utf-8")
    source = tmp_path / "invalid.mcp"
    source.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        mcp_module.handle_import(argparse.Namespace(file=source))

    assert target.read_text(encoding="utf-8") == "old config"
