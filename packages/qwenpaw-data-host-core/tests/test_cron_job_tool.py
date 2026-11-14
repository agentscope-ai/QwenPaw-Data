"""Cron tool contracts on the service SQL scheduler boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import ToolResultState

from qwenpaw_data.host.core.agent.tools import CronJobTool
from qwenpaw_data.host.core.db.tables import CronJobRow
from qwenpaw_data.host.core.repositories.cron import CronJobRepository
from qwenpaw_data.host.core.runtime.builder.data_agent import DataAgentBuilder
from qwenpaw_data.host.core.utils.time import utcnow
from . import test_runtime_settings
from .test_cron_tool import _run_context
from .conftest import TEST_IDENTITY

database = test_runtime_settings.database

CRON_ARGS = {
    "name": "每日晨报",
    "message": "生成销售日报",
    "schedule_type": "cron",
    "cron_expression": "0 8 * * *",
}


def payload(chunk):
    assert chunk.state != ToolResultState.ERROR, chunk.content[0].text
    return json.loads(chunk.content[0].text)


async def test_create_persists_and_explicit_datasource_wins(database):
    tool = CronJobTool(_run_context())
    job = payload(await tool.call(action="create", datasource_id="ds2", **CRON_ARGS))[
        "job"
    ]
    async with database() as db:
        saved = await CronJobRepository(db, TEST_IDENTITY).get(job["id"])
        assert saved["datasource_id"] == "ds2"
        assert saved["source_chat_id"] == "chat-a"
        assert saved["session_id"] is None  # Job owns its session on first execution.
        assert saved["session_policy"] == "reuse"
        row = await db.get(CronJobRow, job["id"])
        assert row.next_run_at is not None and row.running_at is None


@pytest.mark.parametrize("field", ["user_id", "tenant_id", "workspace_id"])
async def test_every_operation_is_scoped_to_trusted_run_identity(database, field):
    owner = CronJobTool(_run_context())
    job = payload(await owner.call(action="create", **CRON_ARGS))["job"]
    other = CronJobTool(
        _run_context(identity=replace(TEST_IDENTITY, **{field: "other"}))
    )
    assert payload(await other.call(action="list")) == {"jobs": [], "count": 0}
    for action in ("get", "replace", "pause", "resume", "run", "delete"):
        kwargs = CRON_ARGS if action == "replace" else {}
        result = await other.call(action=action, job_id=job["id"], **kwargs)
        assert result.state == ToolResultState.ERROR
        assert "not found" in result.content[0].text
    assert payload(await owner.call(action="get", job_id=job["id"]))["job"] == job


async def test_pause_resume_replace_delete_persist_without_inprocess_scheduler(
    database,
):
    tool = CronJobTool(_run_context())
    job_id = payload(await tool.call(action="create", **CRON_ARGS))["job"]["id"]
    paused = payload(await tool.call(action="pause", job_id=job_id))["job"]
    assert paused["enabled"] is False
    async with database() as db:
        assert (await db.get(CronJobRow, job_id)).next_run_at is None
    resumed = payload(await tool.call(action="resume", job_id=job_id))["job"]
    assert resumed["enabled"] is True
    async with database() as db:
        assert (await db.get(CronJobRow, job_id)).next_run_at > utcnow()

    changed = payload(
        await tool.call(
            action="replace",
            job_id=job_id,
            **{**CRON_ARGS, "name": "周报", "cron_expression": "0 9 * * 1"},
        )
    )["job"]
    assert changed["name"] == "周报"
    assert changed["schedule"]["cron"] == "0 9 * * mon"
    assert payload(await tool.call(action="get", job_id=job_id))["job"] == changed
    assert payload(await tool.call(action="delete", job_id=job_id))["deleted"] == job_id
    assert payload(await tool.call(action="list"))["count"] == 0


async def test_run_marks_durable_work_for_scheduler_claim(database):
    tool = CronJobTool(_run_context())
    job_id = payload(await tool.call(action="create", **CRON_ARGS))["job"]["id"]
    before = utcnow()
    assert payload(await tool.call(action="run", job_id=job_id))["ok"] is True
    async with database() as db:
        row = await db.get(CronJobRow, job_id)
        assert before <= row.next_run_at <= utcnow()
        assert row.running_at is None
        assert [j["id"] for j in await CronJobRepository.list_ready_to_run(db)] == [
            job_id
        ]
        assert await CronJobRepository.try_start(db, job_id) is not None
        await db.commit()
    # Another scheduler transaction must not claim the already running job.
    async with database() as db:
        assert await CronJobRepository.try_start(db, job_id) is None


@pytest.mark.parametrize(
    "action", ["get", "replace", "delete", "pause", "resume", "run"]
)
async def test_job_id_required(database, action):
    chunk = await CronJobTool(_run_context()).call(action=action)
    assert chunk.state == ToolResultState.ERROR
    assert "job_id" in chunk.content[0].text


@pytest.mark.parametrize(
    "extra",
    [
        {"tenant_id": "forged"},
        {"user_id": "forged"},
        {"workspace_id": "forged"},
        {"session_id": "pinned-legacy-session"},
    ],
)
async def test_tool_cannot_override_trusted_identity_or_pin_foreign_session(
    database, extra
):
    tool = CronJobTool(_run_context())
    chunk = await tool.call(action="create", **CRON_ARGS, **extra)
    assert chunk.state == ToolResultState.ERROR
    assert payload(await tool.call(action="list"))["count"] == 0


async def test_store_failures_surface_as_tool_errors(database):
    chunk = await CronJobTool(_run_context()).call(action="get", job_id="missing")
    assert chunk.state == ToolResultState.ERROR
    assert chunk.content[0].text.startswith("CronJobError:")


async def test_once_requires_run_at_and_does_not_write(database):
    tool = CronJobTool(_run_context())
    chunk = await tool.call(
        action="create", name="一次性", message="跑一次", schedule_type="once"
    )
    assert chunk.state == ToolResultState.ERROR
    assert "run_at" in chunk.content[0].text
    assert payload(await tool.call(action="list"))["count"] == 0


async def test_schema_and_current_service_builder_registration(database):
    context = _run_context()
    context.workspace = SimpleNamespace(
        list_tools=AsyncMock(return_value=[]),
        list_mcps=AsyncMock(return_value=[]),
        list_skills=AsyncMock(return_value=[]),
    )
    context.user_runtime_config = SimpleNamespace(
        tool_timeout_enabled=True,
        tool_timeout_seconds=10,
    )
    toolkit = await DataAgentBuilder().build_toolkit(run_context=context)
    schemas = await toolkit.get_tool_schemas()
    cron = next(s["function"] for s in schemas if s["function"]["name"] == "CronJob")
    assert set(cron["parameters"]["properties"]["action"]["enum"]) == {
        "list",
        "list_targets",
        "get",
        "create",
        "replace",
        "delete",
        "pause",
        "resume",
        "run",
    }
    tool = CronJobTool(context)
    assert tool.is_read_only is False and tool.is_concurrency_safe is False
