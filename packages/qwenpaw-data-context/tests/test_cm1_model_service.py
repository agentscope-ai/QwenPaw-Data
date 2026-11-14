"""Offline SQL, binding, cipher, client lifetime and usage queue regressions."""

import asyncio
import base64
import json
import secrets
import traceback
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import Column, MetaData, Table, Text, create_engine, event, select
from sqlalchemy.pool import StaticPool

from context_manager.db import CMDatabase
from context_manager.model import EmbeddingSpaceConflict, ModelConfigError, ModelConfigRepository, ModelService
from context_manager.model import repository, service
from context_manager.model.repository import ModelUsageRepository
from context_manager.model.usage import ModelUsageEvent, ModelUsageTracker, bind_usage_context
from context_manager.model.usage_recorder import ModelUsageRecorder
from context_manager.runtime_model_binding import HEADER, RuntimeModelBindingMiddleware, llm_binding
from context_manager.schema.relational import metadata, model_configs, model_usages, provider_refs, workspace_embedding_spaces
from context_manager.settings import model as model_settings
from context_manager.settings.model import ModelSettings


@pytest.fixture
def shared_models(monkeypatch):
    model = ModelSettings.model_construct(
        preferences_master_secret=secrets.token_hex(32), env="development",
    )
    settings = SimpleNamespace(model=model)
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    database = CMDatabase(engine)
    monkeypatch.setattr(repository, "get_database", lambda: database)
    metadata.create_all(engine, tables=[model_configs, provider_refs, workspace_embedding_spaces])
    host = MetaData()
    users = Table("users", host,
        Column("id", Text, primary_key=True), Column("tenant_id", Text), Column("status", Text))
    providers = Table("user_providers", host,
        Column("user_id", Text, primary_key=True), Column("provider_id", Text, primary_key=True),
        Column("api_key_enc", Text), Column("base_url", Text))
    usages = Table("model_usages", host, *[
        Column(column.name, column.type, primary_key=column.name == "id")
        for column in model_usages.columns
    ])
    host.create_all(engine)

    def encrypt(key):
        return "ENC:" + Fernet(base64.urlsafe_b64encode(settings.model.master_key())).encrypt(key.encode()).decode()

    with engine.begin() as conn:
        for user in ("user", "other-user"):
            conn.execute(users.insert().values(id=user, tenant_id="tenant", status="active"))
            for provider in ("chat", "embed"):
                conn.execute(providers.insert().values(
                    user_id=user, provider_id=provider,
                    api_key_enc=encrypt(f"{user}-{provider}-secret"),
                    base_url="https://models.example/v1",
                ))
    models = ModelService(ModelConfigRepository("tenant", "user", workspace_id="workspace"))
    models.save("llm", {"provider_id": "chat", "model": "stored-chat"})
    models.save("embedding", {"provider_id": "embed", "model": "stored-embed", "dim": 2})
    yield SimpleNamespace(models=models, engine=engine, users=users, providers=providers,
                          usages=usages, settings=settings, encrypt=encrypt)
    database.close()


def binding_header(model):
    binding = {"model": model, "base_url": "https://runtime.example/v1", "api_key": "runtime-secret"}
    return base64.urlsafe_b64encode(json.dumps(binding).encode()).decode()


def caller_factory(callers):
    def factory(**options):
        caller = SimpleNamespace(options=options, aclose=AsyncMock(),
            complete=AsyncMock(return_value="pong"), embed=AsyncMock(return_value=((1.0, 2.0),)))
        callers.append(caller)
        return caller
    return factory


async def test_authenticated_bindings_are_request_local_without_repository_writes(shared_models, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "machine-token")
    callers, reads, writes = [], [], []
    models = shared_models.models
    models._llm_factory = caller_factory(callers)
    models._embed_factory = caller_factory(callers)
    original = ModelConfigRepository.read_model

    def read_model(self, channel, **options):
        reads.append(channel)
        return original(self, channel, **options)

    monkeypatch.setattr(ModelConfigRepository, "read_model", read_model)

    def observe(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    event.listen(shared_models.engine, "before_cursor_execute", observe)
    app = FastAPI()
    app.add_middleware(RuntimeModelBindingMiddleware)

    @app.get("/operation")
    async def operation():
        async with models.open() as (chat, embed):
            before = chat.options["model"]
            await asyncio.sleep(0)
            after = await asyncio.to_thread(models.resolve, "llm")
            return {"before": before, "after": after.model, "embedding": embed.options["model"]}

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async def call(model):
                return await client.get("/operation", headers={
                    "Authorization": "Bearer machine-token", HEADER: binding_header(model),
                })

            one, two = await asyncio.gather(call("one"), call("two"))
            assert one.json() == {"before": "one", "after": "one", "embedding": "stored-embed"}
            assert two.json() == {"before": "two", "after": "two", "embedding": "stored-embed"}
            assert reads == ["embedding", "embedding"]
            assert not writes
            assert (await client.get("/operation")).json() == {
                "before": "stored-chat", "after": "stored-chat", "embedding": "stored-embed",
            }
            rejected = await client.get("/operation", headers={
                "Authorization": "Bearer scoped-read-key", HEADER: binding_header("attacker"),
            })
            assert rejected.status_code == 403 and "runtime-secret" not in rejected.text
        assert llm_binding.get() is None
        assert models.resolve("llm").model == "stored-chat"
        assert not writes
        for caller in callers:
            caller.aclose.assert_awaited_once()
            if caller.options["base_url"] == "https://runtime.example/v1":
                assert caller.options["usage_tracker"] is None
            elif "cache_namespace" in caller.options:
                assert caller.options["cache_namespace"] == ("tenant", "workspace", "user")
    finally:
        event.remove(shared_models.engine, "before_cursor_execute", observe)


async def test_binding_never_resolves_candidates_or_unavailable_embeddings(shared_models):
    models = shared_models.models
    token = llm_binding.set({"model": "runtime-chat", "base_url": "https://runtime.example/v1", "api_key": "runtime-secret"})
    try:
        assert models.resolve("llm").provider_id is None
        assert "runtime-secret" not in repr(models.resolve("llm"))
        candidate = models.resolve("llm", selection={"provider_id": "chat", "model": "candidate"})
        assert candidate.provider_id == "chat" and candidate.api_key == "user-chat-secret"
        with pytest.raises(ModelConfigError, match="configure this Provider"):
            models.resolve("llm", selection={"provider_id": "missing", "model": "candidate"})
        with pytest.raises(ValueError, match="selection fields"):
            models.save("llm", {"api_key": "runtime-secret"})
        callers = []
        models._llm_factory = caller_factory(callers)
        assert (await models.probe("llm", {"model": "candidate"}))["success"]
        assert callers[0].options["model"] == "candidate"
        assert callers[0].options["api_key"] == "user-chat-secret"
        callers[0].aclose.assert_awaited_once()
        assert models.repository.read_selections()["llm"]["model"] == "stored-chat"
        with shared_models.engine.begin() as conn:
            conn.execute(shared_models.providers.delete().where(shared_models.providers.c.provider_id == "embed"))
        assert not models.ready
        with pytest.raises(ModelConfigError, match="configure this Provider"):
            async with models.open():
                pytest.fail("binding must never substitute an embedding provider")
        assert len(callers) == 1
    finally:
        llm_binding.reset(token)


@pytest.mark.parametrize("update,match", [
    ({"model": ""}, "model is required"),
    ({"model": " padded"}, "trimmed"),
    ({"provider_id": ""}, "provider_id is required"),
    ({"provider_id": "missing"}, "configure this Provider"),
    ({"dim": True}, "positive integer"),
    ({"dim": 0}, "positive integer"),
])
def test_candidate_provider_and_dimension_validation(shared_models, update, match):
    selected = {"provider_id": "embed", "model": "embed", "dim": 2} | update
    with pytest.raises(ModelConfigError, match=match):
        shared_models.models.resolve("embedding", selection=selected)


@pytest.mark.parametrize("endpoint", [
    "file:///synthetic-secret", "https://synthetic-secret@models.example/v1",
    "https://models.example:synthetic-secret", "https://models.example:99999",
    "https://[invalid", "https://models.example:0", "relative/synthetic-secret",
])
def test_invalid_provider_endpoint_errors_are_redacted(shared_models, endpoint):
    with shared_models.engine.begin() as conn:
        conn.execute(shared_models.providers.update().values(base_url=endpoint))
    with pytest.raises(ModelConfigError) as error:
        shared_models.models.resolve("llm")
    assert "synthetic-secret" not in str(error.value)
    assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("encrypted", [None, "synthetic-secret", "ENC:synthetic-secret", "ENC:非ASCII"])
def test_secret_failures_are_redacted_and_never_fall_back_to_environment(shared_models, encrypted, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    with shared_models.engine.begin() as conn:
        conn.execute(shared_models.providers.update().values(api_key_enc=encrypted))
    with pytest.raises(ModelConfigError) as error:
        shared_models.models.resolve("llm")
    rendered = "".join(traceback.format_exception(error.value))
    assert "synthetic-secret" not in rendered and "environment-secret" not in rendered
    assert "credential" in str(error.value)
    result = shared_models.models.read()
    assert not result["configured"]
    assert result["key_status"] == "undecryptable"
    assert "environment-secret" not in repr(result)


def test_cipher_uses_same_master_key_and_rejects_rotation(shared_models):
    key = shared_models.encrypt("synthetic-secret")
    assert ModelService._decrypt(key) == "synthetic-secret"
    shared_models.settings.model = ModelSettings.model_construct(preferences_master_secret=secrets.token_hex(32))
    with pytest.raises(ModelConfigError, match="could not be decrypted"):
        ModelService._decrypt(key)
    assert "synthetic-secret" not in repr(shared_models.models.resolve.__self__)


def test_cipher_respects_persisted_installation_key(shared_models, tmp_path, monkeypatch):
    monkeypatch.setattr(model_settings, "PathsSettings", lambda: SimpleNamespace(home=tmp_path))
    first = ModelSettings.model_construct(preferences_master_secret="", env="development")
    shared_models.settings.model = first
    first_key = first.master_key()
    encrypted = shared_models.encrypt("installation-secret")
    second = ModelSettings.model_construct(preferences_master_secret="", env="development")
    assert second.master_key() == first_key
    shared_models.settings.model = second
    assert ModelService._decrypt(encrypted) == "installation-secret"
    assert len(first_key) == 32


@pytest.mark.parametrize("secret,environment,match", [
    ("", "production", "must be set in production"),
    ("invalid-synthetic-secret", "development", "64 hex"),
])
def test_master_key_errors_do_not_use_a_source_fallback(shared_models, secret, environment, match):
    shared_models.settings.model = ModelSettings.model_construct(preferences_master_secret=secret, env=environment)
    with pytest.raises(ModelConfigError, match=match) as error:
        ModelService._decrypt("ENC:synthetic-secret")
    assert "invalid-synthetic-secret" not in str(error.value)


async def test_open_closes_both_clients_on_failure_and_partial_acquisition(shared_models):
    models, callers = shared_models.models, []
    models._llm_factory = caller_factory(callers)
    models._embed_factory = caller_factory(callers)
    with pytest.raises(RuntimeError, match="operation failed"):
        async with models.open():
            raise RuntimeError("operation failed")
    assert len(callers) == 2
    for caller in callers:
        caller.aclose.assert_awaited_once()
    callers.clear()

    def fail(**options):
        raise RuntimeError("construction failed")

    models._embed_factory = fail
    with pytest.raises(RuntimeError, match="construction failed"):
        async with models.open():
            pytest.fail("embedder acquisition should fail")
    assert len(callers) == 1
    callers[0].aclose.assert_awaited_once()


async def test_open_enforces_workspace_convention_before_client_creation(shared_models):
    models, callers = shared_models.models, []
    models._llm_factory = caller_factory(callers)
    models._embed_factory = caller_factory(callers)
    with shared_models.engine.begin() as conn:
        conn.execute(workspace_embedding_spaces.update().values(embed_model="other-space"))
    with pytest.raises(EmbeddingSpaceConflict):
        async with models.open():
            pytest.fail("mismatched convention should fail")
    assert not callers


def test_workspace_save_rolls_back_selection_unless_override_is_allowed(shared_models):
    models = shared_models.models
    with pytest.raises(EmbeddingSpaceConflict):
        models.save("embedding", {"model": "replacement", "dim": 3})
    assert models.repository.read_selections()["embedding"]["model"] == "stored-embed"
    assert models.read_embedding_space()["model"] == "stored-embed"
    document, changed = models.save("embedding", {"model": "replacement", "dim": 3}, can_override_space=True)
    assert changed and document["api_key"] == "****"
    assert models.resolve_for_use("embedding").dim == 3
    models.delete()
    assert models.read_embedding_space()["model"] == "replacement"
    with shared_models.engine.connect() as conn:
        assert len(conn.execute(select(shared_models.providers)).all()) == 4
        assert not conn.execute(select(provider_refs)).all()


def test_repository_checks_active_tenant_ownership_and_never_crosses_users(shared_models):
    other = ModelService(ModelConfigRepository("tenant", "other-user", workspace_id="workspace"))
    other.save("llm", {"provider_id": "chat", "model": "other-chat"})
    assert other.resolve("llm").api_key == "other-user-chat-secret"
    wrong_tenant = ModelService(ModelConfigRepository("wrong", "user", workspace_id="workspace"))
    with pytest.raises(ModelConfigError, match="unavailable"):
        wrong_tenant.resolve("llm")
    with shared_models.engine.begin() as conn:
        conn.execute(shared_models.users.update().where(shared_models.users.c.id == "user").values(status="disabled"))
    with pytest.raises(ModelConfigError, match="unavailable"):
        shared_models.models.save("llm", {"model": "update"})


def test_legacy_fallback_only_exists_without_reference_and_is_retired_atomically(shared_models):
    models = shared_models.models
    with shared_models.engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE cm_user_model_config ADD COLUMN llm_api_key_enc TEXT")
        conn.execute(model_configs.update().values(llm_base_url="https://legacy.example/v1"))
        conn.exec_driver_sql("UPDATE cm_user_model_config SET llm_api_key_enc = ?", (shared_models.encrypt("legacy-secret"),))
        conn.execute(shared_models.providers.delete().where(shared_models.providers.c.provider_id == "chat"))
    with pytest.raises(ModelConfigError, match="configure this Provider"):
        models.resolve("llm")
    with shared_models.engine.begin() as conn:
        conn.execute(provider_refs.delete().where(provider_refs.c.channel == "llm"))
    legacy = models.resolve("llm")
    assert legacy.provider_id is None and legacy.api_key == "legacy-secret"
    models.save("llm", {"provider_id": "embed", "model": "adopted"})
    with shared_models.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT llm_api_key_enc FROM cm_user_model_config WHERE user_id = 'user'").scalar_one() is None
    assert models.resolve("llm").provider_id == "embed"


async def test_probe_failure_is_safe_and_closes_client(shared_models):
    models, callers = shared_models.models, []
    models._llm_factory = caller_factory(callers)
    original = models._llm_factory

    def factory(**options):
        caller = original(**options)
        from context_manager.model import LlmError
        caller.complete.side_effect = LlmError("synthetic-secret")
        return caller

    models._llm_factory = factory
    assert await models.probe("llm", {}) == {"success": False, "message": "Model connection failed"}
    callers[0].aclose.assert_awaited_once()


def usage_event():
    return ModelUsageEvent(
        id="usage-1", user_id="user", tenant_id="tenant", workspace_id="workspace",
        session_id="", chat_id="", source="other", source_step="step",
        model_type="chat", provider_id="chat", model_id="chat-model",
        input_tokens=1, output_tokens=2, cache_read_tokens=0, cache_creation_tokens=0,
        duration_ms=1, created_at=datetime.now(UTC),
    )


def test_usage_repository_is_idempotent_and_does_not_create_host_tables(shared_models):
    usages = ModelUsageRepository()
    usages.probe()
    record = usage_event()
    usages.write([record, record])
    usages.write([record])
    with shared_models.engine.connect() as conn:
        assert len(conn.execute(select(shared_models.usages)).all()) == 1


def test_usage_writer_has_bounded_admission_retries_and_shutdown(caplog):
    entered, release, written = Event(), Event(), Event()
    writes = []

    def probe():
        entered.set()
        assert release.wait(2)

    def write(events):
        writes.append(events)
        if len(writes) == 1:
            raise RuntimeError("synthetic-secret write unavailable")
        written.set()

    recorder = ModelUsageRecorder(SimpleNamespace(probe=probe, write=write),
        queue_size=1, batch_size=1, flush_interval=60, retry_base=0)
    record = usage_event()
    assert not recorder.enqueue(record)
    recorder.start()
    try:
        assert entered.wait(2)
        assert recorder.enqueue(record)
        assert not recorder.enqueue(replace(record, id="usage-2"))
        release.set()
        assert written.wait(2)
        assert recorder.stop(2)
        assert writes == [(record,), (record,)]
        assert recorder.usage_dropped_total == 2
        assert not recorder.enqueue(record)
        assert "synthetic-secret" not in caplog.text
    finally:
        release.set()
        recorder.stop(2)


def test_usage_writer_stops_admission_and_does_not_extend_deadline(caplog):
    entered, release, returned = Event(), Event(), Event()

    def write(events):
        entered.set()
        assert release.wait(2)
        returned.set()

    recorder = ModelUsageRecorder(SimpleNamespace(probe=lambda: None, write=write), batch_size=1)
    recorder.start()
    try:
        assert recorder.enqueue(usage_event())
        assert entered.wait(2)
        assert recorder.enqueue(replace(usage_event(), id="queued"))
        assert not recorder.stop(0)
        assert not recorder.stop(1)
        assert recorder.usage_dropped_total == 1
        assert "shutdown deadline" in caplog.text
    finally:
        release.set()
        assert returned.wait(2)
        recorder._thread.join(2)
        assert recorder.stop(2)


def test_usage_callback_failure_does_not_change_business_result(caplog):
    def enqueue(event):
        raise RuntimeError("synthetic-secret enqueue failure")

    tracker = ModelUsageTracker(enqueue, tenant_id="tenant", workspace_id="workspace", user_id="user", provider_id="chat")
    with bind_usage_context(session_id="session", chat_id="chat"):
        with tracker.track_call("chat", "model") as record:
            assert record({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}) is False
    assert "synthetic-secret" not in caplog.text
