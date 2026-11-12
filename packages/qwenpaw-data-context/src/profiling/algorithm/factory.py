"""Profiling 组合根：装配 DefaultProfiler 与依赖。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from .base import (
    LlmClient,
    ProfileConfig,
    ProfileTaskHandler,
    ProfileTaskName,
    Profiler,
    QueryLogsContext,
    TablesContext,
)
from .data_context import SourceTablesContext
from .handler import (
    BizDomainTaskHandler,
    DatasetTaskHandler,
    DimensionCaliberTaskHandler,
    DimensionManageTaskHandler,
    MetricCaliberTaskHandler,
    MetricManageTaskHandler,
)
from .llm import DefaultLlmClient
from .query_context import QueryLogContext


class DefaultProfiler(Profiler):
    """使用注册表提供六个默认任务策略。"""

    def __init__(
        self,
        source_tables: TablesContext,
        query_logs: QueryLogsContext | None,
        llm: LlmClient,
        *,
        out_dir: str | Path | None = None,
        handlers: Sequence[ProfileTaskHandler] | None = None,
        raise_if_cancelled: Callable[[], None] | None = None,
        report_progress: Callable[[int, str], None] | None = None,
    ) -> None:
        super().__init__(source_tables, query_logs, llm)
        resolved_out_dir = Path(
            out_dir
            if out_dir is not None
            else getattr(source_tables, "profile_dir", ".")
        )
        default_handlers = handlers or (
            BizDomainTaskHandler(llm, resolved_out_dir),
            DimensionCaliberTaskHandler(llm, resolved_out_dir),
            # 逐表判定列角色耗时长，需要黑板句柄做增量落盘与断点续跑。
            MetricCaliberTaskHandler(
                llm,
                resolved_out_dir,
                source_tables=source_tables,
            ),
            # 日志模板量大时分批调 LLM，需要黑板/日志句柄做增量落盘与续跑。
            DatasetTaskHandler(
                llm,
                resolved_out_dir,
                source_tables=source_tables,
                query_logs=query_logs,
            ),
            # 这两个任务要先把聚合态落盘，再分批补描述，同样需要黑板句柄。
            DimensionManageTaskHandler(
                llm,
                resolved_out_dir,
                source_tables=source_tables,
            ),
            MetricManageTaskHandler(
                llm,
                resolved_out_dir,
                source_tables=source_tables,
            ),
        )
        self._handlers = {handler.task: handler for handler in default_handlers}
        self._raise_if_cancelled = raise_if_cancelled
        self._report_progress = report_progress

    def handler_for(self, task: ProfileTaskName) -> ProfileTaskHandler:
        try:
            return self._handlers[task]
        except KeyError as exc:
            raise ValueError(f"No handler registered for {task.value}") from exc

    def raise_if_cancelled(self) -> None:
        if self._raise_if_cancelled is not None:
            self._raise_if_cancelled()

    def report_progress(self, pct: int, message: str) -> None:
        if self._report_progress is not None:
            self._report_progress(int(pct), str(message))


def build_execute_sql_func(
    config: ProfileConfig,
    execute_sql: Callable[..., Any] | None = None,
) -> Callable[[str], pd.DataFrame]:
    """把 profiling.sql_executor 的只读入口适配为 ``sql -> DataFrame``。

    注意：``datasource_id`` 必须是 **str**（数字 id 或
    datasource_code 均可），内部会对其调用 ``.strip()``；``ProfileConfig`` 里
    若是 int 必须在此转成字符串，否则会 ``AttributeError``。
    """

    def execute_sql_func(sql: str) -> pd.DataFrame:
        executor = execute_sql
        if executor is None:
            from profiling.sql_executor import execute_read

            executor = execute_read

        result = executor(
            sql,
            datasource_id=str(config.datasource_id),
            max_rows=config.query_max_rows,
        )
        if isinstance(result, pd.DataFrame):
            return result
        error = getattr(result, "error", None)
        if error:
            raise RuntimeError(str(error))
        rows = getattr(result, "rows", None)
        columns = getattr(result, "columns", None)
        if rows is None or columns is None:
            raise TypeError(
                "execute_sql must return DataFrame or an object with rows/columns/error"
            )
        return pd.DataFrame(rows, columns=columns)

    return execute_sql_func


def build_profiler(
    config: ProfileConfig,
    execute_sql: Callable[..., Any] | None = None,
    *,
    source_tables: TablesContext | None = None,
    query_logs: QueryLogsContext | None = None,
    raise_if_cancelled: Callable[[], None] | None = None,
    report_progress: Callable[[int, str], None] | None = None,
) -> DefaultProfiler:
    """根据配置装配默认源表上下文、日志上下文、LLM 和任务 Handler。

    ``source_tables`` / ``query_logs`` 用于注入调用方已经拉好的上下文（编排层在
    线程池里预建，见 ``profiling.services.context``），传 None 则在此就地拉取。
    ``raise_if_cancelled`` / ``report_progress`` 供编排适配层桥接 ProbeContext 钩子。
    """
    execute_sql_func = build_execute_sql_func(config, execute_sql)

    if source_tables is None:
        source_tables = SourceTablesContext.create_from_execute_sql(
            execute_sql_func,
            config.out_dir,
        )

    if query_logs is None and config.dbname:
        query_logs = QueryLogContext.create_from_execute_sql(
            execute_sql_func,
            config.out_dir,
            datasource_type=config.datasource_type,
            dbname=config.dbname,
            max_rows=config.query_max_rows,
            start_ts=config.start_ts,
            end_ts=config.end_ts,
        )

    llm = DefaultLlmClient(
        config.llm_model,
        config.llm_api_key,
        config.out_dir,
    )
    return DefaultProfiler(
        source_tables,
        query_logs,
        llm,
        out_dir=config.out_dir,
        raise_if_cancelled=raise_if_cancelled,
        report_progress=report_progress,
    )


__all__ = [
    "DefaultProfiler",
    "build_execute_sql_func",
    "build_profiler",
]
