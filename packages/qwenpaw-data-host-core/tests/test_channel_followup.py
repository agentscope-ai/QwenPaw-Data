# -*- coding: utf-8 -*-
"""Channel follow-up dispatch: ``_maybe_send_followups`` reads the followups
row committed by FollowUpMiddleware and hands questions to ``send_followups``."""

from __future__ import annotations

from typing import Any

import pytest

from qwenpaw_data.host.core.channels.base import BaseChannel
from qwenpaw_data.host.core.channels.schema import NativePayload
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories.followup import FollowUpRepository
from qwenpaw_data.host.core.settings import clear_settings

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


class _StubChannel(BaseChannel):
    """Minimal BaseChannel: records ``send_followups`` calls."""

    channel = "feishu"

    def __init__(self) -> None:
        super().__init__()
        self._owner_identity = TEST_IDENTITY
        self.followup_sent: list[list[str]] = []

    def resolve_session_id(self, native: Any) -> str:
        return "feishu-abcd1234"

    async def start(self) -> None:
        pass

    async def _stop(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def extract_target_meta(self, native: Any) -> dict[str, Any] | None:
        return None

    async def inject_cron_job(self, cron_job_config: dict[str, Any]) -> None:
        pass

    async def send(self, native: Any, text: str) -> None:
        pass

    async def on_streaming_start(self, native: Any, msg_id: str) -> Any:
        return None

    async def on_streaming_delta(self, native: Any, handle: Any, delta: str) -> None:
        pass

    async def on_streaming_end(self, native: Any, handle: Any, full_text: str) -> None:
        pass

    async def on_streaming_close(self, native: Any, handle: Any, summary: str) -> None:
        pass

    async def on_streaming_reasoning_delta(
        self, native: Any, handle: Any, accumulated: str
    ) -> None:
        pass

    async def on_consume_start(self, native: Any) -> None:
        pass

    async def on_consume_end(self, native: Any, handle: Any, status: str) -> None:
        pass

    async def send_image(self, native: Any, path: str) -> None:
        pass

    async def send_file(self, native: Any, path: str) -> None:
        pass

    async def send_followups(self, native: Any, questions: list[str]) -> None:
        self.followup_sent.append(list(questions))

    async def send_datasource_card(
        self,
        native: Any,
        session: Any,
        items: list[Any],
        selectable: bool = True,
    ) -> None:
        pass

    async def send_clarification_card(self, native: Any, questions: Any) -> str:
        return ""


def _native() -> NativePayload:
    return NativePayload(channel_id="feishu", sender_id="ou_s", content_parts=[])


async def _seed_followup(chat_id: str, questions: list[str]) -> None:
    async with get_session_factory()() as db:
        await FollowUpRepository(db, TEST_IDENTITY).upsert(
            chat_id=chat_id, questions=questions
        )
        await db.commit()


@pytest.mark.asyncio
async def test_maybe_send_followups_hands_questions_to_hook() -> None:
    """Row present → questions forwarded verbatim to send_followups."""
    await reset_engine()
    await init_db()
    try:
        await _seed_followup("chat_f1", ["追问A", "追问B"])
        ch = _StubChannel()
        await ch._maybe_send_followups(_native(), "chat_f1")
        assert ch.followup_sent == [["追问A", "追问B"]]
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_maybe_send_followups_missing_row_is_noop() -> None:
    """No row for this chat → hook not called."""
    await reset_engine()
    await init_db()
    try:
        ch = _StubChannel()
        await ch._maybe_send_followups(_native(), "chat_missing")
        assert ch.followup_sent == []
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_maybe_send_followups_empty_questions_is_noop() -> None:
    """A committed row with no questions (should not happen) → hook not called."""
    await reset_engine()
    await init_db()
    try:
        await _seed_followup("chat_f2", [])
        ch = _StubChannel()
        await ch._maybe_send_followups(_native(), "chat_f2")
        assert ch.followup_sent == []
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_maybe_send_followups_disabled_by_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``QWENPAW_DATA_FOLLOWUP_ENABLED=false`` → dispatch skipped even with a row."""
    await reset_engine()
    await init_db()
    monkeypatch.setenv("QWENPAW_DATA_FOLLOWUP_ENABLED", "false")
    clear_settings()
    try:
        await _seed_followup("chat_f3", ["追问"])
        ch = _StubChannel()
        await ch._maybe_send_followups(_native(), "chat_f3")
        assert ch.followup_sent == []
    finally:
        clear_settings()
        await reset_engine()


@pytest.mark.asyncio
async def test_maybe_send_followups_failure_is_swallowed() -> None:
    """A broken ``send_followups`` must not raise out of the channel loop."""
    await reset_engine()
    await init_db()
    try:
        await _seed_followup("chat_f4", ["追问"])
        ch = _StubChannel()

        async def boom(native: Any, questions: list[str]) -> None:
            raise RuntimeError("boom")

        ch.send_followups = boom  # type: ignore[method-assign]
        await ch._maybe_send_followups(_native(), "chat_f4")  # must not raise
    finally:
        await reset_engine()
