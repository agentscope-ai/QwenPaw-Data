# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from qwenpaw_data.host.core.api.mappers import chat_to_schema
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories.session_fork import SessionForkRepository

TEST_IDENTITY = Identity(
    user_id="fork-user",
    tenant_id="fork-tenant",
    workspace_id="fork-workspace",
)


def _terminal_chat(
    session: Session,
    *,
    status: str,
    agent_state: dict[str, object] | None,
):
    chat = session.open_chat(
        text="分析数据",
        datasource_id="datasource-1",
        has_active_chat=False,
    )
    chat.mark_status(status)
    chat.agent_state = agent_state
    return chat


class SessionForkTest(unittest.TestCase):
    def test_accepts_completed_chat_with_agent_state(self) -> None:
        session = Session.create(identity=TEST_IDENTITY, title="原会话")
        chat = _terminal_chat(
            session,
            status="completed",
            agent_state={"messages": []},
        )

        forked = session.fork(at_chat=chat, has_active_chat=False)

        self.assertEqual(forked.parent_session_id, session.id)
        self.assertEqual(forked.forked_from_chat_id, chat.id)
        self.assertEqual(forked.chat_count, chat.sequence)

    def test_rejects_non_completed_terminal_chat(self) -> None:
        for status in ("failed", "canceled"):
            with self.subTest(status=status):
                session = Session.create(identity=TEST_IDENTITY)
                chat = _terminal_chat(
                    session,
                    status=status,
                    agent_state={"messages": []},
                )

                with self.assertRaisesRegex(RuntimeError, "chat is not completed"):
                    session.fork(at_chat=chat, has_active_chat=False)

    def test_rejects_historical_completed_chat_without_agent_state(self) -> None:
        session = Session.create(identity=TEST_IDENTITY)
        chat = _terminal_chat(
            session,
            status="completed",
            agent_state=None,
        )

        with self.assertRaisesRegex(ValueError, "chat agent_state is required"):
            session.fork(at_chat=chat, has_active_chat=False)

    def test_schema_forkable_requires_completed_stateful_chat(self) -> None:
        session = Session.create(identity=TEST_IDENTITY)
        completed = _terminal_chat(
            session,
            status="completed",
            agent_state={"messages": []},
        )
        historical = _terminal_chat(
            session,
            status="completed",
            agent_state=None,
        )

        self.assertTrue(chat_to_schema(completed).forkable)
        self.assertFalse(chat_to_schema(historical).forkable)


async def test_fork_carries_confidence_into_the_copied_segment() -> None:
    """fork 的 _copy_segments 必须把打分结果带过去：丢了它，副本会
    静默降级为无档位，用户看到同一段对话的两个置信度不同的副本。"""
    verdict = {
        "level": "medium",
        "summary": "口径已确认。",
    }
    fake_fork = SimpleNamespace(
        mapped=lambda old: "chat-new",
        rewrite=lambda value: copy.deepcopy(value),
    )
    segment_row = {
        "segment_id": "seg-1",
        "title": "t",
        "coverage": {"start_seq": 1, "end_seq": 2},
        "behavior": "b",
        "conclusion": "c",
        "input": None,
        "artifact": None,
        "confidence": verdict,
        "started_at": None,
        "ended_at": None,
    }
    segment_repo = AsyncMock()
    segment_repo.list_for_chats.return_value = {"chat-old": [segment_row]}
    forker = SessionForkRepository(db=None, identity=None)  # type: ignore[arg-type]

    with patch(
        "qwenpaw_data.host.core.repositories.session_fork.SegmentRepository",
        return_value=segment_repo,
    ):
        await forker._copy_segments(fake_fork, [SimpleNamespace(id="chat-old")])

    segment_repo.upsert.assert_awaited_once()
    kwargs = segment_repo.upsert.await_args.kwargs
    assert kwargs["chat_id"] == "chat-new"
    assert kwargs["confidence"] == verdict


if __name__ == "__main__":
    unittest.main()
