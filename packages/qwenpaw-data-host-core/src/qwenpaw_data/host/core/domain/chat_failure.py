# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ChatFailureCode = Literal[
    "MODEL_AUTH",
    "MODEL_QUOTA",
    "MODEL_RATE_LIMIT",
    "MODEL_NOT_FOUND",
    "CONTEXT_OVERFLOW",
    "REACT_MAX_ITERS",
    "MODEL_UNAVAILABLE",
    "CONFIG",
    "INTERNAL",
    "VALIDATION",
]

_MESSAGES: dict[ChatFailureCode, str] = {
    "MODEL_AUTH": "模型密钥无效或没有权限，请到智能体配置里检查 API Key。",
    "MODEL_QUOTA": "模型额度不足或已欠费，请检查账号配额后重试。",
    "MODEL_RATE_LIMIT": "调用太频繁，请稍后再试。",
    "MODEL_NOT_FOUND": "当前模型不可用，请在配置里换一个模型。",
    "CONTEXT_OVERFLOW": "这次对话太长，超出模型上下文。请新开会话，或缩短输入后再试。",
    "REACT_MAX_ITERS": "已达到最大 ReAct 循环次数，本轮执行已停止。请在智能体配置中调整循环上限后重试。",
    "MODEL_UNAVAILABLE": "模型服务暂时不可用，请稍后重试。",
    "CONFIG": "模型还没配置完整，请先到智能体配置里补齐。",
    "INTERNAL": "执行失败，请稍后重试。",
    "VALIDATION": "执行失败，请稍后重试。",
}

_HINTS: tuple[tuple[ChatFailureCode, tuple[str, ...]], ...] = (
    ("CONTEXT_OVERFLOW", ("context length", "context window", "too many tokens")),
    ("MODEL_QUOTA", ("quota", "arrearage", "402")),
    ("MODEL_RATE_LIMIT", ("rate limit", "too many requests", "429")),
    ("MODEL_AUTH", ("api key", "apikey", "unauthorized", "401", "403")),
    ("MODEL_NOT_FOUND", ("model not found", "invalid model")),
    ("CONFIG", ("not configured", "unsupported provider")),
    ("MODEL_UNAVAILABLE", ("timeout", "timed out", "502", "503", "504")),
)


@dataclass(frozen=True)
class ChatFailure:
    code: ChatFailureCode
    message: str
    cause: str

    def to_dict(self) -> dict[str, Any]:
        if not self.cause.strip():
            raise ValueError("failure cause is required")
        return {
            "code": self.code,
            "message": self.message,
            "details": {"cause": self.cause},
        }

    @classmethod
    def from_exception(cls, exc: BaseException) -> ChatFailure:
        if isinstance(exc, BaseExceptionGroup):
            return cls.from_exception(exc.exceptions[0])
        cause = str(exc).strip() or type(exc).__name__
        text = f"{type(exc).__name__} {cause}".lower().replace("_", " ")
        status = getattr(exc, "status_code", None)
        if status is not None:
            text = f"{text} {status}"
        for code, hints in _HINTS:
            if any(hint in text for hint in hints):
                return cls(code, _MESSAGES[code], cause)
        return cls("INTERNAL", _MESSAGES["INTERNAL"], cause)

    @classmethod
    def internal(cls, cause: str) -> ChatFailure:
        text = cause.strip()
        if not text:
            raise ValueError("failure cause is required")
        return cls("INTERNAL", _MESSAGES["INTERNAL"], text)

    @classmethod
    def iteration_limit(cls, agent_name: str) -> ChatFailure:
        return cls(
            "REACT_MAX_ITERS",
            _MESSAGES["REACT_MAX_ITERS"],
            f"Agent '{agent_name}' has reached the maximum number of iterations.",
        )
