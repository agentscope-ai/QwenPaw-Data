# -*- coding: utf-8 -*-
"""SettlementConfirmer: after detection, ask CM whether equivalent knowledge exists."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from qwenpaw_data.host.core.algo.biztrace.llm import StructuredLLM

from .calls import StructuredCallError, structured_call
from .cm_utils import SettlementCmClient
from .models import CardType, DetectedItem

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "CONFIRMER.md"


class DuplicateJudgement(BaseModel):
    """LLM duplicate-or-new judgement."""

    duplicate: bool
    reason: str = ""


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _parse_json_payload(text: str) -> Any | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", raw)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def _is_cm_ok(record: dict[str, Any]) -> bool:
    return bool(record) and str(record.get("status", "")).startswith("ok")


def _cm_transport_ok(calls: list[dict[str, Any]]) -> bool:
    """At least one CM call succeeded at the HTTP/envelope layer (incl. no_match)."""
    return any(_is_cm_ok(record) for record in calls)


def _cm_result_useful(
    tool_name: str,
    record: dict[str, Any],
    item: DetectedItem,
) -> bool:
    """Did the primary query return enough signal for a duplicate judgement?

    Conventions (per the CM REST contract):
    - search_metrics: hits are an array; no_match / low_confidence are 200
      objects → fall back
    - get_dimension: a hit is a DimensionDetail; ambiguous is a 200 object;
      404 / missing domain is an error → fall back
    - get_dataset: a hit is a DatasetSchema; ambiguous / not found / target
      column with empty description → fall back
    - search_context: relevance.status == no_match → not useful
    """
    if not _is_cm_ok(record):
        return False
    text = (record.get("result") or "").strip()
    if not text:
        return False

    payload = _parse_json_payload(text)

    if tool_name == "search_metrics":
        if isinstance(payload, list):
            return len(payload) > 0
        if isinstance(payload, dict):
            status = str(payload.get("status") or "").lower()
            # 200 + no_match / low_confidence
            return status not in {"no_match", "low_confidence"}
        return False

    if tool_name == "get_dimension":
        if not isinstance(payload, dict):
            return False
        if payload.get("ambiguous") is True:
            return False
        return bool(str(payload.get("dimension_name") or "").strip())

    if tool_name == "get_dataset":
        if not isinstance(payload, dict):
            return False
        if payload.get("ambiguous") is True:
            return False
        if not str(payload.get("dataset_name") or "").strip():
            return False
        if item.type == CardType.column_meaning:
            col_name = (item.fields.get("column_name") or "").strip()
            if col_name and not _dataset_column_has_desc(payload, col_name):
                return False
        return True

    if tool_name == "search_context":
        return _search_context_useful(payload)

    # Unknown tool: assume useful to avoid a spurious fallback.
    return True


def _search_context_useful(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    relevance = payload.get("relevance")
    if isinstance(relevance, dict):
        if str(relevance.get("status") or "").lower() == "no_match":
            return False
    return True


def _dataset_column_has_desc(payload: dict[str, Any], col_name: str) -> bool:
    """Target column exists and its description is non-empty (CM ColumnMeta.column_name)."""
    columns = payload.get("columns")
    if not isinstance(columns, list) or not columns:
        return False
    target = col_name.lower()
    for col in columns:
        if not isinstance(col, dict):
            continue
        name = str(col.get("column_name") or "").strip()
        if name.lower() != target:
            continue
        return bool(str(col.get("description") or "").strip())
    return False


class SettlementConfirmer:
    """Per-item CM lookup, then per-item LLM duplicate judgement."""

    def __init__(
        self,
        llm: StructuredLLM,
        *,
        cm: SettlementCmClient | None = None,
        datasource_id: str | None = None,
        domain_names: list[str] | None = None,
        concurrency: int = 4,
    ) -> None:
        self._llm = llm
        self._system_prompt = _load_prompt()
        self._datasource_id = datasource_id
        self._domain_names = [d for d in (domain_names or []) if d]
        self._cm = cm or SettlementCmClient(datasource_id=datasource_id)
        if datasource_id and not self._cm.datasource_id:
            self._cm.datasource_id = datasource_id
        self._concurrency = max(1, concurrency)

    async def confirm(self, items: list[DetectedItem]) -> list[DetectedItem]:
        """Return the subset still worth recommending.

        When uncertain, do not recommend: CM unreachable or LLM failure drops
        the item.
        """
        confirmed, _ = await self.confirm_with_details(items)
        return confirmed

    async def confirm_with_details(
        self, items: list[DetectedItem]
    ) -> tuple[list[DetectedItem], list[dict[str, Any]]]:
        """Return (items to recommend, per-item confirmation details)."""
        confirmed: list[DetectedItem] = []
        details: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(self._concurrency)

        async def _confirm_one(item: DetectedItem) -> tuple[DetectedItem, dict[str, Any]]:
            async with sem:
                cm_context, cm_calls = await self._query_cm(item)
                if not cm_context.strip():
                    # CM responded but had no relevant knowledge → treat the
                    # item as new and recommend it. CM entirely failed or no
                    # call was issued → uncertain, do not recommend.
                    if _cm_transport_ok(cm_calls):
                        judgement = DuplicateJudgement(
                            duplicate=False, reason="CM 无有效命中，视为新知识"
                        )
                    else:
                        judgement = DuplicateJudgement(
                            duplicate=True, reason="CM 不可达或调用失败，不确定不推"
                        )
                else:
                    judgement = await self._llm_judge(item, cm_context)
                detail = {
                    "type": item.type,
                    "fields": item.fields,
                    "cm_calls": cm_calls,
                    "cm_context": cm_context[:2000] if cm_context else "",
                    "recommend": not judgement.duplicate,
                    "reason": judgement.reason,
                }
                return item, detail

        results = await asyncio.gather(*[_confirm_one(item) for item in items])
        for item, detail in results:
            details.append(detail)
            if detail["recommend"]:
                confirmed.append(item)
        return confirmed, details

    async def _query_cm(self, item: DetectedItem) -> tuple[str, list[dict[str, Any]]]:
        """Primary tool first; fall back to search_context when it says nothing."""
        tool_name, kwargs, fallback_query = self._build_cm_call(item)
        if not tool_name:
            return "", []

        record = await self._cm.call(tool_name, kwargs)
        calls = [record]
        parts: list[str] = []

        if tool_name == "search_context":
            if _cm_result_useful(tool_name, record, item):
                parts.append(record["result"])
            return "\n---\n".join(parts), calls

        if _cm_result_useful(tool_name, record, item):
            parts.append(record["result"])
        elif fallback_query:
            logger.info(
                "Settlement confirmer: %s not useful (%s), fallback search_context",
                tool_name,
                (record.get("status") or "")[:80],
            )
            metadata = (
                json.dumps({"datasource_id": self._datasource_id})
                if self._datasource_id
                else "{}"
            )
            fallback_record = await self._cm.call(
                "search_context",
                {"query": fallback_query, "metadata": metadata},
            )
            calls.append(fallback_record)
            if _cm_result_useful("search_context", fallback_record, item):
                parts.append(fallback_record["result"])

        return "\n---\n".join(parts), calls

    def _resolve_domain(self, fields: dict[str, str]) -> str | None:
        """Only pass domains on the list_domains allowlist; otherwise omit."""
        raw = (fields.get("domain") or "").strip()
        if not raw or not self._domain_names:
            return None
        if raw in self._domain_names:
            return raw
        lower_map = {d.lower(): d for d in self._domain_names}
        if raw.lower() in lower_map:
            return lower_map[raw.lower()]
        logger.info(
            "Settlement confirmer: drop unknown domain %r (allowed=%s)",
            raw,
            self._domain_names,
        )
        return None

    def _build_cm_call(self, item: DetectedItem) -> tuple[str, dict[str, Any], str]:
        """Return (tool_name, kwargs, fallback_query); empty fallback = no fallback."""
        fields = item.fields
        metadata = json.dumps({"datasource_id": self._datasource_id}) if self._datasource_id else "{}"
        domain = self._resolve_domain(fields)

        if item.type == CardType.metric_caliber:
            kwargs: dict[str, Any] = {"query": fields.get("metric_name", ""), "k": 3, "metadata": metadata}
            if domain:
                kwargs["domain"] = domain
            name = fields.get("metric_name", "")
            caliber = fields.get("caliber", "")
            fallback = f"指标:{name} 口径:{caliber}".strip()
            return "search_metrics", kwargs, fallback

        if item.type == CardType.dimension_def:
            kwargs = {"name": fields.get("dimension_name", ""), "metadata": metadata}
            if domain:
                kwargs["domain"] = domain
            name = fields.get("dimension_name", "")
            col = fields.get("bind_column", "")
            fallback = f"维度:{name} 列:{col}".strip()
            return "get_dimension", kwargs, fallback

        if item.type == CardType.column_meaning:
            kwargs = {"name": fields.get("table", ""), "metadata": metadata}
            if domain:
                kwargs["domain"] = domain
            table = fields.get("table", "")
            col = fields.get("column_name", "")
            meaning = fields.get("meaning", "")
            fallback = f"表:{table} 列:{col} 含义:{meaning}".strip()
            return "get_dataset", kwargs, fallback

        if item.type == CardType.dataset_usage:
            query = f"{fields.get('use_case', '')} {fields.get('recommended_dataset', '')}".strip()
            return "search_context", {"query": query, "metadata": metadata}, ""

        return "", {}, ""

    async def _llm_judge(self, item: DetectedItem, cm_context: str) -> DuplicateJudgement:
        """Ask the LLM whether the item duplicates existing knowledge."""
        user_content = self._build_judge_message(item, cm_context)
        try:
            return await structured_call(
                self._llm,
                system=self._system_prompt,
                user=user_content,
                schema=DuplicateJudgement,
            )
        except StructuredCallError:
            logger.warning(
                "LLM judge failed, treating as duplicate (uncertain → skip)",
                exc_info=True,
            )
            return DuplicateJudgement(
                duplicate=True, reason="LLM 调用失败，不确定不推"
            )

    def _build_judge_message(self, item: DetectedItem, cm_context: str) -> str:
        parts = [
            "## 待沉淀项",
            f"类型: {item.type.value}",
            "字段:",
        ]
        for k, v in item.fields.items():
            parts.append(f"  - {k}: {v}")
        parts.append("")
        parts.append("## 已有知识")
        parts.append(cm_context or "(无)")
        return "\n".join(parts)
