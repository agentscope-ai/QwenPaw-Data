# -*- coding: utf-8 -*-
"""Fetch one chat's typed inputs for grading evidence."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from qwenpaw_data.host.core.algo.evaluation.settings import USER_INPUT_TURNS
from qwenpaw_data.host.core.repositories.chat import ChatRepository

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from qwenpaw_data.host.core.domain.identity import Identity

logger = logging.getLogger(__name__)


async def fetch_user_inputs(
    session_factory: Callable[[], AsyncSession],
    identity: Identity,
    chat_id: str,
) -> list[str]:
    """Recent typed inputs of this chat and its session predecessors."""
    async with session_factory() as db:
        chats_repo = ChatRepository(db, identity)
        chat = await chats_repo.get(chat_id)
        session_chats = await chats_repo.list_for_session(chat.session_id)
    typed = [
        c.user_input
        for c in session_chats
        if c.sequence <= chat.sequence and c.user_input
    ]
    return typed[-USER_INPUT_TURNS:]


__all__ = ["fetch_user_inputs"]
