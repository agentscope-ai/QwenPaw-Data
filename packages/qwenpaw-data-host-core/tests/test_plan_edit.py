# -*- coding: utf-8 -*-
"""Plan projection, plan-edit routes, and steer comment composition."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from qwenpaw_data.host.core.agent.middleware import SteerMiddleware
from qwenpaw_data.host.core.utils.plan import (
    plan_schema_to_sop,
    sop_plan_to_schema,
)

SOP_PLAN = {
    "name": "月度分析",
    "description": "拆解销售异动",
    "expected_outcome": "定位根因",
    "nodes": [
        {
            "node_id": "n1",
            "name": "取数",
            "description": "拉取订单明细",
            "expected_outcome": "明细表",
            "deps": [],
            "state": "done",
        },
        {
            "node_id": "n2",
            "name": "归因",
            "description": "按区域拆解",
            "expected_outcome": "归因结论",
            "deps": ["n1"],
            "state": "in_progress",
        },
    ],
}


# ---------------------------------------------------------------------------
# Projection


def test_sop_plan_to_schema_projects_tasks() -> None:
    plan = sop_plan_to_schema(SOP_PLAN)
    assert plan is not None
    tasks = plan["tasks"]
    assert [t["id"] for t in tasks] == ["n1", "n2"]
    assert tasks[0]["subject"] == "取数"
    assert tasks[0]["state"] == "completed"
    assert tasks[0]["blocks"] == ["n2"]
    assert tasks[1]["state"] == "in_progress"
    assert tasks[1]["blocked_by"] == ["n1"]
    assert tasks[1]["metadata"]["expected_outcome"] == "归因结论"


def test_sop_plan_to_schema_empty_is_none() -> None:
    assert sop_plan_to_schema(None) is None
    assert sop_plan_to_schema({}) is None
    assert sop_plan_to_schema({"name": "x", "nodes": []}) is None
    # Unannotated nodes default to pending.
    plan = sop_plan_to_schema(
        {"nodes": [{"node_id": "a", "name": "任务A", "deps": []}]}
    )
    assert plan["tasks"][0]["state"] == "pending"


def test_plan_schema_to_sop_carries_meta_and_deps() -> None:
    schema = sop_plan_to_schema(SOP_PLAN)
    sop = plan_schema_to_sop(schema, previous=SOP_PLAN)
    assert sop["name"] == "月度分析"
    assert [n["node_id"] for n in sop["nodes"]] == ["n1", "n2"]
    assert sop["nodes"][1]["deps"] == ["n1"]
    assert sop["nodes"][1]["expected_outcome"] == "归因结论"
    # No previous plan → generated meta, description falls back.
    bare = plan_schema_to_sop(
        {"tasks": [{"id": "t1", "subject": "只有标题"}]}
    )
    assert bare["name"]
    assert bare["nodes"][0]["expected_outcome"] == "只有标题"
    assert plan_schema_to_sop(None) is None


# ---------------------------------------------------------------------------
# Steer middleware composes comments into the injected hint


class FakeState:
    def __init__(self) -> None:
        self.reply_id = "reply-1"
        self.context: list[Any] = []


class FakeAgent:
    def __init__(self, middleware: SteerMiddleware) -> None:
        self.name = "qwenpaw-data"
        self.state = FakeState()
        self._reasoning_middlewares = [middleware]


async def test_middleware_composes_comments_into_hint() -> None:
    middleware = SteerMiddleware()
    agent = FakeAgent(middleware)
    comments = [
        {"path": "out/a.md", "line_start": 2, "line_end": 3, "comment": "补充"}
    ]

    steer_task = asyncio.create_task(middleware.steer("改一下", comments))
    await asyncio.sleep(0)

    async def next_handler(**_kwargs: Any):
        yield "sentinel"

    events = [
        event
        async for event in middleware.on_reasoning(agent, {}, next_handler)
    ]
    await steer_task

    hint_event = events[0]
    # The stream event keeps the raw text; comments ride in metadata.
    assert hint_event.hint == "改一下"
    assert hint_event.metadata["artifact_comments"] == comments
    # The model context receives the composed input.
    composed = agent.state.context[0].content[0].hint
    assert "out/a.md" in composed and composed.endswith("改一下")


# ---------------------------------------------------------------------------
# Plan edit routes

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402
from qwenpaw_data.host.core.domain.identity import Identity  # noqa: E402
from qwenpaw_data.host.core.domain.session import Session  # noqa: E402
from qwenpaw_data.host.core.runtime.registry import (  # noqa: E402
    get_runtime_registry,
)


class FakePlanRuntime:
    def __init__(self) -> None:
        self.replacements: list[tuple[dict[str, Any], str | None]] = []

    async def replace_plan(
        self,
        plan: dict[str, Any],
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.replacements.append((plan, reason))
        return plan


@asynccontextmanager
async def plan_client(tmp_path: Path, monkeypatch):
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
            yield http, app


async def _seed_chat(app, *, plan: dict | None = None) -> tuple[str, str]:
    state = app.state.service
    session = Session.create(identity=Identity.anonymous())
    await state.sessions.add(session)
    chat = session.open_chat(text="hi", datasource_id=None, has_active_chat=False)
    if plan is not None:
        chat.plan = plan
    await state.sessions.save(session)
    await state.chats.add(chat)
    return session.id, chat.id


EDIT_BODY = {
    "reason": "去掉冗余步骤",
    "plan": {
        "tasks": [
            {"id": "n1", "subject": "取数", "description": "拉取订单明细"},
            {
                "id": "n2",
                "subject": "归因",
                "description": "按区域拆解",
                "blocked_by": ["n1"],
            },
        ]
    },
}


async def test_plan_edit_idle_chat_persists(tmp_path, monkeypatch) -> None:
    async with plan_client(tmp_path, monkeypatch) as (http, app):
        sid, cid = await _seed_chat(app, plan=SOP_PLAN)
        chat = await app.state.service.chats.get(cid)
        chat.mark_status("completed")
        await app.state.service.chats.save(chat)

        response = await http.post(
            f"/api/v1/sessions/{sid}/chats/{cid}/plan/edit", json=EDIT_BODY
        )
        assert response.status_code == 200, response.text
        tasks = response.json()["plan"]["tasks"]
        assert [t["id"] for t in tasks] == ["n1", "n2"]
        assert tasks[1]["blocked_by"] == ["n1"]

        stored = await app.state.service.chats.get(cid)
        assert stored.plan["name"] == "月度分析"  # meta carried over
        assert [n["node_id"] for n in stored.plan["nodes"]] == ["n1", "n2"]

        # Clearing the plan on an idle chat is allowed.
        cleared = await http.post(
            f"/api/v1/sessions/{sid}/chats/{cid}/plan/edit",
            json={"plan": None},
        )
        assert cleared.status_code == 200
        assert cleared.json()["plan"] is None
        assert (await app.state.service.chats.get(cid)).plan is None


async def test_plan_edit_live_chat_routes_to_runtime(
    tmp_path, monkeypatch
) -> None:
    async with plan_client(tmp_path, monkeypatch) as (http, app):
        sid, cid = await _seed_chat(app)

        # Running chat without a live runtime → 409.
        orphan = await http.post(
            f"/api/v1/sessions/{sid}/chats/{cid}/plan/edit", json=EDIT_BODY
        )
        assert orphan.status_code == 409

        runtime = FakePlanRuntime()
        get_runtime_registry().register(cid, runtime)  # type: ignore[arg-type]
        try:
            ok = await http.post(
                f"/api/v1/sessions/{sid}/chats/{cid}/plan/edit", json=EDIT_BODY
            )
            assert ok.status_code == 200, ok.text
            sop, reason = runtime.replacements[0]
            assert reason == "去掉冗余步骤"
            assert [n["node_id"] for n in sop["nodes"]] == ["n1", "n2"]

            # A running chat cannot have its plan cleared.
            cleared = await http.post(
                f"/api/v1/sessions/{sid}/chats/{cid}/plan/edit",
                json={"plan": None},
            )
            assert cleared.status_code == 400
        finally:
            get_runtime_registry().unregister(cid)


async def test_plan_edit_missing_chat(tmp_path, monkeypatch) -> None:
    async with plan_client(tmp_path, monkeypatch) as (http, _app):
        response = await http.post(
            "/api/v1/sessions/s/chats/c/plan/edit", json={"plan": None}
        )
        assert response.status_code == 404


async def test_chat_schema_projects_plan(tmp_path, monkeypatch) -> None:
    async with plan_client(tmp_path, monkeypatch) as (http, app):
        sid, cid = await _seed_chat(app, plan=SOP_PLAN)
        listed = await http.get(f"/api/v1/sessions/{sid}/chats")
        assert listed.status_code == 200
        chat = listed.json()["items"][0]
        assert chat["plan"]["tasks"][0]["subject"] == "取数"
        assert chat["plan"]["tasks"][0]["state"] == "completed"
