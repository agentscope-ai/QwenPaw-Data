# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402
from qwenpaw_data.host.core.api.models.cron import ScheduleSpec  # noqa: E402
from qwenpaw_data.host.core.core import QwenPawDataHost  # noqa: E402

from test_service_smoke import ScriptedAgent, _script  # noqa: E402


def test_schedule_spec_validation() -> None:
    spec = ScheduleSpec.model_validate({"type": "cron", "cron": "0 8 * * 1,5"})
    assert spec.cron == "0 8 * * mon,fri"
    assert spec.run_at is None

    with pytest.raises(ValueError, match="5 fields"):
        ScheduleSpec.model_validate({"type": "cron", "cron": "0 8 *"})
    with pytest.raises(ValueError, match="run_at"):
        ScheduleSpec.model_validate({"type": "once"})

    once = ScheduleSpec.model_validate(
        {"type": "once", "run_at": "2026-12-01T08:00:00+08:00"}
    )
    assert once.cron is None


@asynccontextmanager
async def service_client(tmp_path: Path, monkeypatch, agent=None):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    if agent is not None:

        async def fake_get_agent(self, *, mode: str, request_context=None):
            return agent

        monkeypatch.setattr(QwenPawDataHost, "get_agent", fake_get_agent)
    app = create_app(home=tmp_path, model=object())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            yield http, app


JOB_BODY = {
    "name": "每日晨报",
    "message": "生成销售日报",
    "datasource_id": "ds1",
    "schedule": {"type": "cron", "cron": "0 8 * * *"},
}


async def test_cron_job_crud_and_scheduler_sync(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, app):
        created = await http.post("/api/v1/cron/jobs", json=JOB_BODY)
        assert created.status_code == 200
        job = created.json()["job"]
        job_id = job["id"]

        scheduler = app.state.service.cron_manager._scheduler
        assert scheduler.get_job(job_id) is not None
        assert scheduler.get_job(job_id).next_run_time is not None

        paused = await http.post(f"/api/v1/cron/jobs/{job_id}/pause")
        assert paused.json()["job"]["enabled"] is False
        assert scheduler.get_job(job_id).next_run_time is None

        resumed = await http.post(f"/api/v1/cron/jobs/{job_id}/resume")
        assert resumed.json()["job"]["enabled"] is True

        listing = await http.get("/api/v1/cron/jobs")
        assert listing.json()["count"] == 1

        # unknown session rejected
        bad = await http.post(
            "/api/v1/cron/jobs", json={**JOB_BODY, "session_id": "ses_missing"}
        )
        assert bad.status_code == 404

        deleted = await http.delete(f"/api/v1/cron/jobs/{job_id}")
        assert deleted.json()["ok"] is True
        assert scheduler.get_job(job_id) is None
        assert (await http.get(f"/api/v1/cron/jobs/{job_id}")).status_code == 404


async def test_cron_run_opens_console_chat(tmp_path, monkeypatch) -> None:
    agent = ScriptedAgent(_script())
    async with service_client(tmp_path, monkeypatch, agent=agent) as (http, app):
        job_id = (
            await http.post("/api/v1/cron/jobs", json=JOB_BODY)
        ).json()["job"]["id"]

        fired = await http.post(f"/api/v1/cron/jobs/{job_id}/run")
        assert fired.status_code == 200

        state = app.state.service
        for _ in range(200):
            sessions, total = await state.sessions.list()
            if total:
                chats = await state.chats.list_for_session(sessions[0][0].id)
                if chats and chats[0].status == "completed":
                    break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("cron run did not complete a chat")

        session = sessions[0][0]
        assert session.title == "每日晨报"
        assert session.datasource_id == "ds1"
        chat = chats[0]
        assert chat.user_input == "生成销售日报"
        events = await state.events.read_after(chat.id, -1)
        assert events[-1].object == "response"
        assert events[-1].status == "completed"


async def test_cron_jobs_restore_on_restart(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, _app):
        job_id = (
            await http.post("/api/v1/cron/jobs", json=JOB_BODY)
        ).json()["job"]["id"]

    # New service instance over the same home: job re-registers.
    async with service_client(tmp_path, monkeypatch) as (http, app):
        assert app.state.service.cron_manager._scheduler.get_job(job_id) is not None
        assert (await http.get("/api/v1/cron/jobs")).json()["count"] == 1
