# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import os
import statistics
import unittest

from tests.loadtest.client import ChatSample, HostClient


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise unittest.SkipTest(f"missing {name}")
    return value


def _pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return int(ordered[lo] * (1 - frac) + ordered[hi] * frac)


class LoadTestCase(unittest.IsolatedAsyncioTestCase):
    client: HostClient
    hold_seconds: float
    repeats: int

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = HostClient(
            base=_require("QWENPAW_DATA_API_BASE"),
            prefix=os.environ.get("QWENPAW_DATA_API_PREFIX", "/console/api").rstrip("/")
            or "/console/api",
            cookie=_require("QWENPAW_DATA_ACCESS"),
            datasource_id=_require("QWENPAW_DATA_DATASOURCE_ID"),
        )
        cls.hold_seconds = float(os.environ.get("QWENPAW_DATA_HOLD_SECONDS", "180"))
        cls.repeats = int(os.environ.get("QWENPAW_DATA_REPEATS", "10"))

    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.run(cls.client.aclose())

    async def asyncSetUp(self) -> None:
        await self.client.allow_bash()

    async def run_chats(self, prompts: list[str]) -> list[ChatSample]:
        tasks = [
            self.client.run_chat(i, prompt, self.hold_seconds)
            for i, prompt in enumerate(prompts)
        ]
        return list(await asyncio.gather(*tasks))

    def dump(self, title: str, samples: list[ChatSample]) -> None:
        t0 = [s.t0_ms for s in samples if s.t0_ms is not None]
        t1 = [s.t1_ms for s in samples if s.t1_ms is not None]
        tmcp = [s.tmcp_ms for s in samples if s.tmcp_ms is not None]
        tllm = [s.tllm_ms for s in samples if s.tllm_ms is not None]
        t3 = [s.t3_ms for s in samples if s.t3_ms is not None]
        tend = [
            s.tend_ms
            for s in samples
            if s.status == "completed" and s.tend_ms is not None
        ]
        print(f"\n== {title} n={len(samples)} ==")
        print(
            f"T0 p50={_pct(t0, 0.5)} p95={_pct(t0, 0.95)} "
            f"T1 p50={_pct(t1, 0.5)} p95={_pct(t1, 0.95)} "
            f"Tmcp p50={_pct(tmcp, 0.5)} p95={_pct(tmcp, 0.95)} "
            f"Tllm p50={_pct(tllm, 0.5)} p95={_pct(tllm, 0.95)} "
            f"T3 p50={_pct(t3, 0.5)} p95={_pct(t3, 0.95)} "
            f"Tend p50={_pct(tend, 0.5)} p95={_pct(tend, 0.95)} completed={len(tend)}"
        )
        if t0:
            print(f"T0 min={min(t0)} max={max(t0)} mean={int(statistics.mean(t0))}")
        if t1:
            print(f"T1 min={min(t1)} max={max(t1)} mean={int(statistics.mean(t1))}")
        if tmcp:
            print(
                f"Tmcp min={min(tmcp)} max={max(tmcp)} mean={int(statistics.mean(tmcp))}"
            )
        if tllm:
            print(
                f"Tllm min={min(tllm)} max={max(tllm)} mean={int(statistics.mean(tllm))}"
            )
        if t3:
            print(f"T3 min={min(t3)} max={max(t3)} mean={int(statistics.mean(t3))}")
        if tend:
            print(
                f"Tend min={min(tend)} max={max(tend)} mean={int(statistics.mean(tend))}"
            )
        for s in samples:
            extra = f" err={s.error}" if s.error else ""
            plugins = ""
            if s.plugin_calls:
                plugins = " plugins=" + ",".join(
                    f"{c.name}:{c.t_ms}" for c in s.plugin_calls
                )
            print(
                f"  [{s.index}] http={s.http_status} status={s.status} "
                f"t0={s.t0_ms} t1={s.t1_ms} tmcp={s.tmcp_ms} tllm={s.tllm_ms} "
                f"t3={s.t3_ms} tend={s.tend_ms} chat={s.chat_id}{plugins}{extra}"
            )
