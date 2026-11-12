"""CALIBER 跨数据源复用所需的 context / evidence 契约。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from profiling.algo.contexts.base import Lifecycle
from profiling.algo.contexts.caliber import CaliberContext
from profiling.algo.contexts.dictionary import DictContext
from profiling.algo.evidence import (
    CaliberEvidence,
    SemanticCatalogEvidence,
    TableEvidence,
    TaskEvidence,
    Unavailable,
    assemble,
)
from profiling.algo.handlers.dictionary import DictHandler
from profiling.algo.handlers.runner import LlmRunner
from profiling.algo.inputs.probe_facts_input import StaticProbeFactsInput
from profiling.algo.snapshots import CaliberSnapshot, DictSnapshot, TableSnapshot
from profiling.algo.stores import ParquetStore
from profiling.algo.tasks import ProfileTask


def _dataset_evidence() -> TableEvidence:
    return TableEvidence(
        TableSnapshot.build(
            pd.DataFrame(
                [
                    {
                        "table_name": "public.orders",
                        "table_comment": "orders",
                        "domain_name": "sales",
                    }
                ]
            ),
            {
                "public.orders": pd.DataFrame(
                    [{"table_name": "public.orders", "column_name": "amount"}]
                )
            },
        )
    )


def _caliber_row(**overrides) -> dict:
    row = {
        "table_name": "public.orders",
        "column_name": "amount",
        "domain_name": "sales",
        "column_role": "metric",
        "dimension_name": None,
        "value_example": None,
        "metric_name": "revenue",
        "time_window": "1d",
        "expr": "SUM(amount)",
        "additivity": "additive",
        "role_reason": None,
        "role_confidence": None,
    }
    row.update(overrides)
    return row


async def test_caliber_context_routes_its_two_lifecycle_priors() -> None:
    staged = CaliberContext(name="columns_caliber_staged", lifecycle=Lifecycle.STAGED)
    confirmed = CaliberContext(
        name="columns_caliber_confirmed", lifecycle=Lifecycle.CONFIRMED
    )
    await staged.save(CaliberSnapshot.build(pd.DataFrame([_caliber_row()])))
    await confirmed.save(
        CaliberSnapshot.build(pd.DataFrame([_caliber_row(expr="SUM(formal_amount)")]))
    )

    assert staged.evidence_for_task(ProfileTask.CALIBER).allow_unstatused is False
    assert staged.evidence_for_task(ProfileTask.DICT).allow_unstatused is True
    assert confirmed.evidence_for_task(ProfileTask.CALIBER).allow_unstatused is True

    evidence = assemble(
        ProfileTask.CALIBER,
        {
            "dataset_confirmed": _dataset_evidence(),
            staged.name: staged.evidence_for_task(ProfileTask.CALIBER),
            confirmed.name: confirmed.evidence_for_task(ProfileTask.CALIBER),
        },
    )

    assert isinstance(evidence, TaskEvidence)
    assert isinstance(evidence.prior_staged, CaliberEvidence)
    assert isinstance(evidence.prior_confirmed, CaliberEvidence)
    assert evidence.prior_staged.calibers.iloc[0]["expr"] == "SUM(amount)"
    assert evidence.prior_confirmed.calibers.iloc[0]["expr"] == "SUM(formal_amount)"


@pytest.mark.parametrize("fact_result", ["none", "empty", "error"])
async def test_caliber_context_refresh_rejects_unreviewed_cache(
    tmp_path: Path,
    fact_result: str,
) -> None:
    """同实例刷新回落缓存后，旧的无状态草稿不能继续供 DICT 使用。"""

    async def fact():
        if fact_result == "error":
            raise RuntimeError("facts unavailable")
        return CaliberSnapshot() if fact_result == "empty" else None

    context = CaliberContext(
        [ParquetStore(tmp_path, compression="zstd")],
        name="columns_caliber_staged",
        fact=fact,
    )
    await context.save(CaliberSnapshot.build(pd.DataFrame([_caliber_row()])))

    refreshed = await context.load(refresh=True)
    assert refreshed.calibers["metric_name"].tolist() == ["revenue"]
    assert "probe_status" not in refreshed.calibers.columns
    evidence = assemble(
        ProfileTask.DICT,
        {context.name: context.evidence_for_task(ProfileTask.DICT)},
    )
    assert isinstance(evidence, Unavailable)
    assert context.evidence_for_task(ProfileTask.CALIBER).allow_unstatused is False

    # 刷新后新生成的结果仍能在本轮继续交给 DICT。
    await context.save(
        CaliberSnapshot.build(
            pd.DataFrame(
                [
                    _caliber_row(metric_name="new_revenue"),
                ]
            )
        )
    )
    evidence = assemble(
        ProfileTask.DICT,
        {context.name: context.evidence_for_task(ProfileTask.DICT)},
    )
    assert isinstance(evidence, TaskEvidence)
    assert evidence.calibers.calibers["metric_name"].tolist() == ["new_revenue"]


async def test_caliber_context_cached_load_keeps_same_run_output() -> None:
    """同一流水线的普通 load 不应撤销刚生成结果的 DICT 消费资格。"""
    fact_calls = 0

    async def fact():
        nonlocal fact_calls
        fact_calls += 1
        return None

    context = CaliberContext(name="columns_caliber_staged", fact=fact)
    snapshot = CaliberSnapshot.build(pd.DataFrame([_caliber_row()]))
    await context.save(snapshot)

    assert await context.load() is snapshot
    assert fact_calls == 0
    evidence = assemble(
        ProfileTask.DICT,
        {context.name: context.evidence_for_task(ProfileTask.DICT)},
    )
    assert isinstance(evidence, TaskEvidence)
    assert evidence.calibers.calibers["metric_name"].tolist() == ["revenue"]
    assert context.evidence_for_task(ProfileTask.CALIBER).allow_unstatused is False


@pytest.mark.parametrize("status", [None, "PENDING", "CONFIRMED"])
async def test_caliber_context_refresh_respects_fact_review_status(status) -> None:
    """有效 facts 刷新后，DICT 继续只消费明确确认的在线候选。"""
    row = _caliber_row(metric_name="from_facts")
    if status is not None:
        row["probe_status"] = status

    async def fact():
        return CaliberSnapshot.build(pd.DataFrame([row]))

    context = CaliberContext(name="columns_caliber_staged", fact=fact)
    await context.save(CaliberSnapshot.build(pd.DataFrame([_caliber_row()])))
    refreshed = await context.load(refresh=True)
    assert refreshed.calibers["metric_name"].tolist() == ["from_facts"]
    evidence = assemble(
        ProfileTask.DICT,
        {context.name: context.evidence_for_task(ProfileTask.DICT)},
    )
    if status == "CONFIRMED":
        assert isinstance(evidence, TaskEvidence)
        assert evidence.calibers.calibers["metric_name"].tolist() == ["from_facts"]
    else:
        assert isinstance(evidence, Unavailable)


async def test_offline_caliber_context_refresh_keeps_unstatused_cache(
    tmp_path: Path,
) -> None:
    context = CaliberContext(
        [ParquetStore(tmp_path, compression="zstd")],
        name="columns_caliber_staged",
    )
    await context.save(CaliberSnapshot.build(pd.DataFrame([_caliber_row()])))
    await context.load(refresh=True)
    evidence = assemble(
        ProfileTask.DICT,
        {context.name: context.evidence_for_task(ProfileTask.DICT)},
    )
    assert isinstance(evidence, TaskEvidence)
    assert evidence.calibers.calibers["metric_name"].tolist() == ["revenue"]


async def test_dict_context_exposes_only_confirmed_entity_catalog_to_caliber() -> None:
    snapshot = DictSnapshot.build(
        pd.DataFrame(
            [
                {
                    "domain_name": "sales",
                    "dimension_name": "order_date",
                    "description": "Order date",
                    "synonyms": ["created_at"],
                    "dimension_role": "time",
                    "tables": ["secret.other_source_table"],
                }
            ]
        ),
        pd.DataFrame(
            [
                {
                    "domain_name": "sales",
                    "metric_name": "revenue",
                    "description": "Gross revenue",
                    "synonyms": ["gmv"],
                    "formulas": ["SUM(secret_amount)"],
                    "time_windows": ["30d"],
                    "tables": ["secret.other_source_table"],
                }
            ]
        ),
    )
    staged = DictContext(name="columns_dict_staged", lifecycle=Lifecycle.STAGED)
    confirmed = DictContext(
        name="columns_dict_confirmed", lifecycle=Lifecycle.CONFIRMED
    )
    await staged.save(snapshot)
    await confirmed.save(snapshot)

    assert staged.evidence_for_task(ProfileTask.CALIBER) is None
    catalog = confirmed.evidence_for_task(ProfileTask.CALIBER)
    assert isinstance(catalog, SemanticCatalogEvidence)
    assert list(catalog.dimensions.columns) == [
        "domain_name",
        "dimension_name",
        "description",
        "synonyms",
        "dimension_role",
    ]
    assert list(catalog.metrics.columns) == [
        "domain_name",
        "metric_name",
        "description",
        "synonyms",
    ]
    assert "secret.other_source_table" not in catalog.dimensions.to_json()
    assert "SUM(secret_amount)" not in catalog.metrics.to_json()

    evidence = assemble(
        ProfileTask.CALIBER,
        {
            "dataset_confirmed": _dataset_evidence(),
            "columns_dict_staged": SemanticCatalogEvidence(
                dimensions=catalog.dimensions,
                metrics=catalog.metrics,
            ),
            confirmed.name: catalog,
        },
    )
    assert isinstance(evidence, TaskEvidence)
    assert evidence.semantic_catalog is catalog


async def test_dict_context_still_routes_both_lifecycles_as_own_prior() -> None:
    snapshot = DictSnapshot.build(
        pd.DataFrame(
            [
                {
                    "domain_name": "sales",
                    "dimension_name": "order_id",
                    "description": None,
                    "synonyms": ["id"],
                    "dimension_role": "categorical",
                    "tables": ["public.orders"],
                }
            ]
        ),
        None,
    )
    staged = DictContext(name="columns_dict_staged", lifecycle=Lifecycle.STAGED)
    confirmed = DictContext(
        name="columns_dict_confirmed", lifecycle=Lifecycle.CONFIRMED
    )
    await staged.save(snapshot)
    await confirmed.save(snapshot)

    evidence = assemble(
        ProfileTask.DICT,
        {
            staged.name: staged.evidence_for_task(ProfileTask.DICT),
            confirmed.name: confirmed.evidence_for_task(ProfileTask.DICT),
            "columns_caliber_confirmed": CaliberEvidence(
                pd.DataFrame([_caliber_row()])
            ),
        },
    )

    assert isinstance(evidence, TaskEvidence)
    assert isinstance(evidence.prior_staged, DictSnapshot)
    assert isinstance(evidence.prior_confirmed, DictSnapshot)
    assert evidence.prior_staged.dimensions.iloc[0]["tables"] == ["public.orders"]


@pytest.mark.parametrize("kind", ["dimension", "metric"])
@pytest.mark.parametrize("duplicate", ["identical", "conflicting", "whitespace"])
@pytest.mark.parametrize("with_staged", [False, True])
def test_dict_rejects_duplicate_formal_caliber_keys(
    kind: str,
    duplicate: str,
    with_staged: bool,
) -> None:
    """正式关系同键多行必须在 DICT 建模前失败，不能靠聚合吞掉歧义。"""
    row = _caliber_row()
    name_field = "metric_name"
    if kind == "dimension":
        name_field = "dimension_name"
        row.update(
            column_role="dimension",
            dimension_name="country",
            metric_name=None,
            time_window=None,
            expr="country",
            additivity=None,
        )
    other = dict(row)
    if duplicate == "conflicting":
        other["expr"] = "different_expression"
    elif duplicate == "whitespace":
        # SQLite 唯一索引不做 trim；证据的自然键会统一去掉首尾空白。
        field = "time_window" if kind == "metric" else name_field
        other[field] = f" {other[field]} "
    parts = {
        "columns_caliber_confirmed": CaliberEvidence(pd.DataFrame([row, other])),
    }
    if with_staged:
        parts["columns_caliber_staged"] = CaliberEvidence(
            pd.DataFrame(
                [
                    dict(row, column_name=column, probe_status="CONFIRMED")
                    for column in ("source_a", "source_b")
                ]
            )
        )

    with pytest.raises(ValueError, match="正式口径自然键重复") as caught:
        assemble(ProfileTask.DICT, parts)

    message = str(caught.value)
    assert all(
        value in message for value in ("sales", "public.orders", row[name_field])
    )
    if kind == "metric":
        assert "1d" in message


def test_dict_preserves_staged_caliber_conflicts_for_review() -> None:
    """同键 staging 候选仍保留各自血缘和公式，冲突由完成落库时校验。"""
    rows = [
        _caliber_row(
            column_name="amount", expr="SUM(amount)", probe_status="CONFIRMED"
        ),
        _caliber_row(
            column_name="paid_amount", expr="SUM(paid_amount)", probe_status="CONFIRMED"
        ),
    ]
    evidence = assemble(
        ProfileTask.DICT,
        {"columns_caliber_staged": CaliberEvidence(pd.DataFrame(rows))},
    )

    assert isinstance(evidence, TaskEvidence)
    assert evidence.calibers.calibers[["column_name", "expr"]].to_dict("records") == [
        {"column_name": "amount", "expr": "SUM(amount)"},
        {"column_name": "paid_amount", "expr": "SUM(paid_amount)"},
    ]


def test_dict_caliber_evidence_merges_formal_with_confirmed_staging_by_key() -> None:
    formal = CaliberEvidence(
        pd.DataFrame(
            [
                _caliber_row(
                    column_name="formal_dimension_expr",
                    column_role="dimension",
                    dimension_name="order_date",
                    metric_name=None,
                    time_window=None,
                    expr="DATE(created_at)",
                    value_example=["formal-value"],
                    additivity=None,
                ),
                _caliber_row(
                    column_name="SUM(amount)",
                    expr="SUM(amount)",
                    additivity="additive",
                ),
            ]
        )
    )
    staged = CaliberEvidence(
        pd.DataFrame(
            [
                _caliber_row(
                    column_name="created_at",
                    column_role="date",
                    dimension_name="order_date",
                    metric_name=None,
                    time_window=None,
                    expr="stale staged dimension expr",
                    value_example=["stale-staging-value"],
                    additivity=None,
                    probe_status="confirmed",
                ),
                _caliber_row(
                    column_name="created_date",
                    column_role="dimension",
                    dimension_name="order_date",
                    metric_name=None,
                    time_window=None,
                    expr="another stale staged dimension expr",
                    value_example=["another-stale-value"],
                    additivity=None,
                    probe_status="CONFIRMED",
                ),
                _caliber_row(
                    column_name="amount",
                    expr="stale staged metric expr",
                    additivity="non_additive",
                    probe_status="CONFIRMED",
                ),
                _caliber_row(
                    column_name="amount_7d",
                    time_window="7d",
                    expr="SUM(amount)",
                    probe_status="CONFIRMED",
                ),
                _caliber_row(
                    metric_name="pending_metric",
                    probe_status="PENDING",
                ),
                _caliber_row(
                    metric_name="skipped_metric",
                    probe_status="SKIPPED",
                ),
            ]
        )
    )

    evidence = assemble(
        ProfileTask.DICT,
        {
            "columns_caliber_confirmed": formal,
            "columns_caliber_staged": staged,
        },
    )

    assert isinstance(evidence, TaskEvidence)
    assert evidence.calibers is not None
    rows = evidence.calibers.calibers.to_dict(orient="records")
    assert len(rows) == 4
    dimensions = [row for row in rows if row.get("dimension_name") == "order_date"]
    assert {row["expr"] for row in dimensions} == {"DATE(created_at)"}
    assert {tuple(row["value_example"]) for row in dimensions} == {("formal-value",)}
    assert {(row["column_name"], row["column_role"]) for row in dimensions} == {
        ("created_at", "date"),
        ("created_date", "dimension"),
    }

    one_day = next(
        row
        for row in rows
        if row.get("metric_name") == "revenue" and row.get("time_window") == "1d"
    )
    assert one_day["expr"] == "SUM(amount)"
    assert one_day["additivity"] == "additive"
    assert one_day["column_name"] == "amount"
    assert {
        row.get("time_window") for row in rows if row.get("metric_name") == "revenue"
    } == {
        "1d",
        "7d",
    }
    assert {row.get("metric_name") for row in rows}.isdisjoint(
        {"pending_metric", "skipped_metric"}
    )


def test_offline_caliber_snapshot_without_lifecycle_is_usable_by_dict() -> None:
    """无状态列表示算法内存/离线快照，不等同于数据库里的 PENDING 行。"""
    staged = CaliberEvidence(pd.DataFrame([_caliber_row()]))

    evidence = assemble(
        ProfileTask.DICT,
        {"columns_caliber_staged": staged},
    )

    assert isinstance(evidence, TaskEvidence)
    assert evidence.calibers is not None
    assert evidence.calibers.calibers["metric_name"].tolist() == ["revenue"]


def test_online_unstatused_caliber_cache_is_not_usable_by_dict() -> None:
    staged = CaliberEvidence(pd.DataFrame([_caliber_row()]), allow_unstatused=False)

    evidence = assemble(
        ProfileTask.DICT,
        {"columns_caliber_staged": staged},
    )

    assert isinstance(evidence, Unavailable)


async def test_dict_keeps_multiple_windows_from_the_same_metric_column() -> None:
    calibers = CaliberEvidence(
        pd.DataFrame(
            [
                _caliber_row(time_window="1d", expr="SUM(amount)"),
                _caliber_row(time_window="7d", expr="SUM(amount_7d)"),
            ]
        )
    )

    evidence = assemble(
        ProfileTask.DICT,
        {"columns_caliber_confirmed": calibers},
    )
    assert isinstance(evidence, TaskEvidence)
    snapshot = await DictHandler(None).handle(evidence)

    assert snapshot.metrics.iloc[0]["time_windows"] == ["1d", "7d"]
    assert snapshot.metrics.iloc[0]["formulas"] == ["SUM(amount)", "SUM(amount_7d)"]


async def test_dict_with_llm_only_describes_and_never_renames_canonical_names() -> None:
    class RecordingLlm:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def complete(
            self,
            system_prompt: str,
            user_prompt: str,
            *,
            temperature: float = 0.2,
        ) -> str:
            self.calls.append((system_prompt, user_prompt))
            return "[]"

    llm = RecordingLlm()
    calibers = CaliberEvidence(
        pd.DataFrame(
            [
                _caliber_row(
                    column_name="gross_amount",
                    metric_name="Gross Revenue",
                    expr="SUM(gross_amount)",
                ),
                _caliber_row(
                    column_name="net_amount",
                    metric_name="Net Revenue",
                    expr="SUM(net_amount)",
                ),
            ]
        )
    )

    snapshot = await DictHandler(
        llm, runner=LlmRunner(llm, concurrency=1), language="en"
    ).handle(TaskEvidence(task=ProfileTask.DICT, calibers=calibers))

    assert set(snapshot.metrics["metric_name"]) == {"Gross Revenue", "Net Revenue"}
    assert llm.calls
    assert all(
        "canonical_name" not in system_prompt + user_prompt
        for system_prompt, user_prompt in llm.calls
    )


async def test_formal_dimension_type_is_projected_as_catalog_dimension_role() -> None:
    facts = StaticProbeFactsInput(
        {
            "dimension": [
                {
                    "domain_name": "sales",
                    "dimension_name": "order_date",
                    "description": "Order date",
                    "synonyms": "created_at$$$created_date",
                    "dimension_role": float("nan"),
                    "dimension_type": "time",
                }
            ]
        }
    )
    read = facts.facts_of("columns_dict", "confirmed")
    assert read is not None
    snapshot = await read()

    assert snapshot.dimensions.iloc[0]["dimension_role"] == "time"
    assert snapshot.dimensions.iloc[0]["synonyms"] == ["created_at", "created_date"]
