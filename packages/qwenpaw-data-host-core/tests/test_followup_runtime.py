# -*- coding: utf-8 -*-
"""Runtime wiring for follow-up recommendation (algo behavior is tested
in the ported test_followup_* suites; this file covers the host side)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

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
