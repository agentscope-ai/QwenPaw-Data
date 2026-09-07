# -*- coding: utf-8 -*-
"""Runtime wiring for follow-up recommendation (algo behavior is tested
in the ported test_followup_* suites; this file covers the host side)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.algo.followup.collector import (  # noqa: E402
    SignalCollector,
)
from qwenpaw_data.host.core.algo.followup.models import (  # noqa: E402
    EntityRecord,
    SignalSnapshot,
)
from qwenpaw_data.host.core.algo.followup.recommend import (  # noqa: E402
    FollowUpRecommend,
)
from qwenpaw_data.host.core.api.app import create_app  # noqa: E402
from qwenpaw_data.host.core.core import QwenPawDataHost  # noqa: E402

from test_service_smoke import (  # noqa: E402
    ScriptedAgent,
    _collect_sse,
    _new_session,
    _script,
)


@asynccontextmanager
async def service_client(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)

    async def fake_get_agent(self, *, mode: str, request_context=None):
        return ScriptedAgent(_script())

    monkeypatch.setattr(QwenPawDataHost, "get_agent", fake_get_agent)
    app = create_app(home=tmp_path, model=object())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            yield http


async def _run_turn(http, session_id: str) -> list[dict]:
    created = await http.post(
        f"/api/v1/sessions/{session_id}/chats", json={"text": "分析Q3"}
    )
    chat_id = created.json()["chat"]["id"]
    _ids, payloads = await _collect_sse(
        http, f"/api/v1/sessions/{session_id}/chats/{chat_id}/events"
    )
    # The terminal SSE frame is published before the chat's terminal status is
    # persisted; wait for the store to catch up so a fast follow-up POST does
    # not race into a spurious has_active_chat CONFLICT.
    import asyncio

    for _ in range(100):
        listed = await http.get(f"/api/v1/sessions/{session_id}/chats")
        chats = {c["id"]: c for c in listed.json()["items"]}
        if chats.get(chat_id, {}).get("status") in (
            "completed",
            "failed",
            "canceled",
        ):
            break
        await asyncio.sleep(0.05)
    return payloads


async def test_followup_event_precedes_terminal_response(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("QWENPAW_DATA_FOLLOWUP_ENABLED", raising=False)

    async def fixed_join(self) -> list[str]:
        return ["对比Q2的渠道结构", "投放成本口径是什么"]

    monkeypatch.setattr(FollowUpRecommend, "join", fixed_join)

    async with service_client(tmp_path, monkeypatch) as http:
        session_id = await _new_session(http)
        payloads = await _run_turn(http, session_id)

        objects = [p["object"] for p in payloads]
        assert "followup.generated" in objects
        # Delivered before the stream's terminal frame.
        assert objects.index("followup.generated") < len(objects) - 1
        assert payloads[-1]["object"] == "response"
        assert payloads[-1]["status"] == "completed"

        followup = next(p for p in payloads if p["object"] == "followup.generated")
        assert followup["followup"]["questions"] == [
            "对比Q2的渠道结构",
            "投放成本口径是什么",
        ]


async def test_slow_model_falls_back_before_terminal_response(
    tmp_path, monkeypatch
) -> None:
    snapshot = SignalSnapshot(
        user_input="分析 GAAP用户数",
        final_answer_summary="GAAP用户数环比下降。",
        anchor_metric="GAAP用户数",
        metrics=(
            EntityRecord(name="GAAP用户数", analyzed=True, relevance=1.0),
        ),
        dimensions=(EntityRecord(name="页面", relevance=0.8),),
        unused_dimensions=("页面",),
        business_entities=("GAAP用户数", "页面"),
    )

    class SlowLLM:
        async def complete(self, prompt: str) -> dict:
            import asyncio

            await asyncio.sleep(0.1)
            return {"questions": []}

    original_init = FollowUpRecommend.__init__

    def short_budget(self, **kwargs):
        original_init(self, timeout_sec=0.01, **kwargs)

    monkeypatch.setattr(
        SignalCollector, "_build_snapshot", lambda self: snapshot
    )
    monkeypatch.setattr(FollowUpRecommend, "__init__", short_budget)
    monkeypatch.setattr(
        FollowUpRecommend, "_build_llm", lambda self: SlowLLM()
    )

    async with service_client(tmp_path, monkeypatch) as http:
        session_id = await _new_session(http)
        payloads = await _run_turn(http, session_id)

    objects = [payload["object"] for payload in payloads]
    followup = next(
        payload
        for payload in payloads
        if payload["object"] == "followup.generated"
    )
    assert followup["followup"]["questions"]
    assert all(
        "GAAP用户数" in question
        for question in followup["followup"]["questions"]
    )
    assert objects.index("followup.generated") < len(objects) - 1
    assert payloads[-1]["object"] == "response"
    assert payloads[-1]["status"] == "completed"


async def test_previous_followups_flow_into_next_chat(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("QWENPAW_DATA_FOLLOWUP_ENABLED", raising=False)

    async def fixed_join(self) -> list[str]:
        return ["第一轮的问题"]

    monkeypatch.setattr(FollowUpRecommend, "join", fixed_join)

    captured: list[tuple[str, ...]] = []
    original_init = FollowUpRecommend.__init__

    def spy_init(self, **kwargs):
        captured.append(tuple(kwargs.get("previous_followups") or ()))
        original_init(self, **kwargs)

    monkeypatch.setattr(FollowUpRecommend, "__init__", spy_init)

    async with service_client(tmp_path, monkeypatch) as http:
        session_id = await _new_session(http)
        await _run_turn(http, session_id)
        await _run_turn(http, session_id)

    assert captured[0] == ()
    assert captured[1] == ("第一轮的问题",)


async def test_followup_disabled_by_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_FOLLOWUP_ENABLED", "0")

    async def boom(self) -> list[str]:  # pragma: no cover - must not run
        raise AssertionError("followup ran while disabled")

    monkeypatch.setattr(FollowUpRecommend, "join", boom)

    async with service_client(tmp_path, monkeypatch) as http:
        session_id = await _new_session(http)
        payloads = await _run_turn(http, session_id)
        assert all(p["object"] != "followup.generated" for p in payloads)
        assert payloads[-1]["status"] == "completed"
