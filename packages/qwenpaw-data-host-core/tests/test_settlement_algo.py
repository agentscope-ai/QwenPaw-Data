# -*- coding: utf-8 -*-
"""Unit tests for the settlement algorithm package (no network, no model)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from qwenpaw_data.host.core.algo.biztrace.llm import StructuredLLMError
from qwenpaw_data.host.core.algo.settlement.cm_utils.feedback import (
    card_to_feedback_payload,
    feedback_ack_status,
    feedback_dry_run_recommendable,
)
from qwenpaw_data.host.core.algo.settlement.confirmer import (
    SettlementConfirmer,
    _cm_result_useful,
)
from qwenpaw_data.host.core.algo.settlement.detector import SettlementDetector
from qwenpaw_data.host.core.algo.settlement.dismissed_filter import DismissedFilter
from qwenpaw_data.host.core.algo.settlement.models import (
    CardType,
    DetectedItem,
    DetectionResult,
)
from qwenpaw_data.host.core.algo.settlement.subject import (
    canonical_datasource,
    canonical_name,
    normalize_item_fields,
    subject_key,
)


class FakeStructuredLLM:
    """Returns queued payload dicts; raises StructuredLLMError when exhausted."""

    def __init__(self, payloads: list[dict[str, Any] | Exception] | None = None):
        self.payloads = list(payloads or [])
        self.calls: list[dict[str, Any]] = []

    async def complete(self, *, system, user, schema, schema_name):
        self.calls.append(
            {"system": system, "user": user, "schema_name": schema_name}
        )
        if not self.payloads:
            raise StructuredLLMError("exhausted")
        item = self.payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeCm:
    """Scripted SettlementCmClient.call replacement."""

    def __init__(self, records: dict[str, dict[str, Any]]):
        self.records = records
        self.datasource_id = "ds1"
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool_name, kwargs=None, *, max_len=2000, mode=None):
        self.calls.append((tool_name, dict(kwargs or {})))
        record = self.records.get(
            tool_name,
            {"tool": tool_name, "kwargs": kwargs, "status": "error: down", "result": ""},
        )
        return {"tool": tool_name, "kwargs": kwargs, **record}

    async def list_domain_names(self) -> list[str]:
        record = await self.call("list_domains", {})
        if not str(record.get("status") or "").startswith("ok"):
            return []
        domains = json.loads(record.get("result") or "[]")
        return [d["name"] for d in domains if isinstance(d, dict) and d.get("name")]


def _metric_item(**overrides) -> DetectedItem:
    fields = {
        "metric_name": "GMV",
        "caliber": "支付金额",
        "domain": "交易",
        "table": "orders",
        "formula_sql": "SELECT SUM(amount)",
    }
    fields.update(overrides)
    return DetectedItem(type=CardType.metric_caliber, fields=fields)


# --- models -----------------------------------------------------------------


def test_detection_result_drops_incomplete_items() -> None:
    result = DetectionResult.model_validate(
        {
            "items": [
                {
                    "type": "metric_caliber",
                    "fields": {
                        "metric_name": "GMV",
                        "caliber": "支付金额",
                        "domain": "交易",
                        "table": "orders",
                        "formula_sql": "SELECT 1",
                    },
                },
                {"type": "metric_caliber", "fields": {"metric_name": "GMV"}},
                {"type": "column_meaning", "fields": {}},
                "not-a-dict",
            ]
        }
    )
    assert len(result.items) == 1
    assert result.items[0].fields["metric_name"] == "GMV"


def test_detection_result_non_list_items() -> None:
    assert DetectionResult.model_validate({"items": "nope"}).items == []


# --- subject ----------------------------------------------------------------


def test_canonical_name_strips_trailing_annotation() -> None:
    assert canonical_name("GMV（含退款）") == "GMV"
    assert canonical_name("GMV") == "GMV"
    assert canonical_name("") == ""


def test_canonical_datasource_keeps_leading_token() -> None:
    assert canonical_datasource("orders（主表）") == "orders"
    assert canonical_datasource("orders") == "orders"


def test_normalize_metric_moves_annotation_into_caliber() -> None:
    fields = normalize_item_fields(
        "metric_caliber",
        {"metric_name": "GMV（含退款）", "caliber": "支付金额", "table": "orders（主表）"},
    )
    assert fields["metric_name"] == "GMV"
    assert "含退款" in fields["caliber"]
    assert fields["table"] == "orders"


def test_subject_key_by_type() -> None:
    assert (
        subject_key("metric_caliber", {"metric_name": "GMV", "domain": "交易", "table": "orders"})
        == "metric:交易:orders:GMV"
    )
    assert (
        subject_key("dimension_def", {"dimension_name": "渠道", "domain": "交易", "table": "orders"})
        == "dimension:交易:orders:渠道"
    )
    assert (
        subject_key("column_meaning", {"column_name": "amt", "domain": "交易", "table": "orders"})
        == "column:交易:orders:amt"
    )
    assert (
        subject_key("dataset_usage", {"recommended_dataset": "orders", "domain": "交易"})
        == "dataset:交易:orders"
    )
    assert subject_key("metric_caliber", {"domain": "交易"}) is None
    assert subject_key("unknown", {"x": "1"}) is None


# --- feedback mapping -------------------------------------------------------


def test_card_to_feedback_payload_metric() -> None:
    payload = card_to_feedback_payload(
        {
            "id": "card_1",
            "session_id": "sess1",
            "source_chat_id": "chat_1",
            "type": "metric_caliber",
            "fields": {
                "metric_name": "GMV",
                "caliber": "支付金额",
                "domain": "交易",
                "table": "orders",
                "formula_sql": "SELECT SUM(amount)",
            },
        },
        datasource_id="ds1",
    )
    assert payload["client_card_id"] == "card_1"
    assert payload["type"] == "metric_caliber"
    assert payload["name"] == "GMV"
    assert payload["dataset"] == "orders"
    assert payload["datasource_id"] == "ds1"
    assert payload["extra"]["session_id"] == "sess1"


def test_card_to_feedback_payload_requires_domain_and_ds() -> None:
    card = {
        "id": "card_1",
        "type": "dataset_usage",
        "fields": {"use_case": "复购分析", "recommended_dataset": "orders"},
    }
    with pytest.raises(ValueError, match="domain"):
        card_to_feedback_payload({**card}, datasource_id="ds1")
    with pytest.raises(ValueError, match="datasource_id"):
        card_to_feedback_payload(
            {**card, "fields": {**card["fields"], "domain": "交易"}},
            datasource_id="",
        )


def test_feedback_ack_helpers() -> None:
    ok = {"status": "ok", "result": json.dumps({"status": "accepted"})}
    dup = {"status": "ok", "result": json.dumps({"status": "duplicate"})}
    err = {"status": "error: boom", "result": "boom"}
    assert feedback_ack_status(ok) == "accepted"
    assert feedback_dry_run_recommendable(ok) is True
    assert feedback_dry_run_recommendable(dup) is False
    assert feedback_ack_status(err) is None
    assert feedback_dry_run_recommendable(err) is False


# --- confirmer usefulness rules ----------------------------------------------


def _record(result: Any, status: str = "ok") -> dict[str, Any]:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return {"status": status, "result": text}


def test_cm_result_useful_search_metrics() -> None:
    item = _metric_item()
    assert _cm_result_useful("search_metrics", _record([{"name": "GMV"}]), item)
    assert not _cm_result_useful("search_metrics", _record([]), item)
    assert not _cm_result_useful(
        "search_metrics", _record({"status": "no_match"}), item
    )
    assert not _cm_result_useful("search_metrics", _record("x", status="error: y"), item)


def test_cm_result_useful_get_dataset_column_meaning() -> None:
    item = DetectedItem(
        type=CardType.column_meaning,
        fields={"column_name": "amt", "meaning": "金额", "table": "orders", "domain": "交易"},
    )
    with_desc = {"dataset_name": "orders", "columns": [{"column_name": "amt", "description": "金额"}]}
    without_desc = {"dataset_name": "orders", "columns": [{"column_name": "amt", "description": ""}]}
    assert _cm_result_useful("get_dataset", _record(with_desc), item)
    assert not _cm_result_useful("get_dataset", _record(without_desc), item)
    assert not _cm_result_useful("get_dataset", _record({"ambiguous": True}), item)


def test_cm_result_useful_search_context() -> None:
    item = _metric_item()
    assert _cm_result_useful(
        "search_context", _record({"relevance": {"status": "high"}}), item
    )
    assert not _cm_result_useful(
        "search_context", _record({"relevance": {"status": "no_match"}}), item
    )


# --- confirmer flow -----------------------------------------------------------


async def test_confirmer_recommends_when_cm_has_no_hit() -> None:
    cm = FakeCm({"search_metrics": {"status": "ok", "result": "[]"}})
    llm = FakeStructuredLLM()
    confirmer = SettlementConfirmer(llm, cm=cm, datasource_id="ds1", domain_names=["交易"])

    confirmed, details = await confirmer.confirm_with_details([_metric_item()])
    # primary + fallback search_context (down) → transport ok → new knowledge
    assert len(confirmed) == 1
    assert details[0]["recommend"] is True
    assert llm.calls == []


async def test_confirmer_drops_when_cm_unreachable() -> None:
    cm = FakeCm({})  # every call errors
    llm = FakeStructuredLLM()
    confirmer = SettlementConfirmer(llm, cm=cm, datasource_id="ds1")

    confirmed, details = await confirmer.confirm_with_details([_metric_item()])
    assert confirmed == []
    assert details[0]["recommend"] is False


async def test_confirmer_llm_judges_duplicates() -> None:
    hit = json.dumps([{"name": "GMV", "caliber": "支付金额"}], ensure_ascii=False)
    cm = FakeCm({"search_metrics": {"status": "ok", "result": hit}})
    llm = FakeStructuredLLM([{"duplicate": True, "reason": "same"}])
    confirmer = SettlementConfirmer(llm, cm=cm, datasource_id="ds1", domain_names=["交易"])

    confirmed, details = await confirmer.confirm_with_details([_metric_item()])
    assert confirmed == []
    assert details[0]["reason"] == "same"
    assert llm.calls and llm.calls[0]["schema_name"] == "DuplicateJudgement"


async def test_confirmer_llm_failure_is_conservative() -> None:
    hit = json.dumps([{"name": "GMV"}])
    cm = FakeCm({"search_metrics": {"status": "ok", "result": hit}})
    llm = FakeStructuredLLM()  # raises
    confirmer = SettlementConfirmer(llm, cm=cm, datasource_id="ds1", domain_names=["交易"])

    confirmed, _ = await confirmer.confirm_with_details([_metric_item()])
    assert confirmed == []


async def test_confirmer_domain_allowlist() -> None:
    cm = FakeCm({"search_metrics": {"status": "ok", "result": "[]"}})
    confirmer = SettlementConfirmer(
        FakeStructuredLLM(), cm=cm, datasource_id="ds1", domain_names=["交易"]
    )
    await confirmer.confirm([_metric_item(domain="不存在的域")])
    tool, kwargs = cm.calls[0]
    assert tool == "search_metrics"
    assert "domain" not in kwargs


# --- detector / dismissed filter ----------------------------------------------


async def test_detector_builds_prompt_and_parses() -> None:
    llm = FakeStructuredLLM(
        [
            {
                "items": [
                    {
                        "type": "metric_caliber",
                        "fields": {
                            "metric_name": "GMV",
                            "caliber": "支付金额",
                            "domain": "交易",
                            "table": "orders",
                            "formula_sql": "SELECT 1",
                        },
                    }
                ]
            }
        ]
    )
    detector = SettlementDetector(llm)
    result = await detector.detect(
        [{"role": "user", "content": "GMV 口径是什么"}],
        [{"id": "card_0", "type": "dimension_def", "fields": {}}],
        domain_names=["交易"],
    )
    assert result is not None and len(result.items) == 1
    user = llm.calls[0]["user"]
    assert "可用业务域" in user and "交易" in user
    assert "对话上下文" in user and "GMV" in user
    assert "card_0" in user


async def test_detector_returns_none_on_llm_failure() -> None:
    detector = SettlementDetector(FakeStructuredLLM())
    assert await detector.detect([{"role": "user", "content": "hi"}], []) is None


async def test_dismissed_filter_removes_indices_and_fails_closed() -> None:
    items = [_metric_item(), _metric_item(metric_name="DAU")]
    dismissed = [{"type": "metric_caliber", "fields": {"metric_name": "GMV"}}]

    kept = await DismissedFilter(
        FakeStructuredLLM([{"dismissed_indices": [0]}])
    ).filter(items, dismissed)
    assert len(kept) == 1
    assert kept[0].fields["metric_name"] == "DAU"

    assert await DismissedFilter(FakeStructuredLLM()).filter(items, dismissed) == []
    # no dismissed history → passthrough without an LLM call
    passthrough = await DismissedFilter(FakeStructuredLLM()).filter(items, [])
    assert passthrough == items
