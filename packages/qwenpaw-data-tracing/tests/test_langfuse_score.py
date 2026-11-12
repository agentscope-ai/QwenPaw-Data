# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace

import pytest
from qwenpaw_data.tracing.core.names import Score
from qwenpaw_data.tracing.core.score import ScoreEvent
from qwenpaw_data.tracing.export.score import (
    score_body,
    tracer_delete_score,
    tracer_send_score,
)


def test_score_event_scenarios() -> None:
    stop = ScoreEvent.terminal(
        chat_id="chat-1", name=Score.STOP, value=1, data_type="BOOLEAN"
    )
    assert stop.id == "stop:chat-1"
    assert score_body(stop, "a" * 32)["traceId"] == "a" * 32

    session = ScoreEvent.create(
        id="bev-1",
        name=Score.SESSION_REVISIT,
        value="session_revisit",
        session_id="ses-1",
    )
    posted = score_body(session, None)
    assert posted["sessionId"] == "ses-1"
    assert "traceId" not in posted


@pytest.mark.asyncio
async def test_score_event_send_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRACING_ENABLED", raising=False)
    ScoreEvent.terminal(
        chat_id="chat-1", name=Score.STOP, value=1, data_type="BOOLEAN"
    )
    await tracer_send_score(
        name=Score.STOP, value=1, chat_id="chat-1", data_type="BOOLEAN"
    )
    await tracer_delete_score(id="stop:chat-1")


def test_score_event_rejects_missing_identity() -> None:
    with pytest.raises(ValueError, match="score id is required"):
        ScoreEvent.create(id="", name="x", value="y", chat_id="c")
    with pytest.raises(ValueError, match="chat_id or session_id is required"):
        ScoreEvent.create(id="1", name="x", value="y")
    with pytest.raises(ValueError, match="chat_id is required"):
        ScoreEvent.terminal(chat_id="", name="stop", value=1)
    with pytest.raises(ValueError, match="chat_id is required"):
        ScoreEvent.update(id="1", name="x", value="y", chat_id="")


def test_score_event_numeric_update() -> None:
    numeric = ScoreEvent.update(
        id="n1", name=Score.CONFIDENCE, value=0.8, chat_id="chat-1", data_type="NUMERIC"
    )
    assert numeric.value_num == 0.8
    assert score_body(numeric, "a" * 32)["value"] == 0.8


@pytest.mark.asyncio
async def test_score_event_send_and_retract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACING_ENABLED", "1")
    monkeypatch.setenv("TRACING_HOST", "https://lf.example")
    monkeypatch.setenv("TRACING_PUBLIC_KEY", "pk")
    monkeypatch.setenv("TRACING_SECRET_KEY", "sk")
    posted: list = []
    deleted: list[str] = []

    class _Resp:
        status_code = 204

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, auth=None):
            posted.append((url, json))
            return _Resp()

        async def delete(self, url, auth=None):
            deleted.append(url)
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await tracer_send_score(
        name=Score.STOP,
        value=1,
        chat_id="chat-1",
        data_type="BOOLEAN",
        trace_id="a" * 32,
    )
    await tracer_delete_score(id="stop:chat-1")
    assert posted[0][0] == "https://lf.example/api/public/scores"
    assert posted[0][1]["id"] == "stop:chat-1"
    assert deleted == ["https://lf.example/api/public/scores/stop:chat-1"]


def test_score_body_boolean_and_categorical() -> None:
    boolean = SimpleNamespace(
        id="fbk_1",
        name="copy",
        data_type="BOOLEAN",
        value_num=1,
        value_text=None,
        comment=None,
        session_id=None,
        metadata_json=None,
    )
    body = score_body(boolean, "a" * 32)
    assert body["traceId"] == "a" * 32
    assert body["value"] == 1
    assert body["dataType"] == "BOOLEAN"
    assert "comment" not in body

    categorical = SimpleNamespace(
        id="bev_1",
        name="trace_view",
        data_type="CATEGORICAL",
        value_num=None,
        value_text="agent",
        comment="agent",
        session_id=None,
        metadata_json=None,
    )
    scored = score_body(categorical, "b" * 32)
    assert scored["value"] == "agent"
    assert scored["comment"] == "agent"

    session_score = SimpleNamespace(
        id="bev_2",
        name="share",
        data_type="CATEGORICAL",
        value_num=None,
        value_text="workspace",
        comment=None,
        session_id="ses-1",
        metadata_json={"share_scope": "workspace"},
    )
    posted = score_body(session_score, None)
    assert posted["sessionId"] == "ses-1"
    assert "traceId" not in posted
    assert posted["metadata"] == {"share_scope": "workspace"}
