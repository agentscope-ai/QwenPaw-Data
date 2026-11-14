from __future__ import annotations

import pytest

from qwenpaw_data.cli.main import build_parser


@pytest.mark.parametrize("command", ["plan", "execute"])
def test_retired_agent_commands_are_not_registered(command: str) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([command])

    assert exc_info.value.code == 2


@pytest.mark.parametrize("command", ["run", "chat"])
def test_remote_agent_commands_are_registered(command: str) -> None:
    args = build_parser().parse_args([command, "--host-url", "http://127.0.0.1:8000"])
    assert args.host_url == "http://127.0.0.1:8000"
    assert callable(args.handler)
