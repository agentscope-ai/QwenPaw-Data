"""黑板的 pandas 实现。

:class:`SourceTablesContext` 继承 :class:`~.base.TablesContext`，是 profiling 全程
唯一的可变状态：既存源表结构，也存各任务与表无关的聚合产物和 ``domain_of``。

版本目录名使用 :class:`~.base.ProfileTaskName` 的值，语义层产物单独一个根目录::

    {profile_dir}/tables/{task}_{YYYYMMDDHHMMSS}/tables_overview.csv   # 表任务
    {profile_dir}/tables/{task}_{YYYYMMDDHHMMSS}/{table}.xlsx         # 表任务
    {profile_dir}/tables/{task}_{YYYYMMDDHHMMSS}/{task}.xlsx           # biz_domain / dataset
    {profile_dir}/tables/domain_{YYYYMMDDHHMMSS}/domain_of.py          # 仅 domain
    {profile_dir}/dimension_metric/dimension_{YYYYMMDDHHMMSS}/dimension.xlsx
    {profile_dir}/dimension_metric/metric_{YYYYMMDDHHMMSS}/metric.xlsx
"""

from __future__ import annotations

import ast
import logging
import re
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .base import (
    DIM_METRIC_REQUIRE_VIEW_PREFIX,
    DOMAIN_RESOLVER_ARTIFACT,
    OVERVIEW_ARTIFACT,
    TABLE_ARTIFACT_TASKS,
    ProfileTaskName,
    SourceTableEvidence,
    TablesContext,
    is_dim_metric_table_name,
    resolver_source_frame,
    resolver_source_of,
)
from .profiler_biz_domain import compile_domain_of

logger = logging.getLogger(__name__)

_OVERVIEW_REQUIRED_COLS = frozenset({"table_name"})
_SYSTEM_SCHEMAS = (
    "pg_catalog",
    "information_schema",
    "hologres",
    "pg_toast",
    "hg_recyclebin",
    "hologres_statistic",
)
_SYSTEM_SCHEMA_SQL = ", ".join(f"'{name}'" for name in _SYSTEM_SCHEMAS)


#: 常驻的 domain 解析函数路径（相对 ``profile_dir``），biz_domain 任务没有版本时的兜底。
_STANDALONE_RESOLVER_PATH = "domain/domain_resolver.py"

#: 语义层任务的产物根目录名；其余任务仍写在 ``tables/`` 下。
_SEMANTIC_DIR_NAME = "dimension_metric"

#: dimension_manage 与 metric_manage 的最终产物**共用一个版本目录**，文件名固定为
#: ``{task}_lib.xlsx``，便于整体交付：
#:     dimension_metric/dimension_metric_{ts}/dimension_lib.xlsx
#:     dimension_metric/dimension_metric_{ts}/metric_lib.xlsx
_LIB_DIR_PREFIX = "dimension_metric"
_LIB_TASK_FILES: dict[ProfileTaskName, str] = {
    ProfileTaskName.DIMENSION_MANAGE: "dimension_lib.xlsx",
    ProfileTaskName.METRIC_MANAGE: "metric_lib.xlsx",
}

#: 产物落在 :data:`_SEMANTIC_DIR_NAME` 下的任务。
_SEMANTIC_DIR_TASKS: frozenset[ProfileTaskName] = frozenset(
    {
        ProfileTaskName.AGG_DIMENSION,
        ProfileTaskName.AGG_METRIC,
        ProfileTaskName.DIMENSION_MANAGE,
        ProfileTaskName.METRIC_MANAGE,
    }
)

_ENUM_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="add_enums")


def _version_order(timestamp: str) -> str:
    """版本排序键：把 12 位（分钟级）时间戳补齐到 14 位再比较。

    否则 ``"202607282202"`` 会排在 ``"20260728220205"`` 之后，导致重放历史版本
    时旧状态覆盖新状态。
    """
    return str(timestamp).ljust(14, "0")


class SourceTablesContext(TablesContext, Mapping[str, pd.DataFrame]):
    """profiling 全程唯一的可变状态（pandas 黑板），四类内容一起维护。

    1. ``_overview`` —— 表清单，列::

           table_name,table_comment,merged_tables
           ads_example_revenue_index_1d,收入指标日汇总,"['..._20260701']"

    2. ``_table_map`` —— ``{table_name: 列详情 DataFrame}``，列随任务推进不断加宽::

           origin_schema     column_name,data_type,is_nullable,column_comment,
                             is_primary_key,ordinal_position
           add_enums        +table_name,is_primary,column_name_cn,
                             column_distinct_num_1d,column_enums_1d
           dimension_caliber+query_log_clause_count,query_log_clause_type,
                             is_dimension_in_query_logs
           metric_caliber   +query_log_aggregation_count,query_log_aggregation_type,
                             query_log_metric_name,metric_formula,is_derived_metric,
                             is_metric_in_query_logs,column_role,dimension_name,
                             metric_name,date_range,is_dimension,is_metric,is_date

    3. ``_task_results`` —— ``{task: 结果 DataFrame}``，与具体表无关的聚合产物
       （biz_domain / dataset / dimension_manage / metric_manage 四张表）。

    4. ``_domain_of`` —— biz_domain 任务生成并编译好的 ``table_name -> domain_name``
       函数，源码同时以 ``domain_of.py`` 落盘，重新加载目录时会重新编译。

    :meth:`update` 是唯一的写入口，按 :class:`~.base.ProfileTaskName` 决定产物落到
    上面哪一部分；:meth:`evidence_for` 是唯一的读出口。
    """

    _OVERVIEW_NAME = "tables_overview.csv"
    _DOMAIN_OF_NAME = "domain_of.py"
    _LEGACY_RESULT_NAME = "task_result.xlsx"
    # 秒级时间戳；仍兼容历史上的 12 位（分钟级）目录名。
    _VERSION_DIR_RE = re.compile(r"^(.+)_(\d{12,14})$")

    def __init__(self, profile_dir: str | Path) -> None:
        self.profile_dir = Path(profile_dir)
        self.tables_dir = self.profile_dir / "tables"
        self.tables_dir.mkdir(parents=True, exist_ok=True)
        # 语义层产物（dimension_manage / metric_manage）单独放一个根目录，便于对外交付。
        self.semantic_dir = self.profile_dir / _SEMANTIC_DIR_NAME
        self.semantic_dir.mkdir(parents=True, exist_ok=True)
        # [{task_name, timestamp, path, artifacts}, ...]，按 timestamp 升序
        self.versions: list[dict[str, Any]] = []
        self._overview = pd.DataFrame()
        self._table_map: dict[str, pd.DataFrame] = {}
        self._task_results: dict[ProfileTaskName, pd.DataFrame] = {}
        self._domain_of: Callable[[str], str | None] | None = None
        self._domain_of_source = ""
        self._enums_future: Future[None] | None = None

    # ------------------------------------------------------------------
    # 读接口
    # ------------------------------------------------------------------

    @property
    def latest(self) -> dict[str, Any] | None:
        """最近一次写入的版本记录；从未写入过时为 ``None``。"""
        return self.versions[-1] if self.versions else None

    @property
    def table_name_to_df_map(self) -> dict[str, pd.DataFrame]:
        """``{table_name: 列详情 DataFrame}`` 的当前快照。"""
        return dict(self._table_map)

    @property
    def domain_of(self) -> Callable[[str], str | None] | None:
        """biz_domain 任务产出的解析函数；biz_domain 未运行或编译失败时为 ``None``。"""
        return self._domain_of

    @property
    def domain_of_source(self) -> str:
        """:attr:`domain_of` 对应的源码，便于落盘与排查。"""
        return self._domain_of_source

    def get_table_overview(self) -> pd.DataFrame:
        """表清单 DataFrame；尚未抓取时返回空表。"""
        return self._overview

    def result_for(self, task: ProfileTaskName) -> pd.DataFrame:
        """某任务的聚合结果表；没有则返回空表。"""
        return self._task_results.get(task, pd.DataFrame())

    def artifacts_for(self, task: ProfileTaskName) -> dict[str, pd.DataFrame]:
        """该任务最近一次写入黑板的产物字典；从未运行过时返回空 dict。

        表任务返回 ``{"tables_overview": df, table_name: df, ...}``，
        结果任务返回 ``{task.value: df}``（domain 另含 ``domain_of``）。
        """
        version = self._latest_version_for(task)
        return dict(version["artifacts"]) if version else {}

    def _version_root(self, task: ProfileTaskName) -> Path:
        """该任务的版本根目录。

        ``dimension`` / ``metric`` 落在 ``{profile_dir}/dimension_metric/``，
        其余任务落在 ``{profile_dir}/tables/``。
        """
        return self.semantic_dir if task in _SEMANTIC_DIR_TASKS else self.tables_dir

    def _latest_version_for(
        self,
        task: ProfileTaskName,
    ) -> dict[str, Any] | None:
        """该任务 timestamp 最大的版本记录；没有则 ``None``。"""
        for version in reversed(self.versions):
            if version["task_name"] == task.value:
                return version
        return None

    def evidence_for(self, task: ProfileTaskName) -> SourceTableEvidence:
        """把黑板当前状态交给任务，见 :class:`~.base.SourceTableEvidence`。

        任务特化：
            - ``dimension_caliber`` / ``metric_caliber``：只暴露逻辑表
              （默认 ``dataset_*`` / ``view_*``；TEMP 开关打开时仅 ``view_*``），
              无逻辑表时回退全量物理表（TEMP 打开时不回退）；
            - ``dimension_manage`` / ``metric_manage``：TEMP 打开时同样只暴露
              ``view_*``，避免物理表列进入维度/指标聚合。
        """
        overview = self._overview
        table_map: dict[str, pd.DataFrame] = dict(self._table_map)

        _caliber_tasks = (
            ProfileTaskName.DIMENSION_CALIBER,
            ProfileTaskName.METRIC_CALIBER,
        )
        _manage_tasks = (
            ProfileTaskName.DIMENSION_MANAGE,
            ProfileTaskName.METRIC_MANAGE,
        )
        apply_logical_filter = task in _caliber_tasks or (
            DIM_METRIC_REQUIRE_VIEW_PREFIX and task in _manage_tasks
        )
        if apply_logical_filter:
            logical = {
                name: frame
                for name, frame in table_map.items()
                if is_dim_metric_table_name(name) and isinstance(frame, pd.DataFrame)
            }
            if logical or DIM_METRIC_REQUIRE_VIEW_PREFIX:
                # TEMP 打开时即便没有 view_* 也不回退物理表。
                table_map = logical
                if (
                    isinstance(overview, pd.DataFrame)
                    and not overview.empty
                    and "table_name" in overview.columns
                ):
                    overview = overview[
                        overview["table_name"].astype(str).map(is_dim_metric_table_name)
                    ].reset_index(drop=True)

        return SourceTableEvidence(
            task=task,
            table_overview=overview,
            table_name_to_df_map=table_map,
            task_result=self.result_for(task),
            upstream=dict(self._task_results),
            domain_of=self._domain_of,
        )

    # ------------------------------------------------------------------
    # 写接口
    # ------------------------------------------------------------------

    def update(
        self,
        task_results: Mapping[str, pd.DataFrame],
        task: ProfileTaskName,
        *,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """把任一任务的产物写回黑板并落盘为 ``tables/{task}_{ts}/`` 新版本。

        ``task_results`` 恒为 ``dict[str, pd.DataFrame]``：
            - ``task in TABLE_ARTIFACT_TASKS``：键是 ``table_name``，更新列详情；
              可另带 ``tables_overview`` 键整体替换表清单；
            - ``task == BIZ_DOMAIN``：``{"biz_domain": 结果表, "domain_of": 源码表}``；
            - 其余任务：``{task.value: 结果表}``。

        返回本次写入的版本记录 ``{task_name, timestamp, path, artifacts}``。
        """
        artifacts = {
            str(name): frame
            for name, frame in (task_results or {}).items()
            if isinstance(frame, pd.DataFrame)
        }
        if not artifacts:
            raise ValueError(f"{task.value} 的产物为空，无法写回黑板")

        if task in TABLE_ARTIFACT_TASKS:
            self._apply_table_artifacts(artifacts)
        else:
            self._apply_result_artifacts(artifacts, task)

        version = self._write_version(artifacts, task, timestamp)
        self.versions.append(version)
        self.versions.sort(key=lambda item: _version_order(item["timestamp"]))
        return version

    def has_version(self, task: ProfileTaskName) -> bool:
        """该任务是否已经有版本目录（哪怕里面一个产物都还没写）。"""
        return self._latest_version_for(task) is not None

    def open_version(
        self,
        task: ProfileTaskName,
        *,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """为逐表增量落盘开一个**空版本**：只建目录并写表清单，不写任何表。

        与 :meth:`update` 的区别：``update`` 会把黑板上全部表写进版本目录，于是
        目录里混着「本阶段没产出过的表」；开空版本再配合 :meth:`checkpoint_table`，
        目录里就只有本阶段真正产出过的内容。列角色判定阶段用的就是这条路径，
        因此 ``llm_dimension_metric_{ts}/`` 里只会出现判定完成的表。

        返回版本记录 ``{task_name, timestamp, path, artifacts}``，``artifacts``
        初始为空。
        """
        task_name = task.value
        root = self._version_root(task)
        ts = self._next_timestamp(task_name, root, timestamp)
        out_dir = root / f"{task_name}_{ts}"
        out_dir.mkdir(parents=True, exist_ok=True)
        if task in TABLE_ARTIFACT_TASKS and not self._overview.empty:
            self._overview.to_csv(out_dir / self._OVERVIEW_NAME, index=False)

        version = {
            "task_name": task_name,
            "timestamp": ts,
            "path": out_dir,
            "artifacts": {},
        }
        self.versions.append(version)
        self.versions.sort(key=lambda item: _version_order(item["timestamp"]))
        logger.info(f"Opened empty version {task_name}_{ts} -> {out_dir}")
        return version

    def checkpoint_table(
        self,
        task: ProfileTaskName,
        table_name: str,
        frame: pd.DataFrame,
    ) -> Path:
        """把单张表的最新列详情立刻写进该任务当前版本目录，并刷新内存快照。

        与 :meth:`update` 的区别：不新建版本，而是原地覆盖已有版本里的那一个
        ``{schema}_{table}.xlsx``。逐表调 LLM 的任务用它做增量持久化，进程中断
        后重启只需补没写过的表。

        Corner cases：
            - 该任务尚未通过 :meth:`update` 建过版本时抛 ``ValueError``；
            - 文件名固定为 ``{table_name}.xlsx``。
        """
        version = self._latest_version_for(task)
        if version is None:
            raise ValueError(
                f"{task.value} 还没有版本目录，无法增量落盘；请先调用 update()"
            )
        name = str(table_name)
        self._table_map[name] = frame
        version["artifacts"][name] = frame
        path = version["path"] / f"{name}.xlsx"
        frame.to_excel(path, index=False)
        return path

    def _table_artifact_path(self, version_dir: Path, table_name: str) -> Path | None:
        """定位某张表的列详情文件；兼容旧版 ``{schema}_{table}.xlsx``。"""
        name = str(table_name)
        preferred = version_dir / f"{name}.xlsx"
        if preferred.exists():
            return preferred
        legacy = sorted(version_dir.glob(f"*_{name}.xlsx"))
        return legacy[0] if legacy else None

    def _apply_table_artifacts(
        self,
        artifacts: Mapping[str, pd.DataFrame],
    ) -> None:
        """更新表清单与列详情；未出现在本次产物里的表保持原样。"""
        overview = artifacts.get(OVERVIEW_ARTIFACT)
        if overview is not None:
            missing = _OVERVIEW_REQUIRED_COLS - set(overview.columns)
            if missing:
                raise ValueError(f"{OVERVIEW_ARTIFACT} 缺少必需列: {sorted(missing)}")
            cleaned = overview.copy()
            if "table_schema" in cleaned.columns:
                cleaned = cleaned.drop(columns=["table_schema"])
            self._overview = cleaned
        elif self._overview.empty:
            raise ValueError(f"首个版本必须提供 {OVERVIEW_ARTIFACT}")

        for name, frame in artifacts.items():
            if name == OVERVIEW_ARTIFACT:
                continue
            cleaned_frame = frame
            if isinstance(frame, pd.DataFrame) and "table_schema" in frame.columns:
                cleaned_frame = frame.drop(columns=["table_schema"])
            self._table_map[name] = cleaned_frame

    def _apply_result_artifacts(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        task: ProfileTaskName,
    ) -> None:
        """更新该任务的聚合结果表；domain 另外编译并保留 ``domain_of``。"""
        result = artifacts.get(task.value)
        if result is None:
            raise ValueError(
                f"{task.value} 的产物缺少 {task.value!r} 键: {sorted(artifacts)}"
            )
        self._task_results[task] = result

        if task is not ProfileTaskName.BIZ_DOMAIN:
            return
        source = resolver_source_of(artifacts.get(DOMAIN_RESOLVER_ARTIFACT))
        if not source:
            logger.warning("domain 产物未提供 domain_of 源码，黑板不保留解析函数")
            return
        try:
            self._domain_of, self._domain_of_source = compile_domain_of(source)
        except Exception as exc:
            self._domain_of, self._domain_of_source = None, ""
            logger.warning(f"domain_of 编译失败，黑板不保留解析函数: {exc}")

    def _lib_version_dir(
        self,
        task: ProfileTaskName,
        timestamp: str | None,
    ) -> Path:
        """为 dimension_manage / metric_manage 的最终产物选定共享版本目录。

        先复用最新的、还没写过本任务文件的 ``dimension_metric_{ts}/``；没有可复用
        的才新建。这样先跑的 dimension 建目录、后跑的 metric 直接写进同一个目录。
        """
        existing = sorted(
            (
                child
                for child in self.semantic_dir.iterdir()
                if child.is_dir()
                and self._VERSION_DIR_RE.match(child.name)
                and self._VERSION_DIR_RE.match(child.name).group(1) == _LIB_DIR_PREFIX
            ),
            key=lambda child: _version_order(
                self._VERSION_DIR_RE.match(child.name).group(2)
            ),
        )
        for child in reversed(existing):
            if not (child / _LIB_TASK_FILES[task]).exists():
                return child

        ts = self._next_timestamp(
            _LIB_DIR_PREFIX,
            self.semantic_dir,
            timestamp,
        )
        out_dir = self.semantic_dir / f"{_LIB_DIR_PREFIX}_{ts}"
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    def _next_timestamp(
        self,
        dir_name: str,
        root: Path,
        timestamp: str | None,
    ) -> str:
        """生成严格递增且不与已有目录重名的版本时间戳。

        版本重放顺序完全由时间戳决定，所以两个版本绝不能同秒——否则重新加载时
        旧状态可能覆盖新状态。同秒时逐秒后移，直到既大于现有最大时间戳、目录名
        又不冲突为止。
        """
        candidate = timestamp or datetime.now().strftime("%Y%m%d%H%M%S")
        newest = max(
            (_version_order(item["timestamp"]) for item in self.versions),
            default="",
        )
        while (
            _version_order(candidate) <= newest
            or (root / f"{dir_name}_{candidate}").exists()
        ):
            moment = datetime.strptime(
                _version_order(candidate),
                "%Y%m%d%H%M%S",
            ) + timedelta(seconds=1)
            candidate = moment.strftime("%Y%m%d%H%M%S")
        return candidate

    def _write_version(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        task: ProfileTaskName,
        timestamp: str | None,
    ) -> dict[str, Any]:
        """落盘一个自包含的版本目录，并返回该版本记录。"""
        task_name = task.value
        if task in _LIB_TASK_FILES:
            out_dir = self._lib_version_dir(task, timestamp)
            ts = self._VERSION_DIR_RE.match(out_dir.name).group(2)
        else:
            root = self._version_root(task)
            ts = self._next_timestamp(task_name, root, timestamp)
            out_dir = root / f"{task_name}_{ts}"
            out_dir.mkdir(parents=True, exist_ok=True)

        stored: dict[str, pd.DataFrame] = {}
        if task in TABLE_ARTIFACT_TASKS:
            # 每个表版本都写全量，保证目录自包含、可单独加载。
            overview = self._overview
            if "table_schema" in overview.columns:
                overview = overview.drop(columns=["table_schema"])
            overview.to_csv(out_dir / self._OVERVIEW_NAME, index=False)
            stored[OVERVIEW_ARTIFACT] = overview
            for row in self._overview.itertuples(index=False):
                name = str(row.table_name)
                frame = self._table_map.get(name)
                if not isinstance(frame, pd.DataFrame):
                    continue
                if "table_schema" in frame.columns:
                    frame = frame.drop(columns=["table_schema"])
                frame.to_excel(out_dir / f"{name}.xlsx", index=False)
                stored[name] = frame
        else:
            result = artifacts.get(task_name)
            if isinstance(result, pd.DataFrame):
                result.to_excel(
                    out_dir / _LIB_TASK_FILES.get(task, f"{task_name}.xlsx"),
                    index=False,
                )
                stored[task_name] = result
            # 额外结果产物（如 dataset 分批进度 dataset_done.xlsx）一并落盘，供续跑读取。
            for name, frame in artifacts.items():
                if name in {task_name, DOMAIN_RESOLVER_ARTIFACT}:
                    continue
                if not isinstance(frame, pd.DataFrame):
                    continue
                frame.to_excel(out_dir / f"{name}.xlsx", index=False)
                stored[name] = frame
            if task is ProfileTaskName.BIZ_DOMAIN and self._domain_of_source:
                (out_dir / self._DOMAIN_OF_NAME).write_text(
                    self._domain_of_source,
                    encoding="utf-8",
                )
                stored[DOMAIN_RESOLVER_ARTIFACT] = resolver_source_frame(
                    self._domain_of_source
                )

        logger.info(f"Updated {task_name}_{ts}: {len(stored)} artifacts -> {out_dir}")
        return {
            "task_name": task_name,
            "timestamp": ts,
            "path": out_dir,
            "artifacts": stored,
        }

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def create_from_dir(cls, profile_dir: str | Path) -> SourceTablesContext:
        """按 timestamp 顺序重放历史版本，还原黑板状态。

        同时扫描 ``tables/`` 与 ``dimension_metric/`` 两个根目录，两边的版本按
        时间戳统一排序后依次重放。
        """
        ctx = cls(profile_dir)
        logger.info("从磁盘还原黑板: profile_dir=%s", ctx.profile_dir)

        entries: list[tuple[str, str, Path]] = []
        for root in (ctx.tables_dir, ctx.semantic_dir):
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                matched = cls._VERSION_DIR_RE.match(child.name)
                if not matched:
                    continue
                if matched.group(1) == _LIB_DIR_PREFIX:
                    # 共享目录：按里面有哪些 *_lib.xlsx 拆成对应任务的版本记录。
                    for lib_task, file_name in _LIB_TASK_FILES.items():
                        if (child / file_name).exists():
                            entries.append((lib_task.value, matched.group(2), child))
                    continue
                entries.append((matched.group(1), matched.group(2), child))

        logger.info("扫描到历史版本目录 %s 个", len(entries))
        for task_name, ts, child in sorted(
            entries,
            key=lambda item: _version_order(item[1]),
        ):
            try:
                task = ProfileTaskName(task_name)
            except ValueError:
                logger.warning(f"Skip version with unknown task: {child}")
                continue
            artifacts = ctx._read_version(task, child)
            if not artifacts:
                logger.warning(f"Skip empty version: {child}")
                continue
            if task in TABLE_ARTIFACT_TASKS:
                ctx._apply_table_artifacts(artifacts)
            else:
                ctx._apply_result_artifacts(artifacts, task)
            ctx.versions.append(
                {
                    "task_name": task_name,
                    "timestamp": ts,
                    "path": child,
                    "artifacts": artifacts,
                }
            )

        if ctx._domain_of is None:
            ctx._load_standalone_domain_resolver()

        logger.info(
            f"Loaded {len(ctx.versions)} versions; tables={len(ctx._table_map)} "
            f"results={sorted(t.value for t in ctx._task_results)} "
            f"domain_of={'yes' if ctx._domain_of else 'no'}"
        )
        return ctx

    def _load_standalone_domain_resolver(self) -> None:
        """biz_domain 任务没有版本时，退回加载手工维护的 ``domain/domain_resolver.py``。

        这个文件不是某次 profiling 的产物，而是可以人工修订的常驻解析函数；只要
        它存在，黑板就能提供 ``domain_of``，下游任务无需感知它从哪来。
        """
        path = self.profile_dir / _STANDALONE_RESOLVER_PATH
        if not path.exists():
            return
        try:
            self._domain_of, self._domain_of_source = compile_domain_of(
                path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(f"加载 {path} 失败，黑板不保留 domain_of: {exc}")
            return
        logger.info(f"Loaded standalone domain_of from {path}")

    def _read_version(
        self,
        task: ProfileTaskName,
        version_dir: Path,
    ) -> dict[str, pd.DataFrame]:
        """从一个版本目录读回产物字典，形态与 :meth:`update` 的入参一致。"""
        logger.info("从磁盘加载版本: task=%s path=%s", task.value, version_dir)
        artifacts: dict[str, pd.DataFrame] = {}
        overview_path = version_dir / self._OVERVIEW_NAME
        if task in TABLE_ARTIFACT_TASKS and overview_path.exists():
            overview = pd.read_csv(overview_path)
            artifacts[OVERVIEW_ARTIFACT] = overview
            for row in overview.itertuples(index=False):
                name = str(row.table_name)
                path = self._table_artifact_path(version_dir, name)
                if path is not None:
                    frame = pd.read_excel(path, engine="openpyxl")
                    if "table_schema" in frame.columns:
                        frame = frame.drop(columns=["table_schema"])
                    artifacts[name] = frame
            if "table_schema" in overview.columns:
                overview = overview.drop(columns=["table_schema"])
                artifacts[OVERVIEW_ARTIFACT] = overview
            logger.info(
                "已加载表任务版本 %s: overview=%s tables=%s",
                version_dir.name,
                len(overview),
                len(artifacts) - 1,
            )
            return artifacts

        primary_names = {
            Path(_LIB_TASK_FILES.get(task, f"{task.value}.xlsx")).stem,
            task.value,
            Path(self._LEGACY_RESULT_NAME).stem,
        }
        for candidate in (
            version_dir / _LIB_TASK_FILES.get(task, f"{task.value}.xlsx"),
            version_dir / f"{task.value}.xlsx",
            version_dir / self._LEGACY_RESULT_NAME,
        ):
            if candidate.exists():
                artifacts[task.value] = pd.read_excel(
                    candidate,
                    engine="openpyxl",
                )
                logger.info(
                    "已加载结果表 %s rows=%s",
                    candidate.name,
                    len(artifacts[task.value]),
                )
                break
        # 同目录下其它 xlsx（如 dataset_done）作为附加产物读回。
        for path in sorted(version_dir.glob("*.xlsx")):
            stem = path.stem
            if stem in primary_names or stem in artifacts:
                continue
            artifacts[stem] = pd.read_excel(path, engine="openpyxl")
            logger.info(
                "已加载附加产物 %s rows=%s",
                path.name,
                len(artifacts[stem]),
            )
        resolver_path = version_dir / self._DOMAIN_OF_NAME
        if resolver_path.exists():
            artifacts[DOMAIN_RESOLVER_ARTIFACT] = resolver_source_frame(
                resolver_path.read_text(encoding="utf-8")
            )
            logger.info("已加载 domain_of 源码: %s", resolver_path)
        return artifacts

    def wait_add_enums(self) -> None:
        """硬等后台 add_enums，并把结果合进当前黑板（metric LLM 前调用）。"""
        if self._enums_future is not None:
            self._enums_future.result()
            self._enums_future = None
            self._reload_add_enums()
        elif self._latest_version_for(ProfileTaskName.ADD_ENUMS) is None:
            self._reload_add_enums()

    def _reload_add_enums(self) -> None:
        """从磁盘最新 ``add_enums_*`` 合并枚举列（保留口径加宽列）。"""
        prefix = ProfileTaskName.ADD_ENUMS.value
        version_dir: Path | None = None
        best_ts = ""
        for child in self.tables_dir.iterdir() if self.tables_dir.is_dir() else ():
            matched = self._VERSION_DIR_RE.match(child.name) if child.is_dir() else None
            if matched is None or matched.group(1) != prefix:
                continue
            ts = matched.group(2)
            if version_dir is None or _version_order(ts) >= _version_order(best_ts):
                version_dir, best_ts = child, ts
        if version_dir is None:
            return
        artifacts = self._read_version(ProfileTaskName.ADD_ENUMS, version_dir)
        enum_cols = (
            "table_name",
            "is_primary",
            "column_name_cn",
            "column_distinct_num_1d",
            "column_enums_1d",
        )
        for name, frame in artifacts.items():
            if name == OVERVIEW_ARTIFACT or not isinstance(frame, pd.DataFrame):
                continue
            if frame.empty or "column_name" not in frame.columns:
                continue
            cols = [c for c in enum_cols if c in frame.columns]
            slim = frame[["column_name", *cols]].drop_duplicates(
                "column_name", keep="last"
            )
            cur = self._table_map.get(name)
            if cur is None or "column_name" not in getattr(cur, "columns", []):
                self._table_map[name] = frame.copy()
                continue
            drop = [c for c in cols if c in cur.columns]
            base = cur.drop(columns=drop) if drop else cur
            self._table_map[name] = base.merge(slim, on="column_name", how="left")
        self.versions.append(
            {
                "task_name": prefix,
                "timestamp": best_ts,
                "path": version_dir,
                "artifacts": artifacts,
            }
        )
        self.versions.sort(key=lambda item: _version_order(item["timestamp"]))

    @classmethod
    def create_from_execute_sql(
        cls,
        execute_sql_func: Callable[[str], Any],
        out_dir: str | Path,
    ) -> SourceTablesContext:
        """优先从 ``out_dir`` 加载缓存；目录无可用版本时再 execute_sql 拉取。

        schema 就绪后立即返回；若尚无 ``add_enums`` 则用同一个 ``execute_sql_func``
        后台探查落盘，依赖方调用 :meth:`wait_add_enums` 硬等。
        """
        mgr = cls.create_from_dir(out_dir)

        if not mgr.versions:
            logger.info(
                f"No cached table versions under {out_dir}; fetching via execute_sql"
            )
            _, overview = extract_infos_for_tables(execute_sql_func)
            schema_map = _build_schema_map(execute_sql_func, overview)
            mgr.update(
                {OVERVIEW_ARTIFACT: overview, **schema_map},
                ProfileTaskName.ORIGIN_SCHEMA,
            )

        if mgr._latest_version_for(ProfileTaskName.ADD_ENUMS) is None:
            path = mgr.profile_dir

            def _job(
                profile_dir: Path = path,
                fn: Callable[[str], Any] = execute_sql_func,
            ) -> None:
                clone = SourceTablesContext.create_from_dir(profile_dir)
                if clone._latest_version_for(ProfileTaskName.ADD_ENUMS) is not None:
                    return
                logger.info("async add_enums start: %s", profile_dir)
                clone.update(
                    _build_enums_map(
                        fn, clone.get_table_overview(), clone.table_name_to_df_map
                    ),
                    ProfileTaskName.ADD_ENUMS,
                )
                logger.info("async add_enums done: %s", profile_dir)

            mgr._enums_future = _ENUM_POOL.submit(_job)

        logger.info(f"fetch finished: versions={len(mgr.versions)}, tables={len(mgr)}")
        return mgr

    # Mapping → 当前列详情
    def __getitem__(self, table_name: str) -> pd.DataFrame:
        return self._table_map[table_name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._table_map)

    def __len__(self) -> int:
        return len(self._table_map)


# ========================================
# 第一步，抽取得到全量的表信息
# ========================================

SourceTableManager = SourceTablesContext

SCHEMA_TABLES_SQL_FOR_PG = """
    SELECT 
        n.nspname AS table_schema,
        c.relname AS table_name,
        d.description AS table_comment,
        v.definition AS view_definition,
        CASE c.relkind 
            WHEN 'r' THEN 'TABLE' 
            WHEN 'v' THEN 'VIEW' 
            WHEN 'm' THEN 'MATERIALIZED_VIEW' 
        END AS object_type
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = 0
    LEFT JOIN pg_views v ON v.viewname = c.relname AND v.schemaname = n.nspname
    WHERE 
        c.relkind IN ('r', 'v', 'm')               -- 'r'=普通表, 'v'=视图, 'm'=物化视图
        AND n.nspname NOT IN ({system_schemas})
        -- ND c.relname !~ '_YYYYMMDD$'   -- 过滤掉以 _YYYYMMDD 结尾的表
        AND c.relname NOT LIKE 'tmp%'  -- 【新增】过滤掉以 tmp 开头的临时表
        {{}}
    ORDER BY n.nspname, c.relname;
""".format(system_schemas=_SYSTEM_SCHEMA_SQL)


def extract_infos_for_tables(execute_sql: Callable[[str], pd.DataFrame]):
    """拉取库内全部普通表，并把分区表合并成一张逻辑表。

    返回 ``(tables_df, merged_df)``。``tables_df`` 是原始清单（仅抓取中间态）::

        table_name,table_comment
        ads_example_revenue_index_1d_20260701,收入指标日汇总
        ads_example_revenue_index_1d_20260702,收入指标日汇总

    ``merged_df`` 是去掉 ``_YYYYMMDD`` 后缀后合并的结果，也就是写进黑板的表清单::

        table_name,merged_tables,table_comment
        ads_example_revenue_index_1d,"['ads_..._20260701', 'ads_..._20260702']",['收入指标日汇总']

    Corner cases：
        - 过滤掉系统 schema 与 ``tmp%`` 临时表；
        - 主键仅为 ``table_name``，不保留 ``table_schema``；
        - ``merged_tables`` / ``table_comment`` 都是去重后的列表。
    """

    def merge_partitioned_tables(df: pd.DataFrame) -> pd.DataFrame:
        """
        合并分区表: 有太多形如`xxx_20261201`的表，实际处理的时候应该只处理其中一个就可以；
        目前 PG 中支持使用 from xxxx where ds = '20261201' 的方式查询，所以我们直接合并得到总表。
        """
        if df.empty:
            return df.copy()

        pattern = r"_\d{6,8}$"
        df = df.copy()
        df["_base_table_name"] = (
            df["table_name"].astype(str).str.replace(pattern, "", regex=True)
        )

        # 所有列统一使用 sorted(set(...)) 聚合
        agg_cols = [c for c in df.columns if c != "_base_table_name"]
        agg_dict = {col: lambda x: sorted(set(x.dropna())) for col in agg_cols}

        result_df = (
            df.groupby("_base_table_name", sort=False)
            .agg(agg_dict)
            .reset_index()
            .rename(
                columns={
                    "_base_table_name": "table_name",
                    "table_name": "merged_tables",
                }
            )
        )
        return result_df

    tables_df = execute_sql(SCHEMA_TABLES_SQL_FOR_PG.format(""))
    # tables_df = tables_df[["table_name", "table_comment", "view_definition"]].reset_index(drop=True)
    merged_df = merge_partitioned_tables(tables_df)

    return tables_df, merged_df


# ========================================
# 针对每个表，都抽取它的所有的列信息
# ========================================

COLUMN_DETAIL_SQL_FOR_PG = """
    SELECT 
        a.attname AS column_name,

        -- 数据类型处理 (数组类型显示为 type[])
        CASE 
            WHEN a.attndims > 0 THEN t.typname || '[]' 
            ELSE t.typname 
        END AS data_type,
        
        -- 是否可空
        CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable,
        
        -- 字段注释
        d.description AS column_comment,
        
        -- 是否为主键
        CASE WHEN con.conname IS NOT NULL THEN 'YES' ELSE 'NO' END AS is_primary_key,
        
        -- 字段顺序
        a.attnum AS ordinal_position
        
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_type t ON t.oid = a.atttypid
    
    LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = a.attnum
    LEFT JOIN pg_constraint con ON con.conrelid = c.oid 
                               AND con.contype = 'p' 
                               AND a.attnum = ANY(con.conkey)
    
                               
    WHERE a.attrelid = {}::regclass
        AND a.attnum > 0               -- 排除系统隐藏列
        AND NOT a.attisdropped         -- 排除已删除列
        
    ORDER BY a.attnum;
"""


def extract_infos_for_columns(
    execute_sql: Callable[[str], pd.DataFrame], table_name: str
):
    """拉取单表列结构。
    输出的结果示例::
        column_name,data_type,is_nullable,column_comment,is_primary_key,ordinal_position
        date_id,text,NO,数据日期,YES,1
        gmv,numeric,YES,成交额,NO,2
    """
    table_sql = "'" + str(table_name).replace("'", "''") + "'"
    sql = COLUMN_DETAIL_SQL_FOR_PG.format(table_sql)
    cols_df = execute_sql(sql)
    return cols_df


# ========================================
# 第二步，针对每个列，都抽取它的枚举值信息，用于判断是否是维度列
# ========================================


def extract_enums_for_column(
    execute_sql: Callable[[str], pd.DataFrame],
    cols_df: pd.DataFrame,
    table_name: str,
    *,
    enums_threshold: int = 101,
) -> pd.DataFrame:
    """在列结构基础上探查枚举值，产出写入黑板的列详情。

    ``table_schema`` 仅用于拼探查 SQL，不写入返回结果。

    输出的结果示例（``column_enums_1d`` 超过 100 个不同值时 distinct 记作 ``100+``）::

        table_name,column_name,is_primary,is_nullable,data_type,column_comment,column_name_cn,column_distinct_num_1d,column_enums_1d
        ads_..._1d,general_type,N,Y,text,业务类型,,3,"['自研', '生态', '其他']"
        ads_..._1d,gmv,N,Y,numeric,成交额,,100+,"[...]"

    Corner cases：
        - 自动识别 ``dt`` / ``ds`` / ``date_id`` 分区列并只探查最新分区，避免全表扫描；
        - 探查 SQL 报错时该列的 distinct 与枚举留空，不影响其余列。
    """

    # 自动识别时间分区列 (ds / dt)
    col_names = {str(c.column_name).lower() for c in cols_df.itertuples(index=False)}
    partition_col = next((c for c in ("dt", "ds", "date_id") if c in col_names), None)
    rows: list[dict[str, Any]] = []
    for col in cols_df.itertuples(index=False):
        comment = getattr(col, "column_comment", None)
        comment = (
            comment
            if comment is None or (isinstance(comment, float) and pd.isna(comment))
            else str(comment).strip()
        )
        column_name = str(col.column_name)
        column_distinct_num_1d = None
        column_enums_1d = None

        # 优化计数逻辑，传入分区列
        table_sql = table_name
        column_sql = '"' + str(column_name).replace('"', '""') + '"'
        # 动态拼接分区过滤条件
        partition_where = ""
        if partition_col:
            safe_part_col = '"' + str(partition_col).replace('"', '""') + '"'
            # 取最新的一个分区进行探查，避免全表扫描
            partition_where = f" WHERE {safe_part_col} = (SELECT MAX({safe_part_col}) FROM {table_sql})"
        #  极速探查 SQL：只取 101 条，找到 101 个不重复的值就立刻停止扫描
        count_sql = f"""
            SELECT DISTINCT {column_sql} as column_enums_1d FROM {table_sql} {partition_where} LIMIT {enums_threshold}
        """
        try:
            count_df = execute_sql(count_sql)
            if not count_df.empty:
                column_distinct_num_1d = len(count_df)
                column_enums_1d = count_df["column_enums_1d"].values
        except Exception as exc:
            logger.warning(
                "enum probe failed for %s.%s: %s",
                table_name,
                column_name,
                exc,
            )

        logger.debug(
            "Extracting column: %s.%s -> %s distinct",
            table_name,
            column_name,
            column_distinct_num_1d,
        )
        rows.append(
            {
                "table_name": table_name,
                "column_name": column_name,
                "is_primary": "Y"
                if str(getattr(col, "is_primary_key", "NO")).upper() == "YES"
                else "N",
                "is_nullable": "Y"
                if str(getattr(col, "is_nullable", "NO")).upper() == "YES"
                else "N",
                "data_type": str(col.data_type),
                "column_comment": comment,
                "column_name_cn": None,
                # 如果超过 100，业务上展示为 "100+" 更直观
                "column_distinct_num_1d": column_distinct_num_1d
                if column_distinct_num_1d is None or column_distinct_num_1d <= 100
                else "100+",
                "column_enums_1d": column_enums_1d
                if column_enums_1d is None or not isinstance(column_enums_1d, list)
                else ",".join(column_enums_1d),
            }
        )
    return pd.DataFrame(rows)


def _parse_merged_tables(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return list(ast.literal_eval(raw))
    if isinstance(raw, list):
        return list(raw)
    raise ValueError(f"Invalid merged_tables: {raw}")


# 1. FETCH_SCHEMA
def _build_schema_map(
    execute_sql_func: Callable[[str], pd.DataFrame],
    overview: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """为表清单里的每张逻辑表拉一次列结构，返回 ``{table_name: 列详情}``。

    分区表只取 ``merged_tables`` 中最大的那个分区作为代表；列详情的形态见
    :func:`extract_infos_for_columns`。空表会被跳过，不出现在返回值里。
    """
    schema_map: dict[str, pd.DataFrame] = {}
    for table in overview.itertuples(index=False):
        table_name = str(table.table_name)
        merged_tables = _parse_merged_tables(table.merged_tables)
        representative_table_name = max(merged_tables)
        logger.info(
            f"Extracting schema for {table_name} "
            f"via representative {representative_table_name} ({len(merged_tables)} similar-name tables)"
        )
        cols_df = extract_infos_for_columns(execute_sql_func, representative_table_name)
        if not isinstance(cols_df, pd.DataFrame) or cols_df.empty:
            logger.info(f"Skipping empty table: {representative_table_name}")
            continue
        schema_map[table_name] = cols_df
    return schema_map


# 2. ADD_ENUMS
def _build_enums_map(
    execute_sql_func: Callable[[str], pd.DataFrame],
    overview: pd.DataFrame,
    schema_map: Mapping[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """在 :func:`_build_schema_map` 的结果上补枚举值，返回 ``{table_name: 列详情}``。

    列详情形态见 :func:`extract_enums_for_column`；缺少列结构的表会被跳过。
    """
    enums_map: dict[str, pd.DataFrame] = {}
    for table in overview.itertuples(index=False):
        table_name = str(table.table_name)
        cols_df = schema_map.get(table_name)
        if cols_df is None or not isinstance(cols_df, pd.DataFrame) or cols_df.empty:
            logger.warning(f"Skip enums for missing column detail: {table_name}")
            continue
        merged_tables = _parse_merged_tables(
            getattr(table, "merged_tables", [table_name])
        )
        representative_table_name = max(merged_tables)
        enums_map[table_name] = extract_enums_for_column(
            execute_sql_func,
            cols_df,
            representative_table_name,
        ).assign(table_name=table_name)
    return enums_map


if __name__ == "__main__":
    # 这个过程应该在配置数据源的第一时间执行，立马获取所有数据表的信息
    from profiling.sql_executor import execute_read

    run_execute_sql = lambda sql: execute_read(  # noqa: E731
        sql,
        datasource_id="1",
        max_rows=100000,
    )
    SourceTablesContext.create_from_execute_sql(
        run_execute_sql, out_dir="data/profile/"
    )
