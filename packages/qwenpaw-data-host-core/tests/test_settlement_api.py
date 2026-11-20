# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories import SessionRepository, SettlementRepository
from qwenpaw_data.host.core.stream.hub import reset_hub

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


@pytest.fixture
def settlement_app(tmp_path: Path):
    asyncio.run(reset_engine())
    reset_hub()

    async def seed() -> tuple[str, str, str, str, str]:
        await init_db()
        async with get_session_factory()() as db:
            sessions = SessionRepository(db, TEST_IDENTITY)
            session = Session.create(
                identity=TEST_IDENTITY,
                agent_id="default",
                title="settlement",
            )
            other = Session.create(
                identity=TEST_IDENTITY,
                agent_id="default",
                title="settlement-other",
            )
            await sessions.add(session)
            await sessions.add(other)
            repo = SettlementRepository(db, TEST_IDENTITY)
            card1 = await repo.add(
                session_id=session.id,
                source_chat_id="chat_1",
                type="metric_caliber",
                fields={
                    "metric_name": "GMV",
                    "caliber": "原口径",
                    "domain": "交易",
                    "table": "dws_trade.orders",
                    "formula_sql": "SELECT SUM(amount)",
                },
            )
            card2 = await repo.add(
                session_id=session.id,
                source_chat_id="chat_1",
                type="dimension_def",
                fields={
                    "dimension_name": "渠道",
                    "bind_column": "channel",
                    "value_samples": "online",
                    "domain": "交易",
                    "table": "dws_trade.orders",
                },
            )
            card3 = await repo.add(
                session_id=other.id,
                source_chat_id="chat_2",
                type="column_meaning",
                fields={
                    "column_name": "channel",
                    "meaning": "投放渠道",
                    "table": "dws_trade.orders",
                    "domain": "交易",
                },
            )
            await db.commit()
            return session.id, other.id, card1["id"], card2["id"], card3["id"]

    session_id, other_session_id, card1_id, card2_id, card3_id = asyncio.run(seed())
    app = create_app()
    yield app, session_id, other_session_id, card1_id, card2_id, card3_id


def test_list_pending_marks_queried_and_confirm_dismiss_still_work(
    settlement_app,
) -> None:
    app, session_id, _other_session_id, card1_id, card2_id, _card3_id = settlement_app
    with TestClient(app) as client:
        listed = client.get(
            f"/api/sessions/{session_id}/settlement/cards",
            params={"status": "pending"},
        )
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["count"] == 2
        assert {c["id"] for c in body["cards"]} == {card1_id, card2_id}
        assert {c["status"] for c in body["cards"]} == {"queried"}

        # Push path keeps returning queried cards until confirm/dismiss.
        again = client.get(
            f"/api/sessions/{session_id}/settlement/cards",
            params={"status": "pending"},
        )
        assert again.status_code == 200, again.text
        again_body = again.json()
        assert again_body["count"] == 2
        assert {c["id"] for c in again_body["cards"]} == {card1_id, card2_id}
        assert {c["status"] for c in again_body["cards"]} == {"queried"}

        confirmed = client.post(
            f"/api/sessions/{session_id}/settlement/cards/{card1_id}/confirm",
            json={
                "fields": {
                    "metric_name": "GMV",
                    "caliber": "支付金额",
                    "domain": "交易",
                    "table": "dws_trade.orders",
                    "formula_sql": "SELECT SUM(pay_amount)",
                }
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["card"]["status"] == "confirmed"
        assert confirmed.json()["card"]["fields"] == {
            "metric_name": "GMV",
            "caliber": "支付金额",
            "domain": "交易",
            "table": "dws_trade.orders",
            "formula_sql": "SELECT SUM(pay_amount)",
        }

        dismissed = client.post(
            f"/api/sessions/{session_id}/settlement/cards/{card2_id}/dismiss"
        )
        assert dismissed.status_code == 200, dismissed.text
        assert dismissed.json()["card"]["status"] == "dismissed"

        after = client.get(
            f"/api/sessions/{session_id}/settlement/cards",
            params={"status": "pending"},
        )
        assert after.status_code == 200, after.text
        assert after.json()["count"] == 0


def test_unknown_session_is_not_found(settlement_app) -> None:
    app, _session_id, _other_session_id, _card1_id, _card2_id, _card3_id = (
        settlement_app
    )
    with TestClient(app) as client:
        response = client.get("/api/sessions/ses_missing/settlement/cards")
        assert response.status_code == 404, response.text


def test_card_errors_mapped(settlement_app) -> None:
    app, session_id, _other_session_id, card1_id, _card2_id, _card3_id = settlement_app
    with TestClient(app) as client:
        missing = client.post(
            f"/api/sessions/{session_id}/settlement/cards/card_missing/confirm",
            json={},
        )
        assert missing.status_code == 404, missing.text
        assert missing.json()["code"] == "NOT_FOUND"

        client.post(
            f"/api/sessions/{session_id}/settlement/cards/{card1_id}/confirm",
            json={},
        )
        again = client.post(
            f"/api/sessions/{session_id}/settlement/cards/{card1_id}/dismiss"
        )
        assert again.status_code == 400, again.text
        assert again.json()["code"] == "VALIDATION"


def test_workspace_list_returns_all_sessions_without_marking_queried(
    settlement_app,
) -> None:
    app, session_id, other_session_id, card1_id, card2_id, card3_id = settlement_app
    with TestClient(app) as client:
        listed = client.get("/api/settlement/cards")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["count"] == 3
        assert {c["id"] for c in body["cards"]} == {card1_id, card2_id, card3_id}
        assert {c["session_id"] for c in body["cards"]} == {
            session_id,
            other_session_id,
        }
        assert {c["status"] for c in body["cards"]} == {"pending"}

        pending = client.get("/api/settlement/cards", params={"status": "pending"})
        assert pending.status_code == 200, pending.text
        assert pending.json()["count"] == 3
        assert {c["status"] for c in pending.json()["cards"]} == {"pending"}
