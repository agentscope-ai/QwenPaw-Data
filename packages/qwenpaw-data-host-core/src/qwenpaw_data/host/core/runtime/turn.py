# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass

from qwenpaw_data.host.core.domain.chat import Chat


@dataclass(frozen=True)
class TurnInput:
    chat_id: str
    user_input: str

    @classmethod
    def from_chat(cls, chat: Chat) -> TurnInput:
        return cls(chat_id=chat.id, user_input=chat.user_input)
