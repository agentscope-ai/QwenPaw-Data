"""LLM 传输层的具体实现。

抽象接口 :class:`~.base.LlmClient` 定义在 ``base.py``；本模块只放可运行实现：
底层 ``model_call`` 与默认客户端 :class:`DefaultLlmClient`。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .base import LlmClient

logger = logging.getLogger(__name__)


def model_call(
    user_prompt: str,
    system_prompt: str = "You are a helpful assistant.",
    temperature: float = 0.8,
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> str:
    """使用 OpenAI 兼容接口调用大模型。"""
    if not api_key:
        return "调用异常：missing api_key"
    try:
        from openai import OpenAI  # type: ignore

        client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        )
        completion = client.chat.completions.create(
            model=model or os.getenv("LLM_MODEL") or "gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"调用异常：{str(e)}"


def extract_json_payload(response: str) -> Any:
    """抽取 fenced/unfenced JSON，并对模型传输错误给出明确异常。"""
    text = (response or "").strip()
    if text.startswith("调用异常"):
        raise ValueError(text)
    fenced = re.search(
        r"```(?:json)?\s*([\s\S]*?)```",
        text,
        flags=re.IGNORECASE,
    )
    if fenced:
        text = fenced.group(1).strip()
    return json.loads(text)


class DefaultLlmClient(LlmClient):
    """基于 :func:`model_call` 的默认传输实现（重试 + prompt/response 落盘）。"""

    def __init__(
        self,
        model: str,
        api_key: str,
        out_dir: str | Path,
        *,
        max_attempts: int = 2,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._out_dir = Path(out_dir) / "llm"
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._max_attempts = max(1, int(max_attempts))
        self._call_index = 0

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.8,
    ) -> str:
        """调用模型；仅对传输异常做通用重试并统一保存请求响应。"""
        self._call_index += 1
        response = ""
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = model_call(
                    user_prompt,
                    system_prompt,
                    temperature,
                    model=self._model,
                    api_key=self._api_key,
                )
                if not (response or "").startswith("调用异常"):
                    break
                last_error = RuntimeError(response)
            except Exception as exc:  # pragma: no cover - SDK defensive edge
                last_error = exc
                response = f"调用异常：{exc}"
            logger.warning(
                f"LLM call attempt {attempt}/{self._max_attempts} failed: {last_error}"
            )

        stem = f"llm_call_{self._call_index:04d}"
        (self._out_dir / f"{stem}_system.txt").write_text(
            system_prompt,
            encoding="utf-8",
        )
        (self._out_dir / f"{stem}_prompt.txt").write_text(
            user_prompt,
            encoding="utf-8",
        )
        (self._out_dir / f"{stem}_response.txt").write_text(
            response or "",
            encoding="utf-8",
        )
        return response
