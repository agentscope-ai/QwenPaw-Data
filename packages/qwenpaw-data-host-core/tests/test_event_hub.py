from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.store.json_store import JSONChatEventStore
from qwenpaw_data.host.core.stream.hub import EventHub, get_hub, reset_hub
from qwenpaw_data.host.core.stream.output_stream import OutputStream


async def test_live_subscription_emits_heartbeat_while_idle() -> None:
    hub = EventHub()
    subscription = hub.subscribe_live("chat-1", heartbeat_interval=0.01)

    assert await anext(subscription) is None

    await hub.close("chat-1")
    with pytest.raises(StopAsyncIteration):
        await anext(subscription)


async def test_publish_fans_out_to_all_subscribers(tmp_path: Path) -> None:
    reset_hub()
    try:
        events = JSONChatEventStore(tmp_path)
        stream = OutputStream(
            events,
            session_id="sess1",
            chat_id="chat-1",
            identity=Identity.anonymous(),
        )

        hub = get_hub()

        async def _collect(chat_id: str) -> list:
            return [obj async for obj in hub.subscribe_live(chat_id)]

        collector_a = asyncio.create_task(_collect("chat-1"))
        collector_b = asyncio.create_task(_collect("chat-1"))
        await asyncio.sleep(0.01)  # let subscribers register their queues

        await stream.response_created()
        await stream.text_delta(msg_id="msg_1", index=0, text="hi")
        await hub.close("chat-1")

        received_a = await collector_a
        received_b = await collector_b

        assert [o.object for o in received_a] == ["response", "content"]
        assert [o.object for o in received_a] == [o.object for o in received_b]
        # Events were also persisted with dense sequence numbers.
        persisted = await events.read_after("chat-1", -1)
        assert [o.sequence_number for o in persisted] == [0, 1]
    finally:
        reset_hub()


async def test_subscribe_other_chat_receives_nothing(tmp_path: Path) -> None:
    hub = EventHub()
    other = hub.subscribe_live("chat-other", heartbeat_interval=0.01)
    events = JSONChatEventStore(tmp_path)
    stream = OutputStream(
        events,
        session_id="sess1",
        chat_id="chat-1",
        identity=Identity.anonymous(),
    )
    stream.hub = hub
    await stream.response_created()
    assert await anext(other) is None  # heartbeat only, no cross-chat leak
    await hub.close("chat-other")


async def test_output_stream_rejects_payload_without_object(tmp_path: Path) -> None:
    stream = OutputStream(
        JSONChatEventStore(tmp_path),
        session_id="sess1",
        chat_id="chat-1",
        identity=Identity.anonymous(),
    )
    with pytest.raises(ValueError, match="object is required"):
        await stream.append({"status": "created"})


async def test_task_status_and_biz_surface_shapes(tmp_path: Path) -> None:
    events = JSONChatEventStore(tmp_path)
    stream = OutputStream(
        events,
        session_id="sess1",
        chat_id="chat-1",
        identity=Identity.anonymous(),
    )
    task = await stream.task_status(
        event_type="graph_updated",
        graph_snapshot={"nodes": []},
    )
    assert task.object == "task_status"
    assert task.chat_id == "chat-1"

    followup = await stream.followup_generated(questions=["next?"])
    assert followup.followup.chat_id == "chat-1"

    biz = await stream.biz_event(event_id="e1", seq=1)
    assert biz.biz_event.chat_id == "chat-1"
