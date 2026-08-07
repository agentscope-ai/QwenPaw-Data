"""OpenAI 兼容客户端单例：统一 ``timeout``，避免 LLM 请求无限挂起拖死 ``as_completed``。

v3 新增：
- ``complete_json``：JSON Mode 结构化输出 + schema 校验 + 重试（用于决策 LLM）。
"""
from __future__ import annotations

import json
import logging
import time
from functools import lru_cache
from typing import Any, Callable, List, Optional

import httpx
from openai import APITimeoutError, OpenAI

from .config import CFG

log = logging.getLogger("context_manager.openai_client")


def _build_openai_client(
    *, timeout_sec: Optional[float] = None, max_retries: Optional[int] = None,
) -> OpenAI:
    """构造 OpenAI 客户端。``timeout_sec`` 为 ``None`` 时使用 ``CFG.llm_http_timeout``。

    ``max_retries`` 为 ``None`` 时默认 5（基准并发下抗瞬时连接错误）；对延迟敏感、
    可降级的调用（如召回精排）传 0，避免端点抖动时的指数退避把单次调用放大成
    数十秒的重试风暴。
    """
    from .model_config_store import get_model_config_store
    store = get_model_config_store()
    kw: dict[str, Any] = {
        "api_key": store.llm_api_key or CFG.openai_api_key or "sk-none",
        "base_url": store.llm_base_url or CFG.openai_base_url,
        "max_retries": 5 if max_retries is None else max(0, int(max_retries)),
    }
    t = CFG.llm_http_timeout if timeout_sec is None else timeout_sec
    if t is not None and t > 0:
        kw["timeout"] = t
    if CFG.llm_http_ignore_proxy:
        kw["http_client"] = httpx.Client(trust_env=False)
    return OpenAI(**kw)


@lru_cache(maxsize=1)
def get_openai_client() -> OpenAI:
    """进程内复用；``LLM_HTTP_TIMEOUT`` 见 ``CFG.llm_http_timeout``。

    设 ``LLM_HTTP_IGNORE_PROXY=1`` 时为本客户端单独构造 ``httpx.Client(trust_env=False)``，
    调用兼容 API 时不读取 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 等（其它库仍可按环境走代理）。
    """
    return _build_openai_client(timeout_sec=None)


def resolve_llm_model(model: Optional[str] = None) -> str:
    """解析主 LLM 模型名：显式 ``model`` > 运行时配置 store > ``CFG.llm_model``（env 默认）。

    运行时模型配置（``models.json`` / 前端设置页）是权威来源；env 仅作初始 seed 与兜底。
    """
    if model:
        return model
    from .model_config_store import get_model_config_store
    store = get_model_config_store()
    return store.llm_model or CFG.llm_model


def usage_to_dict(usage: Any) -> dict[str, Any]:
    """Serialize ``chat.completions`` usage for JSON traces."""
    if usage is None:
        return {}
    try:
        md = getattr(usage, "model_dump", None)
        if callable(md):
            return md()  # type: ignore[no-any-return]
    except Exception:
        pass
    if isinstance(usage, dict):
        return dict(usage)
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def extract_message_reasoning(msg: Any) -> str:
    """从 ChatCompletion 的 assistant message 提取思考内容（Qwen 等 thinking 模式）。

    与 ``api.planner`` 中逻辑一致；**不包含** ``content`` 里的 JSON/SQL 正文，
    供流式 UI 单独展示「思考」时间线。
    """
    reasoning = ""
    for attr in ("reasoning_content", "reasoning"):
        v = getattr(msg, attr, None)
        if v:
            reasoning = str(v).strip()
            break
    if not reasoning and isinstance(getattr(msg, "model_extra", None), dict):
        reasoning = str(msg.model_extra.get("reasoning_content") or "").strip()
    return reasoning


def _coerce_parsed_for_schema(parsed: Any, schema: dict) -> Any:
    """Repair common structured-output mistakes before jsonschema validate.

    Models sometimes emit a JSON array of prose strings instead of
    ``{"strategy_semantics": "..."}``, or a bare ``["a","b"]`` instead of
    ``{"facets": ["a","b"]}``, or put an array in the string field.
    """
    req = schema.get("required") or []
    props = schema.get("properties") or {}

    if isinstance(parsed, list):
        if len(req) == 1:
            key = req[0]
            spec = props.get(key) or {}
            if spec.get("type") == "array":
                out_list: list[Any] = []
                for x in parsed:
                    if isinstance(x, str) and x.strip():
                        out_list.append(x.strip())
                    elif x is not None:
                        s = str(x).strip()
                        if s:
                            out_list.append(s)
                if out_list:
                    return {key: out_list}
            if spec.get("type") == "string":
                chunks: list[str] = []
                for x in parsed:
                    if isinstance(x, str) and x.strip():
                        chunks.append(x.strip())
                    elif x is not None:
                        s = str(x).strip()
                        if s:
                            chunks.append(s)
                if chunks:
                    joined = "\n\n".join(chunks) if len(chunks) > 1 else chunks[0]
                    return {key: joined}
        return parsed

    if isinstance(parsed, str):
        if len(req) == 1:
            key = req[0]
            spec = props.get(key) or {}
            if spec.get("type") == "string" and parsed.strip():
                return {key: parsed.strip()}
        return parsed

    if isinstance(parsed, dict):
        out = dict(parsed)
        for key, spec in props.items():
            if spec.get("type") != "string":
                continue
            val = out.get(key)
            if isinstance(val, list) and val and all(isinstance(x, str) for x in val):
                parts = [x.strip() for x in val if isinstance(x, str) and x.strip()]
                if parts:
                    out[key] = "\n\n".join(parts) if len(parts) > 1 else parts[0]
        return out

    return parsed


def _messages_with_json_keyword(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """DashScope / Qwen: ``response_format`` json_object requires ``json`` to appear in messages."""
    if any("json" in (m.get("content") or "").lower() for m in messages):
        return messages
    suffix = "\n\nRespond with a valid json object matching the requested schema."
    if not messages:
        return [{"role": "user", "content": "Respond with a valid json object."}]
    out = []
    for i, m in enumerate(messages):
        if i == len(messages) - 1:
            c = (m.get("content") or "") + suffix
            out.append({**m, "content": c})
        else:
            out.append(dict(m))
    return out


def complete_json(
    messages: list[dict[str, str]],
    *,
    json_schema: Optional[dict] = None,
    model: Optional[str] = None,
    max_retries: int = 2,
    temperature: float = 0.0,
    reasoning_capture: Optional[List[str]] = None,
    metadata_out: Optional[dict[str, Any]] = None,
    enable_thinking: Optional[bool] = None,
    raw_coerce: Optional[Callable[[Any], Any]] = None,
    http_timeout: Optional[float] = None,
    client_max_retries: Optional[int] = None,
) -> dict[str, Any]:
    """JSON Mode LLM 调用 + schema 校验 + 重试。

    Args:
        messages:    OpenAI chat messages list (role/content dicts).
        json_schema: Optional dict schema for validation. If None, any valid JSON is accepted.
        model:       Model name override (default: CFG.llm_model).
        max_retries: Number of retry attempts on JSON parse or schema failure.
        temperature: Sampling temperature (0 for deterministic).
        enable_thinking: When not None, passed as ``extra_body.enable_thinking`` (Qwen / DashScope).
        raw_coerce:    Optional hook applied to the parsed JSON object (after ``json.loads``)
            before generic repairs and schema validation. Use for domain-specific key aliases.
        http_timeout:  Optional HTTP read timeout (seconds) for this call only. When set, a
            fresh client is used (not the process singleton). ``None`` uses ``get_openai_client``.

    Returns:
        Parsed dict from LLM response.

    Raises:
        ValueError: If all retries fail to produce valid JSON matching the schema.
    """
    if http_timeout is not None or client_max_retries is not None:
        client = _build_openai_client(
            timeout_sec=http_timeout, max_retries=client_max_retries,
        )
    else:
        client = get_openai_client()
    target_model = resolve_llm_model(model)
    api_messages = _messages_with_json_keyword(messages)

    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        t_start = time.perf_counter()
        try:
            create_kw: dict[str, Any] = {
                "model": target_model,
                "messages": api_messages,  # type: ignore[arg-type]
                "response_format": {"type": "json_object"},
                "temperature": temperature,
            }
            if enable_thinking is not None:
                create_kw["extra_body"] = {"enable_thinking": bool(enable_thinking)}
            resp = client.chat.completions.create(**create_kw)
            elapsed_ms = int((time.perf_counter() - t_start) * 1000)
            msg = resp.choices[0].message
            raw = msg.content or "{}"
            parsed_raw = json.loads(raw)
            if raw_coerce is not None:
                parsed_raw = raw_coerce(parsed_raw)
            if json_schema is not None:
                parsed = _coerce_parsed_for_schema(parsed_raw, json_schema)
                if not isinstance(parsed, dict):
                    raise ValueError(
                        f"Expected JSON object after coercion, got {type(parsed).__name__}"
                    )
                _validate_schema(parsed, json_schema)
            else:
                parsed = parsed_raw

            if reasoning_capture is not None:
                reasoning_capture[:] = [extract_message_reasoning(msg)]

            if metadata_out is not None:
                metadata_out.clear()
                cho = resp.choices[0]
                metadata_out.update(
                    {
                        "elapsed_ms": elapsed_ms,
                        "model": target_model,
                        "attempt": attempt + 1,
                        "usage": usage_to_dict(resp.usage),
                        "raw_content": raw,
                        "reasoning": extract_message_reasoning(msg),
                        "finish_reason": getattr(cho, "finish_reason", None),
                    }
                )

            return parsed

        except (json.JSONDecodeError, ValueError, KeyError, APITimeoutError, httpx.TimeoutException) as exc:
            last_error = exc
            log.warning(
                "complete_json attempt %d/%d failed: %s",
                attempt + 1,
                max_retries + 1,
                exc,
            )

    raise ValueError(
        f"complete_json failed after {max_retries + 1} attempts. Last error: {last_error}"
    )


def _validate_schema(obj: dict, schema: dict) -> None:
    """Lightweight schema validation (required fields + enum values only).

    Falls back gracefully if jsonschema is not installed — just checks required fields.
    """
    try:
        import jsonschema  # type: ignore[import-not-found]
        jsonschema.validate(obj, schema)
        return
    except ImportError:
        pass
    except Exception as exc:
        raise ValueError(f"Schema validation failed: {exc}") from exc

    # Fallback: check required fields only
    for field in schema.get("required") or []:
        if field not in obj:
            raise ValueError(f"Missing required field in LLM response: {field!r}")


__all__ = [
    "complete_json",
    "extract_message_reasoning",
    "get_openai_client",
    "resolve_llm_model",
    "usage_to_dict",
]
