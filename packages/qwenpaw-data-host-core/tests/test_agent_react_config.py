# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from agentscope.event import ReplyEndEvent, TextBlockDeltaEvent
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.tool import Toolkit

from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent


def test_default_react_config_raises_cancelled_error() -> None:
    """The default ReAct config must let cancellation propagate to the host.

    AgentScope swallows asyncio.CancelledError when
    interruption_raise_cancelled_error is False, so reply_stream returns
    normally and ChatRuntime records the run as completed even though the
    user pressed stop. Keeping the flag enabled lets ChatRuntime.cancel()
    interrupt the agent and finish as canceled.
    """

    signature = inspect.signature(QwenPawDataAgent.__init__)
    react_config = signature.parameters["react_config"].default

    assert react_config.interruption_raise_cancelled_error is True


@pytest.mark.asyncio
async def test_cancel_propagates_and_same_agent_can_reply_again():
    started, stopped = asyncio.Event(), asyncio.Event()

    class CancellableModel(ChatModelBase):
        def __init__(self):
            self.model = "test-model"
            self.stream = False
            self.max_retries = 0
            self.context_size = 32768
            self.formatter = SimpleNamespace(supported_input_media_types=[])
            self.calls = 0

        async def _call_api(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                try:
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            return ChatResponse(
                content=[TextBlock(text="follow-up completed")], is_last=True
            )

    agent = QwenPawDataAgent(
        name="test",
        system_prompt="test",
        model=CancellableModel(),
        toolkit=Toolkit(),
    )

    async def collect(prompt):
        return [
            event
            async for event in agent.reply_stream(
                inputs=Msg(name="user", role="user", content=[TextBlock(text=prompt)]),
            )
        ]

    task = asyncio.create_task(collect("first request"))
    try:
        try:
            await asyncio.wait_for(started.wait(), 2)
        except TimeoutError:
            if task.done():
                task.result()
            raise
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        events = await asyncio.wait_for(collect("follow up"), 2)
        assert any(
            isinstance(event, TextBlockDeltaEvent)
            and event.delta == "follow-up completed"
            for event in events
        )
        assert isinstance(events[-1], ReplyEndEvent)
        assert events[-1].finished_reason == "completed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
