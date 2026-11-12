# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agentscope.agent._config import ContextConfig

from qwenpaw_data.host.core.agent.middleware.compress_trigger import (
    CompressTriggerMiddleware,
)

_SIZE = 1_000_000


def _middleware(*, trigger_ratio: float = 0.8) -> CompressTriggerMiddleware:
    return CompressTriggerMiddleware(
        run_context=SimpleNamespace(
            user_runtime_config=SimpleNamespace(context_trigger_ratio=trigger_ratio),
        ),
    )


def _agent(
    *,
    estimated: int = 600_000,
    reserve_ratio: float = 0.1,
    context_size: int = _SIZE,
) -> SimpleNamespace:
    return SimpleNamespace(
        context_config=ContextConfig(
            trigger_ratio=0.8,
            reserve_ratio=reserve_ratio,
        ),
        model=SimpleNamespace(
            context_size=context_size,
            count_tokens=AsyncMock(return_value=estimated),
        ),
        _prepare_model_input=AsyncMock(return_value={"messages": [], "tools": None}),
    )


def _response(*, input_tokens: int, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )


async def _passed_config(
    mw: CompressTriggerMiddleware,
    agent: SimpleNamespace,
    incoming: dict[str, Any] | None = None,
) -> Any:
    captured: dict[str, Any] = {}

    async def next_handler(**kwargs: Any) -> None:
        captured.update(kwargs)

    await mw.on_compress_context(agent, incoming or {}, next_handler)
    return captured.get("context_config")


@pytest.mark.asyncio
async def test_no_bill_leaves_config() -> None:
    incoming = ContextConfig(trigger_ratio=0.8, reserve_ratio=0.1)
    passed = await _passed_config(
        _middleware(),
        _agent(),
        {"context_config": incoming},
    )
    assert passed is incoming


@pytest.mark.asyncio
async def test_bill_below_user_trigger_leaves_config() -> None:
    mw = _middleware()
    await mw.on_model_call(
        _agent(),
        {},
        AsyncMock(return_value=_response(input_tokens=700_000, output_tokens=10_000)),
    )
    incoming = ContextConfig(trigger_ratio=0.8, reserve_ratio=0.1)
    passed = await _passed_config(mw, _agent(), {"context_config": incoming})
    assert passed is incoming


@pytest.mark.asyncio
async def test_bill_over_line_swaps_trigger_from_estimated() -> None:
    mw = _middleware()
    agent = _agent(estimated=600_000)
    await mw.on_model_call(
        agent,
        {},
        AsyncMock(return_value=_response(input_tokens=820_000, output_tokens=20_000)),
    )
    passed = await _passed_config(mw, agent)
    assert passed is not None
    assert passed.trigger_ratio == pytest.approx(0.95 * 600_000 / _SIZE)
    assert passed.reserve_ratio == 0.1


@pytest.mark.asyncio
async def test_bill_over_line_clamps_ratio_to_max() -> None:
    mw = _middleware()
    agent = _agent(estimated=960_000)
    await mw.on_model_call(
        agent,
        {},
        AsyncMock(return_value=_response(input_tokens=900_000, output_tokens=20_000)),
    )
    passed = await _passed_config(mw, agent)
    assert passed is not None
    assert passed.trigger_ratio == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_bill_over_line_clamps_ratio_to_reserve() -> None:
    mw = _middleware()
    agent = _agent(estimated=50_000, reserve_ratio=0.1)
    await mw.on_model_call(
        agent,
        {},
        AsyncMock(return_value=_response(input_tokens=820_000, output_tokens=20_000)),
    )
    passed = await _passed_config(mw, agent)
    assert passed is not None
    assert passed.trigger_ratio == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_stream_records_last_chunk_usage() -> None:
    mw = _middleware()
    agent = _agent(estimated=600_000)

    async def stream_handler(**_kwargs: Any):
        async def gen():
            yield _response(input_tokens=1, output_tokens=1)
            yield _response(input_tokens=820_000, output_tokens=20_000)

        return gen()

    result = await mw.on_model_call(agent, {}, stream_handler)
    async for _ in result:
        pass
    passed = await _passed_config(mw, agent)
    assert passed is not None
    assert passed.trigger_ratio == pytest.approx(0.95 * 600_000 / _SIZE)
