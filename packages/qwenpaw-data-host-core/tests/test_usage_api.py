# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    reset_engine,
)
from qwenpaw_data.host.core.db.tables import ModelUsageRow, SessionRow
from qwenpaw_data.host.core.domain.usage_window import background_group_id
from qwenpaw_data.host.core.stream.hub import reset_hub
from qwenpaw_data.host.core.utils.time import utcnow

AS_OF = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)
AS_OF_Q = "2026-09-17T05:00:00Z"
PARAMS = {"period": "7d", "timezone": "Asia/Shanghai", "as_of": AS_OF_Q}


@pytest.fixture
def usage_client():
    asyncio.run(reset_engine())
    reset_hub()
    app = create_app()
    with TestClient(app) as client:
        yield client


def _usage(**kwargs) -> ModelUsageRow:
    payload = {
        "id": kwargs.pop("id"),
        "user_id": TEST_USER_ID,
        "tenant_id": TEST_TENANT_ID,
        "workspace_id": TEST_WORKSPACE_ID,
        "session_id": "",
        "chat_id": "",
        "source": "data-agent",
        "source_step": "step",
        "model_type": "chat",
        "provider_id": "dashscope",
        "model_id": "qwen-max",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "duration_ms": 10,
        "created_at": AS_OF - timedelta(hours=2),
    }
    payload.update(kwargs)
    return ModelUsageRow(**payload)


def _seed() -> None:
    async def _run() -> None:
        now = utcnow()
        async with get_session_factory()() as db:
            db.add_all(
                [
                    SessionRow(
                        id="ses-active",
                        user_id=TEST_USER_ID,
                        tenant_id=TEST_TENANT_ID,
                        workspace_id=TEST_WORKSPACE_ID,
                        title="季度经营分析",
                        created_at=now,
                        updated_at=now,
                    ),
                    SessionRow(
                        id="ses-deleted",
                        user_id=TEST_USER_ID,
                        tenant_id=TEST_TENANT_ID,
                        workspace_id=TEST_WORKSPACE_ID,
                        title="",
                        created_at=now,
                        updated_at=now,
                        deleted_at=now,
                    ),
                    _usage(
                        id="u-s1-chat",
                        session_id="ses-active",
                        chat_id="chat-1",
                        input_tokens=100,
                        output_tokens=40,
                        cache_read_tokens=999,
                        cache_creation_tokens=50,
                        created_at=datetime(2026, 9, 14, 10, 42, tzinfo=timezone.utc),
                    ),
                    _usage(
                        id="u-s1-embed",
                        session_id="ses-active",
                        chat_id="chat-1",
                        model_type="embedding",
                        model_id="text-embedding-v3",
                        input_tokens=20,
                        created_at=datetime(2026, 9, 14, 10, 40, tzinfo=timezone.utc),
                    ),
                    _usage(
                        id="u-s1-text",
                        session_id="ses-active",
                        chat_id="chat-1",
                        model_type="text",
                        model_id="qwen-plus",
                        input_tokens=10,
                        output_tokens=5,
                        created_at=datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc),
                    ),
                    _usage(
                        id="u-s2",
                        session_id="ses-deleted",
                        chat_id="chat-2",
                        input_tokens=30,
                        output_tokens=10,
                        created_at=datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc),
                    ),
                    _usage(
                        id="u-s3-missing",
                        session_id="ses-missing",
                        chat_id="chat-3",
                        input_tokens=8,
                        output_tokens=2,
                        created_at=datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc),
                    ),
                    _usage(
                        id="u-bg-index",
                        source="向量索引维护",
                        model_type="embedding",
                        model_id="text-embedding-v3",
                        input_tokens=50,
                        created_at=datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc),
                    ),
                    _usage(
                        id="u-bg-skill",
                        source="skill-create",
                        input_tokens=200,
                        output_tokens=20,
                        created_at=datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc),
                    ),
                    _usage(
                        id="u-other-tenant",
                        tenant_id="other-tenant",
                        session_id="ses-other",
                        input_tokens=10000,
                        output_tokens=10000,
                    ),
                    _usage(
                        id="u-future",
                        session_id="ses-active",
                        input_tokens=400,
                        output_tokens=400,
                        created_at=AS_OF + timedelta(minutes=1),
                    ),
                    _usage(
                        id="u-image",
                        session_id="ses-active",
                        model_type="image",
                        input_tokens=777,
                        output_tokens=777,
                    ),
                ]
            )
            await db.commit()

    asyncio.run(_run())


def _get(client: TestClient, path: str, **extra) -> dict:
    response = client.get(f"/api/usage/{path}", params={**PARAMS, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_isolation_and_reconciliation(usage_client: TestClient) -> None:
    _seed()
    summary = _get(usage_client, "summary")
    timeseries = _get(usage_client, "timeseries")
    sessions = _get(usage_client, "sessions")
    background = _get(usage_client, "background-tasks")

    assert summary["window"]["start"].startswith("2026-09-10T16:00:00")
    assert summary["generation"] == {
        "input_tokens": 348,
        "output_tokens": 77,
        "total_tokens": 425,
        "calls": 5,
    }
    assert summary["embedding"] == {"tokens": 70, "calls": 2}
    assert summary["total_tokens"] == 495
    assert summary["sessions"] == {"count": 3}
    assert summary["background_tasks"] == {"count": 2}

    assert timeseries["totals"]["generation"] == summary["generation"]
    assert timeseries["totals"]["embedding"] == summary["embedding"]
    assert timeseries["totals"]["total_tokens"] == summary["total_tokens"]
    point_gen = sum(p["generation"]["total_tokens"] for p in timeseries["points"])
    point_emb = sum(p["embedding"]["tokens"] for p in timeseries["points"])
    assert point_gen == summary["generation"]["total_tokens"]
    assert point_emb == summary["embedding"]["tokens"]

    sessions_plus_bg_gen = {
        "input_tokens": sessions["totals"]["generation"]["input_tokens"]
        + background["totals"]["generation"]["input_tokens"],
        "output_tokens": sessions["totals"]["generation"]["output_tokens"]
        + background["totals"]["generation"]["output_tokens"],
        "total_tokens": sessions["totals"]["generation"]["total_tokens"]
        + background["totals"]["generation"]["total_tokens"],
        "calls": sessions["totals"]["generation"]["calls"]
        + background["totals"]["generation"]["calls"],
    }
    sessions_plus_bg_emb = {
        "tokens": sessions["totals"]["embedding"]["tokens"]
        + background["totals"]["embedding"]["tokens"],
        "calls": sessions["totals"]["embedding"]["calls"]
        + background["totals"]["embedding"]["calls"],
    }
    assert sessions_plus_bg_gen == summary["generation"]
    assert sessions_plus_bg_emb == summary["embedding"]
    assert sessions["totals"]["sessions"] == summary["sessions"]["count"]
    assert sessions["pagination"]["total"] == 3
    assert background["totals"]["groups"] == summary["background_tasks"]["count"]
    assert background["pagination"]["total"] == 2


def test_session_titles_models_and_deleted(usage_client: TestClient) -> None:
    _seed()
    body = _get(usage_client, "sessions")
    by_id = {item["session_id"]: item for item in body["items"]}
    active = by_id["ses-active"]
    assert active["title"] == "季度经营分析"
    assert active["session_state"] == "active"
    assert {m["model_id"] for m in active["models"]} == {
        "qwen-max",
        "qwen-plus",
        "text-embedding-v3",
    }
    assert {m["model_type"] for m in active["models"]} == {"text", "embedding"}
    assert all(m["model_type"] != "chat" for m in active["models"])
    assert by_id["ses-deleted"]["session_state"] == "deleted"
    assert by_id["ses-deleted"]["title"] == "未命名 Session"
    assert by_id["ses-missing"]["session_state"] == "deleted"
    assert by_id["ses-missing"]["title"] == "未命名 Session"
    ids = [item["session_id"] for item in body["items"]]
    assert ids == ["ses-active", "ses-deleted", "ses-missing"]


def test_background_group_sort_and_pagination(usage_client: TestClient) -> None:
    _seed()
    first = _get(usage_client, "background-tasks", page=1, page_size=1)
    assert first["pagination"]["total"] == 2
    assert first["pagination"]["page_count"] == 2
    assert first["pagination"]["has_next"] is True
    assert len(first["items"]) == 1
    assert first["items"][0]["source"] == "skill-create"
    assert first["items"][0]["group_id"] == background_group_id("skill-create")
    assert first["totals"]["groups"] == 2
    assert first["totals"]["generation"]["input_tokens"] == 200
    second = _get(usage_client, "background-tasks", page=2, page_size=1)
    assert second["items"][0]["source"] == "向量索引维护"
    assert second["items"][0]["group_id"] == background_group_id("向量索引维护")
    assert "source_step" not in second["items"][0]


def test_1d_buckets_and_half_open_window(usage_client: TestClient) -> None:
    _seed()

    async def _extra() -> None:
        async with get_session_factory()() as db:
            db.add_all(
                [
                    _usage(
                        id="u-1d-morning",
                        session_id="ses-active",
                        input_tokens=3,
                        output_tokens=1,
                        created_at=datetime(2026, 9, 16, 16, 30, tzinfo=timezone.utc),
                    ),
                    _usage(
                        id="u-1d-noon",
                        session_id="ses-active",
                        input_tokens=4,
                        output_tokens=1,
                        created_at=datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc),
                    ),
                    _usage(
                        id="u-1d-as-of",
                        session_id="ses-active",
                        input_tokens=9,
                        output_tokens=9,
                        created_at=AS_OF,
                    ),
                ]
            )
            await db.commit()

    asyncio.run(_extra())
    body = _get(
        usage_client,
        "timeseries",
        period="1d",
        timezone="Asia/Shanghai",
        as_of=AS_OF_Q,
    )
    assert body["granularity"] == "hour4"
    labels = [p["label"] for p in body["points"]]
    assert labels == ["00:00", "04:00", "08:00", "12:00"]
    by_label = {p["label"]: p for p in body["points"]}
    assert by_label["00:00"]["generation"]["input_tokens"] == 3
    assert by_label["08:00"]["generation"]["input_tokens"] == 4
    assert by_label["12:00"]["is_partial"] is True
    assert by_label["12:00"]["generation"]["input_tokens"] == 0


def test_invalid_timezone_and_future_as_of(usage_client: TestClient) -> None:
    bad_tz = usage_client.get(
        "/api/usage/summary",
        params={"timezone": "Not/A_Zone", "as_of": AS_OF_Q},
    )
    assert bad_tz.status_code == 400
    assert bad_tz.json()["code"] == "VALIDATION"
    assert bad_tz.json()["details"]["reason"] == "INVALID_TIMEZONE"
    future = usage_client.get(
        "/api/usage/summary",
        params={"as_of": "2099-01-01T00:00:00Z"},
    )
    assert future.status_code == 400
    assert future.json()["details"]["reason"] == "AS_OF_IN_FUTURE"
