# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import reset_engine
from qwenpaw_data.host.core.stream.hub import reset_hub


@pytest.fixture
def cron_client():
    import asyncio

    asyncio.run(reset_engine())
    reset_hub()
    app = create_app()
    with TestClient(app) as client:
        yield client


def _schedule(cron: str = "0 9 * * *") -> dict:
    return {"type": "cron", "cron": cron, "timezone": "Asia/Shanghai"}


def test_create_list_pause_resume_delete_job(cron_client: TestClient) -> None:
    created = cron_client.post(
        "/api/cron/jobs",
        json={
            "name": "日报",
            "enabled": True,
            "message": "生成本日简报",
            "datasource_id": "ds_demo",
            "schedule": {
                "type": "cron",
                "cron": "30 9 * * 1-5",
                "timezone": "Asia/Shanghai",
            },
        },
    )
    assert created.status_code == 200, created.text
    job = created.json()["job"]
    assert job["name"] == "日报"
    assert job["datasource_id"] == "ds_demo"
    assert "task_type" not in job
    # channel-targeting fields (this branch): a job with no channel/IM target
    # defaults to the console channel and carries no target / bound session.
    assert job["channel"] == "console"
    assert job["target_external_key"] is None
    assert job["session_id"] is None
    job_id = job["id"]

    listed = cron_client.get("/api/cron/jobs")
    assert listed.status_code == 200
    assert any(item["id"] == job_id for item in listed.json()["jobs"])

    paused = cron_client.post(f"/api/cron/jobs/{job_id}/pause")
    assert paused.status_code == 200
    assert paused.json()["job"]["enabled"] is False

    resumed = cron_client.post(f"/api/cron/jobs/{job_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["job"]["enabled"] is True

    deleted = cron_client.delete(f"/api/cron/jobs/{job_id}")
    assert deleted.status_code == 200
    listed = cron_client.get("/api/cron/jobs")
    assert all(item["id"] != job_id for item in listed.json()["jobs"])


def test_get_and_replace_job(cron_client: TestClient) -> None:
    created = cron_client.post(
        "/api/cron/jobs",
        json={
            "name": "周报",
            "message": "生成本周简报",
            "datasource_id": "ds_demo",
            "schedule": _schedule("0 10 * * 1"),
        },
    )
    assert created.status_code == 200, created.text
    job = created.json()["job"]
    job_id = job["id"]

    got = cron_client.get(f"/api/cron/jobs/{job_id}")
    assert got.status_code == 200, got.text
    assert got.json()["job"]["id"] == job_id

    replaced = cron_client.put(
        f"/api/cron/jobs/{job_id}",
        json={
            "name": "双周报",
            "enabled": False,
            "message": "生成双周简报",
            "datasource_id": "ds_demo2",
            "channel": "console",
            "schedule": _schedule("0 10 * * 2"),
        },
    )
    assert replaced.status_code == 200, replaced.text
    new = replaced.json()["job"]
    # replace overwrites the row in place: identity-scoped fields and id are kept.
    assert new["id"] == job_id
    assert new["name"] == "双周报"
    assert new["datasource_id"] == "ds_demo2"
    assert new["enabled"] is False
    # ScheduleSpec normalizes a numeric day-of-week to its alpha form (2 -> tue).
    assert new["schedule"]["cron"] == "0 10 * * tue"
    assert new["channel"] == "console"


def test_list_targets_empty(cron_client: TestClient) -> None:
    """``GET /cron/targets`` lists groups/people the bot has spoken to.

    Empty until a channel binding is seeded; never leaks ``send_meta``.
    """
    listed = cron_client.get("/api/cron/targets", params={"channel": "wecom"})
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["count"] == 0
    assert body["targets"] == []


def test_im_channel_job_requires_valid_session_id(cron_client: TestClient) -> None:
    """A non-console (IM) channel job is rejected on invalid session id.

    It raises ``ValueError`` -> HTTP 400 (code ``VALIDATION``).
    """
    created = cron_client.post(
        "/api/cron/jobs",
        json={
            "name": "IM 定时",
            "message": "推送一条",
            "datasource_id": "ds_demo",
            "channel": "wecom",
            "schedule": _schedule(),
            "session_id": "asdf",
        },
    )

    assert created.status_code == 400, created.text
    assert created.json().get("code") == "VALIDATION"


def test_agent_job_requires_datasource(cron_client: TestClient) -> None:
    created = cron_client.post(
        "/api/cron/jobs",
        json={
            "name": "无数据源",
            "enabled": True,
            "message": "分析一下",
            "schedule": {
                "type": "cron",
                "cron": "0 9 * * *",
                "timezone": "Asia/Shanghai",
            },
        },
    )
    assert created.status_code == 422, created.text
