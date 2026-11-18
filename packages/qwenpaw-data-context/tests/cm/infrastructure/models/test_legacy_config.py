"""Historical key columns stay unused once resolution requires a Host Provider."""

import pytest
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


def test_unbound_legacy_keys_are_not_configuration(legacy):
    models, _, _, _ = legacy
    status = models.read()
    assert not status["configured"]
    assert status["missing_fields"] == ["llm.provider_id", "embedding.provider_id"]
    for channel in ("llm", "embedding"):
        assert "provider_id" in status["errors"][channel]
        with pytest.raises(ModelConfigError, match="provider_id"):
            models.resolve(channel)


async def test_bound_channel_uses_provider_key_only(legacy):
    models, _, _, _ = legacy
    models.save("llm", {"provider_id": "chat", "model": "provider-model"})
    models.save("embedding", {"provider_id": "chat", "model": "provider-embed"})
    status = models.read()
    assert status["configured"] and status["key_status"] == "ok" and models.ready
    async with models.open() as (llm, embedder):
        assert llm.options["api_key"] == "user-1-chat-key"
        assert embedder.options["api_key"] == "user-1-chat-key"
        assert llm.options["model"] == "provider-model"
        assert embedder.options["dimensions"] == 3


@pytest.mark.parametrize(
    "key,error",
    [
        (None, "configure this Provider"),
        ("broken", "decrypted"),
    ],
)
def test_invalid_provider_credentials_do_not_use_legacy_keys(legacy, key, error):
    models, engine, providers, _ = legacy
    with engine.begin() as conn:
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
    assert not models.read()["configured"]
    assert error in models.read()["errors"]["llm"]
    with pytest.raises(ModelConfigError, match=error):
        models.resolve_for_use("llm")


def test_provider_adoption_retires_legacy_keys(legacy):
    models, engine, _, _ = legacy
    with pytest.raises(ModelConfigError, match="provider_id"):
        models.save("llm", {"model": "edited-old"})
    models.save("llm", {"provider_id": "chat", "model": "new"})
    with engine.begin() as conn:
        stored = conn.execute(
            text("SELECT llm_api_key_enc FROM cm_user_model_config")
        ).scalar()
        conn.exec_driver_sql("DELETE FROM cm_model_provider_refs WHERE channel='llm'")
    assert stored is None
    with pytest.raises(ModelConfigError, match="provider_id"):
        models.resolve("llm")
    with pytest.raises(ModelConfigError, match="provider_id"):
        models.resolve("embedding")
    models.delete()
    assert not models.read()["configured"]
    with pytest.raises(ModelConfigError, match="model is required"):
        models.resolve("embedding")


@pytest.mark.parametrize("channel", ["llm", "embedding"])
@pytest.mark.parametrize("invalid", ["bad-port", "large-port", "model-spaces"])
async def test_status_and_real_clients_reject_invalid_provider_options(
    legacy, channel, invalid
):
    models, engine, providers, _ = legacy
    prefix = "llm" if channel == "llm" else "embed"
    endpoint = (
        "https://old.example:invalid/v1"
        if invalid == "bad-port"
        else "https://old.example:65536/v1"
    )
    with engine.begin() as conn:
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
        else:
            conn.execute(
                providers.update()
                .where(
                    providers.c.user_id == "user-1", providers.c.provider_id == "chat"
                )
                .values(base_url=endpoint)
            )
    real = ModelService(models.repository)
    status = real.read()
    assert not status["configured"] and not real.ready
    error = "model must be trimmed" if invalid == "model-spaces" else "endpoint"
    assert error in status["errors"][channel]
    with pytest.raises(ModelConfigError, match=error):
        async with real.llm() if channel == "llm" else real.embedder():
            pytest.fail("Invalid options must be rejected before constructing a client")
