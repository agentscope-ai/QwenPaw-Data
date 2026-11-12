"""Keep historical credentials usable without reviving revoked Provider keys."""

import pytest
from context_manager.api.native.config.views import ModelConfigResponse
from context_manager.model import ModelConfigError, ModelConfigRepository, ModelService
from sqlalchemy import text

from cm.infrastructure.models.support import FakeCallerFactory, create_host_db


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    _, engine, providers, encrypt = create_host_db(tmp_path, monkeypatch)
    with engine.begin() as conn:
        for prefix in ("llm", "embed"):
            conn.exec_driver_sql(
                f"ALTER TABLE cm_user_model_config ADD {prefix}_api_key_enc TEXT"
            )
        conn.execute(
            text("""
            INSERT INTO cm_user_model_config
            (tenant_id,user_id,llm_model,llm_base_url,llm_api_key_enc,
             embed_model,embed_base_url,embed_api_key_enc,embed_dim)
            VALUES ('tenant-1','user-1','chat-old','https://old.example/v1',:key,
                    'embed-old','https://old.example/v1',:key,3)
        """),
            {"key": encrypt("old-key")},
        )
    factory = FakeCallerFactory()
    models = ModelService(
        ModelConfigRepository("tenant-1", "user-1", workspace_id="workspace-1"),
        llm_factory=factory,
        embed_factory=factory,
    )
    yield models, engine, providers, encrypt
    engine.dispose()


@pytest.mark.parametrize("bound", [None, "llm", "embedding"])
async def test_legacy_status_and_clients_share_channel_resolution(legacy, bound):
    models, _, _, _ = legacy
    if bound:
        models.save(bound, {"provider_id": "chat", "model": "provider-model"})
    status = ModelConfigResponse.model_validate(models.read())
    assert status.configured and status.key_status == "ok" and models.ready
    async with models.open() as clients:
        for channel, client in zip(("llm", "embedding"), clients):
            public = getattr(status, channel)
            assert public.provider_id == ("chat" if channel == bound else None)
            assert client.options["api_key"] == (
                "user-1-chat-key" if channel == bound else "old-key"
            )
            assert client.options["model"] == public.model
            assert client.options["base_url"] == public.base_url
        assert clients[1].options["dimensions"] == 3


@pytest.mark.parametrize(
    "bound,key,error",
    [
        (False, None, "missing"),
        (False, "broken", "decrypted"),
        (True, None, "configure this Provider"),
        (True, "broken", "decrypted"),
    ],
)
def test_invalid_credentials_never_fall_back(legacy, bound, key, error):
    models, engine, providers, _ = legacy
    with engine.begin() as conn:
        if bound:
            conn.exec_driver_sql(
                "INSERT INTO cm_model_provider_refs VALUES ('tenant-1','user-1','llm','chat')"
            )
            query = (
                providers.delete()
                if key is None
                else providers.update().values(api_key_enc=key)
            )
            conn.execute(
                query.where(
                    providers.c.user_id == "user-1", providers.c.provider_id == "chat"
                )
            )
        else:
            conn.execute(
                text("UPDATE cm_user_model_config SET llm_api_key_enc=:key"),
                {"key": key},
            )
    assert not models.read()["configured"]
    assert error in models.read()["errors"]["llm"]
    with pytest.raises(ModelConfigError, match=error):
        models.resolve_for_use("llm")


def test_provider_adoption_and_config_deletion_retire_legacy_keys(legacy):
    models, engine, _, _ = legacy
    with pytest.raises(ModelConfigError, match="provider_id"):
        models.save("llm", {"model": "edited-old"})
    models.save("llm", {"provider_id": "chat", "model": "new"})
    # Even removing the reference externally must not revive a retired key.
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM cm_model_provider_refs WHERE channel='llm'")
    with pytest.raises(ModelConfigError, match="missing"):
        models.resolve("llm")
    assert models.resolve("embedding").api_key == "old-key"
    models.delete()
    assert not models.read()["configured"]
    with pytest.raises(ModelConfigError, match="model is required"):
        models.resolve("embedding")


def test_legacy_embedding_requires_its_saved_dimension(legacy):
    models, engine, _, _ = legacy
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE cm_user_model_config SET embed_dim=NULL")
    assert not models.read()["configured"] and not models.ready
    with pytest.raises(ModelConfigError, match="dimension is required"):
        models.resolve_for_use("embedding")


@pytest.mark.parametrize("channel", ["llm", "embedding"])
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("invalid", ["bad-port", "large-port", "model-spaces"])
async def test_status_and_real_clients_reject_invalid_options(
    legacy, channel, bound, invalid
):
    models, engine, providers, _ = legacy
    prefix = "llm" if channel == "llm" else "embed"
    endpoint = (
        "https://old.example:invalid/v1"
        if invalid == "bad-port"
        else "https://old.example:65536/v1"
    )
    with engine.begin() as conn:
        if bound:
            conn.execute(
                text(
                    "INSERT INTO cm_model_provider_refs VALUES ('tenant-1','user-1',:channel,'chat')"
                ),
                {"channel": channel},
            )
        if invalid == "model-spaces":
            conn.execute(
                text(f"UPDATE cm_user_model_config SET {prefix}_model=:model"),
                {"model": " old-model "},
            )
        elif bound:
            conn.execute(
                providers.update()
                .where(
                    providers.c.user_id == "user-1", providers.c.provider_id == "chat"
                )
                .values(base_url=endpoint)
            )
        else:
            conn.execute(
                text(f"UPDATE cm_user_model_config SET {prefix}_base_url=:url"),
                {"url": endpoint},
            )
    real = ModelService(models.repository)
    status = real.read()
    assert not status["configured"] and not real.ready
    error = "model must be trimmed" if invalid == "model-spaces" else "endpoint"
    assert error in status["errors"][channel]
    with pytest.raises(ModelConfigError, match=error):
        async with real.llm() if channel == "llm" else real.embedder():
            pytest.fail("Invalid options must be rejected before constructing a client")
