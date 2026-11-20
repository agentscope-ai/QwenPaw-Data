"""普通 PostgreSQL 探查的可选查询日志与进度回归。"""

from __future__ import annotations

import asyncio
from datetime import date

import pandas as pd
import pytest


def _config(tmp_path, execute_sql, **overrides):
    from profiling.algo.config import AlgoConfig

    fields = {
        "_env_file": None,
        "datasource_id": "ut-postgresql",
        "datasource_type": "postgresql",
        "datasource_connection": {"database": "app"},
        "execute_sql_path": "",
        "execute_sql_func": execute_sql,
        "store_out_dir": str(tmp_path),
        "store_excel_enabled": False,
    }
    fields.update(overrides)
    return AlgoConfig(**fields)


def test_query_log_default_window_is_latest_90_days_including_today(tmp_path):
    async def execute_sql(sql: str, *, max_rows: int | None = None):
        raise AssertionError("只验证窗口解析，不应执行 SQL")

    config = _config(tmp_path, execute_sql)

    assert config.input_query_log_start_ts is None
    assert config.input_query_log_end_ts is None
    assert config.resolved_query_log_window(today=date(2026, 9, 2)) == (
        "20260605",
        "20260902",
        "20260903",
    )


def test_algo_config_build_locks_window_and_next_run_rolls_cache(tmp_path, monkeypatch):
    from profiling.algo import config as config_module
    from profiling.algo.inputs import QueryLogInput

    clock = {"today": date(2026, 9, 2)}

    async def execute_sql(sql: str, *, max_rows: int | None = None):
        raise AssertionError("只验证窗口与指纹，不应执行 SQL")

    baseline = _config(tmp_path, execute_sql)
    monkeypatch.setattr(config_module, "_baseline", lambda: baseline)
    monkeypatch.setattr(config_module, "_local_today", lambda: clock["today"])

    first = config_module.AlgoConfig.build(datasource_id="first")
    first_fingerprint = QueryLogInput.fingerprint(first)
    first_sql = QueryLogInput.create(first).build_sql()
    clock["today"] = date(2026, 9, 3)

    # 已构建任务跨午夜后仍使用原窗口；下一次 build 才自然滚动。
    assert QueryLogInput.fingerprint(first) == first_fingerprint
    assert QueryLogInput.create(first).build_sql() == first_sql
    second = config_module.AlgoConfig.build(datasource_id="second")
    second_fingerprint = QueryLogInput.fingerprint(second)

    assert (first.input_query_log_start_ts, first.input_query_log_end_ts) == (
        "20260605",
        "20260902",
    )
    assert (second.input_query_log_start_ts, second.input_query_log_end_ts) == (
        "20260606",
        "20260903",
    )
    assert first_fingerprint["query_log_window_semantics"] == "half_open_v1"
    assert first_fingerprint != second_fingerprint
    assert "query_date < '20260903'" in first_sql
    assert "query_date < '20260904'" in QueryLogInput.create(second).build_sql()


def test_query_log_explicit_inclusive_window_becomes_half_open_sql(tmp_path):
    from profiling.algo.inputs import QueryLogInput

    async def execute_sql(sql: str, *, max_rows: int | None = None):
        raise AssertionError("只验证 SQL 构造，不应执行 SQL")

    config = _config(
        tmp_path,
        execute_sql,
        input_query_log_start_ts="20260601",
        input_query_log_end_ts="20260801",
    )
    source = QueryLogInput.create(config)
    sql = source.build_sql()

    assert config.resolved_query_log_window() == (
        "20260601",
        "20260801",
        "20260802",
    )
    assert "query_date >= '20260601'" in sql
    assert "query_date < '20260802'" in sql
    assert "query_date <=" not in sql


def test_query_log_window_env_dates_keep_inclusive_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("INPUT_QUERY_LOG_START_TS", "20261201")
    monkeypatch.setenv("INPUT_QUERY_LOG_END_TS", "20261231")

    async def execute_sql(sql: str, *, max_rows: int | None = None):
        raise AssertionError("只验证 env 配置，不应执行 SQL")

    config = _config(tmp_path, execute_sql)

    assert config.resolved_query_log_window() == (
        "20261201",
        "20261231",
        "20270101",
    )


def test_sql_files_fingerprint_stays_independent_of_query_window(tmp_path):
    from profiling.algo.inputs import QueryLogInput

    async def execute_sql(sql: str, *, max_rows: int | None = None):
        raise AssertionError("文件日志不应执行 SQL")

    config = _config(
        tmp_path,
        execute_sql,
        datasource_type="sqlite",
        input_query_log_datasource_type="sql_files",
    )
    fingerprint = QueryLogInput.fingerprint(config)

    assert "input_query_log_start_ts" not in fingerprint
    assert "input_query_log_end_ts" not in fingerprint
    assert "query_log_window_semantics" not in fingerprint


@pytest.mark.parametrize(
    "overrides, error",
    [
        ({"input_query_log_start_ts": "2026-06-01"}, "yyyymmdd"),
        ({"input_query_log_end_ts": "20260230"}, "有效"),
        (
            {
                "input_query_log_start_ts": "20260903",
                "input_query_log_end_ts": "20260902",
            },
            "不能晚于",
        ),
        ({"input_query_log_max_days": 0}, "greater than 0|必须大于 0"),
    ],
)
def test_query_log_window_rejects_invalid_configuration(tmp_path, overrides, error):
    async def execute_sql(sql: str, *, max_rows: int | None = None):
        raise AssertionError("只验证窗口解析，不应执行 SQL")

    with pytest.raises(ValueError, match=error):
        _config(tmp_path, execute_sql, **overrides).resolved_query_log_window(
            today=date(2026, 9, 2)
        )


def test_plain_postgres_without_hologres_log_table_degrades_to_empty(tmp_path):
    from profiling.algo.inputs import QueryLogInput

    calls: list[str] = []

    async def execute_sql(sql: str, *, max_rows: int | None = None):
        calls.append(sql)
        if "to_regclass" in sql:
            return pd.DataFrame({"query_log_relation": [None]})
        raise AssertionError("普通 PostgreSQL 不应查询 Hologres 日志表")

    source = QueryLogInput.create(_config(tmp_path, execute_sql))
    snapshot = asyncio.run(source.fetch())

    assert snapshot.is_empty()
    assert len(calls) == 1
    assert "to_regclass" in calls[0]


def test_postgres_query_log_cancellation_is_not_degraded(tmp_path):
    from profiling.algo.inputs import QueryLogInput

    async def execute_sql(sql: str, *, max_rows: int | None = None):
        raise asyncio.CancelledError

    source = QueryLogInput.create(_config(tmp_path, execute_sql))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(source.fetch())


def test_postgres_input_progress_keeps_loading_baseline(tmp_path):
    from profiling.algo import AlgoContexts
    from profiling.algo.hooks import RunHooks

    async def execute_sql(sql: str, *, max_rows: int | None = None):
        assert "to_regclass" in sql
        return pd.DataFrame({"query_log_relation": [None]})

    progress: list[tuple[int, str]] = []
    contexts = AlgoContexts.build(
        _config(tmp_path, execute_sql),
        hooks=RunHooks(
            report_progress=lambda pct, message: progress.append((pct, message))
        ),
    )

    snapshot = asyncio.run(contexts.query_log_raw.load())

    assert snapshot.is_empty()
    assert progress == [(5, "正在拉取查询日志…")]


def test_query_log_parser_progress_stays_in_loading_phase(tmp_path):
    from profiling.algo import AlgoContexts
    from profiling.algo.hooks import RunHooks

    calls: list[str] = []

    async def execute_sql(sql: str, *, max_rows: int | None = None):
        calls.append(sql)
        if "to_regclass" in sql:
            return pd.DataFrame({"query_log_relation": ["hologres.hg_query_log"]})
        return pd.DataFrame(
            {
                "query": [
                    "SELECT customer_id FROM customers",
                    "SELECT order_id FROM orders",
                    "SELECT amount FROM orders",
                    "SELECT region FROM customers",
                ]
            }
        )

    progress: list[int] = []
    contexts = AlgoContexts.build(
        _config(tmp_path, execute_sql),
        hooks=RunHooks(report_progress=lambda pct, message: progress.append(pct)),
    )

    asyncio.run(contexts.query_log.load())

    assert len(calls) == 2
    assert "to_regclass" in calls[0]
    assert "FROM hologres.hg_query_log" in calls[1]
    assert progress == sorted(progress)
    assert progress[0] == 5
    assert max(progress) == 9


def test_llm_unit_progress_is_scaled_between_algorithm_and_mapping_phases():
    from profiling.algo.algo_profiler import default_handlers
    from profiling.algo.handlers.runner import LlmUnit
    from profiling.algo.tasks import ProfileTask

    class FakeLlm:
        async def complete(
            self,
            system_prompt: str,
            user_prompt: str,
            *,
            temperature: float,
        ) -> str:
            await asyncio.sleep(0)
            return "{}"

    progress = [10]
    handlers = default_handlers(
        FakeLlm(),
        concurrency=1,
        report_progress=lambda pct, message: progress.append(pct),
    )
    runner = handlers[ProfileTask.DATASET]._runner
    assert runner is not None

    async def run_two_batches() -> None:
        await runner.run_units(
            [
                LlmUnit("one", "system", "user"),
                LlmUnit("two", "system", "user"),
            ]
        )
        await runner.run_units([LlmUnit("three", "system", "user")])

    asyncio.run(run_two_batches())
    progress.extend([85, 100])

    assert progress == sorted(progress)
    assert 80 in progress
