# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.runtime.turn import TurnInput, compose_agent_input

COMMENT = {
    "path": "russia_dau_mom.sql",
    "line_start": 2,
    "line_end": 10,
    "comment": "加一个参数",
}

SINGLE_LINE = {
    "path": "readme.md",
    "line_start": 3,
    "line_end": 3,
    "comment": "标题不对",
}


def test_compose_without_comments_returns_text() -> None:
    assert compose_agent_input("hello", None) == "hello"
    assert compose_agent_input("hello", []) == "hello"


def test_compose_range_comment() -> None:
    assert compose_agent_input("请按这些评论改一下", [COMMENT]) == (
        "用户对成果文件的评论：\n"
        "- 文件 `russia_dau_mom.sql` 第 2-10 行：加一个参数\n"
        "\n"
        "请按这些评论改一下"
    )


def test_compose_single_line_comment() -> None:
    assert compose_agent_input("看一下", [SINGLE_LINE]) == (
        "用户对成果文件的评论：\n- 文件 `readme.md` 第 3 行：标题不对\n\n看一下"
    )


def test_compose_missing_field_raises() -> None:
    with pytest.raises(ValueError, match="missing path"):
        compose_agent_input("hello", [{"line_start": 1, "line_end": 1, "comment": "x"}])


def test_from_chat_keeps_comments() -> None:
    chat = Chat.start(
        session_id="sess-1",
        identity=Identity(user_id="u1", tenant_id="t1", workspace_id="w1"),
        sequence=1,
        datasource_id=None,
        text="请按这些评论改一下",
        artifact_comments=[COMMENT],
    )
    turn = TurnInput.from_chat(chat)
    assert turn.user_input == "请按这些评论改一下"
    assert turn.artifact_comments == [COMMENT]
    assert compose_agent_input(turn.user_input, turn.artifact_comments).startswith(
        "用户对成果文件的评论："
    )
