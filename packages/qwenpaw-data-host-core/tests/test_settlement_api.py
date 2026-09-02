# -*- coding: utf-8 -*-
"""Settlement card routes over the live service app."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402

METRIC_FIELDS = {
    "metric_name": "GMV",
    "caliber": "原口径",
    "domain": "交易",
    "table": "dws_trade.orders",
    "formula_sql": "SELECT SUM(amount)",
}
DIMENSION_FIELDS = {
    "dimension_name": "渠道",
    "bind_column": "channel",
    "value_samples": "online",
    "domain": "交易",
    "table": "dws_trade.orders",
}


@asynccontextmanager
async def settlement_client(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    app = create_app(home=tmp_path, model=object())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            state = app.state.service
            created = await http.post("/api/v1/sessions", json={"title": "结算"})
            assert created.status_code == 200, created.text
            session_id = created.json()["session"]["id"]
            card1 = await state.settlement.add(
                user_id="local",
                session_id=session_id,
                source_chat_id="chat_1",
                type="metric_caliber",
                fields=dict(METRIC_FIELDS),
            )
            card2 = await state.settlement.add(
                user_id="local",
                session_id=session_id,
                source_chat_id="chat_1",
                type="dimension_def",
                fields=dict(DIMENSION_FIELDS),
            )
            yield http, session_id, card1["id"], card2["id"]


async def test_list_pending_marks_queried_then_confirm_dismiss(
    tmp_path, monkeypatch
) -> None:
    async with settlement_client(tmp_path, monkeypatch) as (
        http,
        session_id,
        card1_id,
        card2_id,
    ):
        listed = await http.get(
            f"/api/v1/sessions/{session_id}/settlement/cards",
            params={"status": "pending"},
        )
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["count"] == 2
        assert {c["id"] for c in body["cards"]} == {card1_id, card2_id}
        assert {c["status"] for c in body["cards"]} == {"queried"}

        # Poll path keeps returning queried cards until confirm/dismiss.
        again = await http.get(
            f"/api/v1/sessions/{session_id}/settlement/cards",
            params={"status": "pending"},
        )
        assert again.json()["count"] == 2
        assert {c["status"] for c in again.json()["cards"]} == {"queried"}

        confirmed = await http.post(
            f"/api/v1/sessions/{session_id}/settlement/cards/{card1_id}/confirm",
            json={"fields": {**METRIC_FIELDS, "caliber": "支付金额"}},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["card"]["status"] == "confirmed"
        assert confirmed.json()["card"]["fields"]["caliber"] == "支付金额"

        dismissed = await http.post(
            f"/api/v1/sessions/{session_id}/settlement/cards/{card2_id}/dismiss"
        )
        assert dismissed.status_code == 200, dismissed.text
        assert dismissed.json()["card"]["status"] == "dismissed"

        after = await http.get(
            f"/api/v1/sessions/{session_id}/settlement/cards",
            params={"status": "pending"},
        )
        assert after.json()["count"] == 0

        all_cards = await http.get(
            f"/api/v1/sessions/{session_id}/settlement/cards"
        )
        assert all_cards.json()["count"] == 2


async def test_unknown_session_is_not_found(tmp_path, monkeypatch) -> None:
    async with settlement_client(tmp_path, monkeypatch) as (http, *_):
        response = await http.get("/api/v1/sessions/ses_missing/settlement/cards")
        assert response.status_code == 404, response.text


async def test_card_errors_mapped(tmp_path, monkeypatch) -> None:
    async with settlement_client(tmp_path, monkeypatch) as (
        http,
        session_id,
        card1_id,
        _card2_id,
    ):
        missing = await http.post(
            f"/api/v1/sessions/{session_id}/settlement/cards/card_missing/confirm",
            json={},
        )
        assert missing.status_code == 404, missing.text
        assert missing.json()["code"] == "NOT_FOUND"

        confirmed = await http.post(
            f"/api/v1/sessions/{session_id}/settlement/cards/{card1_id}/confirm",
            json={},
        )
        assert confirmed.status_code == 200, confirmed.text

        again = await http.post(
            f"/api/v1/sessions/{session_id}/settlement/cards/{card1_id}/dismiss"
        )
        assert again.status_code == 400, again.text
        assert again.json()["code"] == "VALIDATION"
