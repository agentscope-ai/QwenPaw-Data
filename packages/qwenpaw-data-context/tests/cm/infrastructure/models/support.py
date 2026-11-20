"""Synthetic Host database and model transports for isolated CM tests."""

import base64
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

from context_manager.db import initialize_database
from context_manager.model import (
    LlmCaller,
    ModelConfigRepository,
    ModelService,
    TextEmbedder,
)
from context_manager.settings.db import DbSettings
from cryptography.fernet import Fernet
from sqlalchemy import Column, MetaData, Table, Text


class StaticModelService(ModelService):
    """Return fixed test clients through the operation-scoped client boundary."""

    def __init__(
        self,
        llm: LlmCaller,
        embedder: TextEmbedder | None = None,
        *,
        tenant_id: str = "tenant-1",
        user_id: str = "user-1",
        workspace_id: str = "workspace-1",
        ready: bool = True,
    ) -> None:
        """Bind deterministic clients without reading persisted configuration."""
        resolved_embedder = llm if embedder is None else embedder
        if not isinstance(llm, LlmCaller):
            raise TypeError("llm must implement LlmCaller")
        if not isinstance(resolved_embedder, TextEmbedder):
            raise TypeError("embedder must implement TextEmbedder")
        self.target = (tenant_id, user_id)
        self.workspace_id = workspace_id
        self.llm_client = llm
        self.embedder_client = resolved_embedder
        self._ready = ready
        self.close_count = 0

    @asynccontextmanager
    async def open(self):
        llm, embedder = self.llm_client, self.embedder_client
        try:
            yield llm, embedder
        finally:
            self.close_count += 1
            await llm.aclose()
            if embedder is not llm:
                await embedder.aclose()

    @asynccontextmanager
    async def llm(self, config=None):
        try:
            yield self.llm_client
        finally:
            await self.llm_client.aclose()

    @asynccontextmanager
    async def embedder(self, config=None):
        try:
            yield self.embedder_client
        finally:
            await self.embedder_client.aclose()

    @property
    def ready(self):
        return self._ready


def mock_model_service(llm=None, embedder=None, *, selections=None, error=None):
    """Stub client acquisition for entry-point tests that mock model execution."""
    factory = Mock(spec=ModelService)
    factory.repository = Mock()
    factory.repository.read_selections.return_value = selections or {}
    for name, value in (
        ("open", (llm, embedder)),
        ("llm", llm),
        ("embedder", embedder),
    ):
        context = AsyncMock()
        context.__aenter__.return_value = value
        context.__aenter__.side_effect = error
        getattr(factory, name).return_value = context
    return factory


def create_host_db(tmp_path, monkeypatch):
    secret = "12" * 32
    monkeypatch.setenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", secret)
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-environment-key")
    dsn = f"sqlite:///{tmp_path / 'host.db'}"
    engine = initialize_database(DbSettings(url=dsn)).engine
    schema = MetaData()
    users = Table(
        "users",
        schema,
        Column("id", Text, primary_key=True),
        Column("tenant_id", Text),
        Column("status", Text),
    )
    providers = Table(
        "user_providers",
        schema,
        Column("user_id", Text, primary_key=True),
        Column("provider_id", Text, primary_key=True),
        Column("api_key_enc", Text),
        Column("base_url", Text),
    )
    schema.create_all(engine)

    def encrypt(key):
        return (
            "ENC:"
            + Fernet(base64.urlsafe_b64encode(bytes.fromhex(secret)))
            .encrypt(key.encode())
            .decode()
        )

    with engine.begin() as conn:
        for user in ("user-1", "user-2"):
            conn.execute(
                users.insert().values(id=user, tenant_id="tenant-1", status="active")
            )
            for channel in ("chat", "embed"):
                conn.execute(
                    providers.insert().values(
                        user_id=user,
                        provider_id=channel,
                        api_key_enc=encrypt(f"{user}-{channel}-key"),
                        base_url="https://models.example/v1",
                    )
                )
    return dsn, engine, providers, encrypt


def configure_models(target, *, workspace_id="workspace-1"):
    config = ModelService(ModelConfigRepository(*target, workspace_id=workspace_id))
    config.save("llm", {"provider_id": "chat", "model": "qwen-plus"})
    config.save(
        "embedding",
        {"provider_id": "embed", "model": "embedding-v3", "dim": 1024},
    )


class FakeCaller:
    """Capture model calls without contacting a Provider."""

    def __init__(self, **options):
        self.options = options
        self.complete_calls = []
        self.embed_calls = []
        self.close_count = 0

    async def complete(self, messages, **options):
        self.complete_calls.append((messages, options))
        return "pong"

    async def embed(self, texts, **options):
        options["dimensions"] = (
            options.get("dimensions") or self.options.get("dimensions") or 3
        )
        self.embed_calls.append((texts, options))
        return tuple((1.0,) * options["dimensions"] for _ in texts)

    async def aclose(self):
        self.close_count += 1


class FakeCallerFactory:
    def __init__(self):
        self.callers = []

    def __call__(self, **options):
        caller = OwnedFakeCaller(**options)
        self.callers.append(caller)
        return caller


class OwnedFakeCaller(FakeCaller):
    """Reject use after closure when testing operation-owned transports."""

    async def complete(self, messages, **options):
        assert not self.close_count, "completion after client closure"
        return await super().complete(messages, **options)

    async def embed(self, texts, **options):
        assert not self.close_count, "embedding after client closure"
        return await super().embed(texts, **options)
