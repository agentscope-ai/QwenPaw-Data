"""dataset_field 只跟随已确认 Dataset.source，名称不参与物理来源推断。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal
from profiling.algo import AlgoConfig, AlgoContexts, AlgoProfiler
from profiling.algo.handlers.dataset_fields import dataset_source_columns
from profiling.algo.snapshots import TableSnapshot
from profiling.algo_provider import AlgoProvider
from profiling.provider import ProbeContext
from profiling.repositories import staging_repo
from profiling.services import row_service
from profiling.stages import STAGE_DEFS
from semantic_config.tables import dataset, dataset_field


def physical_snapshot(*names):
    return TableSnapshot.build(
        pd.DataFrame(
            [{"table_name": name, "table_comment": "orders"} for name in names]
        ),
        {
            name: pd.DataFrame(
                [
                    {
                        "table_name": name,
                        "column_name": col,
                        "data_type": dtype,
                        "is_primary": primary,
                        "is_nullable": nullable,
                        "column_comment": comment,
                    }
                    for col, dtype, primary, nullable, comment in [
                        ("id", "bigint", "Y", "N", "订单编号"),
                        ("country", "varchar", "N", "Y", "买方国家"),
                        ("amount", "decimal", "N", "N", "成交金额"),
                    ]
                ]
            )
            for name in names
        },
    )


@pytest.mark.parametrize("source", ["public.orders", "orders", '"public"."orders"'])
def test_physical_source_preserves_local_column_metadata(source):
    tables = physical_snapshot("public.orders")
    result = dataset_source_columns(source, tables)
    assert_frame_equal(result, tables.column_meta_of("public.orders"))
    result.loc[0, "column_name"] = "changed"
    assert tables.column_meta_of("public.orders").iloc[0]["column_name"] == "id"


@pytest.mark.parametrize(
    "sql,names,types",
    [
        ("SELECT country FROM public.orders", ["country"], ["varchar"]),
        (
            "SELECT country AS nation, amount AS revenue FROM public.orders",
            ["nation", "revenue"],
            ["varchar", "decimal"],
        ),
        (
            "SELECT * FROM public.orders",
            ["id", "country", "amount"],
            ["bigint", "varchar", "decimal"],
        ),
        (
            "SELECT o.* FROM public.orders o",
            ["id", "country", "amount"],
            ["bigint", "varchar", "decimal"],
        ),
        ("SELECT public.orders.country FROM public.orders", ["country"], ["varchar"]),
        (
            "WITH x AS (SELECT country AS c FROM public.orders) SELECT c AS nation FROM x",
            ["nation"],
            ["varchar"],
        ),
        (
            "SELECT x.* FROM (SELECT country AS nation FROM public.orders) x",
            ["nation"],
            ["varchar"],
        ),
        (
            "SELECT o.id, r.amount AS refunded FROM public.orders o LEFT JOIN other.orders r ON o.id=r.id",
            ["id", "refunded"],
            ["bigint", "decimal"],
        ),
        ("SELECT SUM(amount) AS total FROM public.orders", ["total"], [None]),
        ("SELECT 1 AS constant", ["constant"], [None]),
        ("-- user query\nSELECT country FROM public.orders;", ["country"], ["varchar"]),
        (
            "SELECT country FROM public.orders UNION ALL SELECT country FROM other.orders",
            ["country"],
            [None],
        ),
        (
            "WITH x(total) AS (SELECT SUM(amount) FROM public.orders) SELECT * FROM x",
            ["total"],
            [None],
        ),
        (
            "WITH __dataset_source_0 AS (SELECT country FROM public.orders) SELECT * FROM __dataset_source_0",
            ["country"],
            ["varchar"],
        ),
    ],
)
def test_sql_uses_output_projection(sql, names, types):
    tables = physical_snapshot("public.orders", "other.orders")
    result = dataset_source_columns(sql, tables)
    assert result["column_name"].tolist() == names
    assert result["data_type"].tolist() == types
    assert set(result["is_primary"]) == {"N"}
    assert set(result["is_nullable"]) == {"Y"}
    assert tables.column_meta_of("public.orders")["column_name"].tolist() == [
        "id",
        "country",
        "amount",
    ]


@pytest.mark.parametrize(
    "source",
    [
        "",
        "missing.orders",
        "orders",
        "public.orders AS o",
        "SELECT * FROM missing.orders",
        "SELECT not_a_column FROM public.orders",
        "SELECT id FROM public.orders a JOIN other.orders b ON a.id=b.id",
        "SELECT id, id FROM public.orders",
        "SELECT * FROM public.orders a JOIN other.orders b ON a.id=b.id",
        "SELECT SUM(amount) FROM public.orders",
        "SELECT country FROM public.orders; SELECT amount FROM public.orders",
        "DELETE FROM public.orders",
        "CREATE TABLE x AS SELECT * FROM public.orders",
        "SELECT * INTO new_table FROM public.orders",
        "WITH x AS (DELETE FROM public.orders RETURNING *) SELECT * FROM x",
        "SELECT ( FROM public.orders",
        "SELECT * FROM (SELECT amount+1 FROM public.orders) x",
        "SELECT country FROM public.orders UNION ALL SELECT country, amount FROM other.orders",
    ],
)
def test_invalid_or_ambiguous_source_never_falls_back_to_parent_columns(source):
    with pytest.raises(ValueError):
        dataset_source_columns(
            source, physical_snapshot("public.orders", "other.orders")
        )


def test_sql_quoted_identifiers_and_output_case():
    tables = TableSnapshot.build(
        pd.DataFrame([{"table_name": 'public."Orders"', "table_comment": ""}]),
        {
            'public."Orders"': pd.DataFrame(
                [
                    {
                        "table_name": 'public."Orders"',
                        "column_name": "Country",
                        "data_type": "varchar",
                        "is_primary": "N",
                        "is_nullable": "Y",
                        "column_comment": "country",
                    }
                ]
            )
        },
    )
    result = dataset_source_columns(
        'SELECT "Country" AS "Nation" FROM public."Orders"', tables
    )
    assert result["column_name"].tolist() == ["Nation"]
    assert result["data_type"].tolist() == ["varchar"]
    with pytest.raises(ValueError):
        dataset_source_columns('SELECT country FROM public."Orders"', tables)


def test_table_schema_and_fullname_are_resolved_without_using_logical_names():
    tables = physical_snapshot("orders")
    tables.table_meta["table_schema"] = "public"
    assert dataset_source_columns("SELECT country FROM public.orders", tables)[
        "column_name"
    ].tolist() == ["country"]
    with pytest.raises(ValueError, match="找不到物理表"):
        dataset_source_columns("SELECT country FROM wrong.orders", tables)
    tables.table_meta["table_fullname"] = "catalog.public.orders"
    assert dataset_source_columns("catalog.public.orders", tables)[
        "column_name"
    ].tolist() == ["id", "country", "amount"]


def test_physical_metadata_name_is_not_sql_case_folding():
    tables = physical_snapshot("Orders")
    tables.table_meta["table_schema"] = "public"
    assert dataset_source_columns("public.Orders", tables)["column_name"].tolist() == [
        "id",
        "country",
        "amount",
    ]
    assert dataset_source_columns('SELECT id FROM public."Orders"', tables)[
        "column_name"
    ].tolist() == ["id"]
    with pytest.raises(ValueError):
        dataset_source_columns("SELECT id FROM public.Orders", tables)


@pytest.mark.parametrize("bad_columns", ["empty", "duplicate", "blank"])
def test_incomplete_physical_metadata_is_not_a_successful_empty_probe(bad_columns):
    tables = physical_snapshot("public.orders")
    if bad_columns == "empty":
        tables = TableSnapshot(table_meta=tables.table_meta)
    else:
        columns = tables.column_meta_of("public.orders")
        columns.loc[0, "column_name"] = "" if bad_columns == "blank" else "country"
    with pytest.raises(ValueError):
        dataset_source_columns("public.orders", tables)


async def stage_dataset(flow, datasource_id="a", **values):
    return await staging_repo.insert_row(
        flow.session,
        "staging_dataset",
        {
            "domain_name": "sales",
            "dataset_name": "logical_orders",
            "source": "public.orders",
            **values,
        },
        datasource_id=datasource_id,
        task_id=None,
        probe_status="PENDING",
        **flow.scope,
    )


async def confirm_domain(flow, source):
    await staging_repo.insert_row(
        flow.session,
        "staging_biz_domain",
        {"domain_name": "sales", "description": "销售"},
        datasource_id=source,
        task_id=None,
        probe_status="CONFIRMED",
        **flow.scope,
    )
    await flow.complete(source, "BIZ_DOMAIN")


@pytest.mark.parametrize("mode", ["create", "rerun", "refine"])
@pytest.mark.parametrize(
    "source,names",
    [
        ("public.orders", {"country", "amount"}),
        ("SELECT country AS customer_country FROM public.orders", {"customer_country"}),
        ("SELECT SUM(amount) AS total FROM public.orders", {"total"}),
    ],
)
async def test_edited_confirmed_dataset_probes_and_promotes_own_source(
    business_flow,
    orders_table,
    dataset_field_tables,
    monkeypatch,
    mode,
    source,
    names,
):
    flow = business_flow
    await flow.add_source("a", [orders_table("a")])
    await confirm_domain(flow, "a")
    row_id = await stage_dataset(flow)
    await row_service.update_row(
        flow.session,
        STAGE_DEFS["DATASET"],
        "a",
        row_id,
        {"dataset_name": "my_view", "source": source},
        flow.principal,
    )
    await flow.review("a", "DATASET")
    await flow.complete("a", "DATASET")
    before = await flow.rows("a", "DATASET")

    def no_discovery(*args, **kwargs):
        pytest.fail("dataset_field must not run Dataset discovery or call an LLM")

    monkeypatch.setattr(AlgoProfiler, "build", no_discovery)
    ctx = replace(await flow.context("a"), mode=mode)
    await AlgoProvider().probe_datasets_fields(ctx, await flow.sink("a"))
    rows = await flow.rows("a", "DATASET_FIELDS")
    assert {r["field_name"] for r in rows} == names
    assert {r["dataset_name"] for r in rows} == {"my_view"}
    assert {r["probe_status"] for r in rows} == {"PENDING"}
    assert await flow.rows("a", "DATASET") == before
    assert not await flow.formal(dataset_field)
    # SUM 等计算字段可以没有描述，确认后应直接晋升正式字段。
    if names == {"total"}:
        assert rows[0]["description"] is None
    await flow.review("a", "DATASET_FIELDS")
    await flow.complete("a", "DATASET_FIELDS")
    [parent] = await flow.formal(dataset)
    assert parent["dataset_name"] == "my_view" and parent["source"] == source
    formal = await flow.formal(dataset_field)
    assert {r["field_name"] for r in formal} == names
    assert {r["dataset_id"] for r in formal} == {parent["id"]}
    assert {r["field_name"]: r["description"] for r in formal} == {
        r["field_name"]: r["description"] for r in rows
    }


async def test_three_sources_same_logical_and_physical_name_stay_local(
    business_flow,
    orders_table,
    dataset_field_tables,
):
    flow = business_flow
    for source in "abc":
        table = orders_table(source)
        table.columns[0]["column_name"] = f"country_{source}"
        await flow.add_source(source, [table])
        await confirm_domain(flow, source)
        await stage_dataset(flow, source, dataset_name="public.orders")
        await flow.review(source, "DATASET")
        await flow.complete(source, "DATASET")
        await AlgoProvider().probe_datasets_fields(
            await flow.context(source), await flow.sink(source)
        )
        assert {r["field_name"] for r in await flow.rows(source, "DATASET_FIELDS")} == {
            f"country_{source}",
            "amount",
        }
        await flow.review(source, "DATASET_FIELDS")
        await flow.complete(source, "DATASET_FIELDS")
    parents = await flow.formal(dataset)
    assert len(parents) == 3 and len({p["domain_id"] for p in parents}) == 1
    for parent in parents:
        names = {
            r["field_name"]
            for r in await flow.formal(dataset_field)
            if r["dataset_id"] == parent["id"]
        }
        assert names == {f"country_{parent['datasource_id']}", "amount"}


async def test_changed_physical_source_and_review_filter(
    business_flow, orders_table, dataset_field_tables
):
    flow = business_flow
    replacement = orders_table("a", physical="public.refunds")
    replacement.columns = [dict(replacement.columns[1], column_name="refunded")]
    await flow.add_source("a", [orders_table("a"), replacement])
    active = await stage_dataset(flow, dataset_name="edited", source="public.refunds")
    skipped = await stage_dataset(flow, dataset_name="skipped", source="missing")
    deleted = await stage_dataset(flow, dataset_name="deleted", source="missing")
    await stage_dataset(flow, dataset_name="pending", source="missing")
    await flow.review("a", "DATASET", ids=[active, deleted])
    await flow.review("a", "DATASET", confirm=False, ids=[skipped])
    await row_service.delete_row(
        flow.session, STAGE_DEFS["DATASET"], "a", deleted, flow.principal
    )
    await AlgoProvider().probe_datasets_fields(
        await flow.context("a"), await flow.sink("a")
    )
    rows = await flow.rows("a", "DATASET_FIELDS")
    assert [(r["dataset_name"], r["field_name"]) for r in rows] == [
        ("edited", "refunded")
    ]


async def test_bad_source_does_not_emit_partial_batch(
    business_flow, orders_table, dataset_field_tables
):
    flow = business_flow
    await flow.add_source("a", [orders_table("a")])
    await stage_dataset(flow, dataset_name="good")
    bad_id = await stage_dataset(
        flow, dataset_name="bad", source="SELECT missing FROM public.orders"
    )
    await flow.review("a", "DATASET")
    before = await flow.rows("a", "DATASET")
    with pytest.raises(ValueError, match=f"bad.*{bad_id}"):
        await AlgoProvider().probe_datasets_fields(
            await flow.context("a"), await flow.sink("a")
        )
    assert not await flow.rows("a", "DATASET_FIELDS")
    assert await flow.rows("a", "DATASET") == before


async def test_loader_uses_current_connection_and_only_loads_physical_metadata(
    monkeypatch,
):
    tables = physical_snapshot("public.orders")
    load = AsyncMock(return_value=tables)
    config_args = {}
    context_args = {}

    def config(**kwargs):
        config_args.update(kwargs)
        return SimpleNamespace(parser_dialect="postgres")

    def contexts(cfg, **kwargs):
        context_args.update(kwargs)
        return SimpleNamespace(config=cfg, source_table=SimpleNamespace(load=load))

    monkeypatch.setattr(AlgoConfig, "build", config)
    monkeypatch.setattr(AlgoContexts, "build", contexts)
    principal = SimpleNamespace(tenant_id="t1", active_workspace_id="w1")
    ctx = ProbeContext(
        "b",
        "postgres",
        {"host": "current"},
        principal,
        facts={"staged_dataset": pd.DataFrame()},
    )
    result, dialect = await AlgoProvider()._load_dataset_field_tables(ctx)
    assert result is tables and dialect == "postgres"
    assert config_args["datasource_id"] == "b"
    assert config_args["datasource_principal"] is ctx.principal
    assert config_args["datasource_connection"] == ctx.connection
    assert context_args["facts"] is ctx.facts
    load.assert_awaited_once()


async def test_no_confirmed_dataset_does_not_load_or_emit(monkeypatch):
    loader = AsyncMock(side_effect=AssertionError("must not fetch source metadata"))
    monkeypatch.setattr(AlgoProvider, "_load_dataset_field_tables", loader)
    sink = SimpleNamespace(emit_dataset_fields=AsyncMock())
    ctx = ProbeContext("a", "postgres", {}, None)
    await AlgoProvider().probe_datasets_fields(ctx, sink)
    loader.assert_not_called()
    sink.emit_dataset_fields.assert_not_called()


async def test_all_confirmed_rows_are_processed_without_name_or_latest_task_dedup(
    monkeypatch,
):
    monkeypatch.setattr(
        AlgoProvider,
        "_load_dataset_field_tables",
        AsyncMock(return_value=(physical_snapshot("public.orders"), "postgres")),
    )
    rows = [
        {
            "id": 1,
            "task_id": "old",
            "dataset_name": "same_view",
            "source": "SELECT country FROM public.orders",
            "probe_status": "CONFIRMED",
        },
        {
            "id": 2,
            "task_id": "new",
            "dataset_name": "same_view",
            "source": "SELECT amount FROM public.orders",
            "probe_status": "CONFIRMED",
        },
        {
            "id": 3,
            "dataset_name": "other_source",
            "source": "missing",
            "probe_status": "CONFIRMED",
            "datasource_id": "b",
        },
    ]
    ctx = ProbeContext(
        "a", "postgres", {}, None, facts={"staged_dataset": pd.DataFrame(rows)}
    )
    sink = SimpleNamespace(emit_dataset_fields=AsyncMock())
    await AlgoProvider().probe_datasets_fields(ctx, sink)
    [items] = sink.emit_dataset_fields.call_args.args
    assert [
        (item.dataset_name, [field.field_name for field in item.fields])
        for item in items
    ] == [
        ("same_view", ["country"]),
        ("same_view", ["amount"]),
    ]


async def test_real_context_loader_never_fetches_query_logs(tmp_path, monkeypatch):
    from profiling.algo.inputs import QueryLogInput, TableInput

    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    tables = physical_snapshot("public.orders")
    fetch_tables = AsyncMock(return_value=tables)
    fetch_logs = AsyncMock(side_effect=AssertionError("must not load query logs"))
    cfg = AlgoConfig(
        _env_file=None,
        datasource_type="sqlite",
        store_out_dir=str(tmp_path),
        store_excel_enabled=False,
        llm_api_key="",
    )
    monkeypatch.setattr(
        AlgoConfig, "build", lambda **kwargs: cfg.model_copy(update=kwargs)
    )
    monkeypatch.setattr(
        TableInput, "create", lambda config: SimpleNamespace(fetch=fetch_tables)
    )
    monkeypatch.setattr(
        QueryLogInput, "create", lambda config: SimpleNamespace(fetch=fetch_logs)
    )
    progress = AsyncMock()
    principal = SimpleNamespace(tenant_id="t1", active_workspace_id="w1")
    ctx = ProbeContext("a", "sqlite", {}, principal, report_progress=progress)
    result, _ = await AlgoProvider()._load_dataset_field_tables(ctx)
    assert result is tables
    fetch_tables.assert_awaited_once()
    fetch_logs.assert_not_called()


async def test_cancellation_before_emit_does_not_save_candidates(monkeypatch):
    from profiling.provider import CancelledProbe

    monkeypatch.setattr(
        AlgoProvider,
        "_load_dataset_field_tables",
        AsyncMock(return_value=(physical_snapshot("public.orders"), "postgres")),
    )
    cancelled = False

    def check():
        if cancelled:
            raise CancelledProbe()

    async def progress(pct, message):
        nonlocal cancelled
        if pct == 10:
            cancelled = True

    ctx = ProbeContext(
        "a",
        "postgres",
        {},
        None,
        facts={
            "staged_dataset": pd.DataFrame(
                [
                    {
                        "id": 1,
                        "dataset_name": "orders",
                        "source": "public.orders",
                        "probe_status": "CONFIRMED",
                    }
                ]
            )
        },
        raise_if_cancelled=check,
        report_progress=progress,
    )
    sink = SimpleNamespace(emit_dataset_fields=AsyncMock())
    with pytest.raises(CancelledProbe):
        await AlgoProvider().probe_datasets_fields(ctx, sink)
    sink.emit_dataset_fields.assert_not_called()
