# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from qwenpaw_data.host.core.domain.tool_confirmation import ToolConfirmation


def _gate(level: str) -> ToolConfirmation:
    return ToolConfirmation(None, "chat", level=level)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("level", "tool_name", "expected"),
    [
        ("off", "Bash", "allow"),
        ("off", "write_file", "allow"),
        ("off", "ask_user_question", "allow"),
        ("auto", "execute_shell_command", "allow"),
        ("auto", "mcp__Bash", "allow"),
        ("auto", "write_file", "allow"),
        ("auto", "ask_user_question", "allow"),
        ("all", "Bash", "ask"),
        ("all", "write_file", "ask"),
        ("all", "ask_user_question", "allow"),
    ],
)
def test_decide(level: str, tool_name: str, expected: str) -> None:
    assert _gate(level).decide(tool_name) == expected


def test_decide_rejects_blank_name() -> None:
    with pytest.raises(ValueError, match="tool name is required"):
        _gate("auto").decide(" ")
