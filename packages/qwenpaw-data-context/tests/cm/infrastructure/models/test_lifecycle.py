"""Verify operation-scoped model configuration and transport ownership."""

import asyncio
from unittest.mock import Mock

import pytest
from context_manager.model import ModelService, ResolvedModel

from cm.infrastructure.models.support import FakeCallerFactory


@pytest.fixture
def clients():
    resolved = ResolvedModel(
        "provider", "model-a", "https://models.invalid/v1", "key-a", 2
    )
    transports = FakeCallerFactory()
    service = ModelService(Mock(), llm_factory=transports, embed_factory=transports)
    service.resolve_pair = Mock(return_value=(resolved, resolved))
    return (
        service,
        transports,
        resolved,
    )


@pytest.mark.asyncio
async def test_cancellation_closes_both_clients(clients):
    factory, transports, _ = clients
    started = asyncio.Event()

    async def operation():
        async with factory.open():
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(client.close_count == 1 for client in transports.callers)
