"""DATASET：两个来源产出语义视图（逻辑表），完整携带列元数据。

------------------------------------------------------------------------
两个来源
------------------------------------------------------------------------
::

    直通   物理对象命中白名单 → 直接成为 dataset，列元数据原样复制
    日志   高频 SQL 模板 → LLM 命名描述 → D1 列继承

两者都开时结果**合并**后一并存回 dataset context。合并归 handler 而不是
evidence 层——后者一旦认识「这个任务有几个来源」，就开始认识每个任务的
内部算法了。

**直通先占名。** dataset_name 是逻辑表主键，直通对象的名字就是真实关系名，
被改名等于身份失真；LLM 命名撞车时加序号消歧的是后者。

两个来源都关（或日志侧不可用而直通侧未开）→ **抛错**，不是产出空快照：
静默的空会被读成「没探到 dataset」，而实际是配置上就没有来源。

------------------------------------------------------------------------
日志来源的 LLM 流程
------------------------------------------------------------------------
移植自 legacy ``DatasetTaskHandler``（分批 20、``validate_dataset_meta_items``
全批校验、无 domain 的候选丢弃、``view_{父表}`` 兜底命名）。两点新架构差异：

- **D1 列元数据继承**（legacy 只产 dataset 行表）：新契约要求 dataset 是
  ``TableSnapshot``——逻辑表完整继承列元数据，此后 CALIBER / DICT 都在
  dataset 之上工作。继承用 ``parser.sql_analysis`` 解析模板投影列映射回
  父表列；解析不出时回退「父表列并集」。LLM 只负责命名与描述，不负责列。
- **分批失败不再中断后续批次**：legacy 靠 ``dataset_done`` 进度表续跑，
  失败即 break；现在断点续跑由 LLM 响应缓存承担，失败批次下次重跑秒过
  已成功的调用，本轮其余批次照常产出。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from typing import Any, Iterable

import pandas as pd
from sqlglot import exp

from ..evidence import DomainEvidence, TaskEvidence
from ..parser.sql_analysis import SqlAnalysis
from ..snapshots import TableSnapshot
from .base import TaskHandler
from .domain import DomainHandler
from .llm import AsyncLlmClient, extract_json_payload
from .prompts import build_dataset_meta_system_prompt, build_dataset_meta_user_prompt
from .runner import LlmRunner, LlmUnit
from .validators import merge_dataset_views, validate_dataset_meta_items

logger = logging.getLogger(__name__)


def _is_star_projection(node: exp.Expression) -> bool:
    if isinstance(node, exp.Star):
        return True
    if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
        return True
    if isinstance(node, exp.Alias):
        return _is_star_projection(node.this)
    return False


def _text(value: Any) -> str | None:
    """单元格 → 非空字符串；NA / 空串归一成 None。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value).strip() or None


def _like_matcher(patterns: Iterable[str]) -> re.Pattern[str] | None:
    """SQL LIKE 白名单 → 一个大小写不敏感的正则。空白名单返回 None。

    内存侧筛选，没有理由迁就 PG ``LIKE`` 的大小写敏感——那条语义属于
    ``input_table_exclude_patterns``，它是拼进 SQL 的。
    """
    alternatives = []
    for pattern in patterns:
        text = str(pattern)
        if not text:
            continue
        alternatives.append(
            "".join(
                ".*" if ch == "%" else "." if ch == "_" else re.escape(ch)
                for ch in text
            )
        )
    if not alternatives:
        return None
    return re.compile("|".join(alternatives), re.IGNORECASE)


def _unique_name(name: str, taken: set[str]) -> str:
    """dataset_name 是逻辑表主键，撞车时加序号消歧。"""
    unique, n = name, 1
    while unique in taken:
        n += 1
        unique = f"{name}__{n}"
    taken.add(unique)
    return unique


class DatasetHandler(TaskHandler):
    """产出 :class:`TableSnapshot`（逻辑表 + 继承的列元数据）。"""

    def __init__(
        self,
        llm: AsyncLlmClient | None,
        *,
        runner: LlmRunner | None = None,
        batch_size: int = 20,
        dialect: str = "postgres",
        derived_from_tables_enabled: bool = True,
        derived_from_tables_include_patterns: tuple[str, ...] = ("view%", "dataset%"),
        parse_from_logs_enabled: bool = True,
        language: str = "zh",
        #: 尚未实现逐草稿 refine；各模式统一复用归属或补分配后抽取 Dataset。
        mode: str = "create",
    ) -> None:
        self._llm = llm
        self._runner = runner
        self._batch_size = max(1, int(batch_size))
        self._dialect = dialect
        self._language = language
        self._derived_from_tables_enabled = bool(derived_from_tables_enabled)
        self._derived_from_tables_matcher = _like_matcher(
            derived_from_tables_include_patterns or ()
        )
        self._parse_from_logs_enabled = bool(parse_from_logs_enabled)

    # -- D1：列元数据继承 -------------------------------------------------

    def _inherit_columns(
        self,
        dataset_name: str,
        sql_content: str,
        parent_names: list[str],
        evidence: TaskEvidence,
    ) -> pd.DataFrame:
        """模板投影列 → 父表列元数据；解析不出时回退父表列并集。"""
        snapshot = evidence.tables.snapshot if evidence.tables else None
        if snapshot is None:
            return pd.DataFrame(columns=["table_name", "column_name"])
        parent_meta = {name: snapshot.column_meta_of(name) for name in parent_names}

        def _lookup(column_name: str) -> dict[str, Any] | None:
            target = column_name.lower()
            for frame in parent_meta.values():
                if not len(frame):
                    continue
                hit = frame[frame["column_name"].astype(str).str.lower() == target]
                if len(hit):
                    return hit.iloc[0].to_dict()
            return None

        rows: list[dict[str, Any]] = []
        try:
            analysis = SqlAnalysis.create_from_sql(sql_content, dialect=self._dialect)
            select = analysis.select_exprs[0]
            for proj in select.expressions:
                if _is_star_projection(proj):
                    rows = []  # 全 * 投影：直接走并集回退
                    raise LookupError
                alias = str(proj.alias).strip() if isinstance(proj, exp.Alias) else ""
                target = proj.this if isinstance(proj, exp.Alias) else proj
                if isinstance(target, exp.Column):
                    inherited = _lookup(analysis.extract_column_name(target))
                    if inherited is not None:
                        row = dict(inherited)
                        if alias:
                            row["column_name"] = alias
                        rows.append(row)
                        continue
                if alias:
                    rows.append(
                        {
                            "column_name": alias,
                            "data_type": "derived",
                            "column_comment": target.sql(dialect=self._dialect),
                        }
                    )
        except LookupError:
            pass
        except Exception as exc:
            logger.info(
                "dataset %s: 模板解析失败，列继承走并集回退: %s", dataset_name, exc
            )
            rows = []

        if not rows:
            frames = [frame for frame in parent_meta.values() if len(frame)]
            if not frames:
                return pd.DataFrame(columns=["table_name", "column_name"])
            union = pd.concat(frames, ignore_index=True)
            union = union.drop_duplicates(subset=["column_name"], keep="first")
            union = union.reset_index(drop=True)
            union["table_name"] = dataset_name
            return union

        frame = pd.DataFrame(rows)
        frame["table_name"] = dataset_name
        return frame

    # -- 行物化（忠实 legacy _materialize_rows）---------------------------

    @staticmethod
    def _parent_names(parents: str) -> list[str]:
        try:
            names = json.loads(parents)
        except (TypeError, json.JSONDecodeError):
            names = [item.strip() for item in str(parents).split(",") if item.strip()]
        return [str(name) for name in names if str(name).strip()]

    def _materialize(
        self,
        enriched: pd.DataFrame,
        evidence: TaskEvidence,
        taken_names: set[str],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        domains = evidence.domains
        for row in enriched.to_dict(orient="records"):
            parent_names = self._parent_names(str(row.get("parents") or ""))
            first_parent = parent_names[0] if parent_names else None
            domain_name = domains.domain_of(first_parent) if domains else None
            if not domain_name:
                logger.info(
                    "dataset: 跳过无 domain 的候选（first_parent=%r）", first_parent
                )
                continue
            name = str(row.get("name") or "").strip() or (
                f"view_{str(first_parent).rsplit('.', 1)[-1]}"
                if first_parent
                else "unnamed_view"
            )
            unique = _unique_name(name, taken_names)
            rows.append(
                {
                    "table_name": unique,
                    "table_comment": str(row.get("description") or "").strip() or None,
                    "domain_name": domain_name,
                    "dataset_type": "VIEW",
                    "sql_content": str(row.get("repeated_sql") or "") or None,
                    "parents": ",".join(parent_names) or None,
                    # v2.1 单指针：日志派生 dataset 的正身就是那段 SQL
                    "source": str(row.get("repeated_sql") or "") or None,
                }
            )
        return rows

    # -- 来源一：物理表直通 ------------------------------------------------

    def _derived_from_tables(
        self,
        evidence: TaskEvidence,
        taken_names: set[str],
    ) -> tuple[list[dict[str, Any]], dict[str, pd.DataFrame]]:
        """命中白名单的物理对象直接成为 dataset，列元数据原样复制。

        不走 D1 继承：直通对象**就是**那张表，没有投影表达式可解析，
        ``column_meta_of`` 拿到的已经是最终形态。
        """
        snapshot = evidence.tables.snapshot if evidence.tables else None
        if snapshot is None or snapshot.is_empty():
            logger.info("dataset: 无物理表证据，直通来源跳过")
            return [], {}
        if self._derived_from_tables_matcher is None:
            logger.warning(
                "dataset: 直通已开启但白名单为空，一张表都不会直通"
                "（dataset_derived_from_tables_include_patterns）"
            )
            return [], {}

        domains = evidence.domains
        rows: list[dict[str, Any]] = []
        column_meta: dict[str, pd.DataFrame] = {}
        skipped_no_domain = 0
        for record in snapshot.table_meta.to_dict(orient="records"):
            name = _text(record.get("table_name"))
            if not name or not self._derived_from_tables_matcher.fullmatch(name):
                continue
            # 与日志来源同一条规则：没有域归属的候选一律丢弃
            domain_name = domains.domain_of(name) if domains else None
            if not domain_name:
                skipped_no_domain += 1
                continue
            unique = _unique_name(name, taken_names)
            # v2.1 L2.source / P2.table_name 都是物理**全名**（含 schema，如
            # public.orders；BQ 为 project.dataset.table）：weave 用
            # (source, field_name) 去 P3 富化样例值，短名对不上就静默落空。
            # 算法内部（血缘/证据）仍用短名，只有对外指针用全名。
            physical_full_name = _text(record.get("table_fullname")) or (
                f"{schema}.{name}"
                if (schema := _text(record.get("table_schema"))) and "." not in name
                else name
            )
            # TODO: 这里过度依赖于列的具体名称，如果切换数据源，将很难适配；后续需要调整，将object_type，view_definition等收归统一管理，类似于table的内容。
            rows.append(
                {
                    "table_name": unique,
                    "table_comment": _text(record.get("table_comment")),
                    "domain_name": str(domain_name),
                    # object_type 由表层 SQL 直接产出（TABLE / VIEW /
                    # MATERIALIZED_VIEW）；老缓存没这一列时退回 TABLE
                    "dataset_type": _text(record.get("object_type")) or "TABLE",
                    # 视图的定义就是它的 sql_content；普通表没有
                    "sql_content": _text(record.get("view_definition")),
                    "parents": name,
                    # v2.1 单指针：直通对象的正身是物理全名 —— 不能用
                    # view_definition（SQLite 是整段 CREATE VIEW DDL，进
                    # source 会被读侧/weave 误判成表名，血缘被垃圾键污染）
                    "source": physical_full_name,
                }
            )
            columns = snapshot.column_meta_of(name)
            if len(columns):
                copied = columns.copy()
                copied["table_name"] = unique
                column_meta[unique] = copied

        if skipped_no_domain:
            logger.info("dataset: 直通跳过 %s 个无 domain 的对象", skipped_no_domain)
        logger.info("dataset: 直通产出 %s 个逻辑表", len(rows))
        return rows, column_meta

    # -- 来源二：日志模板 --------------------------------------------------

    async def _from_logs(
        self,
        evidence: TaskEvidence,
        taken_names: set[str],
    ) -> tuple[list[dict[str, Any]], dict[str, pd.DataFrame]]:
        templates = evidence.logs.of("sql_template")
        if self._llm is None or self._runner is None:
            # D4 降级：未配置 LLM 时日志来源产出空，直通来源不受影响
            logger.warning("dataset: 未配置 LLM，日志来源跳过（D4 降级）")
            return [], {}

        batches = [
            templates.iloc[start : start + self._batch_size].copy()
            for start in range(0, len(templates), self._batch_size)
        ]
        system_prompt = build_dataset_meta_system_prompt(self._language)
        units = [
            LlmUnit(
                label=f"dataset:batch{index + 1}/{len(batches)}",
                system_prompt=system_prompt,
                user_prompt=build_dataset_meta_user_prompt(
                    [
                        {
                            "sql": str(row.repeated_sql),
                            "parents": str(row.parents),
                            "count": int(row.count),
                        }
                        for row in batch.itertuples(index=False)
                    ],
                    self._language,
                ),
            )
            for index, batch in enumerate(batches)
        ]
        logger.info(
            "dataset: 模板 %s 条，分 %s 批 batch_size=%s",
            len(templates),
            len(batches),
            self._batch_size,
        )
        responses = await self._runner.run_units(units)

        meta_rows: list[dict[str, Any]] = []
        for batch, response in zip(batches, responses):
            if response is None:
                continue
            try:
                views = validate_dataset_meta_items(
                    extract_json_payload(response),
                    set(batch["repeated_sql"].astype(str)),
                )
                enriched = merge_dataset_views(batch, views)
            except Exception as exc:
                logger.warning("Invalid dataset response: %s", exc)
                continue
            meta_rows.extend(self._materialize(enriched, evidence, taken_names))

        if not meta_rows:
            logger.info("dataset: 日志来源无有效候选（全部无 domain 或批次失败）")
            return [], {}

        column_meta = {
            str(row["table_name"]): self._inherit_columns(
                str(row["table_name"]),
                str(row.get("sql_content") or ""),
                self._parent_names(str(row.get("parents") or "")),
                evidence,
            )
            for row in meta_rows
        }
        logger.info(
            "dataset: 日志来源产出 %s 个逻辑表（列继承自 %s 张物理表）",
            len(meta_rows),
            len(
                {
                    p
                    for row in meta_rows
                    for p in self._parent_names(str(row.get("parents") or ""))
                }
            ),
        )
        return meta_rows, column_meta

    # -- 主流程 ------------------------------------------------------------

    async def _with_domain_assignments(self, evidence: TaskEvidence) -> TaskEvidence:
        """复用已确认成员；缺失部分（含被拒绝成员）只在正式目录内重新提议。

        补分配只进入本轮 Dataset evidence，不保存 Domain snapshot，也不写
        Domain staging。最终归属随 Dataset 候选交给用户审核。
        """
        assigned = await DomainHandler(
            self._llm, language=self._language
        ).assign_existing(
            evidence,
            runner=self._runner,
        )
        if assigned.is_empty():
            return evidence
        previous = evidence.domains
        members = (
            pd.concat([previous.members, assigned.members], ignore_index=True)
            if previous is not None
            else assigned.members
        )
        return replace(
            evidence,
            domains=DomainEvidence(
                members=members,
                domains=previous.domains
                if previous is not None
                else evidence.domain_catalog,
            ),
        )

    async def handle(self, evidence: TaskEvidence) -> TableSnapshot:
        logs_usable = self._parse_from_logs_enabled and evidence.has_log("sql_template")
        if not logs_usable and not self._derived_from_tables_enabled:
            raise RuntimeError(
                "DATASET 没有任何可用来源："
                + (
                    "dataset_parse_from_logs_enabled=False"
                    if not self._parse_from_logs_enabled
                    else "查询日志无 sql_template 证据"
                )
                + "，而 dataset_derived_from_tables_enabled=False。"
                "开启物理表直通，或恢复日志来源后重跑。"
            )

        evidence = await self._with_domain_assignments(evidence)

        # 直通先跑：它的名字是真实关系名，应当优先占用 dataset_name
        taken_names: set[str] = set()
        meta_rows: list[dict[str, Any]] = []
        column_meta: dict[str, pd.DataFrame] = {}

        if self._derived_from_tables_enabled:
            rows, columns = self._derived_from_tables(evidence, taken_names)
            meta_rows.extend(rows)
            column_meta.update(columns)

        if logs_usable:
            rows, columns = await self._from_logs(evidence, taken_names)
            meta_rows.extend(rows)
            column_meta.update(columns)
        elif self._parse_from_logs_enabled:
            logger.info("dataset: 无 SQL 模板证据，日志来源跳过（直通来源已兜底）")

        if not meta_rows:
            logger.info("dataset: 两个来源均无有效候选，产出空快照")
            return TableSnapshot.build(None)

        logger.info("dataset: 合计产出 %s 个逻辑表", len(meta_rows))
        return TableSnapshot.build(pd.DataFrame(meta_rows), column_meta)


__all__ = ["DatasetHandler"]
