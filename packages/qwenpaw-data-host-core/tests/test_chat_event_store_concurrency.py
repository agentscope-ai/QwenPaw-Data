from __future__ import annotations

import asyncio
from pathlib import Path

from qwenpaw_data.host.core.store.json_store import JSONChatEventStore


async def test_concurrent_appends_produce_dense_monotonic_sequence(
    tmp_path: Path,
) -> None:
    events = JSONChatEventStore(tmp_path)
    total = 50

    async def _append(i: int) -> int:
        obj = await events.append(
            session_id="sess1",
            chat_id="chat_x",
            payload={
                "object": "content",
                "msg_id": "msg_1",
                "type": "text",
                "delta": True,
                "text": f"chunk-{i}",
            },
        )
        return obj.sequence_number

    sequences = await asyncio.gather(*(_append(i) for i in range(total)))
    assert sorted(sequences) == list(range(total))

    persisted = await events.read_after("chat_x", -1)
    assert [e.sequence_number for e in persisted] == list(range(total))
    texts = {e.text for e in persisted}
    assert texts == {f"chunk-{i}" for i in range(total)}


async def test_concurrent_appends_across_chats_are_independent(
    tmp_path: Path,
) -> None:
    events = JSONChatEventStore(tmp_path)

    async def _append(chat_id: str, i: int) -> int:
        obj = await events.append(
            session_id="sess1",
            chat_id=chat_id,
            payload={"object": "response", "id": f"resp_{i}", "status": "created"},
        )
        return obj.sequence_number

    results = await asyncio.gather(
        *(_append("chat_a", i) for i in range(10)),
        *(_append("chat_b", i) for i in range(10)),
    )
    assert sorted(results[:10]) == list(range(10))
    assert sorted(results[10:]) == list(range(10))
    assert await events.last_sequence_number("chat_a") == 9
    assert await events.last_sequence_number("chat_b") == 9
