"""spider2-lite 本地 sqlite 库：inputs 冒烟（全部 30 库）+ 单库四任务全流程。

    cd packages/qwenpaw-data-context
    PYTHONPATH=src .venv/bin/python tests/profiling/integration/algo/test_spider_sqlite.py [库名]

库名默认 ``IPL``（带 2 条 gold SQL，join / 用法证据都能点亮；chinook 这类
无 gold 的库同样能跑，日志按空降级）。唯一前提：完整 Spider2 仓库在
``data/Spider2``，本地库在 ``spider2-lite/resource/databases/spider2-localdb/``。
冒烟不探枚举（30 库里有 Airlines 这类百兆库，DISTINCT 全扫太慢），
枚举链路由 IPL 定点探查与全流程库覆盖。

------------------------------------------------------------------------
spider 数据集的三条前提 → 三处配置
------------------------------------------------------------------------
- **所有表直接就是 dataset**：``dataset_derived_from_tables_enabled=True``
  且白名单 ``("%",)`` 全量直通，``dataset_parse_from_logs_enabled=False``。
  直通对象要有 domain 才落 dataset（handlers/dataset.py 先归属后直通），
  因此 DOMAIN 仍在链路里 —— 零 join 证据时所有表按「未聚类表」进 prompt，
  LLM 只靠表名与注释归域。
- **查询日志 = 评测集的 gold SQL**：``input_query_log_datasource_type=
  "sql_files"`` 走文件来源，按 ``spider2-lite.jsonl`` 的 ``db`` 过滤出本库
  的 1~3 条 ground-truth SQL（``evaluation_suite/gold/sql/``）。条数太少，
  模板阈值降到 1、投影列门槛降到 3，并打开日志式 dataset 来源；无 gold 的
  库自动空降级（日志不在 ``HARD_REQUIREMENTS`` 里，只降级不阻断）。
- **没有 probe facts**：``profiler.build()`` 的装配路径本就不加载
  ``input_facts_path``，无需任何开关。

------------------------------------------------------------------------
费用与缓存
------------------------------------------------------------------------
LLM 是真实客户端（.env 的 OPENAI_API_KEY），会产生费用。out_dir 固定在
包内 ``data/profile/spider2/{库名}``（已被 .gitignore 覆盖）：
source_table 快照指纹命中即不重拉，LLM 响应缓存命中即不花钱 ——
参数不变的重跑近乎免费。跑完打印真实调用次数。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

from profiling.algo.config import AlgoConfig
from profiling.algo.handlers.llm import OpenAiCompatClient
from profiling.algo.inputs.query_log_sql_files import SqlFileQueryLogInput
from profiling.algo.inputs.table_input_sqlite import SqliteSourceTableInput
from profiling.algo.algo_contexts import AlgoContexts
from profiling.algo.algo_profiler import AlgoProfiler
from profiling.algo.sql import (
    ProjectSqlExecutor,
    SqlExecutor,
    default_execute_sql_func_builder,
)
from profiling.algo.stores import TextStore
from profiling.algo.snapshots.table import REQUIRED_COLUMN_META, REQUIRED_TABLE_META
from profiling.algo.tasks import TASK_ORDER

pytestmark = pytest.mark.skip(
    reason="manual Spider2 integration requires external data and a paid live LLM"
)

log = logging.getLogger("test_spider_sqlite")

_PKG = Path(__file__).resolve().parents[4]
SPIDER2 = _PKG.parents[1] / "data" / "Spider2"
DB_DIR = SPIDER2 / "spider2-lite" / "resource" / "databases" / "spider2-localdb"
#: 与 test_spider_bigquery 分目录（spider2/sqlite vs spider2/bigquery），
#: 两个数据集的产物与缓存互不干扰
OUT_ROOT = _PKG / "data" / "profile" / "spider2" / "sqlite"
DEFAULT_DB = "IPL"
GOLD_INDEX = SPIDER2 / "spider2-lite" / "spider2-lite.jsonl"
GOLD_DIR = SPIDER2 / "spider2-lite" / "evaluation_suite" / "gold" / "sql"

#: spider 数据全英文 → 产物必须全英文；出现 CJK 即语言开关失效
_CJK = re.compile(r"[一-鿿]")


class _CountingLlm(OpenAiCompatClient):
    """真实客户端 + 真实调用计数（缓存命中不计）。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.live_calls = 0

    def _call_sync(
        self, system_prompt: str, user_prompt: str, temperature: float
    ) -> str:
        self.live_calls += 1
        return super()._call_sync(system_prompt, user_prompt, temperature)


# ===========================================================================
# 第一段：inputs 冒烟 —— 30 个库全量 fetch（元数据，不探枚举）
# ===========================================================================


async def test_inputs() -> None:
    dbs = sorted(DB_DIR.glob("*.sqlite"))
    assert dbs, f"没找到本地库: {DB_DIR}"

    # local-map 里引用的库必须一一存在 —— 数据集自身的一致性
    local_map = json.loads((DB_DIR / "local-map.jsonl").read_text())
    mapped, on_disk = set(local_map.values()), {p.stem for p in dbs}
    assert mapped <= on_disk, f"local-map 引用了不存在的库: {mapped - on_disk}"

    total_tables = total_cols = 0
    snaps = {}
    for db in dbs:
        # 类型 + 连接参数即可：执行器由输入层构造时自建（自建路线）
        options = AlgoConfig.build(
            datasource_type="sqlite",
            execute_sql_path="",  # 走自建路线，不用项目执行器
            datasource_connection={"db_path": db},
            input_table_enums_enabled=False,
            input_query_log_max_count=10,
        )
        snap = snaps[db.stem] = await SqliteSourceTableInput(options).fetch()

        assert len(snap.table_meta), f"{db.stem}: 没拉到表"
        missing = [c for c in REQUIRED_TABLE_META if c not in snap.table_meta.columns]
        assert not missing, f"{db.stem}: 表元数据缺 {missing}"
        all_cols = snap.all_column_meta()
        missing = [c for c in REQUIRED_COLUMN_META if c not in all_cols.columns]
        assert not missing, f"{db.stem}: 列元数据缺 {missing}"
        assert not any(t.startswith("sqlite_") for t in snap.names()), (
            f"{db.stem}: 系统表未排除"
        )
        total_tables += len(snap.table_meta)
        total_cols += len(all_cols)
    log.info(
        "inputs：%d 库全部通过，表+视图 %d，列 %d", len(dbs), total_tables, total_cols
    )

    # 关键字表名：IPL 的 match 不引号会语法错误，枚举探到值即证明引号生效
    ipl = await SqliteSourceTableInput(
        AlgoConfig.build(
            datasource_type="sqlite",
            execute_sql_path="",  # 走自建路线，不用项目执行器
            datasource_connection={"db_path": DB_DIR / "IPL.sqlite"},
            input_table_enums_enabled=True,  # 本段测的就是枚举链路，不依赖默认值
            input_table_enums_max_distinct=21,
            input_query_log_max_count=10,
        ),
    ).fetch()
    toss = ipl.column_meta_of("match").set_index("column_name")
    assert set(toss.loc["toss_decision", "column_enums"]) == {"bat", "field"}
    # 无类型视图列（SQLite 允许）按 NULL 交出，不是空串冒充类型名
    assert snaps["Pagila"].column_meta_of("film_list")["data_type"].isna().any()
    log.info("inputs 通过：契约 / 系统表排除 / 关键字表名 / 无类型视图列")


# ===========================================================================
# 第二段：sql 执行器 —— 自建 / 注入两路线冒烟
# ===========================================================================


async def test_sql_executor() -> None:
    """自建（方言分派）与注入（逐参数适配）各自成立，builder 只看注入存在性选路。"""
    db_path = DB_DIR / f"{DEFAULT_DB}.sqlite"

    # -- 路线 B：自建 —— datasource_type 直接得到契约执行器，真查 ----------
    executor = SqlExecutor.create("sqlite", {"db_path": db_path})
    frame = await executor(
        "SELECT name FROM sqlite_master WHERE type='table'", max_rows=5
    )
    assert 0 < len(frame) <= 5, "自建 sqlite 执行器查询失败"
    # floor：构造期给下限，调用不传 max_rows 时生效
    floored = SqlExecutor.create("sqlite", {"db_path": db_path}, max_rows_floor=1)
    assert len(await floored("SELECT name FROM sqlite_master")) == 1, "floor 未生效"

    # 未注册类型：硬失败，报错带已支持清单与注入指引
    try:
        SqlExecutor.create("mysql", {})
    except ValueError as exc:
        assert "sqlite" in str(exc) and "execute_sql" in str(exc)
    else:
        raise AssertionError("未注册类型没有报错")
    # 缺连接参数：双清单报错
    try:
        SqlExecutor.create("sqlite", {})
    except ValueError as exc:
        assert "db_path" in str(exc)
    else:
        raise AssertionError("缺 db_path 没有报错")

    # -- 路线 A：注入 —— 逐参数适配，契约形态自动退化为透传 ----------------
    async def contract_stub(sql, *, max_rows=None):
        return pd.DataFrame({"sql": [sql], "max_rows": [max_rows]})

    adapted = ProjectSqlExecutor.create(execute_sql=contract_stub, max_rows_floor=7)
    assert "bound=[]" in repr(adapted), f"契约形态绑定集应为空: {adapted!r}"
    assert (await adapted("SELECT 1")).loc[0, "max_rows"] == 7, "floor 未经适配传导"

    def orchestrator_stub(sql, *, datasource_id, max_rows=None):  # 同步 + 必填参数
        return pd.DataFrame({"ds": [datasource_id]})

    adapted = ProjectSqlExecutor.create(
        execute_sql=orchestrator_stub, datasource_id="spider"
    )
    assert (await adapted("SELECT 1")).loc[0, "ds"] == "spider", "签名要求的参数未绑定"
    try:
        ProjectSqlExecutor.create(execute_sql=orchestrator_stub)  # 缺必填
    except ValueError as exc:
        assert "datasource_id" in str(exc)
    else:
        raise AssertionError("缺绑定参数没有在构造期报错")

    # -- builder：有注入走 A，否则走 B ------------------------------------
    assert isinstance(
        default_execute_sql_func_builder("sqlite", connection={"db_path": db_path}),
        SqlExecutor,
    )
    assert isinstance(
        default_execute_sql_func_builder("sqlite", execute_sql=contract_stub),
        ProjectSqlExecutor,
    )
    log.info("sql 执行器通过：自建分派 / 注入适配 / builder 选路 / 错误双清单")


# ===========================================================================
# 第三段：单库四任务全流程
# ===========================================================================


def spider_config(db_name: str, db_path: Path) -> AlgoConfig:
    """spider 前提落成的配置。未列出的项一律走 AlgoConfig 默认值。"""
    return AlgoConfig.build(
        datasource_id=db_name,  # 只用来分 out_dir
        datasource_type="sqlite",  # 表层 sqlite；日志层同名 → 自动 Null 降级
        # sqlite 的「凭证」就是文件路径：执行器由输入层自建
        datasource_connection={"db_path": str(db_path)},
        execute_sql_path="",  # 清空默认的项目执行器路径，明确走自建路线
        # -- spider 数据全英文，产出（domain / 业务名 / 描述）也全英文 --
        llm_output_language="en",
        # -- 枚举探查：mg-087 后 value_example（L5 绑定样例值）以 column_enums
        #    为唯一来源，全流程必须开着才能覆盖该链路（默认已改 False）--
        input_table_enums_enabled=True,
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
        # -- 解析方言 --
        parser_dialect="sqlite",
        # -- 落盘：只留三个任务产物；输入层快照与直通 dataset 不落盘 --
        store_out_dir=str(OUT_ROOT),
        store_include_contexts=(
            "domain_staged",
            "columns_caliber_staged",
            "columns_dict_staged",
        ),
    )


async def test_pipeline(db_name: str) -> None:
    db_path = DB_DIR / f"{db_name}.sqlite"
    assert db_path.is_file(), f"库不存在: {db_path}"
    config = spider_config(db_name, db_path)
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
    # 执行器由输入层按 config 自建 —— 组合根不再需要传任何执行器
    profiler = AlgoProfiler.build(AlgoContexts.build(config), llm=llm)
    assert set(profiler.handlers) == set(TASK_ORDER)

    # -- 输入层：表是真实来源，日志按 spider 前提必须为空 -----------------
    tables = await profiler.contexts.source_table.load()
    n_tables = len(tables.names())
    assert n_tables > 0, "表层为空"

    # -- 日志层：gold SQL 有几条就该有几条原始日志；有 gold 必有证据 -------
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
        # L7 expr：模型起草的聚合 SQL 片段（日志公式优先）—— 不能全是裸列名
        exprs = [str(e) for e in metric_rows["expr"].dropna()]
        assert exprs and any("(" in e for e in exprs), (
            f"metric expr 未起草聚合片段: {exprs}"
        )
    dim_rows = dictionary.dimensions
    if len(dim_rows):
        roles = {str(r) for r in dim_rows["dimension_role"].dropna()}
        assert roles <= {"categorical", "time"}, f"dimension_role 越界: {roles}"
    dim_cal = cal[cal["column_role"].astype(str).isin(["dimension", "date"])]
    # parquet 回读把 list 存成 numpy 数组 —— 用 len 判非空，不认类型
    filled = sum(
        1
        for v in dim_cal["value_example"]
        if v is not None and hasattr(v, "__len__") and len(v) > 0
    )
    assert filled > 0, "枚举探查已开启，但没有任何维度口径带 value_example"
    # 缺陷 A 回归：date 角色身份必须在快照里保住（time 维度的唯一来源）
    if (cal["column_role"].astype(str) == "date").any():
        time_dims = {str(r) for r in dictionary.dimensions["dimension_role"].dropna()}
        assert "time" in time_dims, "存在 date 角色列但没有任何 time 维度"

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
    """全部 30 库逐个跑三任务链路。单库失败不拖垮批次，收尾汇总。"""
    dbs = sorted(p.stem for p in DB_DIR.glob("*.sqlite"))
    failed: dict[str, str] = {}
    for index, db_name in enumerate(dbs, start=1):
        log.info("===== sqlite [%d/%d] %s =====", index, len(dbs), db_name)
        try:
            await test_pipeline(db_name)
        except Exception as exc:  # noqa: BLE001 - 批量模式逐库容错
            failed[db_name] = f"{type(exc).__name__}: {exc}"
            log.error("%s 失败: %s", db_name, exc)
    log.info("sqlite 批量完成：%d/%d 通过", len(dbs) - len(failed), len(dbs))
    for db_name, reason in failed.items():
        log.error("  失败 %s: %s", db_name, reason)
    if failed:
        raise SystemExit(1)


async def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    assert DB_DIR.is_dir(), f"Spider2 仓库没就绪: {DB_DIR}"
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
