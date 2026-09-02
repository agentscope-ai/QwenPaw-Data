# -*- coding: utf-8 -*-
"""SettlementManager orchestration against real JSON stores and scripted deps."""

from __future__ import annotations

import json
from typing import Any

import qwenpaw_data.host.core.algo.settlement.manager as manager_module
from qwenpaw_data.host.core.algo.settlement import (
    SettlementManager,
    SettlementSettings,
)
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.store.json_store import (
    JSONChatEventStore,
    JSONChatStore,
    JSONSessionStore,
    JSONSettlementStore,
)

from test_settlement_algo import FakeCm, FakeStructuredLLM


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
    def __init__(self, tmp_path, monkeypatch, *, llm_payloads=None, cm_records=None):
        self.identity = Identity.anonymous()
        self.sessions = JSONSessionStore(tmp_path)
        self.chats = JSONChatStore(tmp_path)
        self.events = JSONChatEventStore(tmp_path)
        self.cards = JSONSettlementStore(tmp_path)
        self.manager = SettlementManager(
            sessions=self.sessions,
            chats=self.chats,
            events=self.events,
            cards=self.cards,
            identity=self.identity,
            settings=SettlementSettings(window_size=3),
        )
        self.llm = FakeStructuredLLM(llm_payloads or [])
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
        monkeypatch.setattr(self.manager, "_build_llm", lambda: self.llm)
        monkeypatch.setattr(
            self.manager, "_create_cm_client", lambda **kwargs: self.cm
        )

    async def seed_session(self, *, datasource_id="ds1") -> Session:
        session = Session.create(
            identity=self.identity, agent_id="default", title="t"
        )
        if datasource_id:
            session.bind_datasource(datasource_id)
        await self.sessions.add(session)
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
        await self.events.append(
            session_id=session.id,
            chat_id=chat.id,
            payload={
                "object": "message",
                "id": f"msg_{chat.id}",
                "sequence": 0,
                "type": "message",
                "role": "user",
                "status": "completed",
                "content": [{"object": "content", "type": "text", "text": text}],
            },
        )
        if status != "running":
            chat.mark_status(status)
            await self.chats.save(chat)
        return chat


async def test_detect_commits_cards(tmp_path, monkeypatch) -> None:
    h = _Harness(tmp_path, monkeypatch, llm_payloads=[_detection_payload("GMV")])
    session = await h.seed_session()
    chat = await h.seed_chat(session, sequence=1, status="completed", text="GMV 口径")

    cards = await h.manager.detect_for_chat(chat_id=chat.id, session_id=session.id)

    assert len(cards) == 1
    assert cards[0]["type"] == "metric_caliber"
    assert cards[0]["status"] == "pending"
    assert cards[0]["source_chat_id"] == chat.id
    stored = await h.cards.list_by_session("local", session.id)
    assert [c["id"] for c in stored] == [cards[0]["id"]]
    # detector saw the conversation and the domain allowlist
    assert "GMV 口径" in h.llm.calls[0]["user"]
    assert "交易" in h.llm.calls[0]["user"]


async def test_detect_skips_without_domains(tmp_path, monkeypatch) -> None:
    h = _Harness(
        tmp_path,
        monkeypatch,
        llm_payloads=[_detection_payload("GMV")],
        cm_records={"list_domains": {"status": "ok", "result": "[]"}},
    )
    session = await h.seed_session()
    chat = await h.seed_chat(session, sequence=1, status="completed", text="hi")

    assert await h.manager.detect_for_chat(chat_id=chat.id, session_id=session.id) == []
    assert h.llm.calls == []


async def test_detect_disabled_by_settings(tmp_path, monkeypatch) -> None:
    h = _Harness(tmp_path, monkeypatch)
    h.manager._settings = SettlementSettings(enabled=False)
    assert await h.manager.detect_for_chat(chat_id="c", session_id="s") == []


async def test_watermark_cuts_window(tmp_path, monkeypatch) -> None:
    h = _Harness(
        tmp_path,
        monkeypatch,
        llm_payloads=[_detection_payload("DAU"), _detection_payload("ARPU")],
    )
    session = await h.seed_session()
    first = await h.seed_chat(session, sequence=1, status="completed", text="第一轮 GMV")
    cards = await h.manager.detect_for_chat(chat_id=first.id, session_id=session.id)
    assert len(cards) == 1

    second = await h.seed_chat(session, sequence=2, status="completed", text="第二轮 DAU")
    await h.manager.detect_for_chat(chat_id=second.id, session_id=session.id)
    # the second detection window must not include the settled first turn
    second_user = h.llm.calls[-1]["user"]
    assert "第二轮 DAU" in second_user
    assert "第一轮 GMV" not in second_user


async def test_commit_replaces_same_subject_pending(tmp_path, monkeypatch) -> None:
    h = _Harness(
        tmp_path,
        monkeypatch,
        llm_payloads=[_detection_payload("GMV"), _detection_payload("GMV")],
    )
    session = await h.seed_session()
    first = await h.seed_chat(session, sequence=1, status="completed", text="GMV v1")
    old = await h.manager.detect_for_chat(chat_id=first.id, session_id=session.id)

    # same subject re-detected in a later turn replaces the pending card
    # (fresh manager watermark ignores nothing since the old card is deleted last)
    second = await h.seed_chat(session, sequence=2, status="completed", text="GMV v2")
    # force the window to include the second chat despite the watermark
    monkeypatch.setattr(
        h.manager, "_get_watermark_chat_id", _async_return(None)
    )
    new = await h.manager.detect_for_chat(chat_id=second.id, session_id=session.id)

    stored = await h.cards.list_by_session("local", session.id)
    assert [c["id"] for c in stored] == [new[0]["id"]]
    assert old[0]["id"] != new[0]["id"]


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


async def test_dry_run_duplicate_drops_items(tmp_path, monkeypatch) -> None:
    h = _Harness(
        tmp_path,
        monkeypatch,
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
    assert await h.cards.list_by_session("local", session.id) == []


async def test_missing_datasource_drops_at_dry_run(tmp_path, monkeypatch) -> None:
    h = _Harness(tmp_path, monkeypatch, llm_payloads=[_detection_payload("GMV")])
    session = await h.seed_session(datasource_id=None)
    chat = await h.seed_chat(session, sequence=1, status="completed", text="GMV")

    assert await h.manager.detect_for_chat(chat_id=chat.id, session_id=session.id) == []


async def test_dismissed_history_filters(tmp_path, monkeypatch) -> None:
    h = _Harness(
        tmp_path,
        monkeypatch,
        llm_payloads=[
            _detection_payload("GMV"),
            {"dismissed_indices": [0]},  # DismissedFilter call
        ],
    )
    session = await h.seed_session()
    dismissed = await h.cards.add(
        user_id="local",
        session_id=session.id,
        source_chat_id="chat_0",
        type="metric_caliber",
        fields={"metric_name": "GMV"},
    )
    await h.cards.mark_queried("local", session.id, [dismissed["id"]])
    await h.cards.dismiss("local", dismissed["id"], session_id=session.id)

    chat = await h.seed_chat(session, sequence=1, status="completed", text="GMV")
    assert await h.manager.detect_for_chat(chat_id=chat.id, session_id=session.id) == []
    assert len(h.llm.calls) == 2


async def test_on_chat_finish_swallows_errors(tmp_path, monkeypatch) -> None:
    h = _Harness(tmp_path, monkeypatch)

    async def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(h.manager, "detect_for_chat", _boom)
    await h.manager.on_chat_finish(chat_id="c", session_id="s")  # must not raise


async def test_schedule_cm_ingest_requires_datasource(tmp_path, monkeypatch) -> None:
    h = _Harness(tmp_path, monkeypatch)
    ingested: list[dict[str, Any]] = []

    async def _fake_ingest(card, **kwargs):
        ingested.append({"card": card, **kwargs})
        return {"status": "ok (accepted)"}

    monkeypatch.setattr(manager_module, "settlement_ingest", _fake_ingest)

    h.manager.schedule_cm_ingest({"id": "card_1"}, datasource_id="")
    assert ingested == []

    h.manager.schedule_cm_ingest({"id": "card_1"}, datasource_id="ds1")
    for task in list(manager_module._BACKGROUND_TASKS):
        await task
    assert len(ingested) == 1
    assert ingested[0]["datasource_id"] == "ds1"
