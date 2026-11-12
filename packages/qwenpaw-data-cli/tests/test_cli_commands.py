from __future__ import annotations

import pytest

from qwenpaw_data.cli.main import build_parser


@pytest.mark.parametrize("command", ["run", "chat", "plan", "execute"])
def test_retired_agent_commands_are_not_registered(command: str) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([command])

    assert exc_info.value.code == 2
