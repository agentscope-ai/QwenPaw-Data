"""Reconcile CM-recorded usage through Host APIs against a shared SQLite file."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from context_manager.db import close_database as close_cm_database
from context_manager.db import initialize_database
from context_manager.model import (
    ChatMessage,
    ModelConfigRepository,
    ModelService,
    OpenAIEmbedCaller,
    OpenAILlmCaller,
    ResolvedModel,
    bind_usage_context,
)
from context_manager.model import usage as cm_usage
from context_manager.model.repository import ModelUsageRepository
from context_manager.model.usage_recorder import ModelUsageRecorder
from context_manager.settings import (
    clear_settings as clear_cm_settings,
)
from context_manager.settings import (
    get_settings as get_cm_settings,
)
from context_manager.settings.db import DbSettings
from fastapi.testclient import TestClient
from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
)
from qwenpaw_data.host.core.db.tables import ModelUsageRow, SessionRow
from qwenpaw_data.host.core.domain.usage_window import background_group_id
from qwenpaw_data.host.core.settings import (
    clear_settings as clear_host_settings,
)
from qwenpaw_data.host.core.settings import (
    get_settings,
)
from sqlalchemy import select

AS_OF = datetime(2026, 9, 17, 5, 0, tzinfo=UTC)
AS_OF_Q = "2026-09-17T05:00:00Z"
PARAMS = {"period": "7d", "timezone": "Asia/Shanghai", "as_of": AS_OF_Q}


@pytest.fixture
def usage_client(tmp_path, monkeypatch):
    """Own Host configuration and app lifecycle without Host test fixtures."""
    monkeypatch.setenv(
        "QWENPAW_DATA_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'usage.sqlite3'}"
    )
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "local")
    monkeypatch.setenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", "ab" * 32)
    monkeypatch.delenv("QWENPAW_DATA_AUTH_SECRET", raising=False)
    monkeypatch.setenv("QWENPAW_DATA_CHANNELS", "feishu,dingtalk,wecom,wechat")
    monkeypatch.setenv("QWENPAW_DATA_PROVIDERS", "dashscope,openai")
    clear_host_settings()
    try:
        with TestClient(
            create_app(),
            base_url="http://localhost",
            client=("127.0.0.1", 50000),
        ) as client:
            yield client
    finally:
        clear_host_settings()


def _get(client: TestClient, path: str, **extra) -> dict:
    response = client.get(f"/api/v1/usage/{path}", params={**PARAMS, **extra})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def cm_recorder(usage_client, monkeypatch):
    """Write through CM's separate pool into the actual Host-owned schema."""
    clear_cm_settings()
    initialize_database(DbSettings(url=get_settings().db.url))
    monkeypatch.setattr(get_cm_settings().embedding, "cache_ttl_seconds", 0)
    recorder = ModelUsageRecorder(ModelUsageRepository())
    recorder.start()
    try:
        yield recorder
    finally:
        recorder.stop()
        close_cm_database()
        clear_cm_settings()


def test_cm_recorded_usage_reconciles_through_host_api(
    usage_client, cm_recorder, monkeypatch
):
    for endpoint in ("summary", "timeseries", "sessions", "background-tasks"):
        body = _get(usage_client, endpoint)
        totals = body if endpoint == "summary" else body["totals"]
        assert totals["generation"] == {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
        }
        assert totals["embedding"] == {"tokens": 0, "calls": 0}
        assert totals["total_tokens"] == 0
        if endpoint in {"sessions", "background-tasks"}:
            assert body["items"] == [] and body["pagination"]["total"] == 0

    chat = AsyncMock(
        return_value=SimpleNamespace(
            model="served-chat",
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
            usage=SimpleNamespace(
                prompt_tokens=12,
                completion_tokens=4,
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=3, cache_creation_input_tokens=2
                ),
            ),
        )
    )
    embedding = AsyncMock(
        return_value=SimpleNamespace(
            model="served-embedding",
            usage=SimpleNamespace(total_tokens=7),
            data=[SimpleNamespace(index=0, embedding=[1.0, 2.0])],
        )
    )

    async def raw_embedding(**options):
        response = await embedding(**options)
        return SimpleNamespace(parse=lambda: response)

    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat)),
        embeddings=SimpleNamespace(
            with_raw_response=SimpleNamespace(create=raw_embedding)
        ),
    )
    clock = Mock(wraps=datetime)
    monkeypatch.setattr(cm_usage, "datetime", clock)
    config = ResolvedModel(
        "cm-provider", "requested", "https://models.invalid/v1", "test-key", 2
    )
    scope = (TEST_TENANT_ID, TEST_WORKSPACE_ID, TEST_USER_ID)
    window_start = datetime(2026, 9, 10, 16, tzinfo=UTC)

    async def record(
        channel,
        *,
        identity=scope,
        when=AS_OF - timedelta(hours=2),
        session_id="",
        source="知识抽取",
        source_step="knowledge_extract#doc-a",
    ):
        tenant_id, workspace_id, user_id = identity
        models = ModelService(
            ModelConfigRepository(tenant_id, user_id, workspace_id=workspace_id),
            llm_factory=lambda **options: OpenAILlmCaller(client=sdk, **options),
            embed_factory=lambda **options: OpenAIEmbedCaller(client=sdk, **options),
            enqueue_usage=cm_recorder.enqueue,
        )
        clock.now.return_value = when
        with bind_usage_context(
            session_id=session_id,
            chat_id="cm-chat" if session_id else "",
            source=source,
            source_step=source_step,
            probe=False,
        ):
            if channel == "chat":
                async with models.llm(config) as caller:
                    assert (
                        await caller.complete((ChatMessage("user", "question"),))
                        == "answer"
                    )
            else:
                async with models.embedder(config) as caller:
                    assert await caller.embed(("Revenue",)) == ((1.0, 2.0),)

    async def produce():
        async with get_session_factory()() as db:
            db.add(
                SessionRow(
                    id="cm-session",
                    tenant_id=TEST_TENANT_ID,
                    workspace_id=TEST_WORKSPACE_ID,
                    user_id=TEST_USER_ID,
                    title="CM exploration",
                    created_at=AS_OF,
                    updated_at=AS_OF,
                )
            )
            await db.commit()
        await record("chat", session_id="cm-session", source_step="journey_explore")
        await record(
            "embedding",
            session_id="cm-session",
            when=window_start,
            source_step="search_vector",
        )
        await record("chat")
        await record("embedding", source_step="knowledge_extract#doc-b")
        await record(
            "embedding", source="向量索引维护", source_step="vector_index_rebuild#job"
        )
        for identity in (
            ("other-tenant", TEST_WORKSPACE_ID, TEST_USER_ID),
            (TEST_TENANT_ID, "other-workspace", TEST_USER_ID),
            (TEST_TENANT_ID, TEST_WORKSPACE_ID, "other-user"),
        ):
            await record("chat", identity=identity, session_id="foreign-session")
        await record(
            "chat",
            when=window_start - timedelta(microseconds=1),
            session_id="cm-session",
        )
        await record("chat", when=AS_OF, session_id="cm-session")

    asyncio.run(produce())
    assert cm_recorder.stop()
    assert cm_recorder.usage_dropped_total == 0
    assert chat.await_count == 7 and embedding.await_count == 3

    async def committed_rows():
        async with get_session_factory()() as db:
            return (await db.scalars(select(ModelUsageRow))).all()

    rows = asyncio.run(committed_rows())
    assert len(rows) == len({row.id for row in rows}) == 10
    assert sum(row.cache_read_tokens for row in rows) == 21
    assert sum(row.cache_creation_tokens for row in rows) == 14
    summary = _get(usage_client, "summary")
    timeseries = _get(usage_client, "timeseries")
    sessions = _get(usage_client, "sessions")
    background = _get(usage_client, "background-tasks")
    assert summary["generation"] == {
        "input_tokens": 24,
        "output_tokens": 8,
        "total_tokens": 32,
        "calls": 2,
    }
    assert summary["embedding"] == {"tokens": 21, "calls": 3}
    assert summary["total_tokens"] == 53
    assert summary["sessions"] == {"count": 1}
    assert summary["background_tasks"] == {"count": 2}
    assert all(
        body["window"] == summary["window"]
        for body in (timeseries, sessions, background)
    )
    assert timeseries["totals"] == {
        key: summary[key] for key in ("generation", "embedding", "total_tokens")
    }
    assert (
        sum(point["generation"]["total_tokens"] for point in timeseries["points"]) == 32
    )
    assert sum(point["embedding"]["tokens"] for point in timeseries["points"]) == 21
    assert timeseries["points"][0]["embedding"] == {"tokens": 7, "calls": 1}
    (session,) = sessions["items"]
    assert (session["session_id"], session["title"], session["session_state"]) == (
        "cm-session",
        "CM exploration",
        "active",
    )
    assert session["total_tokens"] == 23
    assert {
        (model["model_type"], model["model_id"]) for model in session["models"]
    } == {
        ("text", "served-chat"),
        ("embedding", "served-embedding"),
    }
    groups = {item["source"]: item for item in background["items"]}
    assert {source: item["total_tokens"] for source, item in groups.items()} == {
        "知识抽取": 23,
        "向量索引维护": 7,
    }
    assert all(
        item["group_id"] == background_group_id(source)
        for source, item in groups.items()
    )
    assert (
        sessions["pagination"]["total"] == 1 and background["pagination"]["total"] == 2
    )
    for kind in ("generation", "embedding"):
        for field, expected in summary[kind].items():
            assert (
                sessions["totals"][kind][field] + background["totals"][kind][field]
                == expected
            )
    assert (
        sessions["totals"]["total_tokens"] + background["totals"]["total_tokens"] == 53
    )
