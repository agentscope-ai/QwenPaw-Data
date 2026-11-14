"""Verify generic completion, embedding, validation, and lifecycle behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from context_manager.model import (
    LlmError,
    OpenAIEmbedCaller,
)


def _sdk_client() -> tuple[SimpleNamespace, AsyncMock, AsyncMock]:
    """Build one fully in-memory shape of the SDK endpoints used by the adapter."""
    completion = AsyncMock()
    embedding = AsyncMock()

    async def raw_embedding(**options):
        response = await embedding(**options)
        return SimpleNamespace(parse=lambda: response)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=completion)),
        embeddings=SimpleNamespace(
            with_raw_response=SimpleNamespace(create=raw_embedding)
        ),
    )
    return client, completion, embedding


def _embedding_response(values):
    return SimpleNamespace(
        data=[
            SimpleNamespace(index=index, embedding=vector)
            for index, vector in reversed(tuple(enumerate(values)))
        ]
    )


@pytest.mark.asyncio
async def test_embedding_batches_bound_concurrency_and_restore_global_order():
    sdk, _, embedding = _sdk_client()
    first_release = asyncio.Event()
    completed = []
    active = peak = 0

    async def request(**options):
        nonlocal active, peak
        start = int(options["input"][0])
        active += 1
        peak = max(peak, active)
        try:
            if start == 0:
                await first_release.wait()
            else:
                await asyncio.sleep(0)
            completed.append(start)
            if start == 2:
                first_release.set()
            return _embedding_response([(int(text), 1) for text in options["input"]])
        finally:
            active -= 1

    embedding.side_effect = request
    caller = OpenAIEmbedCaller(
        model="model-a",
        batch_size=2,
        max_concurrency=2,
        client=sdk,
    )
    result = await asyncio.wait_for(
        caller.embed(tuple(str(i) for i in range(13)), dimensions=2), timeout=2
    )

    assert result == tuple((float(i), 1.0) for i in range(13))
    assert peak == 2 and active == 0 and completed[0] == 2
    assert [len(call.kwargs["input"]) for call in embedding.await_args_list] == [
        2,
        2,
        2,
        2,
        2,
        2,
        1,
    ]
    assert all(call.kwargs["model"] == "model-a" for call in embedding.await_args_list)
    assert all(call.kwargs["dimensions"] == 2 for call in embedding.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", (False, True))
async def test_batch_failure_or_cancellation_drains_all_workers(cancel):
    sdk, _, embedding = _sdk_client()
    both_started = asyncio.Event()
    active = calls = 0

    async def request(**options):
        nonlocal active, calls
        calls += 1
        active += 1
        if calls == 2:
            both_started.set()
        try:
            await both_started.wait()
            if options["input"] == ["0"] and not cancel:
                raise RuntimeError("private provider failure")
            await asyncio.Event().wait()
        finally:
            active -= 1

    embedding.side_effect = request
    caller = OpenAIEmbedCaller(
        model="model-a",
        batch_size=1,
        max_concurrency=2,
        client=sdk,
    )
    task = asyncio.create_task(caller.embed(tuple(str(i) for i in range(5))))
    await asyncio.wait_for(both_started.wait(), timeout=2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(LlmError, match="embedding request failed"):
            await asyncio.wait_for(task, timeout=2)
    assert active == 0 and calls == 2
