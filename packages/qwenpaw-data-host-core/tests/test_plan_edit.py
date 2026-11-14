"""Native TaskContext projection and SQL-command-driven plan editing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agentscope.state import AgentState

from qwenpaw_data.host.core.agent.middleware import SteerMiddleware
from qwenpaw_data.host.core.api.mappers import plan_to_schema
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.domain.chat_command import ChatCommand
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories import (
    ChatCommandRepository,
    ChatRepository,
    SessionRepository,
)
from qwenpaw_data.host.core.utils.plan import to_task_context

from .conftest import TEST_IDENTITY
from . import test_runtime_settings

service = test_runtime_settings.service

TASK_PLAN = {
    "tasks": [
        {
            "id": "n1",
            "subject": "取数",
            "description": "拉取订单明细",
            "state": "completed",
            "created_at": "2026-11-14T00:00:00",
        },
        {
            "id": "n2",
            "subject": "归因",
            "description": "按区域拆解",
            "state": "in_progress",
            "created_at": "2026-11-14T00:00:00",
            "blocked_by": ["n1"],
            "metadata": {"expected_outcome": "归因结论"},
        },
    ],
}
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


def test_task_context_projection_preserves_state_metadata_and_dependencies() -> None:
    context = to_task_context(TASK_PLAN)
    schema = plan_to_schema(context)
    tasks = schema.model_dump(mode="json")["tasks"]
    assert [task["id"] for task in tasks] == ["n1", "n2"]
    assert tasks[0]["state"] == "completed"
    assert tasks[0]["blocks"] == ["n2"]
    assert tasks[1]["state"] == "in_progress"
    assert tasks[1]["blocked_by"] == ["n1"]
    assert tasks[1]["metadata"]["expected_outcome"] == "归因结论"
    assert to_task_context(schema.model_dump(mode="json")) == context


@pytest.mark.parametrize("plan", [None, {}, {"tasks": []}])
def test_empty_task_context_projects_to_no_plan(plan) -> None:
    assert plan_to_schema(to_task_context(plan)) is None


async def _seed_chat(*, active=False):
    async with get_session_factory()() as db:
        session = Session.create(identity=TEST_IDENTITY, title="plan edits")
        sessions = SessionRepository(db, TEST_IDENTITY)
        await sessions.add(session)
        chat = session.open_chat(text="hi", datasource_id=None, has_active_chat=False)
        chat.plan = to_task_context(TASK_PLAN)
        if not active:
            chat.mark_status("completed")
        await sessions.save(session)
        await ChatRepository(db, TEST_IDENTITY).add(chat)
        await db.commit()
        return session.id, chat.id


async def test_plan_edit_idle_chat_persists_and_clears(service) -> None:
    sid, cid = await _seed_chat()
    path = f"/api/sessions/{sid}/chats/{cid}/plan/edit"
    response = await service.post(path, json=EDIT_BODY)
    assert response.status_code == 200, response.text
    tasks = response.json()["plan"]["tasks"]
    assert [task["id"] for task in tasks] == ["n1", "n2"]
    assert tasks[1]["blocked_by"] == ["n1"]
    async with get_session_factory()() as db:
        stored = await ChatRepository(db, TEST_IDENTITY).get(cid)
        assert stored.plan == to_task_context(response.json()["plan"])
        assert await ChatCommandRepository(db).take_plans(cid) == []

    snapshot = await service.get(f"/api/sessions/{sid}/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["chats"][0]["plan"] == response.json()["plan"]

    cleared = await service.post(path, json={"plan": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"plan": None}
    async with get_session_factory()() as db:
        assert not (await ChatRepository(db, TEST_IDENTITY).get(cid)).plan.tasks


async def test_running_plan_edits_are_durable_commands_without_local_runtime(
    service,
) -> None:
    sid, cid = await _seed_chat(active=True)
    path = f"/api/sessions/{sid}/chats/{cid}/plan/edit"
    changed = await service.post(path, json=EDIT_BODY)
    assert changed.status_code == 200, changed.text
    cleared = await service.post(path, json={"plan": None, "reason": "重新规划"})
    assert cleared.status_code == 200, cleared.text

    async with get_session_factory()() as db:
        stored = await ChatRepository(db, TEST_IDENTITY).get(cid)
        assert stored.plan == to_task_context(TASK_PLAN)
        commands = await ChatCommandRepository(db).take_plans(cid)
        assert [command["reason"] for command in commands] == [
            "去掉冗余步骤",
            "重新规划",
        ]
        assert to_task_context(commands[0]["plan"]) == to_task_context(
            changed.json()["plan"]
        )
        assert commands[1]["plan"] is None
        await db.commit()

    # Consumption is persistent; another executor connection cannot replay it.
    async with get_session_factory()() as db:
        assert await ChatCommandRepository(db).take_plans(cid) == []


async def test_plan_edit_wrong_session_and_missing_chat_do_not_mutate(service) -> None:
    sid, cid = await _seed_chat(active=True)
    other_sid, _ = await _seed_chat()
    for session_id, chat_id in ((other_sid, cid), (sid, "missing")):
        response = await service.post(
            f"/api/sessions/{session_id}/chats/{chat_id}/plan/edit",
            json=EDIT_BODY,
        )
        assert response.status_code == 404, response.text
    async with get_session_factory()() as db:
        assert (
            await ChatRepository(db, TEST_IDENTITY).get(cid)
        ).plan == to_task_context(TASK_PLAN)
        assert await ChatCommandRepository(db).take_plans(cid) == []


async def test_invalid_plan_does_not_enqueue_or_overwrite(service) -> None:
    sid, cid = await _seed_chat(active=True)
    response = await service.post(
        f"/api/sessions/{sid}/chats/{cid}/plan/edit",
        json={
            "plan": {
                "tasks": [
                    {
                        "id": "bad",
                        "subject": "bad",
                        "description": "bad",
                        "blocked_by": ["missing"],
                    }
                ]
            }
        },
    )
    assert response.status_code == 400, response.text
    async with get_session_factory()() as db:
        assert await ChatCommandRepository(db).take_plans(cid) == []
        assert (
            await ChatRepository(db, TEST_IDENTITY).get(cid)
        ).plan == to_task_context(TASK_PLAN)


async def test_queued_steer_comments_reach_hint_and_model_context(service) -> None:
    _, cid = await _seed_chat(active=True)
    comments = [{"path": "out/a.md", "line_start": 2, "line_end": 3, "comment": "补充"}]
    factory = get_session_factory()
    async with factory() as db:
        chat = await ChatRepository(db, TEST_IDENTITY).get(cid)
        await ChatCommandRepository(db).add(
            ChatCommand.for_steer(chat, "改一下", comments)
        )
        await db.commit()

    middleware = SteerMiddleware(SimpleNamespace(session_factory=factory, chat_id=cid))
    state = AgentState()
    state.reply_id = "reply-1"
    agent = SimpleNamespace(name="qwenpaw-data", state=state)

    async def next_handler(**kwargs):
        yield "sentinel"

    events = [event async for event in middleware.on_reasoning(agent, {}, next_handler)]
    assert events[0].hint == "改一下"
    assert events[0].metadata["artifact_comments"] == comments
    composed = state.context[0].content[0].hint
    assert "out/a.md" in composed and composed.endswith("改一下")
    assert events[-1] == "sentinel"
    assert [
        event async for event in middleware.on_reasoning(agent, {}, next_handler)
    ] == ["sentinel"]
