"""LLM 传输层：协议 + OpenAI 兼容默认实现 + 响应缓存 + 审计。

------------------------------------------------------------------------
错误用异常表达，不用字符串前缀
------------------------------------------------------------------------
旧版 ``model_call`` 把所有异常吞成 ``"调用异常：..."`` 前缀串，判据散落在
调用点。本层失败一律抛 :class:`LlmError`——各 handler 决定降级还是放弃。

------------------------------------------------------------------------
落盘全部交给 store
------------------------------------------------------------------------
本层不认识路径。组合根注入一个 :class:`~..stores.TextStore`，写什么、往
哪写由 store 决定，本层只决定 **key 与内容**::

    {digest}.system     system prompt 原文
    {digest}.user       user prompt 原文
    {digest}.response   模型回答原文
    _meta/{digest}      model / temperature / status / attempts / error / created_at

三份正文都是裸 ``.txt``，点开即原文；结构化元信息走 store 已有的
``write_meta`` / ``read_meta`` JSON 侧车，协议一个方法都不用加。

------------------------------------------------------------------------
断点续跑 = 响应缓存 = 审计
------------------------------------------------------------------------
以 ``(model, system, user, temperature)`` 内容哈希为 key。中断重跑时已答过
的调用秒回，替代旧版三套各不相同的 checkpoint 机制；handler 因此保持纯
函数。缓存也给了录制回放测试：录一次真响应，测试永远离线跑。

**审计与缓存是同一份数据。** 缓存命中时直接返回、不再落盘，所以一个
digest 至多对应一次真实 API 调用——旧版按运行序号命名的
``llm_call_{NNNN}_*`` 会随每次重跑不断累积（61 份缓存曾对应 183 个审计
文件），以 digest 为键之后重跑是覆盖，不再堆积。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from ..stores import TextStore

logger = logging.getLogger(__name__)


class LlmError(RuntimeError):
    """LLM 调用在传输层重试后仍失败。"""


@runtime_checkable
class AsyncLlmClient(Protocol):
    """算法层看到的唯一 LLM 形状。失败抛 :class:`LlmError`。"""

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
    ) -> str: ...


def extract_json_payload(response: str) -> Any:
    """抽取 fenced/unfenced JSON。解析失败抛 ``ValueError``/``JSONDecodeError``。"""
    text = (response or "").strip()
    fenced = re.search(
        r"```(?:json)?\s*([\s\S]*?)```",
        text,
        flags=re.IGNORECASE,
    )
    if fenced:
        text = fenced.group(1).strip()
    return json.loads(text)


class OpenAiCompatClient:
    """OpenAI 兼容接口的默认实现（同步 SDK 下线程 + 重试 + 缓存 + 审计）。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        store: TextStore | None = None,
        max_attempts: int = 2,
    ) -> None:
        """``store=None`` 时不缓存也不留痕，每次都真调——单测与一次性跑批用。"""
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._max_attempts = max(1, int(max_attempts))
        self._store = store

    # -- 缓存 key --------------------------------------------------------

    def _digest(self, system_prompt: str, user_prompt: str, temperature: float) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "model": self._model,
                    "system": system_prompt,
                    "user": user_prompt,
                    "temperature": temperature,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    async def _cached(self, digest: str) -> str | None:
        """命中的判据是 **meta 里 status=ok**，不只是正文存在。

        失败的调用也留正文（写的是 ``<failed: ...>``，便于排查当时发生了
        什么），所以只看正文会把失败当成答案。
        """
        if self._store is None:
            return None
        meta = await self._store.read_meta(digest)
        if not meta or meta.get("status") != "ok":
            return None
        return await self._store.read(f"{digest}.response")

    async def _record(
        self,
        digest: str,
        system_prompt: str,
        user_prompt: str,
        response: str,
        *,
        status: str,
        attempts: int,
        error: str | None,
        temperature: float,
    ) -> None:
        """落盘失败不抛——已经拿到的答案不该因为留不下痕迹而丢掉。"""
        if self._store is None:
            return
        try:
            await asyncio.gather(
                self._store.write(f"{digest}.system", system_prompt),
                self._store.write(f"{digest}.user", user_prompt),
                self._store.write(f"{digest}.response", response),
            )
            await self._store.write_meta(
                digest,
                {
                    "model": self._model,
                    "temperature": temperature,
                    "status": status,
                    "attempts": attempts,
                    "error": error,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:
            logger.warning("LLM 落盘失败（不影响本次结果）: %s", exc)

    # -- 调用 ------------------------------------------------------------

    def _call_sync(
        self, system_prompt: str, user_prompt: str, temperature: float
    ) -> str:
        from openai import OpenAI  # 延迟导入：无 openai 依赖也能 import 本模块

        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        completion = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
        )
        content = completion.choices[0].message.content
        if not content:
            raise LlmError("模型返回空内容")
        return content

    async def _call_async(
        self, system_prompt: str, user_prompt: str, temperature: float
    ) -> str:
        return await asyncio.to_thread(
            self._call_sync, system_prompt, user_prompt, temperature
        )

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
    ) -> str:
        digest = self._digest(system_prompt, user_prompt, temperature)
        cached = await self._cached(digest)
        if cached is not None:
            logger.info("LLM cache hit: %s", digest[:16])
            return cached

        last_error: Exception | None = None
        response: str | None = None
        attempts = 0
        for attempt in range(1, self._max_attempts + 1):
            attempts = attempt
            try:
                response = await self._call_async(
                    system_prompt, user_prompt, temperature
                )
                break
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "LLM call attempt %s/%s failed: %s",
                    attempt,
                    self._max_attempts,
                    exc,
                )

        await self._record(
            digest,
            system_prompt,
            user_prompt,
            response if response is not None else f"<failed: {last_error}>",
            status="ok" if response is not None else "failed",
            attempts=attempts,
            error=None if response is not None else str(last_error),
            temperature=temperature,
        )
        if response is None:
            raise LlmError(
                f"LLM 调用失败（{self._max_attempts} 次）: {last_error}"
            ) from last_error
        return response


__all__ = ["AsyncLlmClient", "LlmError", "OpenAiCompatClient", "extract_json_payload"]
