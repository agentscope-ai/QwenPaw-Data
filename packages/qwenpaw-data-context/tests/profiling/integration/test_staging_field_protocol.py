"""第四步字段协议：算法边界映射、staging、编辑、Excel 往返及晋升。"""

import io
import json

import openpyxl
import pandas as pd
import pytest

from profiling.algo.inputs.probe_facts_input import StaticProbeFactsInput
from profiling.algo_provider import map_biz_domains, map_dimensions, map_metrics
from profiling.provider import (
    CandidateBizDomain,
    CandidateDimensionCaliber,
    CandidateMetricCaliber,
)
from profiling.services import row_service, upload_service
from profiling.stages import STAGE_DEFS
from profiling.tables import PROFILING_TABLES, staging_biz_domain, staging_dimension
from semantic_config.tables import dimension_dict, domain, metric_dict


async def _roundtrip(flow, stage):
    sdef = STAGE_DEFS[stage]
    content = await upload_service.export_stage(flow.session, sdef, "a", flow.principal)
    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    try:
        headers = list(next(workbook.active.iter_rows(values_only=True)))
    finally:
        workbook.close()
    assert headers == list(sdef.excel_columns)
    await upload_service.upload_stage(
        flow.session,
        sdef,
        "a",
        "stage.xlsx",
        content,
        flow.principal,
    )
    return headers


def test_stage_fields_match_storage_and_preserve_internal_evidence():
    assert "display_name" not in staging_biz_domain.c
    assert "datasource_scope" not in staging_biz_domain.c
    assert "dimension_role" not in staging_dimension.c
    assert "dimension_type" in staging_dimension.c
    for sdef in STAGE_DEFS.values():
        columns = set(PROFILING_TABLES[sdef.table].c.keys())
        assert set(sdef.editable) <= columns
        assert set(sdef.excel_columns) <= columns
    for stage, internal in (
        ("BIZ_DOMAIN", {"member_tables", "origin"}),
        ("DIMENSION_CALIBER", {"column_name", "column_role", "origin"}),
        ("METRIC_CALIBER", {"column_name", "origin"}),
    ):
        assert internal <= set(STAGE_DEFS[stage].excel_columns)
        assert internal.isdisjoint(STAGE_DEFS[stage].editable)


async def test_domain_drops_display_fields_but_keeps_members_through_review(
    business_flow,
):
    flow = business_flow
    await flow.add_source("a", [])
    candidates = map_biz_domains(
        {
            "domains": pd.DataFrame(
                [
                    {
                        "domain_name": "sales",
                        "display_name": "不落库的算法展示名",
                        "description": "销售业务",
                        "origin": "refine",
                    }
                ]
            ),
            "members": pd.DataFrame(
                [{"domain_name": "sales", "table_name": "public.orders"}]
            ),
        }
    )
    assert "display_name" not in candidates[0].model_dump()
    await (await flow.sink("a")).emit_biz_domains(candidates)
    [row] = await flow.rows("a", "BIZ_DOMAIN")
    await row_service.update_row(
        flow.session,
        STAGE_DEFS["BIZ_DOMAIN"],
        "a",
        row["id"],
        {"description": "人工确认描述", "display_name": "忽略", "member_tables": []},
        flow.principal,
    )
    await flow.review("a", "BIZ_DOMAIN")
    headers = await _roundtrip(flow, "BIZ_DOMAIN")
    assert not {"display_name", "datasource_scope"} & set(headers)
    [restored] = await flow.rows("a", "BIZ_DOMAIN")
    assert restored["probe_status"] == "CONFIRMED"
    assert restored["origin"] == "refine"
    assert json.loads(restored["member_tables"])[0]["source_table"] == "public.orders"
    assert (await flow.complete("a", "BIZ_DOMAIN"))["promoted"] == 1
    [formal] = await flow.formal(domain)
    assert formal["domain_name"] == "sales"
    assert formal["description"] == "人工确认描述"
    assert formal["datasource_scope"] == ""


@pytest.mark.parametrize("priority", ["HIGH", None])
async def test_dictionary_fields_roundtrip_and_promote_without_renaming_algo(
    business_flow, priority
):
    flow = business_flow
    await flow.add_source("a", [])
    await (await flow.sink("a")).emit_biz_domains(
        [CandidateBizDomain(domain_name="sales", description="销售")]
    )
    await flow.review("a", "BIZ_DOMAIN")
    await flow.complete("a", "BIZ_DOMAIN")
    sink = await flow.sink("a")
    dimensions = map_dimensions(
        {
            "dimensions": pd.DataFrame(
                [
                    {
                        "domain_name": "sales",
                        "dimension_name": "order_date",
                        "dimension_role": "time",
                        "synonyms": [],
                    }
                ]
            )
        }
    )
    assert dimensions[0].dimension_type == "time"
    assert "dimension_role" not in dimensions[0].model_dump()
    await sink.emit_dimension(dimensions)
    await sink.emit_metric(
        map_metrics(
            {
                "metrics": pd.DataFrame(
                    [
                        {
                            "domain_name": "sales",
                            "metric_name": "revenue",
                            "synonyms": ["收入"],
                        }
                    ]
                )
            }
        )
    )

    for stage in ("DIMENSION_MANAGE", "METRIC_MANAGE"):
        [row] = await flow.rows("a", stage)
        assert row["priority"] == "MEDIUM"
        changes = {"description": "人工描述", "priority": priority}
        if stage == "DIMENSION_MANAGE":
            assert row["synonyms"] == ""
            changes.update(
                dimension_type="time",
                child_dimensions="order_month$$$order_year",
                synonyms=None,
                dimension_role="categorical",
            )
        await row_service.update_row(
            flow.session,
            STAGE_DEFS[stage],
            "a",
            row["id"],
            changes,
            flow.principal,
        )
        headers = await _roundtrip(flow, stage)
        assert "priority" in headers and "dimension_role" not in headers
        [restored] = await flow.rows("a", stage)
        assert restored["priority"] == priority
        if stage == "DIMENSION_MANAGE":
            assert restored["dimension_type"] == "time"
            assert restored["child_dimensions"] == "order_month$$$order_year"

    # 存储字段名统一，但 CALIBER/DICT 消费的算法内部词汇仍保持 dimension_role。
    inputs = StaticProbeFactsInput((await flow.context("a")).facts)
    snapshot = await inputs.facts_of("columns_dict", "staged")()
    assert snapshot.dimensions["dimension_role"].tolist() == ["time"]
    assert (await flow.complete("a", "DIMENSION_MANAGE"))["promoted_dimension"] == 1
    assert (await flow.complete("a", "METRIC_MANAGE"))["promoted_metric"] == 1
    [dim] = await flow.formal(dimension_dict)
    [metric] = await flow.formal(metric_dict)
    assert dim["dimension_type"] == "time"
    assert dim["child_dimensions"] == "order_month$$$order_year"
    assert dim["synonyms"] == ""
    assert dim["priority"] == metric["priority"] == (priority or "MEDIUM")

    # 再次命中正式实体时不覆盖其元信息，包括新接通的人工字段。
    for stage in ("DIMENSION_MANAGE", "METRIC_MANAGE"):
        [row] = await flow.rows("a", stage)
        await row_service.update_row(
            flow.session,
            STAGE_DEFS[stage],
            "a",
            row["id"],
            {"priority": "LOW", "description": "不得覆盖", "child_dimensions": "other"},
            flow.principal,
        )
        result = await flow.complete("a", stage)
        assert (
            result[
                "promoted_dimension"
                if stage == "DIMENSION_MANAGE"
                else "promoted_metric"
            ]
            == 0
        )
    assert await flow.formal(dimension_dict) == [dim]
    assert await flow.formal(metric_dict) == [metric]


@pytest.mark.parametrize("stage", ["DIMENSION_CALIBER", "METRIC_CALIBER"])
async def test_caliber_lineage_survives_excel_without_becoming_editable(
    business_flow, stage
):
    flow = business_flow
    await flow.add_source("a", [])
    sink = await flow.sink("a")
    if stage == "DIMENSION_CALIBER":
        await sink.emit_dimension_calibers(
            [
                CandidateDimensionCaliber(
                    domain_name="sales",
                    dataset_name="public.orders",
                    dimension_name="order_date",
                    expr="DATE(created_at)",
                    value_example="2026-09-01",
                    column_name="created_at",
                    column_role="date",
                    origin="refine",
                )
            ]
        )
    else:
        await sink.emit_metric_calibers(
            [
                CandidateMetricCaliber(
                    domain_name="sales",
                    dataset_name="public.orders",
                    metric_name="revenue",
                    expr="SUM(amount)",
                    time_window="7d",
                    additivity="additive",
                    column_name="amount",
                    origin="refine",
                )
            ]
        )
    await flow.review("a", stage)
    [before] = await flow.rows("a", stage)
    await row_service.update_row(
        flow.session,
        STAGE_DEFS[stage],
        "a",
        before["id"],
        {"column_name": "discard", "column_role": "dimension", "origin": "create"},
        flow.principal,
    )
    await _roundtrip(flow, stage)
    [after] = await flow.rows("a", stage)
    for key in STAGE_DEFS[stage].excel_columns:
        assert after[key] == before[key], key
