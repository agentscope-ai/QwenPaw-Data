# -*- coding: utf-8 -*-
"""Settlement orchestration against SQL repositories with scripted model/Context."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import qwenpaw_data.host.core.algo.settlement.manager as manager_module
from qwenpaw_data.host.core.algo.settlement import (
    SettlementManager,
    SettlementSettings,
)
from qwenpaw_data.host.core.algo.settlement.retriever import SettlementRetriever
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
from qwenpaw_data.host.core.repositories.chat_event import (
    ChatEventRepository,
    ChatEventRecord,
)
from qwenpaw_data.host.core.repositories.settlement import SettlementRepository
from qwenpaw_data.host.core.utils.time import utcnow
from .conftest import TEST_IDENTITY
from . import test_runtime_settings
from .test_settlement_algo import FakeCm, FakeStructuredLLM

database = test_runtime_settings.database


class _ScriptedModel(FakeStructuredLLM):
    async def generate_structured_output(self, *, structured_model, **kwargs):
        if structured_model["title"] == "DuplicateJudgement":
            return SimpleNamespace(content={"duplicate": False, "reason": "new"})
        return await super().generate_structured_output(
            structured_model=structured_model, **kwargs
        )


@pytest.fixture
async def harness(database, monkeypatch):
    before = set(manager_module._BACKGROUND_TASKS)
    async with database() as db:
        try:
            yield lambda **kwargs: _Harness(db, database, monkeypatch, **kwargs)
        finally:
            tasks = set(manager_module._BACKGROUND_TASKS) - before
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 5)


def _detection_payload(*names: str) -> dict[str, Any]:
    return {
        "items": [
            {
                "type": "metric_caliber",
                "fields": {
                    "metric_name": name,
                    "caliber": "支付金额",
                    "domain": "交易",
                    "table": "orders",
                    "formula_sql": "SELECT 1",
                },
            }
            for name in names
        ]
    }


class _Harness:
    def __init__(self, db, factory, monkeypatch, *, llm_payloads=None, cm_records=None):
        self.identity = TEST_IDENTITY
        self.db, self.factory = db, factory
        self.sessions = SessionRepository(db, self.identity)
        self.chats = ChatRepository(db, self.identity)
        self.events = ChatEventRepository(db, self.identity)
        self.cards = SettlementRepository(db, self.identity)
        self.manager = SettlementManager(
            db, identity=self.identity, settings=SettlementSettings(window_size=3)
        )
        self.llm = _ScriptedModel(llm_payloads or [])
        self.retrieval = AsyncMock(return_value='{"results": []}')
        monkeypatch.setattr(SettlementRetriever, "retrieve", self.retrieval)
        self.cm = FakeCm(
            cm_records
            if cm_records is not None
            else {
                "list_domains": {
                    "status": "ok",
                    "result": json.dumps([{"name": "交易"}], ensure_ascii=False),
                },
                # confirmer primary lookup misses → new knowledge
                "search_metrics": {"status": "ok", "result": "[]"},
                # dry-run accepts
                "feedback_card": {
                    "status": "ok",
                    "result": json.dumps({"status": "accepted"}),
                },
            }
        )
        monkeypatch.setattr(
            self.manager, "_build_model", AsyncMock(return_value=self.llm)
        )
        monkeypatch.setattr(
            self.manager, "_create_cm_client", AsyncMock(return_value=self.cm)
        )
        monkeypatch.setattr(
            self.manager,
            "_resolve_access_token",
            AsyncMock(return_value="fixture-token"),
        )

    async def seed_session(self, *, datasource_id="ds1") -> Session:
        session = Session.create(identity=self.identity, agent_id="default", title="t")
        if datasource_id:
            session.bind_datasource(datasource_id)
        await self.sessions.add(session)
        await self.db.commit()
        return session

    async def seed_chat(
        self, session: Session, *, sequence: int, status: str, text: str
    ) -> Chat:
        chat = Chat.start(
            session_id=session.id,
            identity=self.identity,
            sequence=sequence,
            datasource_id=session.datasource_id,
            text=text,
        )
        await self.chats.add(chat)
        now = utcnow()
        await self.events.persist_batch(
            [
                ChatEventRecord(
                    chat_id=chat.id,
                    session_id=session.id,
                    sequence_number=0,
                    object="response",
                    created_at=now,
                    payload={
                        "object": "response",
                        "chat_id": chat.id,
                        "session_id": session.id,
                        "sequence_number": 0,
                        "id": "response_" + chat.id,
                        "status": "created",
                    },
                ),
                ChatEventRecord(
                    chat_id=chat.id,
                    session_id=session.id,
                    sequence_number=1,
                    object="message",
                    created_at=now,
                    payload={
                        "object": "message",
                        "chat_id": chat.id,
                        "session_id": session.id,
                        "sequence_number": 1,
                        "id": "msg_" + chat.id,
                        "sequence": 0,
                        "type": "message",
                        "role": "user",
                        "status": "completed",
                        "content": [
                            {"object": "content", "type": "text", "text": text}
                        ],
                    },
                ),
            ]
        )
        if status != "running":
            chat.mark_status(status)
            await self.chats.save(chat)
        await self.db.commit()
        return chat


async def test_detect_commits_cards(harness, monkeypatch) -> None:
    h = harness(llm_payloads=[_detection_payload("GMV")])
    session = await h.seed_session()
    chat = await h.seed_chat(session, sequence=1, status="completed", text="GMV 口径")

    cards = await h.manager.detect_for_chat(chat_id=chat.id, session_id=session.id)

    assert len(cards) == 1
    assert cards[0]["type"] == "metric_caliber"
    assert cards[0]["status"] == "pending"
    assert cards[0]["source_chat_id"] == chat.id
    stored = await h.cards.list_by_session(session.id)
    assert [c["id"] for c in stored] == [cards[0]["id"]]
    await h.db.commit()
    async with h.factory() as db:
        reloaded = await SettlementRepository(db, h.identity).list_by_session(
            session.id
        )
        assert [c["id"] for c in reloaded] == [cards[0]["id"]]
    # detector saw the conversation and the domain allowlist
    assert "GMV 口径" in h.llm.calls[0]["user"]
    assert "交易" in h.llm.calls[0]["user"]


async def test_detect_skips_without_domains(harness, monkeypatch) -> None:
    h = harness(
        llm_payloads=[_detection_payload("GMV")],
        cm_records={"list_domains": {"status": "ok", "result": "[]"}},
    )
    session = await h.seed_session()
    chat = await h.seed_chat(session, sequence=1, status="completed", text="hi")

    assert await h.manager.detect_for_chat(chat_id=chat.id, session_id=session.id) == []
    assert h.llm.calls == []


async def test_detect_disabled_by_settings(harness, monkeypatch) -> None:
    h = harness()
    h.manager._settings = SettlementSettings(enabled=False)
    assert await h.manager.detect_for_chat(chat_id="c", session_id="s") == []


async def test_watermark_cuts_window(harness, monkeypatch) -> None:
    h = harness(
        llm_payloads=[_detection_payload("DAU"), _detection_payload("ARPU")],
    )
    session = await h.seed_session()
    first = await h.seed_chat(
        session, sequence=1, status="completed", text="第一轮 GMV"
    )
    cards = await h.manager.detect_for_chat(chat_id=first.id, session_id=session.id)
    assert len(cards) == 1

    second = await h.seed_chat(
        session, sequence=2, status="completed", text="第二轮 DAU"
    )
    await h.manager.detect_for_chat(chat_id=second.id, session_id=session.id)
    # the second detection window must not include the settled first turn
    second_user = h.llm.calls[-1]["user"]
    assert "第二轮 DAU" in second_user
    assert "第一轮 GMV" not in second_user


async def test_commit_replaces_same_subject_pending(harness, monkeypatch) -> None:
    h = harness(
        llm_payloads=[_detection_payload("GMV"), _detection_payload("GMV")],
    )
    session = await h.seed_session()
    first = await h.seed_chat(session, sequence=1, status="completed", text="GMV v1")
    old = await h.manager.detect_for_chat(chat_id=first.id, session_id=session.id)

    # A later turn replaces the actionable card without replaying settled history.
    second = await h.seed_chat(session, sequence=2, status="completed", text="GMV v2")
    new = await h.manager.detect_for_chat(chat_id=second.id, session_id=session.id)

    stored = await h.cards.list_by_session(session.id)
    assert [c["id"] for c in stored] == [new[0]["id"]]
    assert old[0]["id"] != new[0]["id"]


async def test_dry_run_duplicate_drops_items(harness, monkeypatch) -> None:
    h = harness(
        llm_payloads=[_detection_payload("GMV")],
        cm_records={
            "list_domains": {
                "status": "ok",
                "result": json.dumps([{"name": "交易"}], ensure_ascii=False),
            },
            "search_metrics": {"status": "ok", "result": "[]"},
            "feedback_card": {
                "status": "ok",
                "result": json.dumps({"status": "duplicate"}),
            },
        },
    )
    session = await h.seed_session()
    chat = await h.seed_chat(session, sequence=1, status="completed", text="GMV")

    assert await h.manager.detect_for_chat(chat_id=chat.id, session_id=session.id) == []
    assert await h.cards.list_by_session(session.id) == []
    assert any(tool == "feedback_card" for tool, _ in h.cm.calls)


async def test_missing_datasource_drops_at_dry_run(harness, monkeypatch) -> None:
    h = harness(llm_payloads=[_detection_payload("GMV")])
    session = await h.seed_session(datasource_id=None)
    chat = await h.seed_chat(session, sequence=1, status="completed", text="GMV")

    assert await h.manager.detect_for_chat(chat_id=chat.id, session_id=session.id) == []


async def test_dismissed_history_filters(harness, monkeypatch) -> None:
    h = harness(
        llm_payloads=[
            _detection_payload("GMV"),
            {"dismissed_indices": [0]},  # DismissedFilter call
        ],
    )
    session = await h.seed_session()
    dismissed = await h.cards.add(
        session_id=session.id,
        source_chat_id="chat_0",
        type="metric_caliber",
        fields={"metric_name": "GMV"},
    )
    await h.cards.mark_queried(session.id, [dismissed["id"]])
    await h.cards.dismiss(dismissed["id"], session_id=session.id)

    chat = await h.seed_chat(session, sequence=1, status="completed", text="GMV")
    assert await h.manager.detect_for_chat(chat_id=chat.id, session_id=session.id) == []
    assert len(h.llm.calls) == 2


async def test_on_chat_finish_swallows_errors(harness, monkeypatch) -> None:
    h = harness()

    async def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(h.manager, "detect_for_chat", _boom)
    await h.manager.on_chat_finish(chat_id="c", session_id="s")  # must not raise


async def test_schedule_cm_ingest_requires_datasource(harness, monkeypatch) -> None:
    h = harness()
    ingested: list[dict[str, Any]] = []

    async def _fake_ingest(card, **kwargs):
        ingested.append({"card": card, **kwargs})
        return {"status": "ok (accepted)"}

    monkeypatch.setattr(manager_module, "settlement_ingest", _fake_ingest)

    await h.manager.schedule_cm_ingest({"id": "card_1"}, datasource_id="")
    assert ingested == []

    await h.manager.schedule_cm_ingest({"id": "card_1"}, datasource_id="ds1")
    for task in list(manager_module._BACKGROUND_TASKS):
        await task
    assert len(ingested) == 1
    assert ingested[0]["datasource_id"] == "ds1"


@pytest.mark.parametrize("field", ["user_id", "tenant_id", "workspace_id"])
async def test_detection_cannot_read_another_identity_conversation(
    harness, monkeypatch, field
):
    h = harness(llm_payloads=[_detection_payload("GMV")])
    session = await h.seed_session()
    chat = await h.seed_chat(
        session, sequence=1, status="completed", text="private GMV"
    )
    other = SettlementManager(h.db, identity=replace(TEST_IDENTITY, **{field: "other"}))
    model = AsyncMock(return_value=h.llm)
    monkeypatch.setattr(other, "_build_model", model)
    assert await other.detect_for_chat(chat_id=chat.id, session_id=session.id) == []
    model.assert_not_awaited()
    assert h.llm.calls == [] and h.cm.calls == []
