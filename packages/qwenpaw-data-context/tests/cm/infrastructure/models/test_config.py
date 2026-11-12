"""Exercise direct model resolution and user-bound runtime integration."""

import asyncio
from contextlib import AsyncExitStack

import pytest
from context_manager.model import (
    ChatMessage,
    ModelConfigError,
    ModelConfigRepository,
    ModelService,
)
from context_manager.schema.relational import (
    model_configs,
    provider_refs,
)
from sqlalchemy import select

from cm.infrastructure.models.support import FakeCallerFactory, create_host_db


@pytest.fixture
def host_db(tmp_path, monkeypatch):
    db = create_host_db(tmp_path, monkeypatch)
    yield db
    db[1].dispose()


def configured_models(user, factory):
    models = ModelService(
        ModelConfigRepository("tenant-1", user, workspace_id="workspace-1"),
        llm_factory=factory,
        embed_factory=factory,
    )
    models.save("llm", {"provider_id": "chat", "model": f"{user}-chat"})
    models.save(
        "embedding", {"provider_id": "embed", "model": "embedding-v3", "dim": 3}
    )
    return models


@pytest.mark.asyncio
async def test_shared_database_user_isolation_rotation_and_persistence(host_db):
    async with AsyncExitStack() as operation:
        _, engine, providers, encrypt = host_db
        first_factory, second_factory = FakeCallerFactory(), FakeCallerFactory()
        first = configured_models("user-1", first_factory)
        second = configured_models("user-2", second_factory)
        first_llm, first_embedder = await operation.enter_async_context(first.open())
        second_llm, _ = await operation.enter_async_context(second.open())
        await asyncio.gather(
            first_llm.complete((ChatMessage("user", "a"),)),
            second_llm.complete((ChatMessage("user", "b"),)),
        )
        await first_embedder.embed(("a",))
        assert first_factory.callers[0].options["api_key"] == "user-1-chat-key"
        assert second_factory.callers[0].options["api_key"] == "user-2-chat-key"
        assert first_factory.callers[1].options["api_key"] == "user-1-embed-key"
        with engine.begin() as conn:
            conn.execute(
                providers.update()
                .where(
                    providers.c.user_id == "user-1", providers.c.provider_id == "chat"
                )
                .values(
                    api_key_enc=encrypt("rotated-key"),
                    base_url="https://rotated.example/v1",
                )
            )
        rotated_llm, _ = await operation.enter_async_context(first.open())
        await rotated_llm.complete((ChatMessage("user", "after rotation"),))
        assert first_factory.callers[2].options["api_key"] == "rotated-key"
        assert (
            first_factory.callers[2].options["base_url"] == "https://rotated.example/v1"
        )
        with engine.connect() as conn:
            configs = conn.execute(select(model_configs)).mappings().all()
            refs = conn.execute(select(provider_refs)).mappings().all()
        assert len(configs) == 2 and len(refs) == 4
        assert "api_key" not in repr(configs)
        assert "rotated-key" not in repr(first.read())
        reloaded_config = ModelService(
            ModelConfigRepository("tenant-1", "user-1", workspace_id="workspace-1")
        )
        assert reloaded_config.read()["llm"]["model"] == "user-1-chat"
        with pytest.raises(ModelConfigError, match="this tenant"):
            ModelConfigRepository(
                "other-tenant", "user-1", workspace_id="workspace-1"
            ).read_selections()
