# -*- coding: utf-8 -*-
"""Steer is now a DB-backed command, consumed by SteerMiddleware.

The old in-memory ``SteerQueue`` is gone; these tests assert the equivalent
guarantees on ``ChatCommandRepository``: FIFO ordering, exactly-once take,
per-chat isolation, release on clear and artifact-comment round-trip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.chat_command import ChatCommand
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories import ChatCommandRepository

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)

_BASE = datetime(2026, 1, 1, tzinfo=UTC)

_PAYLOADS = ("steer", "clarification", "plan", "tool_confirmation", "ui")


def test_direct_command_payload_defaults_are_data_not_factory_methods() -> None:
    command = ChatCommand(
        id="cmd-direct", chat_id="chat-1", identity=TEST_IDENTITY,
        kind="cancel", status="waiting", created_at=_BASE,
    )
    assert {name: getattr(command, name) for name in _PAYLOADS} == dict.fromkeys(_PAYLOADS)


@pytest.mark.parametrize("kind,kwargs", [
    ("steer", {"text": "updated question", "artifact_comments": []}),
    ("clarification", {"clarification_id": "q1", "result": {"answer": "yes"}}),
    ("plan", {"reason": "revise", "plan": {"nodes": []}}),
    ("tool_confirmation", {"call_id": "t1", "decision": "approved"}),
    ("ui", {"call_id": "u1", "outcome": {"status": "executed", "result": {}}}),
])
def test_command_factories_set_only_their_own_payload(kind, kwargs) -> None:
    chat = _chat()
    command = getattr(ChatCommand, f"for_{kind}")(chat, **kwargs)
    assert command.kind == kind
    assert command.chat_id == chat.id
    assert command.identity == chat.identity
    assert isinstance(getattr(command, kind), dict)
    assert all(getattr(command, name) is None for name in _PAYLOADS if name != kind)


def _chat(chat_id: str = "chat-1") -> Chat:
    chat = Chat.start(
        session_id="ses-1",
        identity=TEST_IDENTITY,
        sequence=1,
        text="hello",
        datasource_id=None,
    )
    chat.id = chat_id
    return chat


def _steer(
    chat: Chat,
    text: str,
    comments: list[dict] | None = None,
    *,
    offset: int = 0,
) -> ChatCommand:
    command = ChatCommand.for_steer(chat, text, list(comments or []))
    command.created_at = _BASE + timedelta(seconds=offset)
    return command


@pytest.mark.asyncio
async def test_steer_commands_are_taken_in_fifo_order_once() -> None:
    await reset_engine()
    await init_db()
    try:
        factory = get_session_factory()
        chat = _chat()
        async with factory() as db:
            repo = ChatCommandRepository(db)
            for offset, text in enumerate(["a", "b", "c"]):
                await repo.add(_steer(chat, text, offset=offset))
            await db.commit()

        async with factory() as db:
            repo = ChatCommandRepository(db)
            pending = await repo.take_steers(chat.id)
            await db.commit()

        assert [item["text"] for item in pending] == ["a", "b", "c"]

        # A taken steer is marked done, so it is never handed out twice.
        async with factory() as db:
            repo = ChatCommandRepository(db)
            assert await repo.take_steers(chat.id) == []
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_steer_take_is_isolated_per_chat() -> None:
    await reset_engine()
    await init_db()
    try:
        factory = get_session_factory()
        first = _chat("chat-1")
        second = _chat("chat-2")
        async with factory() as db:
            repo = ChatCommandRepository(db)
            await repo.add(_steer(first, "first", offset=0))
            await repo.add(_steer(second, "second", offset=1))
            await db.commit()

        async with factory() as db:
            repo = ChatCommandRepository(db)
            first_taken = await repo.take_steers("chat-1")
            second_taken = await repo.take_steers("chat-2")
            await db.commit()

        assert [item["text"] for item in first_taken] == ["first"]
        assert [item["text"] for item in second_taken] == ["second"]
        async with factory() as db:
            assert await ChatCommandRepository(db).take_steers("chat-1") == []
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_cancel_command_releases_and_clears_pending_steers() -> None:
    await reset_engine()
    await init_db()
    try:
        factory = get_session_factory()
        first = _chat("chat-1")
        second = _chat("chat-2")
        async with factory() as db:
            repo = ChatCommandRepository(db)
            await repo.add(_steer(first, "x", offset=0))
            await repo.add(_steer(first, "y", offset=1))
            await repo.add(_steer(second, "z", offset=2))
            await db.commit()

        # A cancel command is how an ended chat is signalled to the executor.
        async with factory() as db:
            repo = ChatCommandRepository(db)
            assert await repo.has_cancel("chat-1") is False
            await repo.add(ChatCommand.cancel(first))
            await db.commit()
        async with factory() as db:
            assert await ChatCommandRepository(db).has_cancel("chat-1") is True

        # Clearing (chat teardown) releases pending steers, scoped to the chat.
        async with factory() as db:
            repo = ChatCommandRepository(db)
            await repo.clear("chat-1")
            await db.commit()
        async with factory() as db:
            repo = ChatCommandRepository(db)
            assert await repo.take_steers("chat-1") == []
            assert await repo.has_cancel("chat-1") is False
            assert [item["text"] for item in await repo.take_steers("chat-2")] == ["z"]
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_new_chat_does_not_inherit_pending_steers() -> None:
    await reset_engine()
    await init_db()
    try:
        factory = get_session_factory()
        old = _chat("chat-1")
        async with factory() as db:
            repo = ChatCommandRepository(db)
            await repo.add(_steer(old, "old", offset=0))
            await db.commit()
        async with factory() as db:
            repo = ChatCommandRepository(db)
            await repo.clear("chat-1")
            await db.commit()

        fresh = _chat("chat-2")
        async with factory() as db:
            repo = ChatCommandRepository(db)
            assert await repo.take_steers(fresh.id) == []
            await repo.add(_steer(fresh, "new", offset=1))
            await db.commit()

        async with factory() as db:
            taken = await ChatCommandRepository(db).take_steers("chat-2")
        assert [item["text"] for item in taken] == ["new"]
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_steer_keeps_artifact_comments() -> None:
    await reset_engine()
    await init_db()
    try:
        factory = get_session_factory()
        chat = _chat()
        comments = [
            {
                "path": "russia_dau_mom.sql",
                "line_start": 2,
                "line_end": 10,
                "comment": "加一个参数",
            }
        ]
        async with factory() as db:
            repo = ChatCommandRepository(db)
            await repo.add(_steer(chat, "nudge", comments))
            await db.commit()

        async with factory() as db:
            taken = await ChatCommandRepository(db).take_steers(chat.id)

        assert taken[0]["text"] == "nudge"
        assert taken[0]["artifact_comments"] == comments
    finally:
        await reset_engine()


def test_empty_text_rejected() -> None:
    chat = _chat()
    with pytest.raises(ValueError, match="text is required"):
        ChatCommand.for_steer(chat, "   ", [])
