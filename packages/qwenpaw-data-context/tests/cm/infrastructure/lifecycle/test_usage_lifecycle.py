"""Protect usage SQL isolation and Runtime resource lifetimes."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from context_manager.db import get_database
from context_manager.model import ChatMessage, ResolvedModel, bind_usage_context
from context_manager.model.repository import ModelUsageRepository
from context_manager.runtime.startup import (
    create_service_runtime,
)
from context_manager.settings import get_settings
from context_manager.settings.cron import CronSettings
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    inspect,
    text,
)

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import create_external_tables
from cm.infrastructure.lifecycle.test_startup import (
    CapturingDriverFactory,
    _settings,
)

CHAT = ResolvedModel(
    "chat-provider", "requested-chat", "https://models.invalid/v1", "test-key"
)
EMBED = ResolvedModel(
    "embed-provider", "requested-embed", "https://models.invalid/v1", "test-key", 2
)


def create_host_usage_table(engine):
    """The Host, not CM initialization, owns this compatible table."""
    assert not inspect(engine).has_table("model_usages")
    schema = MetaData()
    Table(
        "model_usages",
        schema,
        Column("id", Text, primary_key=True),
        *(
            Column(name, Text, nullable=False)
            for name in (
                "user_id",
                "tenant_id",
                "workspace_id",
                "session_id",
                "chat_id",
                "source",
                "source_step",
                "model_type",
                "provider_id",
                "model_id",
            )
        ),
        *(
            Column(name, Integer, nullable=False)
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
                "duration_ms",
            )
        ),
        Column("created_at", DateTime, nullable=False),
    )
    schema.create_all(engine)


def usage_rows(dsn):
    """Read committed results independently of CM's engine and repository."""
    engine = create_engine(dsn)
    try:
        with engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    text("SELECT * FROM model_usages ORDER BY model_type")
                ).mappings()
            ]
    finally:
        engine.dispose()


def usage_threads():
    return {
        thread for thread in threading.enumerate() if thread.name == "cm-model-usage"
    }


@pytest.fixture
def sdk_clients(monkeypatch):
    clients = []
    monkeypatch.setattr(get_settings().embedding, "cache_ttl_seconds", 0)
    monkeypatch.setattr(get_settings().model, "ignore_proxy", False)

    def sdk(**options):
        embedding_response = SimpleNamespace(
            model="served-embed",
            data=[SimpleNamespace(index=0, embedding=[1.0, 2.0])],
            usage=SimpleNamespace(total_tokens=7),
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(
                        return_value=SimpleNamespace(
                            model="served-chat",
                            choices=[
                                SimpleNamespace(
                                    message=SimpleNamespace(content="Revenue is 42.")
                                )
                            ],
                            usage=SimpleNamespace(
                                prompt_tokens=12,
                                completion_tokens=4,
                                prompt_tokens_details=SimpleNamespace(
                                    cached_tokens=3, cache_creation_input_tokens=2
                                ),
                            ),
                        )
                    )
                )
            ),
            embeddings=SimpleNamespace(
                with_raw_response=SimpleNamespace(
                    create=AsyncMock(
                        return_value=SimpleNamespace(parse=lambda: embedding_response)
                    ),
                )
            ),
            close=AsyncMock(),
        )
        clients.append(client)
        return client

    monkeypatch.setattr("context_manager.model.llm.AsyncOpenAI", sdk)
    monkeypatch.setattr("context_manager.model.embedding.AsyncOpenAI", sdk)
    yield clients
    for client in clients:
        client.close.assert_awaited_once()


async def complete(models):
    with bind_usage_context(
        session_id="session-1", chat_id="chat-1", source_step="answer"
    ):
        async with models.llm(CHAT) as caller:
            assert (
                await caller.complete((ChatMessage("user", "What is revenue?"),))
                == "Revenue is 42."
            )


async def embed(models):
    with bind_usage_context(
        session_id="", chat_id="", source="知识抽取", source_step="embed-document"
    ):
        async with models.embedder(EMBED) as caller:
            assert await caller.embed(("Revenue",)) == ((1.0, 2.0),)


def assert_usage(dsn):
    rows = usage_rows(dsn)
    assert [(row["model_id"], row["input_tokens"]) for row in rows] == [
        ("served-chat", 12),
        ("served-embed", 7),
    ]
    return rows


def assert_stopped(models):
    tracker = models.create_usage_tracker(CHAT)
    assert tracker is not None
    with tracker.track_call("chat", "late-response") as record:
        assert record(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=99))) is False


@pytest.mark.asyncio
async def test_service_shutdown_flushes_usage_produced_during_business_cleanup(
    tmp_path, monkeypatch, sdk_clients
):
    get_settings().db.url = dsn = f"sqlite:///{tmp_path / 'service-usage.db'}"

    async def dependencies():
        create_external_tables(get_database().engine)
        create_host_usage_table(get_database().engine)

    driver = CapturingDriverFactory()
    runtime = create_service_runtime(
        _settings(database=False),
        driver_factory=driver,
        semantic_initializer=dependencies,
        cron_settings=CronSettings(enabled=False),
    )
    before = usage_threads()
    loop = asyncio.get_running_loop()
    flushing, release = asyncio.Event(), threading.Event()
    write = ModelUsageRepository.write
    released_before_write = []

    def held_write(repository, events):
        loop.call_soon_threadsafe(flushing.set)
        released_before_write.append(release.wait(timeout=2))
        write(repository, events)

    async def release_flush():
        await asyncio.wait_for(flushing.wait(), timeout=5)
        # Joining the writer must not block this loop or dispose SQL beforehand.
        with get_database().engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar() == 1
        release.set()

    monkeypatch.setattr(ModelUsageRepository, "write", held_write)
    release_task = asyncio.create_task(release_flush())
    try:
        await runtime.start()
        models = runtime.models(authorization())
        await complete(models)
        close_jobs = runtime.embedding_jobs.aclose

        async def finish_jobs():
            await close_jobs()
            await embed(models)

        monkeypatch.setattr(runtime.embedding_jobs, "aclose", finish_jobs)
        await runtime.aclose()
        await release_task
        assert released_before_write and all(released_before_write)
        first = assert_usage(dsn)
        assert_stopped(models)
        await runtime.aclose()
        runtime.close()
        assert usage_rows(dsn) == first
        assert runtime.closed and driver.calls == []
        assert usage_threads() == before
        with pytest.raises(RuntimeError, match="not been initialized"):
            get_database()
    finally:
        release.set()
        release_task.cancel()
        await asyncio.gather(release_task, return_exceptions=True)
        await runtime.aclose()
