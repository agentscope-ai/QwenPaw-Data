"""Follow-up runtime wiring through real agent middleware, HTTP and SQL."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from qwenpaw_data.host.core.agent.middleware.followup import FollowUpMiddleware
from qwenpaw_data.host.core.algo.followup.models import Candidate
from qwenpaw_data.host.core.algo.followup.recommend import FollowUpRecommend
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.repositories.followup import FollowUpRepository
from qwenpaw_data.host.core.runtime.builder import data_agent
from qwenpaw_data.host.core.settings import clear_settings
from . import test_runtime_settings
from .conftest import TEST_IDENTITY
from .test_qwenpaw_data_agent import FakeTextModel
from .test_service_smoke import _collect_sse, _new_session, _post_turn, _url

service = test_runtime_settings.service
QUESTIONS = ["对比Q2的渠道结构", "投放成本口径是什么"]


def _candidates():
    return [
        Candidate(text=q, intent_category="comparison", source_channel="rules")
        for q in QUESTIONS
    ]


@pytest.fixture
async def followup_service(service, monkeypatch, seed_test_preferences):
    await seed_test_preferences(get_session_factory())
    monkeypatch.setenv("QWENPAW_DATA_FOLLOWUP_ENABLED", "1")
    clear_settings()
    monkeypatch.setattr(data_agent, "build_model", lambda *_a, **_k: FakeTextModel())
    # Keep the real builder/agent and follow-up middleware; unrelated network-backed
    # business middleware is not part of this HTTP/SQL integration test.
    monkeypatch.setattr(
        data_agent,
        "_middlewares",
        lambda context, envelope: [
            FollowUpMiddleware(run_context=context, envelope=envelope),
        ],
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.McpRepository.sync",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.fetch_datasource_type",
        AsyncMock(return_value=None),
    )
    return service


async def _saved(chat_id):
    async with get_session_factory()() as db:
        return (await FollowUpRepository(db, TEST_IDENTITY).list_for_chats([chat_id]))[
            chat_id
        ]


async def test_followup_event_precedes_terminal_response(followup_service, monkeypatch):
    async def recommend(self, snapshot):
        await snapshot
        return _candidates()

    monkeypatch.setattr(FollowUpRecommend, "_run_pipeline", recommend)
    http = followup_service
    sid = await _new_session(http)
    chat = await _post_turn(http, sid)
    _, payloads = await _collect_sse(http, _url(sid, chat["id"]))
    objects = [p["object"] for p in payloads]
    assert objects.index("followup.generated") < len(objects) - 1
    assert (
        payloads[-1]["object"] == "response" and payloads[-1]["status"] == "completed"
    )
    event = next(p for p in payloads if p["object"] == "followup.generated")
    assert event["followup"]["questions"] == QUESTIONS
    assert (await _saved(chat["id"]))["questions"] == QUESTIONS


async def test_late_followup_persists_without_writing_after_terminal(
    followup_service, monkeypatch
):
    release = asyncio.Event()
    units = []

    async def recommend(self, snapshot):
        units.append(self)
        await snapshot
        await release.wait()
        return _candidates()

    monkeypatch.setattr(FollowUpRecommend, "_run_pipeline", recommend)
    monkeypatch.setenv("QWENPAW_DATA_FOLLOWUP_TIMEOUT_SEC", "0.01")
    clear_settings()
    http = followup_service
    sid = await _new_session(http)
    try:
        chat = await asyncio.wait_for(_post_turn(http, sid), 5)
        ids, before = await _collect_sse(http, _url(sid, chat["id"]))
        assert before[-1]["status"] == "completed"
        assert all(p["object"] != "followup.generated" for p in before)
        assert await _saved(chat["id"]) is None
        assert len(units) == 1 and units[0]._late is not None
        release.set()
        await asyncio.wait_for(units[0]._late, 5)
        assert (await _saved(chat["id"]))["questions"] == QUESTIONS
        # Late delivery is snapshot-only, never appended after the terminal cursor.
        assert await _collect_sse(
            http, _url(sid, chat["id"]), headers={"Last-Event-ID": str(ids[-1])}
        ) == ([], [])
        assert await _collect_sse(http, _url(sid, chat["id"])) == (ids, before)
        snapshot = await http.get(f"/api/sessions/{sid}/snapshot")
        assert snapshot.status_code == 200
        assert snapshot.json()["chats"][0]["followup"]["questions"] == QUESTIONS
    finally:
        release.set()
        await asyncio.gather(
            *[u._late for u in units if u._late is not None], return_exceptions=True
        )


async def test_previous_followups_are_loaded_only_from_the_same_session(
    followup_service, monkeypatch
):
    captured = []

    async def recommend(self, snapshot):
        await snapshot
        captured.append(self.previous_followups)
        return _candidates()

    monkeypatch.setattr(FollowUpRecommend, "_run_pipeline", recommend)
    http = followup_service
    sid = await _new_session(http)
    await _post_turn(http, sid)
    await _post_turn(http, sid)
    other = await _new_session(http)
    await _post_turn(http, other)
    assert captured == [(), tuple(QUESTIONS), ()]


async def test_disabled_followup_does_not_construct_recommender(
    followup_service, monkeypatch
):
    monkeypatch.setenv("QWENPAW_DATA_FOLLOWUP_ENABLED", "0")
    clear_settings()

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled follow-up must not construct a recommender")

    monkeypatch.setattr(FollowUpRecommend, "__init__", forbidden)
    http = followup_service
    sid = await _new_session(http)
    chat = await _post_turn(http, sid)
    _, payloads = await _collect_sse(http, _url(sid, chat["id"]))
    assert all(p["object"] != "followup.generated" for p in payloads)
    assert payloads[-1]["status"] == "completed"
    assert await _saved(chat["id"]) is None
