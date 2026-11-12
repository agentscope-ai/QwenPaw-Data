"""查询日志上下文的 pandas 实现。

:class:`QueryLogContext` 继承 :class:`~.base.QueryLogsContext`，以 DataFrame
承载原始日志，并按任务抽取证据。目录习惯对齐 :mod:`.data_context`::

    {profile_dir}/query_logs/raw.csv
    {profile_dir}/query_logs/raw_original.csv   # 可选
    {profile_dir}/query_logs/parsed_analyses.json  # SQL 解析缓存（JSON）
    {profile_dir}/raw.csv                      # 兼容旧落盘位置
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlglot import exp

from .base import (
    LogEvidence,
    ProfileTaskName,
    QueryLogsContext,
    TASK_ORDER,
)
from .profiler_biz_domain import aggregate_join_evidence
from .profiler_dataset import filter_dataset_records, iter_dataset_occurrences
from .profiler_dimension_caliber import (
    group_dimensions_from_select,
    where_dimensions_from_select,
)
from .profiler_metric_caliber import (
    expression_metrics_from_select,
    metric_name_from_agg,
    scalar_aggs_from_select,
)
from .utils_ast import SqlAnalysis

logger = logging.getLogger(__name__)

# 走 hologres.hg_query_log 的数据源类型别名（配置层的 DatasourceType 只有 postgresql）
_HOLO_LOG_TYPES = frozenset({"holo", "hologres", "postgresql", "postgres"})

#: Plain PostgreSQL table that mirrors hologres.hg_query_log (psycopg2 path).
QUERY_LOG_MIRROR_TABLE_ENV = "QWENPAW_DATA_QUERY_LOG_MIRROR_TABLE"
DEFAULT_QUERY_LOG_MIRROR_TABLE = "public.query_log_mirror"
_TABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")
_EXPR = getattr(exp, "Expr", exp.Expression)


def query_log_mirror_table() -> str:
    """Return the configured mirror table; only ``table`` or ``schema.table``."""
    table = (
        os.environ.get(QUERY_LOG_MIRROR_TABLE_ENV) or ""
    ).strip() or DEFAULT_QUERY_LOG_MIRROR_TABLE
    if not _TABLE_NAME_RE.fullmatch(table):
        raise ValueError(
            f"{QUERY_LOG_MIRROR_TABLE_ENV} must be an unquoted table or schema.table name"
        )
    return table


def _is_expression_class(name: Any) -> bool:
    """Accept a dumped class name only for an already-imported sqlglot node."""
    if not isinstance(name, str):
        return False
    module_name, _, class_name = name.rpartition(".")
    if module_name and not (
        module_name == exp.__name__ or module_name.startswith(f"{exp.__name__}.")
    ):
        return False
    module = sys.modules.get(module_name) if module_name else exp
    candidate = getattr(module, class_name, None) if module is not None else None
    return isinstance(candidate, type) and issubclass(candidate, _EXPR)


def _check_ast_payload(payload: Any) -> None:
    """Reject AST dumps naming anything but sqlglot expression classes.

    sqlglot's loader imports and instantiates whichever class a payload names,
    so the cache file must not be able to choose it.
    """
    pending = [payload]
    while pending:
        nodes = pending.pop()
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("malformed AST payload")
        for node in nodes:
            if not isinstance(node, dict):
                raise ValueError("malformed AST node")
            name = node.get("c")
            if (
                name is not None
                and name != "DataType.Type"
                and not _is_expression_class(name)
            ):
                raise ValueError(f"unexpected AST class: {name!r}")
            if "t" in node:
                pending.append(node["t"])


def _dump_analysis(analysis: SqlAnalysis) -> dict[str, Any]:
    return {
        "original_sql": analysis.original_sql,
        "cleaned_sql": analysis.cleaned_sql,
        "dialect": analysis.dialect_name,
        "root": analysis.root_expr.dump(),
    }


def _load_analysis(item: Any) -> SqlAnalysis:
    """Rebuild one cached analysis through SqlAnalysis' own state protocol."""
    keys = ("original_sql", "cleaned_sql", "dialect")
    if not isinstance(item, dict) or not all(
        isinstance(item.get(key), str) for key in keys
    ):
        raise ValueError("malformed analysis entry")
    _check_ast_payload(item.get("root"))
    root = exp.Expression.load(item["root"])
    if not isinstance(root, _EXPR):
        raise ValueError("cached AST root is not an expression")
    analysis = SqlAnalysis.__new__(SqlAnalysis)
    # __setstate__ rebuilds the derived indexes from the root, as unpickling did.
    analysis.__setstate__({**{key: item[key] for key in keys}, "root": root})
    return analysis


def fetch_query_logs(
    execute_sql_func: Callable[[str], pd.DataFrame],
    out_dir: str,
    *,
    datasource_type: str,
    dbname: str,
    max_rows: int = 100000,
    start_ds: str | None = None,
    end_ds: str | None = None,
    query_column: str = "query",
) -> Path:
    """拉取 query logs 并返回缓存 CSV 路径；已存在则直接复用。"""
    if not end_ds:
        end_ds = datetime.now().strftime("%Y%m%d")
    if not start_ds:
        start_ds = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    save_path = out_dir_path / "raw.csv"

    if save_path.exists():
        logger.info(f"日志文件已存在，无需重复抽取: {save_path}")
        return save_path

    datname = dbname.replace("-", "_")
    # postgresql / hologres 共用 hologres.hg_query_log；psycopg2 走本地的镜像表。
    if datasource_type in _HOLO_LOG_TYPES:
        query_logs_sql = f"""\
            SELECT * FROM hologres.hg_query_log 
            WHERE datname = '{datname}' 
              AND query_date >= '{start_ds}' AND query_date <= '{end_ds}'
              AND command_tag = 'SELECT'
              AND result_rows > 0 AND read_rows > 0 
              AND query != 'SELECT 1'
            LIMIT {max_rows}
        """
    elif datasource_type == "psycopg2":
        query_logs_sql = f"""\
        SELECT instance, datname, command_tag, result_rows, read_rows, query_date, ds, query
        FROM {query_log_mirror_table()}
        WHERE datname = '{datname}'
        AND command_tag = 'SELECT'
        AND ds >= '{start_ds}' AND ds <= '{end_ds}'
        AND table_read IS NOT NULL
        LIMIT {max_rows}
    """

    else:
        raise ValueError(f"暂不支持的数据源类型: {datasource_type}")

    logger.info(f"开始从 {datasource_type} 抽取 {start_ds} 至 {end_ds} 的日志...")
    query_logs = execute_sql_func(query_logs_sql)

    need_columns = ["query_id", "query_date", "datname", "usename", "query"]
    existing_columns = [col for col in need_columns if col in query_logs.columns]
    if len(existing_columns) == len(need_columns):
        query_logs_simple = query_logs[existing_columns]
        query_logs_simple.to_csv(save_path, index=False)
        query_logs.to_csv(out_dir_path / "raw_original.csv", index=False)
    else:
        query_logs.to_csv(save_path, index=False)

    logger.info(f"日志抽取并保存成功: {save_path}")
    return save_path


class QueryLogContext(QueryLogsContext):
    """查询日志上下文（pandas 实现）。

    - 原始语料以 ``logs_df``（至少含 ``query`` 列）保存；
    - SQL 只解析一次，各任务的 :meth:`evidence_for` 复用同一份 AST 缓存；
    - 每个任务只调用对应 ``profiler_*`` 模块的公开抽取入口；
    - 证据结果以 DataFrame / dict 形式放入 :class:`~.base.LogEvidence.records`。
    """

    _RAW_NAME = "raw.csv"
    # JSON, not pickle: the profile directory outlives this process.
    _PARSED_CACHE_NAME = "parsed_analyses.json"
    _QUERY_COLUMN = "query"

    def __init__(
        self,
        query_logs_dir: str | Path,
        *,
        logs_df: pd.DataFrame | None = None,
        dialect: str = SqlAnalysis.DEFAULT_DIALECT,
        dataset_min_count: int = 10,
        query_column: str = _QUERY_COLUMN,
    ) -> None:
        self.query_logs_dir = Path(query_logs_dir)
        self.query_logs_dir.mkdir(parents=True, exist_ok=True)
        # 每个任务的日志证据落盘到 {profile_dir}/log_evidence/{task}.csv
        self.log_evidence_dir = self.query_logs_dir.parent / "log_evidence"
        self._query_column = query_column
        self._dialect = dialect
        self._dataset_min_count = max(1, int(dataset_min_count))
        self._logs_df = (
            logs_df.copy()
            if isinstance(logs_df, pd.DataFrame)
            else pd.DataFrame(columns=[query_column])
        )
        self._parsed_analyses: tuple[SqlAnalysis, ...] | None = None
        self._evidence_cache: dict[ProfileTaskName, LogEvidence] = {}
        for task in TASK_ORDER:
            path = self.log_evidence_dir / f"{task.value}.csv"
            if path.exists():
                frame = pd.read_csv(path)
                if task == ProfileTaskName.DATASET:
                    frame = filter_dataset_records(frame, dialect=self._dialect)
                self._evidence_cache[task] = LogEvidence(task=task, records=frame)
                logger.info(
                    "从磁盘加载 log_evidence: task=%s path=%s rows=%s",
                    task.value,
                    path,
                    len(frame),
                )

    @property
    def logs_df(self) -> pd.DataFrame:
        """原始查询日志表（只读语义，调用方勿原地修改）。"""
        return self._logs_df

    @property
    def logs(self) -> tuple[str, ...]:
        """从 DataFrame 抽出的非空 SQL 文本。"""
        if self._logs_df.empty or self._query_column not in self._logs_df.columns:
            return ()
        series = self._logs_df[self._query_column].dropna().astype(str)
        return tuple(text.strip() for text in series if text.strip())

    def _raw_fingerprint(self) -> dict[str, Any]:
        """用 raw.csv 元信息 + dialect / 条数校验解析缓存是否仍有效。"""
        raw_path = self.query_logs_dir / self._RAW_NAME
        if not raw_path.exists():
            return {
                "exists": False,
                "dialect": self._dialect,
                "n_logs": len(self.logs),
            }
        stat = raw_path.stat()
        return {
            "exists": True,
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "dialect": self._dialect,
            "n_logs": len(self.logs),
        }

    def _load_parsed_analyses(self) -> tuple[SqlAnalysis, ...] | None:
        cache_path = self.query_logs_dir / self._PARSED_CACHE_NAME
        if not cache_path.exists():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Failed to load parsed analyses cache: {exc}")
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("fingerprint") != self._raw_fingerprint():
            logger.info(f"Parsed analyses cache stale, will re-parse: {cache_path}")
            return None
        analyses = payload.get("analyses")
        if not isinstance(analyses, list):
            return None
        try:
            loaded = tuple(_load_analysis(item) for item in analyses)
        except Exception as exc:
            logger.warning(f"Ignoring invalid parsed analyses cache: {exc}")
            return None
        logger.info(
            f"Loaded parsed analyses cache: {len(loaded)} items from {cache_path}"
        )
        return loaded

    def _save_parsed_analyses(self, analyses: tuple[SqlAnalysis, ...]) -> None:
        cache_path = self.query_logs_dir / self._PARSED_CACHE_NAME
        try:
            payload = {
                "fingerprint": self._raw_fingerprint(),
                "analyses": [_dump_analysis(analysis) for analysis in analyses],
            }
            cache_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            logger.info(
                f"Saved parsed analyses cache: {len(analyses)} items -> {cache_path}"
            )
        except Exception as exc:
            logger.warning(f"Failed to save parsed analyses cache: {exc}")

    @classmethod
    def create_from_dir(
        cls,
        query_logs_dir: str | Path,
    ) -> QueryLogContext:
        """从 ``query_logs/raw.csv``（或兼容的根目录 ``raw.csv``）加载。"""
        ctx = cls(query_logs_dir)
        path = ctx.query_logs_dir / cls._RAW_NAME
        if not path.exists():
            logger.info(f"No query log csv under {ctx.query_logs_dir}")
            return ctx
        frame = pd.read_csv(path, na_values=["\\N"])
        if ctx._query_column not in frame.columns:
            raise KeyError(
                f"column {ctx._query_column!r} not in CSV: {list(frame.columns)}"
            )
        ctx._logs_df = frame
        logger.info(f"Loaded query logs: rows={len(frame)} path={path}")
        return ctx

    @classmethod
    def create_from_logs(
        cls,
        logs: list[str],
        query_logs_dir: str | Path,
    ) -> QueryLogContext:
        """从已有 SQL 文本列表构建（可传 ``profile_dir`` / ``out_dir``）。"""
        query_column = cls._QUERY_COLUMN
        cleaned = [str(log).strip() for log in logs if str(log).strip()]
        logs_df = pd.DataFrame({query_column: cleaned})
        return cls(query_logs_dir, logs_df=logs_df)

    @classmethod
    def create_from_execute_sql(
        cls,
        execute_sql_func: Callable[[str], Any],
        out_dir: str | Path,
        *,
        datasource_type: str,
        dbname: str,
        max_rows: int = 100_000,
        start_ts: str | None = None,
        end_ts: str | None = None,
    ) -> QueryLogContext:
        """优先从 ``out_dir/query_logs`` 加载；目录无日志时再 execute_sql 拉取。"""
        if not dbname:
            raise ValueError("dbname is required when fetching query logs")

        query_logs_dir = Path(out_dir) / "query_logs"
        ctx = cls.create_from_dir(query_logs_dir)
        if ctx.logs:
            return ctx

        logger.info(
            f"No cached query logs under {query_logs_dir}; fetching via execute_sql"
        )
        query_logs_path = fetch_query_logs(
            execute_sql_func,
            str(query_logs_dir),
            datasource_type=datasource_type.lower(),
            dbname=dbname,
            max_rows=max_rows,
            start_ds=start_ts,
            end_ds=end_ts,
            query_column=cls._QUERY_COLUMN,
        )
        return cls.create_from_dir(Path(query_logs_path).parent)

    @property
    def analyses(self) -> tuple[SqlAnalysis, ...]:
        """惰性解析全部 SQL；优先读磁盘缓存，未命中再解析并落盘。"""
        if self._parsed_analyses is not None:
            return self._parsed_analyses

        cached = self._load_parsed_analyses()
        if cached is not None:
            self._parsed_analyses = cached
            return self._parsed_analyses

        parsed: list[SqlAnalysis] = []
        for sql in self.logs:
            analysis = SqlAnalysis.create_from_sql_or_none(
                sql,
                dialect=self._dialect,
            )
            if analysis is not None:
                parsed.append(analysis)
        self._parsed_analyses = tuple(parsed)
        logger.info(f"Parsed query logs once: {len(parsed)}/{len(self.logs)}")
        self._save_parsed_analyses(self._parsed_analyses)
        return self._parsed_analyses

    def evidence_for(self, task: ProfileTaskName) -> LogEvidence:
        """按任务从同一份 AST 缓存提取证据。

        Domain 分支只调用 :meth:`_extract_domain_records`；该方法再单次委托
        :func:`aggregate_join_evidence`。JOIN 遍历、聚合和聚类均由
        ``profiler_biz_domain`` 完成。
        """
        if task in self._evidence_cache:
            cached = self._evidence_cache[task]
            records = cached.records
            n = (
                len(records)
                if isinstance(records, pd.DataFrame)
                else (
                    len(records.get("clusters", {}))
                    if isinstance(records, dict)
                    else "?"
                )
            )
            logger.info(
                "log_evidence 命中缓存/磁盘: task=%s size=%s",
                task.value,
                n,
            )
            return cached

        logger.info("log_evidence 开始抽取: task=%s", task.value)
        if task == ProfileTaskName.BIZ_DOMAIN:
            records: Any = self._extract_domain_records()
        elif task == ProfileTaskName.DATASET:
            records = self._extract_dataset_records()
        elif task in {
            ProfileTaskName.DIMENSION_CALIBER,
            ProfileTaskName.DIMENSION_MANAGE,
        }:
            records = self._extract_dimension_records()
        elif task in {
            ProfileTaskName.METRIC_CALIBER,
            ProfileTaskName.METRIC_MANAGE,
        }:
            records = self._extract_metric_records()
        else:
            records = pd.DataFrame()
        self._persist_log_evidence(task, records)
        evidence = LogEvidence(task=task, records=records)
        self._evidence_cache[task] = evidence
        n = (
            len(records)
            if isinstance(records, pd.DataFrame)
            else (
                len(records.get("clusters", {})) if isinstance(records, dict) else "?"
            )
        )
        logger.info("log_evidence 抽取完成: task=%s size=%s", task.value, n)
        return evidence

    def _persist_log_evidence(self, task: ProfileTaskName, records: Any) -> None:
        """把每个任务的日志证据落盘到 ``{profile_dir}/log_evidence/{task}.csv``。

        - ``records`` 为 DataFrame 时直接写；
        - 为 dict（如 Domain 的 JoinEvidence）时只写其中的 DataFrame 成员：
          仅一个用 ``{task}.csv``，多个则用 ``{task}_{key}.csv``。

        持久化失败只告警，不影响主流程。
        """
        try:
            if isinstance(records, pd.DataFrame):
                self.log_evidence_dir.mkdir(parents=True, exist_ok=True)
                records.to_csv(self.log_evidence_dir / f"{task.value}.csv", index=False)
                return
            if isinstance(records, dict):
                frames = {
                    key: value
                    for key, value in records.items()
                    if isinstance(value, pd.DataFrame)
                }
                if not frames:
                    return
                self.log_evidence_dir.mkdir(parents=True, exist_ok=True)
                if len(frames) == 1:
                    next(iter(frames.values())).to_csv(
                        self.log_evidence_dir / f"{task.value}.csv", index=False
                    )
                else:
                    for key, frame in frames.items():
                        frame.to_csv(
                            self.log_evidence_dir / f"{task.value}_{key}.csv",
                            index=False,
                        )
        except Exception as exc:
            logger.warning(f"Failed to persist log evidence for {task.value}: {exc}")

    def _extract_domain_records(self) -> dict[str, Any]:
        """委托 Domain 模块完成逐 SQL JOIN 提取、聚合与图聚类。

        返回 :class:`~.profiler_biz_domain.JoinEvidence`，键::

            join_edges          DataFrame，见下方列说明
            clusters            {"cluster_0": ["orders", "order_items"], ...}
            table_to_cluster_id {"orders": "cluster_0", "order_items": "cluster_0"}
            query_table_names   ["items", "orders", ...]  # 日志里出现过的全部表
            table_frequencies   {"orders": 12, "items": 3, ...}  # 每条日志计一次

        ``join_edges`` 输出的结果示例::

            left_table,right_table,join_type,on_columns,count
            order_items,orders,INNER JOIN,order_id=id,37
            orders,users,LEFT OUTER JOIN,user_id=id,12

        Corner cases：
            - 无日志 / 无 JOIN 时 ``join_edges`` 为带列的空表，``clusters`` 为 {}；
            - 表名统一去掉 schema 与分区后缀，与黑板主键一致。
        """
        return aggregate_join_evidence(self.analyses)

    def _extract_dataset_records(self) -> pd.DataFrame:
        """按结构等价的 SQL 模板聚合重复查询，作为语义视图候选。

        每条日志 SQL 先展开成「根查询 + 每一层嵌套子查询」（FROM 子查询、CTE
        body、scalar/EXISTS 子查询），各层分别参与判重计数，因此被反复嵌套使用
        的内层查询也能被识别为高频模式。

        输出的结果示例（``count`` 已按 ``dataset_min_count`` 过滤并降序）::

            parents,repeated_sql,count
            "[""public.ads_example_revenue_index_1d""]",SELECT date_id, SUM(gmv) FROM ...,124
            "[""public.orders"", ""public.users""]",SELECT u.name, COUNT(*) FROM ...,18

        列说明：
            - ``parents``：该模板依赖的物理表，JSON 数组字符串（带 schema）；
            - ``repeated_sql``：该模板下第一条代表 SQL（对应某一层 Select）；
            - ``count``：结构等价的 Select 出现次数（含各层子查询）。

        Corner cases：
            - 没有任何 SQL 达到阈值时返回列齐全的空表。
        """
        rows: list[dict[str, Any]] = []
        for analysis in self.analyses:
            rows.extend(iter_dataset_occurrences(analysis))
        if not rows:
            return pd.DataFrame(columns=["parents", "repeated_sql", "count"])
        grouped = (
            pd.DataFrame(rows)
            .groupby("norm_key", as_index=False)
            .agg(
                count=("norm_key", "size"),
                repeated_sql=("repeated_sql", "first"),
                parents=("parents", "first"),
            )
        )
        filtered = (
            grouped[grouped["count"] >= self._dataset_min_count][
                ["parents", "repeated_sql", "count"]
            ]
            .sort_values(
                ["count", "repeated_sql"],
                ascending=[False, True],
            )
            .reset_index(drop=True)
        )
        # 再过一遍 AST 宽度规则（兼容旧缓存 / 边界方言渲染差异）。
        return filter_dataset_records(filtered, dialect=self._dialect)

    def _extract_dimension_records(self) -> pd.DataFrame:
        """抽取 GROUP BY / WHERE 中被当作维度使用的列并按表聚合。

        输出的结果示例::

            dimension_name,table_name,count,sources
            year_b,ads_example_dashboard_overview_1d,1,['group_by']
            type_level1,ads_example_product_index_1d,1,['where']
            date_id,ads_example_revenue_index_1d_last_cm_fy,21,"['group_by', 'where']"
            general_type,ads_example_revenue_index_1d_last_cm_fy,20,"['group_by', 'where']"

        列说明：
            - ``dimension_name``：物理列名；
            - ``table_name``：归属表，已去掉分区后缀；列无法唯一归表时可能是
              ``"a,b"`` 形式的多表引用，由 handler 再展开；
            - ``count``：出现该用法的 SQL 条数；
            - ``sources``：``group_by`` / ``where`` 的去重列表。

        Corner cases：
            - WHERE 只统计 ``col = 字面量`` 与 ``col IN (...)``，其余谓词不计；
            - 无日志时返回列齐全的空表。
        """
        rows: list[dict[str, Any]] = []
        for analysis in self.analyses:
            seen: set[tuple[str, str, str]] = set()
            for select in analysis.select_exprs:
                extracted = group_dimensions_from_select(
                    analysis,
                    select,
                ) + where_dimensions_from_select(analysis, select)
                for row in extracted:
                    key = (
                        row["table_name"],
                        row["dimension_name"],
                        row["source"],
                    )
                    if key not in seen:
                        seen.add(key)
                        rows.append(row)
        if not rows:
            return pd.DataFrame(
                columns=["dimension_name", "table_name", "count", "sources"]
            )
        frame = pd.DataFrame(rows)
        frame["table_name"] = (
            frame["table_name"]
            .astype(str)
            .str.replace(
                r"_\d{6,8}$",
                "",
                regex=True,
            )
        )
        return (
            frame.groupby(["dimension_name", "table_name"], as_index=False)
            .agg(
                count=("dimension_name", "size"),
                sources=("source", lambda values: list(values.unique())),
            )
            .sort_values(["table_name", "dimension_name"])
            .reset_index(drop=True)
        )

    def _extract_metric_records(self) -> pd.DataFrame:
        """抽取聚合表达式并按 (指标名, 表, 公式) 聚合。

        输出的结果示例::

            metric_name,table_name,formula,count,metric_type,sources,agg_function,agg_column
            total_gmv,ads_example_revenue_index_1d,SUM(gmv),12,scalar_agg,['select'],SUM,gmv
            like_rate,ads_example_dashboard_overview_1d,"CASE WHEN SUM(a) > 0 THEN ...",3,expression,['select'],"SUM",a,b

        列说明：
            - ``metric_name``：SELECT 别名，没有别名时按 ``fn_col`` 生成；
            - ``formula``：``SUM(gmv)`` 或整段复合表达式的 SQL 文本；
            - ``metric_type``：``scalar_agg``（单个聚合函数）/ ``expression``（含
              聚合的复合表达式）；
            - ``sources``：``select`` / ``having``；
            - ``agg_function`` / ``agg_column``：逗号拼接的聚合函数与依赖列。

        Corner cases：
            - 窗口聚合（``OVER(...)``）不计入指标；
            - ``COUNT(*)`` 归到 ``agg_column="*"``，且只在能唯一定位到表时保留。
        """
        rows: list[dict[str, Any]] = []
        for analysis in self.analyses:
            seen: set[tuple[str, str, str, str, str]] = set()
            for select in analysis.select_exprs:
                expression_rows, claimed_ids = expression_metrics_from_select(
                    analysis,
                    select,
                )
                for row in expression_rows:
                    key = (
                        row["metric_name"],
                        row["table_name"],
                        row["formula"],
                        row["source"],
                        row["metric_type"],
                    )
                    if key not in seen:
                        seen.add(key)
                        rows.append(row)
                for fn, table, column, aggregate, source in scalar_aggs_from_select(
                    analysis,
                    select,
                    skip_agg_ids=claimed_ids,
                ):
                    metric_name, formula = metric_name_from_agg(
                        select,
                        aggregate,
                        fn,
                        column,
                    )
                    key = (
                        metric_name,
                        table,
                        formula,
                        source,
                        "scalar_agg",
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(
                        {
                            "metric_name": metric_name,
                            "table_name": table,
                            "source": source,
                            "formula": formula,
                            "metric_type": "scalar_agg",
                            "agg_function": fn,
                            "agg_column": column,
                        }
                    )
        if not rows:
            return pd.DataFrame(
                columns=[
                    "metric_name",
                    "table_name",
                    "formula",
                    "count",
                    "metric_type",
                    "sources",
                    "agg_function",
                    "agg_column",
                ]
            )
        frame = pd.DataFrame(rows)
        frame["table_name"] = (
            frame["table_name"]
            .astype(str)
            .str.replace(
                r"_\d{6,8}$",
                "",
                regex=True,
            )
        )
        return (
            frame.groupby(
                ["metric_name", "table_name", "formula"],
                as_index=False,
            )
            .agg(
                count=("metric_name", "size"),
                metric_type=("metric_type", "first"),
                sources=("source", lambda values: list(values.unique())),
                agg_function=("agg_function", "first"),
                agg_column=("agg_column", "first"),
            )
            .sort_values(["table_name", "metric_name"])
            .reset_index(drop=True)
        )


if __name__ == "__main__":
    path = Path(
        "data/profile/sales_20260601_20260707/"
    )
    loaded = QueryLogContext.create_from_dir(path)
    print(f"ok: rows={len(loaded.logs_df)} sqls={len(loaded.logs)} ")
    domain = loaded.evidence_for(ProfileTaskName.BIZ_DOMAIN)
    print(f"domain evidence keys={list((domain.records or {}).keys())}")
