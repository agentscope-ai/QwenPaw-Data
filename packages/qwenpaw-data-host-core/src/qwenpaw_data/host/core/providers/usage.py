# -*- coding: utf-8 -*-
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from agentscope.model import ChatModelBase, ChatResponse, ChatUsage

from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.domain.model_usage import ModelUsageContext
from qwenpaw_data.host.core.repositories.model_usage import ModelUsageRepository


def attach_model_usage(
    model: ChatModelBase,
    ctx: ModelUsageContext,
    *,
    provider_id: str,
) -> ChatModelBase:
    original = model._call_api

    async def tracked(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        if hasattr(result, "__aiter__"):
            return _track_stream(result, ctx, provider_id, model.model)
        await _persist(ctx, provider_id, model.model, result.usage)
        return result

    model._call_api = tracked  # type: ignore[method-assign]
    return model


async def _track_stream(
    result: AsyncGenerator[ChatResponse, None],
    ctx: ModelUsageContext,
    provider_id: str,
    model_id: str,
) -> AsyncGenerator[ChatResponse, None]:
    seen: ChatUsage | None = None
    async for chunk in result:
        if chunk.usage is not None:
            seen = chunk.usage
        yield chunk
    await _persist(ctx, provider_id, model_id, seen)


async def _persist(
    ctx: ModelUsageContext,
    provider_id: str,
    model_id: str,
    usage: ChatUsage | None,
) -> None:
    if usage is None:
        raise ValueError("model response missing usage")
    async with get_session_factory()() as db:
        await ModelUsageRepository(db, ctx.identity).add(
            ctx,
            provider_id=provider_id,
            model_id=model_id,
            input_tokens=int(usage.input_tokens),
            output_tokens=int(usage.output_tokens),
            duration_ms=int(usage.time * 1000),
            cache_read_tokens=int(usage.cache_input_tokens),
            cache_creation_tokens=int(usage.cache_creation_input_tokens),
        )
        await db.commit()
