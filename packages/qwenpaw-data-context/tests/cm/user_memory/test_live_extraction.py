"""Run an opt-in User Graph extraction smoke test against the configured model."""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from context_manager.model import ModelConfigRepository, ModelService
from context_manager.schema import HAS_MEMORY, USER, USER_MEMORY
from context_manager.user.loader import (
    ChatLoader,
    ChatLoaderCheckpointRepository,
    UserHistoryScope,
)
from context_manager.user.loader.loader import USER_GRAPH_UPDATER_VERSION
from cryptography.fernet import Fernet
from dotenv import dotenv_values

from cm.infrastructure.access.support import authorization
from cm.knowledge.support import MemoryGraphStore
from cm.user_memory.support import history_adapter
from cm.user_memory.test_history_workflow import (
    _history_database,
    _runtime_with_memory_graph,
)

_LIVE_TEST_FLAG = "CM_RUN_LIVE_USER_GRAPH_TESTS"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _configured_model(database_path: Path, monkeypatch) -> ModelService:
    """Configure the synthetic user with the explicitly selected live provider."""
    env_file = Path(
        os.environ.get("CM_LIVE_ENV_FILE", Path(__file__).resolve().parents[5] / ".env")
    )
    config = {**dotenv_values(env_file), **os.environ}
    required = (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "LLM_MODEL",
        "EMBED_MODEL",
        "EMBED_DIM",
    )
    missing = [name for name in required if not config.get(name)]
    if missing:
        pytest.fail(
            "live User Graph model configuration is missing: " + ", ".join(missing)
        )
    master = secrets.token_bytes(32)
    monkeypatch.setenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", master.hex())
    cipher = Fernet(base64.urlsafe_b64encode(master))
    encrypted_key = "ENC:" + cipher.encrypt(config["OPENAI_API_KEY"].encode()).decode()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE user_providers (user_id TEXT, provider_id TEXT, api_key_enc TEXT, base_url TEXT, PRIMARY KEY (user_id, provider_id))"
        )
        connection.execute(
            "INSERT INTO user_providers VALUES ('user-1', 'live-provider', ?, ?)",
            (encrypted_key, config["OPENAI_BASE_URL"]),
        )
    models = ModelService(
        ModelConfigRepository("tenant-1", "user-1", workspace_id="workspace-1")
    )
    models.save("llm", {"provider_id": "live-provider", "model": config["LLM_MODEL"]})
    models.save(
        "embedding",
        {
            "provider_id": "live-provider",
            "model": config["EMBED_MODEL"],
            "dim": int(config["EMBED_DIM"]),
        },
    )
    return models


@pytest.mark.asyncio
async def test_configured_model_extracts_durable_user_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evaluate one real completion and embedding through the ingest path."""
    enabled = os.environ.get(_LIVE_TEST_FLAG, "").strip().lower()
    if enabled not in _TRUE_VALUES:
        pytest.skip(f"set {_LIVE_TEST_FLAG}=1 to run the configured model")

    database_path = tmp_path / "unified.sqlite3"
    _history_database(
        database_path,
        include_hybrid_chat=False,
    )
    checkpoints = ChatLoaderCheckpointRepository()
    loader = ChatLoader(checkpoints)
    store = MemoryGraphStore()
    config = _configured_model(database_path, monkeypatch)
    assert config.ready
    runtime = _runtime_with_memory_graph(store, config, monkeypatch)
    target = UserHistoryScope("tenant-1", "workspace-1", "user-1")

    try:
        result = await history_adapter(runtime, loader=loader).ingest(
            cutoff_at=datetime(2026, 8, 31, 15, tzinfo=UTC),
            target_scope=target,
        )
    finally:
        await runtime.aclose()

    read_scope = store.access.issue_read_scope(authorization())
    users = store.list_nodes(
        USER,
        filters={"user_id": "user-1"},
        limit=2,
        scope=read_scope,
    )
    memories = store.list_nodes(USER_MEMORY, scope=read_scope)
    edges = store.list_edges(HAS_MEMORY, scope=read_scope)
    memory_types = {str(memory.properties["type"]) for memory in memories}

    assert result["status"] == "success", result
    assert result["messages_processed"] == 3
    assert len(users) == 1
    user = users[0]
    profile = json.loads(str(user.properties["profile"]))
    assert profile.get("display_name") == "Alice", profile
    timezone = str(profile.get("timezone", "")).casefold()
    assert any(
        token in timezone
        for token in ("asia/shanghai", "北京时间", "utc+8", "china standard")
    ), profile
    assert {"principle", "preference"} <= memory_types, memory_types
    assert len(edges) == len(memories)
    assert (
        len(checkpoints.snapshot(target, updater_version=USER_GRAPH_UPDATER_VERSION))
        == 3
    )
