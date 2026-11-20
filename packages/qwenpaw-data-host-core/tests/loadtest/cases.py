# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

from tests.loadtest.base import LoadTestCase

LIGHT = "只用一句话回答：2+2 等于几？不要调用任何工具。"
HEAVY_CPU = (
    '调用 Bash，执行：python3 -c "'
    "import time; t=time.time()\n"
    "while time.time()-t<180:\n"
    "    x=sum(i*i for i in range(10000))\n"
    "print('done')\"。"
    "不要解释，直接执行。"
)
HEAVY_STDOUT = (
    '调用 Bash，执行：python3 -c "'
    "import sys,time\n"
    "for i in range(200):\n"
    "    sys.stdout.write(('LINE-%d-'%i)+('x'*4000)+'\\n')\n"
    "    sys.stdout.flush()\n"
    "    time.sleep(0.05)\n"
    "print('done')\"。"
    "不要解释，直接执行。"
)

LIGHT_CONCURRENCY = (1, 4, 8, 16, 32, 64, 128)
HEAVY_CONCURRENCY = (1, 2, 4)
NEIGHBOR_CONCURRENCY = (8, 16)


class LoadCases(LoadTestCase):
    async def test_first_packet_cold(self) -> None:
        """空载首包：连续多次单路轻问数，记录 T0 / T1 / T3。"""
        samples = []
        for i in range(self.repeats):
            batch = await self.run_chats([LIGHT])
            batch[0].index = i
            samples.extend(batch)
        self.dump("first_packet_cold", samples)

    async def test_first_packet_warm(self) -> None:
        """预热首包：先丢弃一路预热，再连续多次单路轻问数。"""
        await self.run_chats([LIGHT])
        samples = []
        for i in range(self.repeats):
            batch = await self.run_chats([LIGHT])
            batch[0].index = i
            samples.extend(batch)
        self.dump("first_packet_warm", samples)

    async def test_light_ramp(self) -> None:
        """轻问数爬坡：按 LIGHT_CONCURRENCY 并发提问，观察时延与终态。"""
        for n in LIGHT_CONCURRENCY:
            samples = await self.run_chats([LIGHT] * n)
            self.dump(f"light_ramp n={n}", samples)

    async def test_heavy_cpu(self) -> None:
        """CPU 爬坡：Bash 占满 CPU 180 秒，按 HEAVY_CONCURRENCY 并发。"""
        for n in HEAVY_CONCURRENCY:
            samples = await self.run_chats([HEAVY_CPU] * n)
            self.dump(f"heavy_cpu n={n}", samples)

    async def test_heavy_stdout(self) -> None:
        """大输出爬坡：Bash 持续写 stdout，按 HEAVY_CONCURRENCY 并发。"""
        for n in HEAVY_CONCURRENCY:
            samples = await self.run_chats([HEAVY_STDOUT] * n)
            self.dump(f"heavy_stdout n={n}", samples)

    async def test_mixed(self) -> None:
        """混合负载：2 路重 CPU 与 4 路轻问数同时运行，看轻问数首包是否被拖住。"""
        prompts = [HEAVY_CPU, HEAVY_CPU, LIGHT, LIGHT, LIGHT, LIGHT]
        samples = await self.run_chats(prompts)
        self.dump("mixed", samples)

    async def test_neighbor_cpu(self) -> None:
        """邻居干扰：1 路 CPU 工具先启动，再打 8 / 16 路轻问数。"""
        await self._neighbor(HEAVY_CPU, "neighbor_cpu")

    async def test_neighbor_stdout(self) -> None:
        """邻居干扰：1 路大输出工具先启动，再打 8 / 16 路轻问数。"""
        await self._neighbor(HEAVY_STDOUT, "neighbor_stdout")

    async def _neighbor(self, heavy_prompt: str, title: str) -> None:
        for n in NEIGHBOR_CONCURRENCY:
            heavy_task = asyncio.create_task(
                self.client.run_chat(0, heavy_prompt, self.hold_seconds)
            )
            started = asyncio.get_running_loop().time()
            while True:
                await asyncio.sleep(0.4)
                if heavy_task.done():
                    break
                if asyncio.get_running_loop().time() - started >= 8:
                    break
            lights = await self.run_chats([LIGHT] * n)
            heavy = await heavy_task
            self.dump(f"{title} heavy+{n}", [heavy, *lights])
