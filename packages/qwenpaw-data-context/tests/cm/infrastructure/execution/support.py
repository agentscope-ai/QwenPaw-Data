"""Run service units directly in tests that do not exercise execution budgets."""

import asyncio
from inspect import iscoroutinefunction
from types import SimpleNamespace


async def run(operation, /, *args, **kwargs):
    if iscoroutinefunction(operation):
        return await operation(*args, **kwargs)
    return await asyncio.to_thread(operation, *args, **kwargs)


executor = SimpleNamespace(run=run, run_sync=asyncio.to_thread)
