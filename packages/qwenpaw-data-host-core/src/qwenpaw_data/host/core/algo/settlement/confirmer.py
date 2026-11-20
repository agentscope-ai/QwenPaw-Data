# -*- coding: utf-8 -*-
"""SettlementConfirmer：检测后调 CM 确认，过滤掉语义层已有的等价知识。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from agentscope.message import SystemMsg, UserMsg
from agentscope.model import ChatModelBase
from qwenpaw_data.tracing.core.names import Step
from pydantic import BaseModel

from .cm_utils import SettlementCmClient
from .llm import StructuredCallError, structured_call
from .models import CardType, DetectedItem
from .retriever import SettlementRetriever

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "CONFIRMER.md"


class DuplicateJudgement(BaseModel):
    """LLM 去重判断结果。"""

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
        return None


def _is_cm_ok(record: dict[str, Any]) -> bool:
    return bool(record) and str(record.get("status", "")).startswith("ok")


def _cm_result_useful(
    tool_name: str,
    payload: Any,
    item: DetectedItem,
) -> bool:
    """主查询是否返回了足以做去重判断的有效信息。

    约定（按 CM REST 契约）：
    - search_metrics: 命中为数组；no_match / low_confidence 为 200 对象 → 需 fallback
    - get_dimension: 命中为 DimensionDetail；ambiguous 为 200 对象；404/域缺失为 error → 需 fallback
    - get_dataset: 命中为 DatasetSchema；ambiguous / not found / 目标列 description 空 → 需 fallback
    """
    if tool_name == "search_metrics":
        return (
            isinstance(payload, list)
            and bool(payload)
            and all(
                isinstance(row, dict)
                and row.get("metric_name")
                and str(row.get("description") or "").strip()
                for row in payload
            )
        )

    if tool_name == "get_dimension":
        if not isinstance(payload, dict):
            return False
        if payload.get("ambiguous") is True:
            return False
        return bool(payload.get("dimension_name")) and bool(
            payload.get("description") or payload.get("calculate_expr")
        )

    if tool_name == "get_dataset":
        column = _dataset_target_column(payload, item)
        return column is not None and bool(str(column.get("description") or "").strip())

    return False


def _dataset_target_column(payload: Any, item: DetectedItem) -> dict[str, Any] | None:
    """区分目标列的描述为空与目标本身尚未定位。"""
    if not isinstance(payload, dict) or payload.get("ambiguous") is True:
        return None
    names = [payload.get("dataset_name"), *(payload.get("physical_tables") or [])]
    if item.fields["table"].casefold() not in {str(name).casefold() for name in names}:
        return None
    columns = payload.get("columns")
    if not isinstance(columns, list):
        return None
    return next(
        (
            col
            for col in columns
            if isinstance(col, dict)
            and str(col.get("column_name") or "").casefold()
            == item.fields["column_name"].casefold()
        ),
        None,
    )


def _format_knowledge(context: dict[str, Any]) -> str:
    """将检索结果整理为知识内容，接口封装只留在调用记录中。"""

    metadata = context["source"] == "http"
    scope = context["scope"]
    knowledge = (
        json.dumps(context["data"], ensure_ascii=False, indent=2)
        if metadata
        else context["data"]
    )
    return "\n".join(
        [
            f"知识来源：{'共享知识' if metadata else '个人知识'}",
            f"检索业务域：{scope['domain']}",
            f"检索数据源：{scope['datasource_id'] or '未指定'}",
            "",
            knowledge,
        ]
    )


class SettlementConfirmer:
    """检测后 CM 去重确认：逐条查 CM → 逐条 LLM 判断。"""

    def __init__(
        self,
        model: ChatModelBase,
        *,
        session_id: str,
        chat_id: str,
        cm: SettlementCmClient | None = None,
        cm_client: SettlementCmClient | None = None,
        datasource_id: str | None = None,
        domain_names: list[str] | None = None,
        concurrency: int = 4,
        retrieval_max_iters: int = 8,
        cm_timeout: float = 180.0,
    ) -> None:
        self._model = model
        self._system_prompt = _load_prompt()
        self._datasource_id = datasource_id
        self._domain_names = [d for d in (domain_names or []) if d]
        # cm_client 兼容旧参数名
        self._cm = cm or cm_client or SettlementCmClient(datasource_id=datasource_id)
        if datasource_id and not self._cm.datasource_id:
            self._cm.datasource_id = datasource_id
        self._concurrency = max(1, concurrency)
        self._retriever = SettlementRetriever(
            model,
            access_token=self._cm.access_token,
            session_id=session_id,
            chat_id=chat_id,
            max_iters=retrieval_max_iters,
            timeout=cm_timeout,
        )
        self._cm_timeout = cm_timeout

    async def confirm(self, items: list[DetectedItem]) -> list[DetectedItem]:
        """返回仍需推荐的子集。不确定时不推（CM 不可达 / LLM 失败则丢弃）。"""
        confirmed, _ = await self.confirm_with_details(items)
        return confirmed

    async def confirm_with_details(
        self, items: list[DetectedItem]
    ) -> tuple[list[DetectedItem], list[dict[str, Any]]]:
        """返回 (仍需推荐的子集, 每条的确认详情)。"""
        confirmed: list[DetectedItem] = []
        details: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(self._concurrency)

        async def _confirm_one(
            item: DetectedItem,
        ) -> tuple[DetectedItem, dict[str, Any]]:
            async with sem:
                calls: list[dict[str, Any]] = []
                context: dict[str, Any] = {}
                retrieval_status = "error"
                recommend = False
                try:
                    context = await self._query_cm(item, calls)
                except TimeoutError:
                    reason = "CM 检索总时限耗尽"
                except Exception as exc:
                    logger.warning("Settlement CM retrieval failed", exc_info=True)
                    reason = f"CM 检索失败：{type(exc).__name__}"
                else:
                    retrieval_status = "ok"
                    judgement = await self._llm_judge(item, _format_knowledge(context))
                    recommend, reason = not judgement.duplicate, judgement.reason
                cm_context = json.dumps(context, ensure_ascii=False)
                detail = {
                    "type": item.type,
                    "fields": item.fields,
                    "cm_calls": calls,
                    "retrieval_status": retrieval_status,
                    "cm_context": cm_context[:2000],
                    "recommend": recommend,
                    "reason": reason,
                }
                return item, detail

        results = await asyncio.gather(*[_confirm_one(item) for item in items])
        for item, detail in results:
            details.append(detail)
            if detail["recommend"]:
                confirmed.append(item)
        return confirmed, details

    async def _query_cm(
        self, item: DetectedItem, calls: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """API 与 fallback 共用单条候选的总时限。"""
        domain = self._resolve_domain(item.fields)
        if not domain:
            raise ValueError("无法确定有效业务域")
        scope = {"domain": domain, "datasource_id": self._datasource_id}
        async with asyncio.timeout(self._cm_timeout):
            tool_name, kwargs = self._build_cm_call(item)
            shared_uncertain = False
            if tool_name != "query_graph":
                record = await self._cm.call(tool_name, kwargs, max_len=0)
                calls.append(
                    {key: value for key, value in record.items() if key != "result"}
                )
                payload = _parse_json_payload(record.get("result") or "")
                if not _is_cm_ok(record):
                    # 实体缺失可以补查；认证、服务、域错误不能降级为“无知识”。
                    entity_missing = record.get("http_status") == 404 and any(
                        marker in (record.get("result") or "")
                        for marker in ("Dimension not found", "Dataset not found")
                    )
                    if not entity_missing:
                        raise RuntimeError("Metadata API 调用失败")
                elif _cm_result_useful(tool_name, payload, item):
                    if item.type == CardType.column_meaning:
                        payload = {
                            **payload,
                            "columns": [_dataset_target_column(payload, item)],
                        }
                    return {
                        "source": "http",
                        "scope": {**scope, "graph": "shared_metadata"},
                        "data": payload,
                    }
                if _is_cm_ok(record):
                    shared_uncertain = not (
                        payload == []
                        or (
                            isinstance(payload, dict)
                            and payload.get("status") == "no_match"
                        )
                    )
                if (
                    item.type == CardType.column_meaning
                    and _dataset_target_column(payload, item) is not None
                ):
                    shared_uncertain = False
            result = await self._retriever.retrieve(
                item,
                datasource_id=self._datasource_id,
                domain=domain,
            )
            if shared_uncertain:
                raise RuntimeError("共享 Metadata 的歧义或信息缺失尚未解决")
            return {
                "source": "mcp",
                "scope": {**scope, "graph": "personal_graph"},
                "data": result,
            }

    def _resolve_domain(self, fields: dict[str, str]) -> str | None:
        """只用 list_domains 白名单内的业务域；未命中则停止本条检索。"""
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

    def _build_cm_call(self, item: DetectedItem) -> tuple[str, dict[str, Any]]:
        """返回主查询名称与参数；数据集用法直接交给图检索。"""
        fields = item.fields
        metadata = (
            json.dumps({"datasource_id": self._datasource_id})
            if self._datasource_id
            else "{}"
        )
        domain = self._resolve_domain(fields)

        if item.type == CardType.metric_caliber:
            kwargs: dict[str, Any] = {
                "query": fields.get("metric_name", ""),
                "k": 3,
                "metadata": metadata,
            }
            if domain:
                kwargs["domain"] = domain
            return "search_metrics", kwargs

        if item.type == CardType.dimension_def:
            kwargs = {"name": fields.get("dimension_name", ""), "metadata": metadata}
            if domain:
                kwargs["domain"] = domain
            return "get_dimension", kwargs

        if item.type == CardType.column_meaning:
            kwargs = {"name": fields.get("table", ""), "metadata": metadata}
            if domain:
                kwargs["domain"] = domain
            return "get_dataset", kwargs

        if item.type == CardType.dataset_usage:
            return "query_graph", {}

        return "", {}

    async def _llm_judge(
        self, item: DetectedItem, cm_context: str
    ) -> DuplicateJudgement:
        """LLM 判断待沉淀项与已有知识是否重复。"""
        user_content = self._build_judge_message(item, cm_context)
        messages = [
            SystemMsg(name="system", content=self._system_prompt),
            UserMsg(name="user", content=user_content),
        ]
        try:
            return await structured_call(
                self._model,
                messages,
                DuplicateJudgement,
                source_step=Step.SETTLEMENT_CONFIRM,
            )
        except StructuredCallError:
            logger.warning(
                "LLM judge failed, treating as duplicate (uncertain → skip)",
                exc_info=True,
            )
            return DuplicateJudgement(duplicate=True, reason="LLM 调用失败，不确定不推")

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
