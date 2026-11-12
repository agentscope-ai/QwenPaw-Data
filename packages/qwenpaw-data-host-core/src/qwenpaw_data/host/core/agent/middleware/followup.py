# -*- coding: utf-8 -*-
"""把一轮 reply 的裸事件旁路投喂给 FollowUpRecommend。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from agentscope.agent import Agent
from agentscope.event import ReplyEndEvent
from agentscope.message import Msg
from agentscope.middleware import MiddlewareBase

from qwenpaw_data.host.core.algo.followup.recommend import FollowUpRecommend
from qwenpaw_data.host.core.repositories.chat import ChatRepository
from qwenpaw_data.host.core.repositories.followup import FollowUpRepository
from qwenpaw_data.host.core.settings import get_settings

if TYPE_CHECKING:
    from qwenpaw_data.host.core.runtime.context import RunContext
    from qwenpaw_data.host.core.runtime.envelope import Envelope

logger = logging.getLogger(__name__)


class FollowUpMiddleware(MiddlewareBase):
    """事件原样透传，同时逐条投喂给 FollowUpRecommend；成功收口时写出追问。

    ask_user_question 会把一次逻辑回合拆成两次 reply_stream/on_reply。
    本中间件跨暂停复用同一个 Recommend，只在见到 ReplyEndEvent 时 EOF+join。
    """

    def __init__(
        self,
        *,
        run_context: RunContext,
        envelope: Envelope,
    ) -> None:
        self._run_context = run_context
        self._envelope = envelope
        self._unit: FollowUpRecommend | None = None

    @classmethod
    def find(cls, agent: Agent) -> FollowUpMiddleware | None:
        for middleware in agent._reply_middlewares:
            if isinstance(middleware, cls):
                return middleware
        return None

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        settings = get_settings().followup
        # 总开关在 Host：关闭时不创建、不投喂 Recommend，事件原样透传。
        if not settings.enabled:
            async for event in next_handler():
                yield event
            return

        if self._unit is None:
            previous = await self._load_previous_followups()
            # 配置归 Host 所有，算法侧不自行读环境变量。
            self._unit = FollowUpRecommend(
                run_context=self._run_context,
                previous_followups=previous,
                deliver=self._envelope.send_followup,
                timeout_sec=settings.timeout_sec,
                max_questions=settings.max_questions,
                max_metrics=settings.max_metrics,
                max_dimensions=settings.max_dimensions,
                min_relevance=settings.min_relevance,
            )
        unit = self._unit
        await unit.start()
        inputs = input_kwargs.get("inputs")
        msgs = inputs if isinstance(inputs, list) else [inputs]
        for msg in msgs:
            # only the first, real user message may become a user_input entry.
            if isinstance(msg, Msg):
                await unit.append({"kind": "user_input", "payload": msg.model_dump()})
        ended = False
        try:
            async for event in next_handler():
                if isinstance(event, ReplyEndEvent):
                    ended = True
                if hasattr(event, "model_dump"):
                    await unit.append(
                        {"kind": "agent_event", "payload": event.model_dump()}
                    )
                yield event
        finally:
            if ended:
                await unit.append(None, last=True)
                questions = await unit.join()
                if questions:
                    await self._envelope.send_followup(questions)

    async def end_stream(self) -> None:
        """Cancel path: freeze the collector without joining/sending questions."""
        if self._unit is not None:
            await self._unit.append(None, last=True)

    async def _load_previous_followups(self) -> tuple[str, ...]:
        """Session 内此前各 Chat 已推荐的问题，供去重。"""
        try:
            async with self._envelope._session_factory() as db:
                identity = self._envelope.stream.identity
                chats = await ChatRepository(db, identity).list_for_session(
                    self._run_context.session_id
                )
                chat_ids = [
                    chat.id for chat in chats if chat.id != self._run_context.chat_id
                ]
                by_chat = await FollowUpRepository(db, identity).list_for_chats(
                    chat_ids
                )
            questions: list[str] = []
            for chat_id in chat_ids:
                row = by_chat.get(chat_id)
                if row is None:
                    continue
                questions.extend(row.get("questions") or [])
            return tuple(questions)
        except Exception:
            logger.exception(
                "followup: failed to load previous followups for session %s",
                self._run_context.session_id,
            )
            return ()
