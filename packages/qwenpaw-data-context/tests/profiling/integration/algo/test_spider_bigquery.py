"""spider2-lite BigQuery：schema JSON 构快照 → ga360 四任务全流程，零 SQL。

    cd packages/qwenpaw-data-context
    PYTHONPATH=src .venv/bin/python tests/profiling/integration/algo/test_spider_bigquery.py

唯一前提：完整 Spider2 仓库在 ``data/Spider2``。spider 给每张表都发了
schema JSON（``spider2-lite/resource/databases/bigquery/...``），表快照由
:class:`~..inputs.BigQueryJsonTableInput` 直接从文件构造 —— 不需要
execute_sql、不需要凭证、零 BigQuery 查询费。（真连凭证在
``Spider2/credentials/``，真连路径住 ``inputs.BigQuerySourceTableInput``，
本测试不用。）

spider 三条前提 → 配置（与 ``test_spider_sqlite`` 相同）：所有表直接就是
dataset（全量直通白名单 ``("%",)``）；查询日志 = 评测集的 gold SQL
（``input_query_log_datasource_type="sql_files"``，按 ``spider2-lite.jsonl``
的 ``db`` 过滤，ga360 有 12 条；BigQuery 的 ``ga_sessions_201707*`` 这类
分片形态由 parser 按方言折成 ``ga_sessions_*``，与 JSON 侧族名对齐）；
没有 probe facts（``profiler.build()`` 装配路径本就不加载）。

LLM 是真实客户端（.env 的 OPENAI_API_KEY），会产生费用；响应缓存在
``{out_dir}/llm/``，参数不变的重跑近乎免费。out_dir 固定在包内
``data/profile/spider2/ga360``（已被 .gitignore 覆盖）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

from profiling.algo.config import AlgoConfig
from profiling.algo.handlers.llm import OpenAiCompatClient
from profiling.algo.inputs.query_log_sql_files import SqlFileQueryLogInput
from profiling.algo.inputs.table_input_bigquery import BigQueryJsonTableInput
from profiling.algo.algo_contexts import AlgoContexts
from profiling.algo.algo_profiler import AlgoProfiler
from profiling.algo.sql import (
    ProjectSqlExecutor,
    SqlExecutor,
    default_execute_sql_func_builder,
)
from profiling.algo.snapshots.table import REQUIRED_COLUMN_META
from profiling.algo.stores import TextStore
from profiling.algo.tasks import TASK_ORDER

pytestmark = pytest.mark.skip(
    reason="manual Spider2 integration requires external data and a paid live LLM"
)

log = logging.getLogger("test_spider_bigquery")

_PKG = Path(__file__).resolve().parents[4]
SPIDER2 = _PKG.parents[1] / "data" / "Spider2"
SCHEMA_ROOT = SPIDER2 / "spider2-lite" / "resource" / "databases" / "bigquery"
#: 与 test_spider_sqlite 分目录（spider2/bigquery vs spider2/sqlite），
#: 两个数据集的产物与缓存互不干扰
OUT_ROOT = _PKG / "data" / "profile" / "spider2" / "bigquery"
GOLD_INDEX = SPIDER2 / "spider2-lite" / "spider2-lite.jsonl"
GOLD_DIR = SPIDER2 / "spider2-lite" / "evaluation_suite" / "gold" / "sql"

#: 冒烟默认跑 ga360：逻辑表少（LLM 便宜），但覆盖分片表族
#: （ga_sessions_YYYYMMDD × 366 → ga_sessions_*）与 STRUCT 列。
DEFAULT_DB = "ga360"

#: spider 数据全英文 → 产物必须全英文；出现 CJK 即语言开关失效
_CJK = re.compile(r"[一-鿿]")


class _CountingLlm(OpenAiCompatClient):
    """真实客户端 + 真实调用计数（缓存命中不计），同 ``test_holo_pg``。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.live_calls = 0

    def _call_sync(
        self, system_prompt: str, user_prompt: str, temperature: float
    ) -> str:
        self.live_calls += 1
        return super()._call_sync(system_prompt, user_prompt, temperature)


def spider_config(db_name: str) -> AlgoConfig:
    """spider 前提落成的配置。schema 目录就是全部「连接信息」。"""
    return AlgoConfig.build(
        datasource_id=db_name,  # 只用来分 out_dir，各库一格
        datasource_type="bigquery_json",
        execute_sql_path="",  # JSON 来源零 SQL
        datasource_connection={"schema_dir": str(SCHEMA_ROOT / db_name)},
        # -- spider 数据全英文，产出（domain / 业务名 / 描述）也全英文 --
        llm_output_language="en",
        # -- 所有表直接就是 dataset；gold SQL 另作日志式 dataset 来源 --
        dataset_derived_from_tables_enabled=True,
        dataset_derived_from_tables_include_patterns=("%",),
        dataset_parse_from_logs_enabled=True,
        # -- 查询日志 = 评测集 gold SQL（文件来源，零 SQL 执行）--
        input_query_log_datasource_type="sql_files",
        input_query_log_path=str(GOLD_INDEX),
        input_query_log_dir=str(GOLD_DIR),
        input_query_log_dbname=db_name,
        # -- 1~3 条 gold 击穿了「十万条日志」的默认阈值 --
        parser_template_min_count=1,
        parser_template_min_columns=3,
        # -- 解析方言：分片形态按 bigquery 折成族名 --
        parser_dialect="bigquery",
        # -- 落盘：只留三个任务产物；输入层快照与直通 dataset 不落盘 --
        store_out_dir=str(OUT_ROOT),
        store_include_contexts=(
            "domain_staged",
            "columns_caliber_staged",
            "columns_dict_staged",
        ),
    )


# ===========================================================================
# inputs 冒烟：JSON → 快照的关键事实
# ===========================================================================


async def test_inputs() -> None:
    """固定用 ga360 验证 JSON → 快照的关键事实。"""
    options = AlgoConfig.build(
        datasource_type="bigquery_json",
        execute_sql_path="",  # JSON 来源零 SQL
        datasource_connection={"schema_dir": str(SCHEMA_ROOT / DEFAULT_DB)},
        input_query_log_max_count=10,
    )
    # JSON 来源零 SQL：不需要任何执行器
    snap = await BigQueryJsonTableInput(options).fetch()
    all_cols = snap.all_column_meta()
    missing = [c for c in REQUIRED_COLUMN_META if c not in all_cols.columns]
    assert not missing, f"列元数据缺 {missing}"
    assert "ga_sessions_*" in snap.names(), f"分片族未归并: {sorted(snap.names())}"

    ga = snap.column_meta_of("ga_sessions_*").set_index("column_name")
    # 列面 = nested 的叶子路径：指标都在叶子层（totals.visits 等）；
    # totals / hits 是 STRUCT/ARRAY 容器行，不是可取值的列，必须滤掉
    assert "totals.visits" in ga.index, "nested 叶子列没进列面"
    assert "channelGrouping" in ga.index, "顶层标量列丢失"
    assert "totals" not in ga.index and "hits" not in ga.index, "容器行未滤除"
    # description 与 nested 对齐，叶子直接命中；GA 有少量字段官方就没写注释，
    # 断言多数覆盖而不是全覆盖
    assert ga.loc["totals.visits", "column_comment"], "叶子列注释未对齐进来"
    assert ga["column_comment"].notna().mean() > 0.5, "列注释覆盖率过低"
    # 枚举来自 sample_rows 的真实取值（嵌套路径按段下钻），计数未知按 None
    assert len(ga.loc["channelGrouping", "column_enums"]) > 0
    assert len(ga.loc["totals.visits", "column_enums"]) > 0, "嵌套路径采样未下钻"
    assert ga.loc["channelGrouping", "column_distinct_num"] is None
    # 通配 fullname 就是真实查询里写的表路径
    meta = snap.table_meta.set_index("table_name")
    assert str(meta.loc["ga_sessions_*", "table_fullname"]).endswith(".ga_sessions_*")
    log.info(
        "inputs 通过：%d 逻辑表，%d 列，注释覆盖 %d 列",
        len(snap.table_meta),
        len(all_cols),
        int(all_cols["column_comment"].notna().sum()),
    )


# ===========================================================================
# sql 执行器：分派面冒烟（零凭证 —— 不构造真 BigQuery 客户端）
# ===========================================================================


async def test_sql_executor() -> None:
    """bigquery/bq 键已认领；``bigquery_json`` 刻意无执行器（JSON 来源零 SQL）。"""
    supported = SqlExecutor.supported()
    assert {"bigquery", "bq", "sqlite", "postgresql", "pg"} <= supported, (
        f"方言键缺失: {supported}"
    )

    # bigquery_json 走注册分派会硬失败 —— 它只该经 BigQueryJsonTableInput 用
    try:
        SqlExecutor.create("bigquery_json", {})
    except ValueError as exc:
        assert "bigquery_json" in str(exc)
    else:
        raise AssertionError("bigquery_json 不该有自建执行器")

    # 注入路线与 BigQuery 零耦合：契约桩直接适配（本测试全程零凭证）
    async def stub(sql, *, max_rows=None):
        return pd.DataFrame({"n": [1]})

    built = default_execute_sql_func_builder("bigquery_json", execute_sql=stub)
    assert isinstance(built, ProjectSqlExecutor)
    assert len(await built("SELECT 1")) == 1
    log.info("sql 执行器分派面通过：键认领 / json 无执行器 / 注入零耦合")


# ===========================================================================
# 四任务全流程
# ===========================================================================


async def test_pipeline(db_name: str) -> None:
    config = spider_config(db_name)
    assert config.llm_enabled, (
        "缺少 LLM api key —— .env 里配 OPENAI_API_KEY，否则 handler 层"
        "按 D4 降级，直通链路测不到真产物"
    )

    llm = _CountingLlm(
        model=config.llm_model,
        api_key=config.llm_api_key,
        base_url=config.llm_base_url,
        store=TextStore(config.datasource_out_dir / "llm"),
        max_attempts=config.llm_max_attempts,
    )
    # 表来源零 SQL（JSON）；日志来源零 SQL（文件）—— 整条流水线不执行任何 SQL
    profiler = AlgoProfiler.build(AlgoContexts.build(config), llm=llm)
    assert set(profiler.handlers) == set(TASK_ORDER)

    # -- 输入层：表来自 JSON，日志按 spider 前提必须为空 -------------------
    tables = await profiler.contexts.source_table.load()
    n_tables = len(tables.names())
    assert n_tables > 0, "表层为空"
    if db_name == DEFAULT_DB:
        assert "ga_sessions_*" in tables.names(), (
            f"分片族未归并: {sorted(tables.names())}"
        )

    # -- 日志层：gold SQL 有几条就该有几条原始日志；证据表名必须落在表面上 --
    expected_gold = len(SqlFileQueryLogInput.entries(config))
    raw_logs = await profiler.contexts.query_log_raw.load()
    assert len(raw_logs.logs) == expected_gold, (
        f"原始日志 {len(raw_logs.logs)} 条 ≠ gold {expected_gold} 条"
    )
    logs = await profiler.contexts.query_log.load()
    counts = {
        kind: len(logs.of(kind))
        for kind in ("join", "sql_template", "dimension_usage", "metric_expr")
    }
    if expected_gold:
        assert any(counts.values()), (
            f"有 {expected_gold} 条 gold 却没有任何证据: {counts}"
        )
        assert len(logs.failed) == 0, (
            f"gold SQL 解析失败: {logs.failed.to_dict('records')}"
        )
        referenced = {
            str(n)
            for kind in ("join", "dimension_usage", "metric_expr")
            for col in ("left_table", "right_table", "table_name")
            if col in logs.of(kind).columns
            for n in logs.of(kind)[col]
            if str(n) != "_"
        }
        assert referenced <= tables.names(), (
            f"证据表名未与表面对齐（分片归一失效？）: {sorted(referenced - tables.names())}"
        )
    else:
        assert not any(counts.values()), f"无 gold 的库不该有证据，实得 {counts}"
    log.info(
        "%s：表 %d 张，gold SQL %d 条 → 证据 %s ✓",
        db_name,
        n_tables,
        expected_gold,
        counts,
    )

    # -- 四个任务 --------------------------------------------------------
    outcomes = {o.task: o for o in await profiler.run_all()}
    for task in TASK_ORDER:
        assert outcomes[task].executed, f"{task.value}: {outcomes[task].reason}"

    # 持久化的三个产物用 refresh=True 走一遍 parquet 回读；dataset 不落盘，
    # 只能读进程内产物（refresh 会绕过 _current 而无处回读 → 空）
    domains = await profiler.contexts.domain_staged.load(refresh=True)
    datasets = await profiler.contexts.dataset_staged.load()
    calibers = await profiler.contexts.columns_caliber_staged.load(refresh=True)
    dictionary = await profiler.contexts.columns_dict_staged.load(refresh=True)

    # 直通：表名子集；日志式：来自 gold SQL 的视图，sql_content 必须是 SQL。
    # 域覆盖是软门禁，容许个别表被跳过
    names = datasets.names()
    passthrough, derived = names & tables.names(), names - tables.names()
    assert len(passthrough) >= max(1, n_tables // 2), (
        f"直通 dataset 仅 {len(passthrough)}/{n_tables}，domain 归属大面积缺失"
    )
    if derived:
        meta = datasets.table_meta
        derived_meta = meta[meta["table_name"].astype(str).isin(derived)]
        assert "sql_content" in meta.columns and (
            derived_meta["sql_content"]
            .astype(str)
            .str.contains("select", case=False)
            .all()
        ), f"日志式 dataset 缺 SQL: {sorted(derived)}"
    log.info(
        "%s：直通 %d + 日志式 %d 个 dataset", db_name, len(passthrough), len(derived)
    )
    if db_name == DEFAULT_DB:
        assert "ga_sessions_*" in passthrough, "分片族没走到 dataset"
    assert len(domains.domains) > 0, "domain 产物为空"
    assert len(calibers.calibers) > 0, "caliber 产物为空"
    assert len(dictionary.dimensions) + len(dictionary.metrics) > 0, "dict 产物为空"

    # -- v2.1 契约（mg-087）：窗口/可加性值域 + 维度样例值列 ----------------
    cal = calibers.calibers
    for col in ("time_window", "expr", "additivity", "value_example"):
        assert col in cal.columns, f"caliber 缺 v2.1 契约列 {col}"
    metric_rows = cal[cal["column_role"].astype(str) == "metric"]
    if len(metric_rows):
        windows = {str(w) for w in metric_rows["time_window"].dropna()}
        assert windows <= {"1d", "7d", "30d", "mtd"}, f"time_window 越界: {windows}"
        addv = {str(a) for a in metric_rows["additivity"].dropna()}
        assert addv <= {"additive", "semi_additive", "non_additive"}, (
            f"additivity 越界: {addv}"
        )
        assert metric_rows["time_window"].notna().all(), "metric 行缺 time_window"
    dim_rows = dictionary.dimensions
    if len(dim_rows):
        roles = {str(r) for r in dim_rows["dimension_role"].dropna()}
        assert roles <= {"categorical", "time"}, f"dimension_role 越界: {roles}"

    # -- 产出语言：spider 全英文，named 产物不得出现 CJK ------------------
    named_outputs = "\n".join(
        frame.to_string()
        for frame in (domains.domains, dictionary.dimensions, dictionary.metrics)
    )
    hit = _CJK.search(named_outputs)
    assert hit is None, f"产物含中文（llm_output_language=en 未生效）: …{hit.group()}…"

    # -- 落盘：只有三个任务产物；输入层与 dataset 不该出现 -----------------
    for name in ("domain_staged", "columns_caliber_staged", "columns_dict_staged"):
        assert list((config.datasource_out_dir / name).glob("*.parquet")), (
            f"{name} 未落盘"
        )
    for name in ("source_table", "dataset_staged", "query_log", "query_log_raw"):
        assert not (config.datasource_out_dir / name).exists(), f"{name} 不该落盘"

    log.info(
        "%s 全流程 ✓ domain=%d 域 / dataset=%d/%d 直通(+日志式) / caliber=%d 列 / "
        "dict=%d 维度+%d 指标 / LLM 真实调用 %d 次（其余命中缓存）→ %s",
        db_name,
        len(domains.domains),
        len(names),
        n_tables,
        len(calibers.calibers),
        len(dictionary.dimensions),
        len(dictionary.metrics),
        llm.live_calls,
        config.datasource_out_dir,
    )


async def run_all_dbs() -> None:
    """全部 bigquery 库逐个跑三任务链路。单库失败不拖垮批次，收尾汇总。"""
    dbs = sorted(p.name for p in SCHEMA_ROOT.iterdir() if p.is_dir())
    failed: dict[str, str] = {}
    for index, db_name in enumerate(dbs, start=1):
        log.info("===== bigquery [%d/%d] %s =====", index, len(dbs), db_name)
        try:
            await test_pipeline(db_name)
        except Exception as exc:  # noqa: BLE001 - 批量模式逐库容错
            failed[db_name] = f"{type(exc).__name__}: {exc}"
            log.error("%s 失败: %s", db_name, exc)
    log.info("bigquery 批量完成：%d/%d 通过", len(dbs) - len(failed), len(dbs))
    for db_name, reason in failed.items():
        log.error("  失败 %s: %s", db_name, reason)
    if failed:
        raise SystemExit(1)


async def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    assert SCHEMA_ROOT.is_dir(), f"Spider2 仓库没就绪: {SCHEMA_ROOT}"
    if target == "all":
        await test_inputs()
        await test_sql_executor()
        await run_all_dbs()
    else:
        await test_inputs()
        await test_sql_executor()
        await test_pipeline(target)
    log.info("全部通过")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-7s %(name)s | %(message)s"
    )
    asyncio.run(main())
