"""Verify user-bound configuration management and temporary connection probes."""

import pytest
from context_manager.model import ModelConfigRepository, ModelService

from cm.infrastructure.models.support import (
    FakeCallerFactory,
    configure_models,
    create_host_db,
)


@pytest.mark.asyncio
async def test_probe_is_temporary_and_saving_persists_only_model_choices(
    tmp_path, monkeypatch
):
    _dsn, _, _, _ = create_host_db(tmp_path, monkeypatch)
    target = "tenant-1", "user-1"
    configure_models(target)
    factory = FakeCallerFactory()
    config = ModelService(
        ModelConfigRepository(*target, workspace_id="workspace-1"),
        llm_factory=factory,
        embed_factory=factory,
    )
    assert (await config.probe("llm", {"model": "probe-model"}))["success"]
    assert (await config.probe("embedding", {"model": "probe-embedding"}))[
        "detected_dim"
    ] == 1024
    assert config.read()["llm"]["model"] == "qwen-plus"
    assert all(caller.close_count == 1 for caller in factory.callers)
    with pytest.raises(ValueError, match="selection fields"):
        config.save("llm", {"api_key": "old-secret"})
    with pytest.raises(ValueError, match="match the selected Host Provider"):
        await config.probe("llm", {"base_url": "https://foreign.example/v1"})
    assert len(factory.callers) == 2
    updated, _ = config.save("llm", {"model": "qwen-max"})
    embedding, rebuild = config.save(
        "embedding", {"model": "embedding-v4", "dim": 1536}, can_override_space=True
    )
    assert updated["api_key"] == embedding["api_key"] == "****"
    assert rebuild is True
