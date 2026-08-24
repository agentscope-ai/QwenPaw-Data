"""L1 search_context path_hint synthesis over the multi-hop subgraph."""
from __future__ import annotations

import logging
from typing import Any

from ...openai_client import complete_json, resolve_llm_model

log = logging.getLogger("context_manager.runtime.synthesis.path_hint")

PATH_HINT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"path_hint": {"type": "string"}},
    "required": ["path_hint"],
}

_SYSTEM = """You are a data-analysis path planner for a Chinese BI agent.
Given a question and a multi-hop subgraph (MG metrics/tables/columns, TG
experience, KG constraints), write ONE Chinese sentence describing the analysis
path: anchored domain, the metric->table->column hop chain, a concrete how-to-fetch
suggestion, and any caliber/partition caveat. Output ONE JSON object only:
{"path_hint": "..."}. No markdown, no extra text. Do NOT invent entities absent
from the subgraph."""

_USER_TEMPLATE = """问题：{query}
已锚定业务域：{domain}
时间提示：{time_hints}

多跳子图：
{subgraph_text}

请输出 path_hint。"""


def synthesize_path_hint(
    *,
    subgraph_text: str,
    domain: str,
    query: str,
    time_hints: list[str],
    fallback: str,
    cfg: dict[str, Any],
) -> str:
    """Return an LLM-synthesized path_hint, or `fallback` on disable/empty/error."""
    if not cfg.get("enabled", True):
        return fallback
    if not (subgraph_text or "").strip():
        return fallback
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _USER_TEMPLATE.format(
            query=query or "",
            domain=domain or "(未锚定)",
            time_hints=", ".join(time_hints) if time_hints else "(none)",
            subgraph_text=subgraph_text,
        )},
    ]
    from ...config import CFG

    try:
        parsed = complete_json(
            messages,
            json_schema=PATH_HINT_JSON_SCHEMA,
            # One-sentence NL synthesis; an explicit ``synthesis.path_hint.model``
            # in config still wins, otherwise fall back to the main LLM model.
            model=resolve_llm_model(cfg.get("model")),
            max_retries=1,
            temperature=float(cfg.get("temperature", 0.2)),
            http_timeout=float(cfg.get("timeout_sec", 12.0)),
            enable_thinking=False,
        )
    except Exception as exc:  # noqa: BLE001 - never crash search_context
        log.warning("synthesize_path_hint failed: %s", exc)
        return fallback
    out = str(parsed.get("path_hint") or "").strip()
    return out or fallback
