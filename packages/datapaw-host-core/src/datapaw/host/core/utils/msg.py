from __future__ import annotations

from agentscope.message import Msg, TextBlock


def user_msg(text: str) -> Msg:
    return Msg(
        name="user",
        content=[TextBlock(type="text", text=text)],
        role="user",
    )
