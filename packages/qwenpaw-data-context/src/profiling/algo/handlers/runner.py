"""LLM 单元执行器：有界并发 + 取消 + 逐单元容错。

一个「单元」是一次独立的 LLM 调用（CALIBER 的一张表、DATASET/DICT 的一批、
DICT 归一的一个域）。旧版全部串行，130+ 张表就是几十分钟；这里以
``asyncio.Semaphore`` 有界并发，``raise_if_cancelled`` 打在每个单元之前——
「只在任务边界查一次等于不可取消」。

runner 不碰 JSON 与校验——解析、校验、降级是各 handler 的任务语义。
单元失败（含传输重试耗尽）返回 ``None``，由调用方决定降级路径。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Sequence

from .llm import AsyncLlmClient

logger = logging.getLogger(__name__)


def _noop_progress(pct: int, message: str) -> None:
    return None


@dataclass(frozen=True)
class LlmUnit:
    """一次独立 LLM 调用的完整描述。``label`` 只用于日志。"""

    label: str
    system_prompt: str
    user_prompt: str


class LlmRunner:
    def __init__(
        self,
        llm: AsyncLlmClient,
        *,
        temperature: float = 0.2,
        concurrency: int = 4,
        raise_if_cancelled: Callable[[], None] = lambda: None,
        report_progress: Callable[[int, str], None] = _noop_progress,
    ) -> None:
        self._llm = llm
        self._temperature = temperature
        self._semaphore = asyncio.Semaphore(max(1, int(concurrency)))
        self._raise_if_cancelled = raise_if_cancelled
        self._report_progress = report_progress

    async def run_one(self, unit: LlmUnit) -> str:
        """执行单个单元；传输层失败向上抛。"""
        self._raise_if_cancelled()
        async with self._semaphore:
            return await self._llm.complete(
                unit.system_prompt,
                unit.user_prompt,
                temperature=self._temperature,
            )

    async def run_units(self, units: Sequence[LlmUnit]) -> list[str | None]:
        """并发执行全部单元，结果按输入顺序对齐；失败的单元记 ``None``。"""
        total = len(units)
        done = 0

        async def _guarded(index: int, unit: LlmUnit) -> tuple[int, str | None]:
            nonlocal done
            response: str | None
            if total:
                self._report_progress(
                    int(done / total * 100),
                    f"LLM unit {unit.label} ({index + 1}/{total})",
                )
            try:
                response = await self.run_one(unit)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("LLM unit %s failed: %s", unit.label, exc)
                response = None
            else:
                logger.info("LLM unit %s done (%s/%s)", unit.label, index + 1, total)
            done += 1
            if total:
                self._report_progress(
                    int(done / total * 100),
                    f"LLM unit {unit.label} done ({done}/{total})",
                )
            return index, response

        results: list[str | None] = [None] * len(units)
        for index, response in await asyncio.gather(
            *(_guarded(i, unit) for i, unit in enumerate(units))
        ):
            results[index] = response
        return results


__all__ = ["LlmRunner", "LlmUnit"]
