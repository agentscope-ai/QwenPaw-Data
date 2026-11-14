"""Keep workspace vector conventions separate from each user's credentials."""

from contextlib import AsyncExitStack

import pytest
from context_manager.model import (
    EmbeddingSpaceConflict,
    ModelConfigError,
    ModelConfigRepository,
    ModelService,
)
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError

from cm.infrastructure.models.support import (
    FakeCallerFactory,
    configure_models,
    create_host_db,
)


@pytest.mark.asyncio
async def test_admin_space_change_blocks_new_mismatched_operations_but_not_issued_clients(
    tmp_path,
    monkeypatch,
):
    async with AsyncExitStack() as operation:
        _, _, _, _ = create_host_db(tmp_path, monkeypatch)
        for user in ("user-1", "user-2"):
            configure_models(("tenant-1", user))
        factory = FakeCallerFactory()
        clients_config = ModelService(
            ModelConfigRepository("tenant-1", "user-2", workspace_id="workspace-1")
        )
        clients = ModelService(
            clients_config.repository, llm_factory=factory, embed_factory=factory
        )
        _, fixed = await operation.enter_async_context(clients.open())
        ModelService(
            ModelConfigRepository("tenant-1", "user-1", workspace_id="workspace-1")
        ).save("embedding", {"model": "new-model"}, can_override_space=True)

        current = ModelService(
            ModelConfigRepository(
                clients_config.repository.tenant_id,
                clients_config.repository.user_id,
                workspace_id="workspace-1",
            )
        ).read()
        assert current["embedding_space"]["aligned"] is False
        with pytest.raises(EmbeddingSpaceConflict):
            await operation.enter_async_context(clients.open())
        assert len(factory.callers) == 2
        assert len((await fixed.embed(("still in progress",)))[0]) == 1024

        ModelService(
            ModelConfigRepository(
                clients_config.repository.tenant_id,
                clients_config.repository.user_id,
                workspace_id="workspace-1",
            )
        ).save("embedding", {"model": "new-model"})
        _, next_client = await operation.enter_async_context(clients.open())
        assert next_client.options["model"] == "new-model"
        assert next_client.options["api_key"] == "user-2-embed-key"
        assert fixed.options["model"] == "embedding-v3"


@pytest.mark.parametrize("adopted", [False, True])
def test_selection_provider_reference_and_space_roll_back_together(
    tmp_path,
    monkeypatch,
    adopted,
):
    _, engine, _, _ = create_host_db(tmp_path, monkeypatch)
    target = "tenant-1", "user-1"
    if adopted:
        configure_models(target)
    before = ModelService(
        ModelConfigRepository(*target, workspace_id="workspace-1")
    ).read()

    def fail_after_space_write(_conn, _cursor, statement, _parameters, _context, _many):
        prefix = "UPDATE" if adopted else "INSERT INTO"
        if statement.startswith(f"{prefix} cm_workspace_embedding_space"):
            raise SQLAlchemyError("simulated persistence failure")

    event.listen(engine, "after_cursor_execute", fail_after_space_write)
    try:
        with pytest.raises(ModelConfigError, match="could not be saved"):
            ModelService(
                ModelConfigRepository(*target, workspace_id="workspace-1")
            ).save(
                "embedding",
                {"provider_id": "chat", "model": "new-model", "dim": 1024},
                can_override_space=True,
            )
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_space_write)
    assert (
        ModelService(ModelConfigRepository(*target, workspace_id="workspace-1")).read()
        == before
    )


@pytest.mark.asyncio
async def test_workspace_conventions_are_isolated_by_tenant_and_workspace(
    tmp_path,
    monkeypatch,
):
    async with AsyncExitStack() as operation:
        _, engine, providers, encrypt = create_host_db(tmp_path, monkeypatch)
        configure_models(("tenant-1", "user-1"))
        original = ModelConfigRepository(
            "tenant-1", "user-1", workspace_id="workspace-1"
        ).read_embedding_space()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, status) VALUES (:id, :tenant, 'active')"
                ),
                {"id": "user-3", "tenant": "tenant-2"},
            )
            conn.execute(
                providers.insert().values(
                    user_id="user-3",
                    provider_id="embed",
                    api_key_enc=encrypt("third-user-secret"),
                    base_url="https://models.example/v1",
                )
            )
        for tenant, workspace, user in (
            ("tenant-1", "workspace-2", "user-1"),
            ("tenant-2", "workspace-1", "user-3"),
        ):
            ModelService(
                ModelConfigRepository(tenant, user, workspace_id=workspace)
            ).save(
                "embedding",
                {"provider_id": "embed", "model": "isolated-model", "dim": 1024},
            )
            assert (
                ModelConfigRepository(
                    tenant, user, workspace_id=workspace
                ).read_embedding_space()["model"]
                == "isolated-model"
            )
        assert (
            ModelConfigRepository(
                "tenant-1", "user-1", workspace_id="workspace-1"
            ).read_embedding_space()
            == original
        )
        # User selections are shared across workspaces, but conventions are not.
        factory = FakeCallerFactory()
        old_workspace_config = ModelService(
            ModelConfigRepository("tenant-1", "user-1", workspace_id="workspace-1")
        )
        old_workspace = ModelService(
            old_workspace_config.repository, llm_factory=factory, embed_factory=factory
        )
        new_workspace_config = ModelService(
            ModelConfigRepository("tenant-1", "user-1", workspace_id="workspace-2")
        )
        new_workspace = ModelService(
            new_workspace_config.repository, llm_factory=factory, embed_factory=factory
        )
        with pytest.raises(EmbeddingSpaceConflict):
            await operation.enter_async_context(old_workspace.open())
        _, embedder = await operation.enter_async_context(new_workspace.open())
        assert embedder.options["model"] == "isolated-model"
