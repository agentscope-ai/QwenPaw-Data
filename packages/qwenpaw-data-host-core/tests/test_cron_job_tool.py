# -*- coding: utf-8 -*-
"""Unit tests for the agent-callable ``cron_job`` tool."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from agentscope.message import ToolResultState

from qwenpaw_data.host.core.agent.toolkit import build_qwenpaw_data_toolkit
from qwenpaw_data.host.core.agent.tools import (
    CRON_JOB_TOOL_NAME,
    CronJobTool,
    CronToolServices,
)
from qwenpaw_data.host.core.orchestration import RuntimeStateManager


class FakeCronStore:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, ...]] = []
        self._next = 0

    async def list(self, user_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list", user_id))
        return [j for j in self.jobs.values() if j["user_id"] == user_id]

    async def get(self, user_id: str, job_id: str) -> dict[str, Any]:
        self.calls.append(("get", user_id, job_id))
        job = self.jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
            raise KeyError(job_id)
        return job

    async def create(self, user_id: str, body: Any) -> dict[str, Any]:
        self.calls.append(("create", user_id))
        self._next += 1
        job_id = f"job-{self._next}"
        job = {"id": job_id, "user_id": user_id, **body.model_dump(mode="json")}
        self.jobs[job_id] = job
        return job

    async def replace(self, user_id: str, job_id: str, body: Any) -> dict[str, Any]:
        self.calls.append(("replace", user_id, job_id))
        await self.get(user_id, job_id)
        job = {"id": job_id, "user_id": user_id, **body.model_dump(mode="json")}
        self.jobs[job_id] = job
        return job

    async def set_enabled(
        self,
        user_id: str,
        job_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        self.calls.append(("set_enabled", user_id, job_id, str(enabled)))
        job = await self.get(user_id, job_id)
        job["enabled"] = enabled
        return job

    async def delete(self, user_id: str, job_id: str) -> None:
        self.calls.append(("delete", user_id, job_id))
        await self.get(user_id, job_id)
        del self.jobs[job_id]


class FakeCronManager:
    def __init__(self) -> None:
        self.synced: list[str] = []
        self.removed: list[str] = []
        self.ran = asyncio.Event()
        self.ran_job: dict[str, Any] | None = None

    def sync(self, job: dict[str, Any]) -> None:
        self.synced.append(job["id"])

    def remove(self, job_id: str) -> None:
        self.removed.append(job_id)

    async def run(self, job: dict[str, Any]) -> None:
        self.ran_job = job
        self.ran.set()


class FakeSessions:
    def __init__(self, known: set[str] | None = None) -> None:
        self.known = known or set()

    async def get(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.known:
            raise KeyError(session_id)
        return {"id": session_id}


def build_tool(
    *,
    context: dict[str, Any] | None = None,
    sessions: FakeSessions | None = None,
) -> tuple[CronJobTool, FakeCronStore, FakeCronManager]:
    cron = FakeCronStore()
    manager = FakeCronManager()
    services = CronToolServices(
        cron=cron,
        cron_manager=manager,
        sessions=sessions or FakeSessions(),
    )
    tool = CronJobTool(
        lambda: services,
        request_context_getter=lambda: dict(
            context if context is not None else {"user_id": "u1", "datasource_id": "ds1"},
        ),
    )
    return tool, cron, manager


def payload(chunk: Any) -> dict[str, Any]:
    return json.loads(chunk.content[0].text)


def error_text(chunk: Any) -> str:
    return chunk.content[0].text


CRON_ARGS = {
    "name": "每日晨报",
    "message": "生成销售日报",
    "schedule_type": "cron",
    "cron_expression": "0 8 * * *",
}


async def test_create_writes_store_and_scheduler() -> None:
    tool, cron, manager = build_tool()

    chunk = await tool.call(action="create", **CRON_ARGS)

    job = payload(chunk)["job"]
    assert job["id"] in cron.jobs
    assert manager.synced == [job["id"]]
    # Unset datasource falls back to the current chat's datasource.
    assert job["datasource_id"] == "ds1"


async def test_explicit_datasource_wins_over_context() -> None:
    tool, _cron, _manager = build_tool()

    chunk = await tool.call(action="create", datasource_id="ds2", **CRON_ARGS)

    assert payload(chunk)["job"]["datasource_id"] == "ds2"


async def test_list_and_get_are_scoped_to_the_caller() -> None:
    tool, cron, _manager = build_tool()
    created = payload(await tool.call(action="create", **CRON_ARGS))["job"]

    listed = payload(await tool.call(action="list"))
    assert listed["count"] == 1
    assert listed["jobs"][0]["id"] == created["id"]
    assert all(call[1] == "u1" for call in cron.calls)


async def test_pause_and_resume_flip_enabled() -> None:
    tool, _cron, manager = build_tool()
    job_id = payload(await tool.call(action="create", **CRON_ARGS))["job"]["id"]

    paused = payload(await tool.call(action="pause", job_id=job_id))["job"]
    assert paused["enabled"] is False
    resumed = payload(await tool.call(action="resume", job_id=job_id))["job"]
    assert resumed["enabled"] is True
    assert manager.synced == [job_id, job_id, job_id]


async def test_replace_overwrites_the_whole_job() -> None:
    tool, _cron, _manager = build_tool()
    job_id = payload(await tool.call(action="create", **CRON_ARGS))["job"]["id"]

    chunk = await tool.call(
        action="replace",
        job_id=job_id,
        name="周报",
        message="生成周报",
        schedule_type="cron",
        cron_expression="0 9 * * 1",
    )

    job = payload(chunk)["job"]
    assert job["name"] == "周报"
    assert job["schedule"]["cron"] == "0 9 * * mon"


async def test_delete_removes_from_store_and_scheduler() -> None:
    tool, cron, manager = build_tool()
    job_id = payload(await tool.call(action="create", **CRON_ARGS))["job"]["id"]

    chunk = await tool.call(action="delete", job_id=job_id)

    assert payload(chunk)["deleted"] == job_id
    assert cron.jobs == {}
    assert manager.removed == [job_id]


async def test_run_fires_in_the_background_without_blocking() -> None:
    tool, _cron, manager = build_tool()
    job_id = payload(await tool.call(action="create", **CRON_ARGS))["job"]["id"]

    chunk = await tool.call(action="run", job_id=job_id)

    assert payload(chunk)["ok"] is True
    # The tool returns before the fired run finishes.
    await asyncio.wait_for(manager.ran.wait(), timeout=1)
    assert manager.ran_job is not None
    assert manager.ran_job["id"] == job_id


async def test_missing_identity_is_refused() -> None:
    tool, cron, _manager = build_tool(context={"datasource_id": "ds1"})

    chunk = await tool.call(action="list")

    assert chunk.state == ToolResultState.ERROR
    assert "identity" in error_text(chunk)
    assert cron.calls == []


async def test_pinned_session_must_exist() -> None:
    tool, cron, manager = build_tool(sessions=FakeSessions({"s-known"}))

    ok = await tool.call(action="create", session_id="s-known", **CRON_ARGS)
    assert ok.state != ToolResultState.ERROR

    missing = await tool.call(action="create", session_id="s-gone", **CRON_ARGS)
    assert missing.state == ToolResultState.ERROR
    # The rejected job was never written or scheduled.
    assert len(cron.jobs) == 1
    assert len(manager.synced) == 1


@pytest.mark.parametrize(
    "action",
    ["get", "replace", "delete", "pause", "resume", "run"],
)
async def test_actions_requiring_a_job_id_say_so(action: str) -> None:
    tool, _cron, _manager = build_tool()

    chunk = await tool.call(action=action)

    assert chunk.state == ToolResultState.ERROR
    assert "job_id" in error_text(chunk)


async def test_unknown_parameters_are_rejected() -> None:
    tool, _cron, _manager = build_tool()

    chunk = await tool.call(action="list", tenant_id="t1")

    assert chunk.state == ToolResultState.ERROR


async def test_store_failures_surface_as_tool_errors() -> None:
    tool, _cron, _manager = build_tool()

    chunk = await tool.call(action="get", job_id="nope")

    assert chunk.state == ToolResultState.ERROR
    assert error_text(chunk).startswith("CronJobError:")


async def test_once_schedule_requires_run_at() -> None:
    tool, _cron, _manager = build_tool()

    chunk = await tool.call(
        action="create",
        name="一次性",
        message="跑一次",
        schedule_type="once",
    )

    assert chunk.state == ToolResultState.ERROR
    assert "run_at" in error_text(chunk)


def test_schema_advertises_every_action() -> None:
    tool, _cron, _manager = build_tool()

    actions = tool.input_schema["properties"]["action"]["enum"]

    assert set(actions) == {
        "list",
        "get",
        "create",
        "replace",
        "delete",
        "pause",
        "resume",
        "run",
    }
    assert tool.name == CRON_JOB_TOOL_NAME
    assert tool.is_read_only is False


class FakeWorkspace:
    async def list_tools(self) -> list:
        return []

    async def list_mcps(self) -> list:
        return []

    async def list_skills(self) -> list:
        return []


async def toolkit_tool_names(*, with_cron: bool, mode: str) -> set[str]:
    services = CronToolServices(
        cron=FakeCronStore(),
        cron_manager=FakeCronManager(),
        sessions=FakeSessions(),
    )
    toolkit = await build_qwenpaw_data_toolkit(
        RuntimeStateManager(),
        workspace=FakeWorkspace(),
        cron_services_factory=(lambda: services) if with_cron else None,
    )
    toolkit.set_qwenpaw_data_mode(mode)
    return {
        schema["function"]["name"]
        for schema in await toolkit.get_tool_schemas([mode])
    }


async def test_registered_for_agent_mode_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduling is an execution side effect, not part of drafting a plan."""
    monkeypatch.delenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", raising=False)

    assert CRON_JOB_TOOL_NAME in await toolkit_tool_names(
        with_cron=True,
        mode="agent",
    )
    assert CRON_JOB_TOOL_NAME not in await toolkit_tool_names(
        with_cron=True,
        mode="plan",
    )


@pytest.mark.parametrize("mode", ["plan", "agent"])
async def test_absent_without_cron_collaborators(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI-style hosts have no cron manager, so the tool is not offered."""
    monkeypatch.delenv("QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED", raising=False)

    names = await toolkit_tool_names(with_cron=False, mode=mode)

    assert CRON_JOB_TOOL_NAME not in names
