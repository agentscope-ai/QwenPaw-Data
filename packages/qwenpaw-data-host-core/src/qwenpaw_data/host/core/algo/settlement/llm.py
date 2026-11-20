# -*- coding: utf-8 -*-
"""Settlement 模型工具。

- ``for_structured_calls``: 将 ChatModelBase 调整为结构化抽取模式
- ``structured_call``: 带超时和重试的 generate_structured_output 封装
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from enum import StrEnum

from pydantic import BaseModel
from tenacity import (
    retry,
    stop_after_attempt,
    retry_if_exception_type,
    before_sleep_log,
)

if TYPE_CHECKING:
    from agentscope.model import ChatModelBase

logger = logging.getLogger(__name__)

# Thinking 必须关：Qwen3 在 thinking 模式下会拒绝 forced tool_choice，
# 而 generate_structured_output 依赖该路径；重试也会把超时叠成 ~60s。
_CALL_PROFILE: dict[str, Any] = {"thinking_enable": False, "temperature": 0.0}

DEFAULT_ATTEMPTS = 2
DEFAULT_TIMEOUT = 30.0


class StructuredCallError(RuntimeError):
    """generate_structured_output 多次尝试仍无法返回合规结果。"""


def for_structured_calls(model: "ChatModelBase") -> "ChatModelBase":
    """将模型调整为适合结构化抽取的模式：非流式、关闭 thinking。"""
    model.stream = False
    fields = type(model.parameters).model_fields
    update = {k: v for k, v in _CALL_PROFILE.items() if k in fields}
    if update:
        model.parameters = model.parameters.model_copy(update=update)

    # OpenAIChatModel 走 DashScope compatible 时，parameters.thinking_enable
    # 不会映射成 enable_thinking；Qwen3 默认开 thinking，必须写进 extra_body。
    base_url = ""
    cred = getattr(model, "credential", None)
    if cred is not None:
        base_url = str(getattr(cred, "base_url", "") or "")
    model_type = str(getattr(model, "type", "") or "")
    if "dashscope" in base_url.lower() or model_type == "dashscope":
        body = dict(getattr(model, "extra_body", None) or {})
        body["enable_thinking"] = False
        model.extra_body = body
    return model


async def structured_call(
    model: "ChatModelBase",
    messages: list[Any],
    schema: type[BaseModel],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT,
    source_step: str | StrEnum | None = None,
) -> BaseModel:
    """调用 generate_structured_output，失败时重试。

    返回 validated Pydantic 实例；全部尝试失败抛 StructuredCallError。
    """
    json_schema = schema.model_json_schema()

    @retry(
        stop=stop_after_attempt(attempts),
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger, logging.DEBUG),
        reraise=True,
    )
    async def _call() -> BaseModel:
        response = await asyncio.wait_for(
            model.generate_structured_output(
                messages=messages,
                structured_model=json_schema,
                **({"source_step": source_step} if source_step else {}),
            ),
            timeout=timeout,
        )
        payload = response.content
        if not isinstance(payload, dict):
            raise ValueError(f"response is not a dict: {type(payload)}")
        return schema.model_validate(payload)

    try:
        return await _call()
    except Exception as exc:
        raise StructuredCallError(
            f"{schema.__name__} generation failed after {attempts} attempts: {exc}"
        ) from exc
