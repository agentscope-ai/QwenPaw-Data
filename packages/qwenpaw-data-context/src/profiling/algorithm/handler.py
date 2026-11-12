"""各 Profile 任务的 handler 实现。

普通任务复用单次 LLM 调用模板；Domain 在此编排输入准备、覆盖率检查和一次重试。
dimension_caliber / metric_caliber 不产出候选行，它们的产物就是「更新后的源表」：
它们的 ``handle`` 返回 ``{table_name: 列详情 DataFrame}``，由黑板直接吸收；
其中 metric_caliber 会额外逐表调用 LLM 判定各列的维度 / 指标 / 日期角色。
确定性的 JOIN 提取、响应校验及结果转换留在 ``profiler_biz_domain``。
"""

from __future__ import annotations

import ast
import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pandas as pd

from .profiler_biz_domain import (
    MIN_DOMAIN_COVERAGE,
    DomainInput,
    DomainResolution,
    JoinEvidence,
    build_domain_result_rows,
    parse_domain_resolution,
    prepare_domain_input,
)
from .profiler_dataset import (
    merge_dataset_views,
    validate_dataset_meta_items,
)
from .profiler_dimension_caliber import merge_dimension_evidence_into_tables
from .profiler_metric_caliber import merge_metric_evidence_into_tables
from .profiler_dimension_and_metric import (
    validate_dimension_description_items,
    validate_metric_description_items,
    validate_name_normalization_items,
)
from .prompts import (
    COLUMN_ROLES,
    build_name_normalization_system_prompt,
    build_name_normalization_user_prompt,
    METRIC_DATE_RANGES,
    build_column_role_system_prompt,
    build_column_role_user_prompt,
    build_dataset_meta_system_prompt,
    build_dataset_meta_user_prompt,
    build_dimension_meta_system_prompt,
    build_dimension_meta_user_prompt,
    build_domain_retry_prompt,
    build_domain_system_prompt,
    build_domain_user_prompt,
    build_metric_system_prompt,
    build_metric_user_prompt,
)
from .base import (
    DIM_METRIC_REQUIRE_VIEW_PREFIX,
    DOMAIN_RESOLVER_ARTIFACT,
    OVERVIEW_ARTIFACT,
    LlmClient,
    LogEvidence,
    ProfileTaskHandler,
    ProfileTaskName,
    QueryLogsContext,
    SourceTableEvidence,
    TablesContext,
    is_dim_metric_table_name,
    resolver_source_frame,
    rows_to_dataframe,
)
from .data_context import SourceTablesContext
from .llm import extract_json_payload

logger = logging.getLogger(__name__)


def _upstream_records(
    evidence: SourceTableEvidence,
    task: ProfileTaskName,
) -> list[dict[str, Any]]:
    upstream = evidence.upstream.get(task)
    if isinstance(upstream, pd.DataFrame):
        if upstream.empty:
            return []
        rows = upstream.to_dict(orient="records")
    elif isinstance(upstream, list):
        rows = [dict(item) for item in upstream if isinstance(item, Mapping)]
    else:
        return []

    cleaned: list[dict[str, Any]] = []
    for row in rows:
        cleaned.append(
            {
                key: (
                    None
                    if value is None or (isinstance(value, float) and pd.isna(value))
                    else value
                )
                for key, value in row.items()
            }
        )
    return cleaned


def _domain_by_table(
    evidence: SourceTableEvidence,
) -> dict[str, str]:
    """``{table_name: domain_name}``，优先用黑板上编译好的 ``domain_of``。

    输出的结果示例::

        {"ads_example_revenue_index_1d": "revenue",
         "ads_example_dashboard_overview_1d": "dashboard"}

    Corner cases：
        - biz_domain 任务没跑过（``domain_of`` 为 ``None``）时，退回解析 biz_domain 结果表
          的 ``member_tables`` JSON；两者都没有则返回空 dict。
    """
    mapping: dict[str, str] = {}
    resolver = evidence.domain_of
    if resolver is not None:
        for table_name in evidence.table_name_to_df_map or {}:
            name = str(table_name)
            try:
                domain_name = resolver(name)
                if not domain_name:
                    short = name.rsplit(".", 1)[-1]
                    if short != name:
                        domain_name = resolver(short)
            except Exception as exc:
                logger.warning(f"domain_of({name!r}) 执行失败: {exc}")
                continue
            if domain_name:
                mapping[name] = str(domain_name)
        if mapping:
            return mapping

    for item in _upstream_records(evidence, ProfileTaskName.BIZ_DOMAIN):
        domain_name = str(item.get("domain_name") or "")
        if not domain_name:
            continue
        members = item.get("member_tables") or []
        if isinstance(members, str):
            try:
                members = json.loads(members)
            except (TypeError, json.JSONDecodeError):
                members = []
        if not isinstance(members, list):
            continue
        for member in members:
            if isinstance(member, Mapping):
                source_table = str(member.get("source_table") or "")
            else:
                source_table = str(getattr(member, "source_table", "") or "")
            table_name = source_table.rsplit(".", 1)[-1]
            if not table_name:
                continue
            mapping[table_name] = domain_name
    return mapping


def _resolve_table_domain(
    domains: Mapping[str, str],
    table_name: str | None,
) -> str | None:
    """按全名或末段短名解析 domain；解析不到返回 None。"""
    if not table_name:
        return None
    name = str(table_name).strip()
    if not name:
        return None
    domain = domains.get(name) or domains.get(name.rsplit(".", 1)[-1])
    return str(domain) if domain else None


def _tables_with_domain(
    evidence: SourceTableEvidence,
) -> dict[str, pd.DataFrame]:
    """只保留能映射到非空 domain 的表列详情。"""
    domains = _domain_by_table(evidence)
    return {
        name: frame
        for name, frame in (evidence.table_name_to_df_map or {}).items()
        if isinstance(frame, pd.DataFrame) and _resolve_table_domain(domains, name)
    }


def _transform_log_records_to_table_name_rows(
    records: Any,
    table_names: set[str],
) -> Any:
    """把日志中的表引用转换为每个无 schema table_name 一行。"""
    if not isinstance(records, pd.DataFrame) or "table_name" not in records.columns:
        return records

    def extract_table_names(value: Any) -> list[str]:
        return [
            table_ref.rsplit(".", 1)[-1]
            for item in str(value or "").split(",")
            if (table_ref := item.strip()) and table_ref != "_"
        ]

    transformed = records.copy()
    transformed["table_name"] = transformed["table_name"].map(extract_table_names)
    transformed = transformed.explode("table_name", ignore_index=True)
    transformed = transformed[transformed["table_name"].notna()]
    if table_names:
        transformed = transformed[transformed["table_name"].isin(table_names)]
    return transformed.reset_index(drop=True)


def _as_source_list(value: Any) -> list[str]:
    """把 ``sources`` 归一化为字符串列表。

    日志证据在内存中是 ``list``，经 CSV 落盘再读回则是 ``"['group_by']"``
    这样的字面量字符串，两种形态都要能解析。
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, (list, tuple, set)):
                return [str(item) for item in parsed]
        return [text]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    if hasattr(value, "tolist"):
        listed = value.tolist()
        if isinstance(listed, list):
            return [str(item) for item in listed]
    return [str(value)]


def _aggregate_dimension_log_records(records: Any) -> pd.DataFrame | None:
    """按 ``(table_name, dimension_name)`` 归并展开后的维度证据。

    ``_transform_log_records_to_table_name_rows`` 会把 ``"a,b"`` 这类多表引用展开
    成多行，跨记录可能出现重复主键；而证据合并阶段是 ``merge(on="column_name")``，
    重复主键会让表的列行成倍膨胀，因此这里先聚合掉。
    """
    if not isinstance(records, pd.DataFrame):
        return None
    if records.empty or not {"table_name", "dimension_name"}.issubset(records.columns):
        return records

    frame = records.copy()
    frame["table_name"] = frame["table_name"].astype(str)
    frame["dimension_name"] = frame["dimension_name"].astype(str)
    if "count" in frame.columns:
        frame["count"] = (
            pd.to_numeric(frame["count"], errors="coerce").fillna(0).astype(int)
        )
    else:
        frame["count"] = 1
    if "sources" not in frame.columns:
        frame["sources"] = [[] for _ in range(len(frame))]

    def union_sources(values: Any) -> list[str]:
        merged: list[str] = []
        for value in values:
            for item in _as_source_list(value):
                if item not in merged:
                    merged.append(item)
        return merged

    return (
        frame.groupby(["table_name", "dimension_name"], as_index=False)
        .agg(count=("count", "sum"), sources=("sources", union_sources))
        .sort_values(["table_name", "dimension_name"])
        .reset_index(drop=True)
    )


def _as_int(value: Any, *, default: int = 0) -> int:
    """把计数字段转成 int；``None`` / ``NaN`` / 非数值一律退回默认值。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


#: 维度的 synonyms / enums 在最终产物里的分隔符。
MULTI_VALUE_SEPARATOR = "$$$"

_QUOTED_TOKEN_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _as_enum_list(value: Any) -> list[str]:
    """把 ``column_enums_1d`` 归一为去重后的字符串列表。

    枚举探查结果落盘后有三种形态，都要能解析::

        "['总体*客户' '模型类型*客户']"   numpy 数组 repr：空格分隔、可能换行
        "[201364]"                      数值数组 repr
        "电力,渔业,农业"                 逗号拼接

    输出的结果示例::

        ["总体*客户", "模型类型*客户"]

    Corner cases：
        - 绝不能对 numpy repr 用 ``ast.literal_eval``：相邻字符串字面量会被
          静默拼成一个（``['a' 'b']`` -> ``["ab"]``），因此这里按引号切分；
        - xlsx 单元格有长度上限，超长枚举会被截断，末尾残缺的 token 照常保留。
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    elif hasattr(value, "tolist") and not isinstance(value, str):
        listed = value.tolist()
        items = [str(item) for item in listed] if isinstance(listed, list) else []
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        quoted = _QUOTED_TOKEN_RE.findall(text)
        if quoted:
            items = [single or double for single, double in quoted]
        else:
            items = re.split(r"[,\s]+", text)

    seen: list[str] = []
    for item in items:
        cleaned = str(item).strip().strip("'\"")
        if cleaned and cleaned.lower() != "nan" and cleaned not in seen:
            seen.append(cleaned)
    return seen


def _join_unique(values: list[str]) -> str | None:
    """按 :data:`MULTI_VALUE_SEPARATOR` 拼接去重后的取值；空列表返回 ``None``。"""
    unique: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in unique:
            unique.append(text)
    return MULTI_VALUE_SEPARATOR.join(unique) or None


def _as_confidence(value: Any, *, default: float = 0.0) -> float:
    """把置信度归一到 ``[0, 1]``；解析不出数值时用默认值。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return min(1.0, max(0.0, number))


def _resolve_boolean_value(value: Any, *, default: bool) -> bool:
    """把 LLM 常见的布尔值表达转换为 bool；无法识别时使用 evidence 默认值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not pd.isna(value):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return default


def _iter_column_rows(
    evidence: SourceTableEvidence,
) -> list[dict[str, Any]]:
    """展开 ``table_name_to_df_map`` 为列行，保留各列上的全部标记。

    行内容就是列详情 DataFrame 的原始字段（NaN 归一为 ``None``），额外补上
    ``table_name``。dimension_caliber / metric_caliber 写入的
    ``is_dimension`` / ``is_metric`` / ``query_log_*`` 等富化结果因此对下游可见。
    """
    rows: list[dict[str, Any]] = []
    for table_name, frame in (evidence.table_name_to_df_map or {}).items():
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        name = str(table_name)
        for column_row in frame.to_dict(orient="records"):
            column_name = column_row.get("column_name")
            if column_name is None or (
                isinstance(column_name, float) and pd.isna(column_name)
            ):
                continue
            row = {
                key: (
                    None
                    if value is None or (isinstance(value, float) and pd.isna(value))
                    else value
                )
                for key, value in column_row.items()
                if key != "table_schema"
            }
            # 黑板以 table_name 为主键，列详情里的同名字段不可信。
            row["table_name"] = name
            row["column_name"] = str(column_name)
            rows.append(row)
    return rows


class _BaseTaskHandler(ProfileTaskHandler):
    """共享 JSON 解析、LLM 适配和空响应回退，但不包含任务语义。"""

    def __init__(
        self,
        llm: LlmClient,
        out_dir: str | Path | None = None,
    ) -> None:
        super().__init__(llm, out_dir)

    def _records(self, evidence: LogEvidence | None) -> Any:
        return None if evidence is None else evidence.records

    def _parse_json(self, response: str) -> Any:
        return extract_json_payload(response)

    def _run_model_call(
        self,
        user_prompt: str,
        system_prompt: str,
        temperature: float = 0.2,
    ) -> str:
        """兼容 profiler_* 里 ``(user, system, temperature)`` 签名。"""
        logger.info("LLM call: task=%s via _run_model_call", self.task.value)
        return self.llm.complete(
            system_prompt,
            user_prompt,
            temperature=temperature,
        )


class BizDomainTaskHandler(_BaseTaskHandler):
    """业务域探查：JOIN 聚类 + LLM 生成 domain_of。

    ``filter_unused_tables``（默认 True）：有日志频次时，从 Domain 输入中去掉
    从未出现在历史 query log 的源表；无日志时自动不筛。仅 handler 内配置，
    不向外暴露。
    """

    def __init__(
        self,
        llm: LlmClient,
        out_dir: str | Path | None = None,
        *,
        filter_unused_tables: bool = True,
    ) -> None:
        super().__init__(llm, out_dir)
        self._filter_unused_tables = filter_unused_tables

    @property
    def task(self) -> ProfileTaskName:
        return ProfileTaskName.BIZ_DOMAIN

    @staticmethod
    def _parse_or_empty(
        response: str,
        domain_input: DomainInput,
        *,
        attempt: str,
    ) -> DomainResolution:
        try:
            return parse_domain_resolution(response, domain_input)
        except Exception as exc:
            logger.warning(f"Invalid {attempt} domain response: {exc}")
            return DomainResolution(
                assignments=(),
                resolver_source="",
                coverage=(1.0 if not domain_input.expected_tables else 0.0),
                unmapped_tables=tuple(sorted(domain_input.expected_tables)),
            )

    @staticmethod
    def _as_artifacts(resolution: DomainResolution) -> dict[str, pd.DataFrame]:
        """domain 产物：结果表 + ``domain_of`` 源码表。

        输出的结果示例（``domain`` 表）::

            domain_name,display_name,description,aliases,member_tables
            revenue,收入域,收入与毛利相关事实表,收入、营收,"[{""source_table"": ""ads_example_revenue_index_1d"", ""dataset_name"": ""ads_example_revenue_index_1d""}]"

        ``domain_of`` 表见 :func:`~.base.resolver_source_frame`。
        """
        return {
            ProfileTaskName.BIZ_DOMAIN.value: rows_to_dataframe(
                build_domain_result_rows(resolution)
            ),
            DOMAIN_RESOLVER_ARTIFACT: resolver_source_frame(resolution.resolver_source),
        }

    def handle(
        self,
        table_evidence: SourceTableEvidence,
        log_evidence: LogEvidence | None = None,
    ) -> dict[str, pd.DataFrame]:
        """JOIN 证据 → LLM 生成 domain_of → 覆盖率不足时整体重试一次。"""
        records = self._records(log_evidence)
        join_evidence = (
            cast(JoinEvidence, records) if isinstance(records, Mapping) else None
        )
        domain_input = prepare_domain_input(
            table_evidence,
            join_evidence,
            filter_unused_tables=self._filter_unused_tables,
        )
        if not domain_input.expected_tables:
            return {ProfileTaskName.BIZ_DOMAIN.value: pd.DataFrame()}

        system_prompt = build_domain_system_prompt()
        logger.info(
            "LLM call: biz_domain initial tables=%s",
            len(domain_input.expected_tables),
        )
        initial_response = self.llm.complete(
            system_prompt,
            build_domain_user_prompt(domain_input.request),
            0.2,
        )
        previous_error: str | None = None
        try:
            initial = parse_domain_resolution(initial_response, domain_input)
        except Exception as exc:
            previous_error = str(exc)
            logger.warning(f"Invalid initial domain response: {exc}")
            initial = DomainResolution(
                assignments=(),
                resolver_source="",
                coverage=(1.0 if not domain_input.expected_tables else 0.0),
                unmapped_tables=tuple(sorted(domain_input.expected_tables)),
            )
        if initial.coverage >= MIN_DOMAIN_COVERAGE:
            logger.info(
                "biz_domain: initial coverage=%.2f%%，无需重试",
                initial.coverage * 100,
            )
            return self._as_artifacts(initial)

        logger.info(
            "LLM call: biz_domain retry coverage=%.2f%% error=%s",
            initial.coverage * 100,
            previous_error or "(coverage)",
        )
        retry_response = self.llm.complete(
            system_prompt,
            build_domain_retry_prompt(
                domain_input.request,
                initial.unmapped_tables,
                initial_response,
                previous_error=previous_error,
            ),
            0.2,
        )
        retried = self._parse_or_empty(
            retry_response,
            domain_input,
            attempt="retried",
        )
        chosen = retried if retried.coverage >= initial.coverage else initial
        if chosen.coverage < MIN_DOMAIN_COVERAGE:
            logger.warning(
                "Domain coverage remains below threshold after retry: "
                f"{chosen.coverage:.2%} < {MIN_DOMAIN_COVERAGE:.2%}"
            )
        return self._as_artifacts(chosen)


#: dataset 分批进度产物键；与结果表一并落在 ``dataset_{ts}/dataset_done.xlsx``。
_DATASET_DONE_ARTIFACT = "dataset_done"


class DatasetTaskHandler(_BaseTaskHandler):
    """把日志里的高频 SQL 模板提炼成语义视图（dataset）候选。

    产物 ``{"dataset": DataFrame}``，输出的结果示例::

        domain_name,dataset_name,dataset_comment,dataset_type,sql_content,parents
        revenue,revenue_daily_overview,按日汇总的收入总览,VIEW,SELECT date_id...,public.ads_..._1d

    日志模板量大时按 :attr:`_batch_size` 分批调 LLM，每批成功后立刻
    ``update`` 落盘；重启时从黑板读已有结果与 ``dataset_done``，只补未处理 SQL。
    """

    _batch_size: int = 20

    def __init__(
        self,
        llm: LlmClient,
        out_dir: str | Path | None = None,
        *,
        source_tables: TablesContext | None = None,
        query_logs: QueryLogsContext | None = None,
        batch_size: int = 20,
    ) -> None:
        super().__init__(llm, out_dir)
        self._source_tables = source_tables
        self._query_logs = query_logs
        self._batch_size = max(1, int(batch_size))
        self._records_frame = pd.DataFrame()
        self._table_to_domain: dict[str, str] = {}

    @property
    def task(self) -> ProfileTaskName:
        return ProfileTaskName.DATASET

    def build_system_prompt(self) -> str:
        return build_dataset_meta_system_prompt()

    def build_user_prompt(
        self,
        table_evidence: SourceTableEvidence,
        log_evidence: LogEvidence | None,
    ) -> str:
        records = self._records(log_evidence)
        self._records_frame = (
            records.copy()
            if isinstance(records, pd.DataFrame)
            else pd.DataFrame(columns=["parents", "repeated_sql", "count"])
        )
        self._table_to_domain = _domain_by_table(table_evidence)
        return build_dataset_meta_user_prompt(
            self._payload_from_frame(self._records_frame)
        )

    @staticmethod
    def _payload_from_frame(frame: pd.DataFrame) -> list[dict[str, Any]]:
        return [
            {
                "sql": str(row.repeated_sql),
                "parents": str(row.parents),
                "count": int(row.count),
            }
            for row in frame.itertuples(index=False)
        ]

    def _materialize_rows(self, enriched: pd.DataFrame) -> list[dict[str, Any]]:
        """把 LLM 命名结果转成黑板行；无 domain 的候选丢弃。"""
        rows: list[dict[str, Any]] = []
        for row in enriched.to_dict(orient="records"):
            parents = str(row.get("parents") or "")
            try:
                parent_names = json.loads(parents)
            except (TypeError, json.JSONDecodeError):
                parent_names = [
                    item.strip() for item in parents.split(",") if item.strip()
                ]
            first_parent = parent_names[0] if parent_names else None
            domain_name = _resolve_table_domain(self._table_to_domain, first_parent)
            if not domain_name:
                logger.info(
                    "dataset: 跳过无 domain 的候选（first_parent=%r）",
                    first_parent,
                )
                continue
            rows.append(
                {
                    "domain_name": domain_name,
                    "dataset_name": str(row.get("name") or "").strip()
                    or (
                        f"view_{str(first_parent).rsplit('.', 1)[-1]}"
                        if first_parent
                        else "unnamed_view"
                    ),
                    "dataset_comment": str(row.get("description") or "").strip()
                    or None,
                    "dataset_type": "VIEW",
                    "sql_content": str(row.get("repeated_sql") or "") or None,
                    "parents": ",".join(parent_names) or None,
                }
            )
        return rows

    def parse_response(self, response: str) -> list[dict[str, Any]]:
        """把 LLM 的命名与描述合并回 SQL 模板，产出 dataset 候选行。

        Corner cases：
            - 日志为空、响应非法或未覆盖全部 SQL 时返回 ``[]``（写入空结果表）；
            - 模型没给 ``name`` 时按 ``view_{第一个父表}`` 兜底。
        """
        if self._records_frame.empty:
            return []
        try:
            parsed = self._parse_json(response)
            views = validate_dataset_meta_items(
                parsed,
                set(self._records_frame["repeated_sql"].astype(str)),
            )
            enriched = merge_dataset_views(self._records_frame, views)
        except Exception as exc:
            logger.warning(f"Invalid dataset response: {exc}")
            return []
        return self._materialize_rows(enriched)

    def _load_existing_artifacts(
        self,
    ) -> tuple[pd.DataFrame, set[str]]:
        """从黑板/磁盘读已有 dataset 结果与已处理 SQL 集合。"""
        result = pd.DataFrame()
        done: set[str] = set()
        if self._source_tables is None:
            return result, done
        existing = self._source_tables.artifacts_for(self.task)
        frame = existing.get(self.task.value)
        if isinstance(frame, pd.DataFrame):
            result = frame.copy()
        done_frame = existing.get(_DATASET_DONE_ARTIFACT)
        if (
            isinstance(done_frame, pd.DataFrame)
            and not done_frame.empty
            and "repeated_sql" in done_frame.columns
        ):
            done = set(done_frame["repeated_sql"].astype(str))
        elif (
            isinstance(result, pd.DataFrame)
            and not result.empty
            and "sql_content" in result.columns
        ):
            done = {
                str(sql)
                for sql in result["sql_content"].dropna().astype(str)
                if str(sql).strip()
            }
        if existing:
            logger.info(
                "dataset: 从磁盘/黑板加载已有结果 rows=%s done_sqls=%s keys=%s",
                len(result),
                len(done),
                sorted(str(k) for k in existing),
            )
        return result, done

    def _persist(
        self,
        result_frame: pd.DataFrame,
        done_sqls: set[str],
    ) -> dict[str, pd.DataFrame]:
        artifacts = {
            self.task.value: result_frame,
            _DATASET_DONE_ARTIFACT: pd.DataFrame({"repeated_sql": sorted(done_sqls)}),
        }
        if self._source_tables is not None:
            self._source_tables.update(artifacts, self.task)
            logger.info(
                "dataset: 落盘 rows=%s done_sqls=%s",
                len(result_frame),
                len(done_sqls),
            )
        return artifacts

    def is_complete(self, artifacts: Mapping[str, Any]) -> bool:
        """全部输入 SQL 模板都已处理完才算完成（含无 domain 被跳过的）。"""
        if not artifacts:
            return False
        if self._query_logs is None:
            return self.task.value in artifacts

        records = self._query_logs.evidence_for(self.task).records
        if not isinstance(records, pd.DataFrame) or records.empty:
            return self.task.value in artifacts

        expected = set(records["repeated_sql"].astype(str))
        done_frame = artifacts.get(_DATASET_DONE_ARTIFACT)
        if (
            isinstance(done_frame, pd.DataFrame)
            and not done_frame.empty
            and "repeated_sql" in done_frame.columns
        ):
            done = set(done_frame["repeated_sql"].astype(str))
        else:
            frame = artifacts.get(self.task.value)
            if (
                isinstance(frame, pd.DataFrame)
                and not frame.empty
                and "sql_content" in frame.columns
            ):
                done = set(frame["sql_content"].dropna().astype(str))
            else:
                done = set()
        return expected <= done

    def handle(
        self,
        table_evidence: SourceTableEvidence,
        log_evidence: LogEvidence | None = None,
    ) -> dict[str, pd.DataFrame]:
        """分批调用 LLM 生成 dataset；每批成功后落盘，重启只补未处理 SQL。"""
        records = self._records(log_evidence)
        self._table_to_domain = _domain_by_table(table_evidence)
        if not isinstance(records, pd.DataFrame) or records.empty:
            empty = pd.DataFrame()
            return self._persist(empty, set())

        result_frame, done_sqls = self._load_existing_artifacts()
        pending = records[
            ~records["repeated_sql"].astype(str).isin(done_sqls)
        ].reset_index(drop=True)
        if pending.empty:
            logger.info(
                "dataset: 磁盘已有全部 %s 条 SQL 结果，跳过 LLM",
                len(done_sqls),
            )
            return {
                self.task.value: result_frame,
                _DATASET_DONE_ARTIFACT: pd.DataFrame(
                    {"repeated_sql": sorted(done_sqls)}
                ),
            }

        accumulated = (
            result_frame.to_dict(orient="records")
            if isinstance(result_frame, pd.DataFrame) and not result_frame.empty
            else []
        )
        system_prompt = self.build_system_prompt()
        total = (len(pending) + self._batch_size - 1) // self._batch_size
        logger.info(
            "dataset: 待处理 SQL=%s（已完成 %s），分 %s 批 batch_size=%s",
            len(pending),
            len(done_sqls),
            total,
            self._batch_size,
        )
        artifacts: dict[str, pd.DataFrame] = {
            self.task.value: result_frame,
            _DATASET_DONE_ARTIFACT: pd.DataFrame({"repeated_sql": sorted(done_sqls)}),
        }

        for start in range(0, len(pending), self._batch_size):
            batch = pending.iloc[start : start + self._batch_size].copy()
            index = start // self._batch_size + 1
            self._records_frame = batch
            try:
                logger.info(
                    "LLM call: dataset batch %s/%s size=%s",
                    index,
                    total,
                    len(batch),
                )
                response = self.llm.complete(
                    system_prompt,
                    build_dataset_meta_user_prompt(self._payload_from_frame(batch)),
                    0.2,
                )
                parsed = self._parse_json(response)
                views = validate_dataset_meta_items(
                    parsed,
                    set(batch["repeated_sql"].astype(str)),
                )
                enriched = merge_dataset_views(batch, views)
            except Exception as exc:
                logger.warning(
                    "dataset batch %s/%s failed, will retry on resume: %s",
                    index,
                    total,
                    exc,
                )
                break

            rows = self._materialize_rows(enriched)
            accumulated.extend(rows)
            done_sqls |= set(batch["repeated_sql"].astype(str))
            result_frame = pd.DataFrame(accumulated) if accumulated else pd.DataFrame()
            artifacts = self._persist(result_frame, done_sqls)
            logger.info(
                "dataset batch %s/%s: +%s rows, done=%s/%s",
                index,
                total,
                len(rows),
                len(done_sqls),
                len(records),
            )

        return artifacts


class DimensionCaliberTaskHandler(_BaseTaskHandler):
    """维度证据：把日志中的 GROUP BY / WHERE 使用情况合并进各表列详情。

    不调用 LLM——一列是否被当作维度使用完全由日志证据决定。
    """

    @property
    def task(self) -> ProfileTaskName:
        return ProfileTaskName.DIMENSION_CALIBER

    def handle(
        self,
        table_evidence: SourceTableEvidence,
        log_evidence: LogEvidence | None = None,
    ) -> dict[str, pd.DataFrame]:
        """合并 ``dimension_name/table_name/count/sources`` 证据。

        产物是 ``{table_name: 列详情 DataFrame}``，在原列详情上追加三列，示例
        （表 ``ads_example_revenue_index_1d``）::

            column_name,data_type,...,query_log_clause_count,query_log_clause_type,is_dimension_in_query_logs
            date_id,text,...,21.0,"['group_by', 'where']",yes
            gmv,numeric,...,,,no

        Corner cases：
            - 日志为空 / 无 query logs：所有列 ``is_dimension_in_query_logs=no``；
            - 日志里出现源表清单中不存在的表：整行丢弃，绝不新增表或列；
            - 无法映射到非空 domain 的表不参与合并、不写入口径产物。
        """
        source_table_map = _tables_with_domain(table_evidence)
        if not source_table_map:
            logger.info("dimension_caliber: 无带 domain 的表，跳过")
            return {}
        records = _aggregate_dimension_log_records(
            _transform_log_records_to_table_name_rows(
                self._records(log_evidence),
                set(source_table_map),
            )
        )
        merged = merge_dimension_evidence_into_tables(records, source_table_map)
        logger.info(f"Merged dimension evidence into {len(merged)} tables")
        return merged


class MetricCaliberTaskHandler(_BaseTaskHandler):
    """指标证据：合并日志聚合证据，再逐表用 LLM 判定各列角色。

    分两个阶段，中间必定落盘一次：
        1. 合并日志聚合证据 → 立刻 ``update()`` 出一个版本，保证纯确定性的成果
           不会因为后面的 LLM 阶段中断而丢失；
        2. 逐表调用 LLM 判定列角色，**每完成一张表就 checkpoint 一次**，中断后
           重启只补没判定过的表（见 :meth:`is_complete`）。

    列角色是四选一的 ``column_role``（dimension / metric / date / other），
    同时生成规范业务名称；metric 还会生成 ``date_range``。
    """

    #: 枚举值只是角色判定的参考，过长会挤占 prompt，按此长度截断。
    _MAX_ENUM_CHARS = 200
    #: 列角色判定阶段单独成一个版本，与证据合并阶段区分开。
    _llm_stage_task = ProfileTaskName.LLM_DIMENSION_METRIC
    #: LLM 判定结果写入的列；一张表齐了这些列即视为已判定。
    _ROLE_COLUMNS = (
        "column_role",
        "dimension_name",
        "metric_name",
        "date_range",
        "role_reason",
        "role_confidence",
        "is_dimension",
        "is_metric",
        "is_date",
    )

    def __init__(
        self,
        llm: LlmClient,
        out_dir: str | Path | None = None,
        *,
        source_tables: TablesContext | None = None,
    ) -> None:
        super().__init__(llm, out_dir)
        # 逐表增量落盘需要黑板句柄；为 None 时退化为全内存执行（便于测试）。
        self._source_tables = source_tables

    @property
    def task(self) -> ProfileTaskName:
        return ProfileTaskName.METRIC_CALIBER

    def is_complete(self, artifacts: Mapping[str, Any]) -> bool:
        """所有表都判定过列角色才算跑完，否则从没判定的表继续。

        证据合并（``metric_caliber``）与列角色判定（``llm_dimension_metric``）是两
        个版本，完成与否只取决于后者，因此这里优先看 LLM 阶段的产物。
        """
        checked = artifacts
        expected: set[str] | None = None
        if self._source_tables is not None:
            checked = self._source_tables.artifacts_for(self._llm_stage_task)
            # 与 handle / pending 对齐：只期望「有 domain」的表；有逻辑表时再
            # 进一步收窄（TEMP：仅 view_*）。用 BIZ_DOMAIN 证据取 domain_of。
            domains = _domain_by_table(
                self._source_tables.evidence_for(ProfileTaskName.BIZ_DOMAIN)
            )
            all_names = {
                name
                for name in self._source_tables.table_name_to_df_map
                if name != OVERVIEW_ARTIFACT and _resolve_table_domain(domains, name)
            }
            logical = {name for name in all_names if is_dim_metric_table_name(name)}
            expected = (
                logical if DIM_METRIC_REQUIRE_VIEW_PREFIX else (logical or all_names)
            )

        classified = {
            name
            for name, frame in checked.items()
            if name != OVERVIEW_ARTIFACT
            and isinstance(frame, pd.DataFrame)
            and self._is_classified(frame)
        }
        if expected is not None:
            # 没有任何带 domain 的表时视为本阶段完成（空产物合法）。
            if not expected:
                return True
            return expected <= classified
        return bool(classified) and len(classified) == len(
            [n for n in checked if n != OVERVIEW_ARTIFACT]
        )

    @classmethod
    def _is_classified(cls, frame: pd.DataFrame) -> bool:
        """该表是否已经完成列角色判定。"""
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return True
        return set(cls._ROLE_COLUMNS) <= set(frame.columns)

    @classmethod
    def _build_column_role_payload(
        cls,
        table_frame: pd.DataFrame,
    ) -> list[dict[str, Any]]:
        """把表列详情转换为适合 JSON Prompt 的记录。

        输出的结果示例（``list[dict]``，键就是列详情的字段）::

            [{"column_name": "gmv", "data_type": "numeric", "column_comment": "成交额",
              "column_distinct_num_1d": "100+", "column_enums_1d": "[...]",
              "is_dimension_in_query_logs": "no", "is_metric_in_query_logs": "yes",
              "is_derived_metric": False}]

        Corner cases：
            - ``column_enums_1d`` 超过 :attr:`_MAX_ENUM_CHARS` 时截断，避免宽表
              把 prompt 撑爆；
            - 上一轮写入的角色列会被剔除，重跑时不给模型看自己的旧答案。
        """
        records: list[dict[str, Any]] = []
        for row in table_frame.to_dict(orient="records"):
            record: dict[str, Any] = {}
            for key, value in row.items():
                if key in cls._ROLE_COLUMNS:
                    # metric evidence 本身也有 metric_name。为了不把旧的角色输出
                    # 再喂给模型，统一改名为 query_log_metric_name。
                    if (
                        key == "metric_name"
                        and not row.get("query_log_metric_name")
                        and value is not None
                        and not (isinstance(value, float) and pd.isna(value))
                    ):
                        record["query_log_metric_name"] = value
                    continue
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    record[key] = None
                elif key == "column_enums_1d":
                    text = str(value)
                    record[key] = (
                        text
                        if len(text) <= cls._MAX_ENUM_CHARS
                        else f"{text[: cls._MAX_ENUM_CHARS]}..."
                    )
                else:
                    record[key] = value
            records.append(record)
        return records

    @staticmethod
    def _fallback_role(row: Mapping[str, Any]) -> str:
        """LLM 没给出可用答案时，按日志证据与数据类型推一个角色。"""
        data_type = str(row.get("data_type") or "").lower()
        if any(token in data_type for token in ("date", "time", "timestamp")):
            return "date"
        if str(row.get("is_metric_in_query_logs") or "").lower() == "yes":
            return "metric"
        if str(row.get("is_dimension_in_query_logs") or "").lower() == "yes":
            return "dimension"
        return "other"

    @staticmethod
    def _fallback_date_range(
        row: Mapping[str, Any],
        table_name: str,
    ) -> str:
        """模型未给有效时间范围时，从表名、指标名和公式做保守推断。"""
        metric_evidence = " ".join(
            str(value or "").lower()
            for value in (
                row.get("column_name"),
                row.get("query_log_metric_name"),
                row.get("metric_name"),
                row.get("metric_formula"),
                row.get("column_comment"),
            )
        )

        def infer(evidence: str) -> str | None:
            if "30d" in evidence or "30_day" in evidence:
                return "30d"
            if any(token in evidence for token in ("1m", "monthly", "month", "月")):
                return "1m"
            if any(token in evidence for token in ("1d", "daily", "当日", "单日")):
                return "1d"
            return None

        # 指标自身名称/公式比表的公共时间粒度更精确，优先使用。
        inferred = infer(metric_evidence) or infer(str(table_name).lower())
        if inferred is not None:
            return inferred
        return "nd"

    @staticmethod
    def _semantic_name(value: Any) -> str | None:
        """把模型返回的名称清洗为非空字符串。"""
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        return str(value).strip() or None

    @classmethod
    def _apply_column_roles(
        cls,
        table_frame: pd.DataFrame,
        response: str,
        *,
        table_name: str = "",
    ) -> pd.DataFrame:
        """校验列名并把 LLM 的角色判定合并回当前表。

        输出在入参列详情右侧追加角色、语义名称和时间范围，示例::

            column_name,...,column_role,dimension_name,metric_name,date_range,
            role_reason,role_confidence
            date_id,...,date,日期,,,分区日期字段,0.95
            monthly_DAU,...,metric,,日活跃度,1m,月度日活指标,0.92
            country,...,dimension,国家,,,国家维度字段,0.90
            row_md5,...,other,,,,技术校验字段,0.80

        ``is_dimension`` / ``is_metric`` / ``is_date`` 由 ``column_role`` 派生，
        供下游 dimension_manage / metric_manage 任务直接筛选；``date`` 单独标记为
        日期列，**不**计入维度（维度筛选需排除日期列）。

        Corner cases：
            - 响应非法、缺列或角色取值不在 :data:`COLUMN_ROLES` 内时，该列退回
              :meth:`_fallback_role`，``role_confidence=0.0`` 且理由写明兜底原因；
            - 置信度解析不出数值时记 0.0，超出 [0, 1] 时截断；
            - 必填业务名称缺失时退回 query-log 名称或物理列名，并把置信度
              上限降为 0.5；
            - metric 缺少有效 ``date_range`` 时，从表名 / 指标名 / 公式推断，
              无法确定固定周期时使用 ``nd``。
        """
        try:
            parsed = extract_json_payload(response)
            items = parsed if isinstance(parsed, list) else []
        except Exception as exc:
            logger.warning(f"Invalid column role response: {exc}")
            items = []

        roles_by_column = {
            str(item.get("column_name") or ""): item
            for item in items
            if isinstance(item, Mapping) and str(item.get("column_name") or "")
        }
        resolved = table_frame.copy()
        if "query_log_metric_name" not in resolved.columns:
            resolved["query_log_metric_name"] = (
                resolved["metric_name"] if "metric_name" in resolved.columns else None
            )
        roles: list[str] = []
        dimension_names: list[str | None] = []
        metric_names: list[str | None] = []
        date_ranges: list[str | None] = []
        reasons: list[str] = []
        confidences: list[float] = []

        for row in resolved.to_dict(orient="records"):
            column_name = str(row.get("column_name") or "")
            item = roles_by_column.get(column_name, {})
            role = str(item.get("column_role") or "").strip().lower()
            if role in COLUMN_ROLES:
                reason = str(item.get("reason") or "").strip()
                confidence = _as_confidence(item.get("confidence"))
            else:
                role = cls._fallback_role(row)
                reason = "LLM 未给出可用判定，按日志证据与数据类型兜底"
                confidence = 0.0

            dimension_name = None
            metric_name = None
            date_range = None
            if role in {"dimension", "date"}:
                dimension_name = cls._semantic_name(item.get("dimension_name"))
                if dimension_name is None:
                    dimension_name = column_name
                    confidence = min(confidence, 0.5)
            elif role == "metric":
                metric_name = cls._semantic_name(item.get("metric_name"))
                if metric_name is None:
                    metric_name = (
                        cls._semantic_name(row.get("query_log_metric_name"))
                        or column_name
                    )
                    confidence = min(confidence, 0.5)
                proposed_range = str(item.get("date_range") or "").strip().lower()
                date_range = (
                    proposed_range
                    if proposed_range in METRIC_DATE_RANGES
                    else cls._fallback_date_range(row, table_name)
                )
                if proposed_range not in METRIC_DATE_RANGES:
                    # date_range 是 metric 的必填输出；模型漏答或越界时即使能
                    # 规则兜底，也不能保留过高置信度。
                    confidence = min(confidence, 0.5)

            roles.append(role)
            dimension_names.append(dimension_name)
            metric_names.append(metric_name)
            date_ranges.append(date_range)
            reasons.append(reason)
            confidences.append(confidence)

        resolved["column_role"] = roles
        resolved["dimension_name"] = dimension_names
        resolved["metric_name"] = metric_names
        resolved["date_range"] = date_ranges
        resolved["role_reason"] = reasons
        resolved["role_confidence"] = confidences
        # 派生布尔列：日期列单独用 is_date，不计入 is_dimension。
        resolved["is_dimension"] = [role == "dimension" for role in roles]
        resolved["is_metric"] = [role == "metric" for role in roles]
        resolved["is_date"] = [role == "date" for role in roles]
        return resolved

    def _classify_one_table(
        self,
        table_name: str,
        table_frame: pd.DataFrame,
    ) -> pd.DataFrame:
        """判定一张表的列角色；调用失败时整表走兜底，不中断整体。"""
        payload = self._build_column_role_payload(table_frame)
        try:
            logger.info(
                "LLM call: metric_caliber column_roles table=%s columns=%s",
                table_name,
                len(payload),
            )
            response = self.llm.complete(
                build_column_role_system_prompt(),
                build_column_role_user_prompt(table_name, payload),
                0.2,
            )
        except Exception as exc:
            logger.warning(f"Column role classification failed for {table_name}: {exc}")
            response = "[]"
        return self._apply_column_roles(
            table_frame,
            response,
            table_name=table_name,
        )

    def _classify_column_roles(
        self,
        table_map: dict[str, pd.DataFrame],
    ) -> dict[str, pd.DataFrame]:
        """逐表判定列角色，每完成一张就落盘一次。

        已经带齐角色列的表直接跳过，因此中断后重启只会补没做完的表。
        """
        # 证据合并会把角色列一并 drop 掉（那一步要保持幂等），所以已经判定过的表
        # 要从 LLM 阶段的版本里取回来，否则重启会全量重跑。
        if self._source_tables is not None:
            loaded = 0
            for name, frame in self._source_tables.artifacts_for(
                self._llm_stage_task
            ).items():
                if (
                    name != OVERVIEW_ARTIFACT
                    and name in table_map
                    and self._is_classified(frame)
                ):
                    table_map[name] = frame
                    loaded += 1
            if loaded:
                logger.info(
                    "metric_caliber: 从磁盘/黑板加载已判定列角色的表 %s 张",
                    loaded,
                )

        logical_pending = [
            name
            for name, frame in table_map.items()
            if is_dim_metric_table_name(name) and not self._is_classified(frame)
        ]
        # 与 evidence_for 一致：TEMP 关闭且没有逻辑表时，对全量物理表做角色判定。
        if DIM_METRIC_REQUIRE_VIEW_PREFIX:
            pending = logical_pending
        else:
            pending = logical_pending or [
                name
                for name, frame in table_map.items()
                if not self._is_classified(frame)
            ]

        done = len(table_map) - len(pending)
        logger.info(f"Column roles: {done} already classified, {len(pending)} pending")

        # 逐表 checkpoint 需要一个已存在的版本目录。这里刻意开「空版本」而不是
        # update()：该目录只应留下 LLM 判定完成的表，不掺证据态的输入。
        if (
            pending
            and self._source_tables is not None
            and not self._source_tables.has_version(self._llm_stage_task)
        ):
            self._source_tables.open_version(self._llm_stage_task)

        for index, table_name in enumerate(pending, start=1):
            classified = self._classify_one_table(
                table_name,
                table_map[table_name],
            )
            table_map[table_name] = classified
            if self._source_tables is not None:
                self._source_tables.checkpoint_table(
                    ProfileTaskName.LLM_DIMENSION_METRIC,
                    table_name,
                    classified,
                )
            roles = classified["column_role"].value_counts().to_dict()
            logger.info(f"Column roles {index}/{len(pending)} {table_name}: {roles}")
        return table_map

    def handle(
        self,
        table_evidence: SourceTableEvidence,
        log_evidence: LogEvidence | None = None,
    ) -> dict[str, pd.DataFrame]:
        """合并日志聚合证据（先落盘）→ 逐表判定列角色（每表落盘）。

        产物是 ``{table_name: 列详情 DataFrame}``，两个阶段叠加在同一份表上，示例
        （表 ``ads_example_revenue_index_1d``）::

            column_name,...,query_log_metric_name,metric_formula,column_role,
            dimension_name,metric_name,date_range,role_reason,role_confidence
            gmv,...,total_gmv,SUM(gmv),metric,,成交额,1d,日志中被 SUM 聚合,0.92
            total_gmv,...,total_gmv,SUM(gmv),metric,,成交额,1d,日志派生指标,0.90
            date_id,...,,,date,日期,,,分区日期字段,0.95

        Corner cases：
            - 日志里的派生指标名在源表不存在时会**新增一行**，``data_type=derived``，
              这是与 dimension_caliber 的关键差异；
            - 阶段 1 结束立刻落盘，LLM 阶段中断不会丢掉证据合并的成果；
            - 重启后 :meth:`is_complete` 判定未完成，只补没判定过的表；
            - 无法映射到非空 domain 的表不参与合并与列角色判定；
            - 列角色 LLM 依赖 ``column_enums_1d``，入口会硬等异步 ``add_enums``。
        """
        # 列角色 LLM 需要枚举；create 阶段已异步探查，这里硬等并 refresh 证据。
        if isinstance(self._source_tables, SourceTablesContext):
            self._source_tables.wait_add_enums()
            table_evidence = self._source_tables.evidence_for(self.task)

        source_table_map = _tables_with_domain(table_evidence)
        if not source_table_map:
            logger.info("metric_caliber: 无带 domain 的表，跳过")
            return {}
        records = _transform_log_records_to_table_name_rows(
            self._records(log_evidence),
            set(source_table_map),
        )
        merged = merge_metric_evidence_into_tables(
            records if isinstance(records, pd.DataFrame) else None,
            source_table_map,
            allow_new_columns=True,
        )
        logger.info(f"Merged metric evidence into {len(merged)} tables")

        # 先把确定性的证据落盘，再进入耗时且可能中断的 LLM 阶段。
        if self._source_tables is not None and not self._source_tables.artifacts_for(
            self.task
        ):
            self._source_tables.update(merged, self.task)
            logger.info("Persisted metric evidence before column-role stage")

        return self._classify_column_roles(merged)


class _SemanticAggregationTaskHandler(_BaseTaskHandler):
    """dimension_manage / metric_manage 共用的两阶段模板：先聚合落盘，再分批补描述。

    两个任务的逻辑完全一致，只有「收哪些列、叫什么名字、额外带哪些字段」不同：

        1. 从 metric_caliber 富化后的列详情里筛出目标列
           （``column_role == "dimension"`` 或 ``"metric"``；日期列不进维度）；
        2. 按 ``(domain_name, 规范业务名)`` 跨表聚合——业务名是列角色阶段归一好的
           中文名，因此 ``industry`` 与 ``industry_name`` 会落到同一行；
        3. **聚合结果立刻落盘**成 ``agg_*`` 版本，LLM 阶段中断也不用重算；
        4. 分批把聚合结果交给 LLM 补描述，最后写最终结果表。
    """

    #: 单个条目进入**产物**的枚举/公式上限。
    _MAX_ENUMS = 50
    #: 单个条目进入 **Prompt** 的枚举样例上限；描述用不着更多，多了只会撑大 prompt。
    _PROMPT_ENUMS = 8
    #: 单个枚举样例在 Prompt 里的最大长度，超长的公司名/URL 会被截断。
    _PROMPT_ENUM_CHARS = 24
    #: 每次 LLM 调用处理的条目数量。
    _BATCH_SIZE = 40

    #: 子类指定：聚合态落盘用的任务名。
    _agg_task: ProfileTaskName
    #: 子类指定：业务名与描述在产物里的列名。
    _name_field: str
    _description_field: str
    #: 子类指定：归一 prompt 里对这类对象的称呼（「维度」/「指标」）。
    _name_kind: str

    def __init__(
        self,
        llm: LlmClient,
        out_dir: str | Path | None = None,
        *,
        source_tables: TablesContext | None = None,
    ) -> None:
        super().__init__(llm, out_dir)
        # 聚合态中间落盘需要黑板句柄；为 None 时退化为只产出最终结果。
        self._source_tables = source_tables
        # {(domain_name, 业务名): [列详情行, ...]}
        self._members: dict[tuple[str, str], list[dict[str, Any]]] = {}

    # -- 子类实现 --------------------------------------------------------

    def _accepts(self, column: Mapping[str, Any]) -> bool:
        """该列是否属于本任务要收集的角色。"""
        raise NotImplementedError

    def _semantic_name(self, column: Mapping[str, Any]) -> str:
        """该列归一后的业务名；取不到时退回物理列名。"""
        raise NotImplementedError

    def _extra_payload(self, members: list[dict[str, Any]]) -> dict[str, Any]:
        """进 Prompt 的任务特有字段。"""
        return {}

    def _extra_row(self, members: list[dict[str, Any]]) -> dict[str, Any]:
        """进产物的任务特有字段。"""
        return {}

    def _validate(
        self,
        parsed: Any,
        keys: set[tuple[str, str]],
    ) -> dict[tuple[str, str], str]:
        """校验 LLM 响应并返回 ``{(domain, 业务名): 描述}``。"""
        raise NotImplementedError

    def _build_user_prompt(self, payload: list[dict[str, Any]]) -> str:
        raise NotImplementedError

    # -- 聚合 ------------------------------------------------------------

    def _group_members(
        self,
        table_evidence: SourceTableEvidence,
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """筛出目标列并按 ``(domain_name, 业务名)`` 聚合。

        输出的结果示例::

            {("sales", "行业"): [<industry 列行>, <industry_name 列行>],
             ("sales", "地区"): [<province 列行>]}

        Corner cases：
            - ``domain_of`` 没命中该表时 domain 为空串，这些条目会聚在一起；
            - 业务名和物理列名都为空的行直接丢弃。
        """
        domains = _domain_by_table(table_evidence)
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for column in _iter_column_rows(table_evidence):
            if not self._accepts(column):
                continue
            name = self._semantic_name(column)
            if not name:
                continue
            table_name = str(column.get("table_name") or "")
            domain_name = (
                domains.get(
                    table_name,
                    domains.get(table_name.rsplit(".", 1)[-1]),
                )
                or ""
            )
            grouped.setdefault((domain_name, name), []).append(column)
        return grouped

    @staticmethod
    def _dedupe_members(
        members: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按 ``(table_name, column_name)`` 去重，合并分组时避免重复计数。"""
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, Any]] = []
        for member in members:
            key = (
                str(member.get("table_name") or ""),
                str(member.get("column_name") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(member)
        return unique

    def _merge_identical_columns(
        self,
        grouped: dict[tuple[str, str], list[dict[str, Any]]],
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """同一 domain 下**物理列集合完全相同**的条目合并成一条。

        逐表命名必然产生同义异名（``ds`` 被叫成「分区日期」「日期分区」「统计日期」）。
        物理列集合一模一样时可以确定它们就是同一个概念，无需模型介入。
        名称取成员列最多的那个，并列时取字典序最小，保证结果可复现。

        输出的结果示例（合并前 3 条 → 合并后 1 条）::

            ("", "分区日期") ("", "日期分区") ("", "统计日期")   都只含物理列 ds
            -> ("", "分区日期")   # 成员数相同，取字典序最小
        """
        by_columns: dict[
            tuple[str, tuple[str, ...]], list[tuple[str, list[dict[str, Any]]]]
        ] = {}
        for (domain_name, name), members in grouped.items():
            key = (domain_name, tuple(sorted(self._columns_of(members))))
            by_columns.setdefault(key, []).append((name, members))

        merged: dict[tuple[str, str], list[dict[str, Any]]] = {}
        collapsed = 0
        for (domain_name, _columns), items in by_columns.items():
            if len(items) == 1:
                name, members = items[0]
                merged[(domain_name, name)] = members
                continue
            items.sort(key=lambda item: (-len(item[1]), item[0]))
            name = items[0][0]
            merged[(domain_name, name)] = self._dedupe_members(
                [member for _, members in items for member in members]
            )
            collapsed += len(items) - 1
        if collapsed:
            logger.info(
                f"{self.task.value}: rule-merged {collapsed} duplicate names "
                f"sharing identical columns"
            )
        return merged

    def _normalize_names(
        self,
        grouped: dict[tuple[str, str], list[dict[str, Any]]],
    ) -> dict[tuple[str, str], str]:
        """按业务域整体交给 LLM 做跨表名称归一，返回 ``{(域, 原名): 规范名}``。

        归一只在同一个 domain 内进行，因此天然按域切批；域内条目再多也是一次
        调用看全量，模型才可能发现「统计日期」和「日期」是一回事。

        Corner cases：
            - 单个域调用失败或校验不过时，该域保持原名（记 warning），不影响其他域。
        """
        by_domain: dict[str, list[str]] = {}
        for domain_name, name in grouped:
            by_domain.setdefault(domain_name, []).append(name)

        system_prompt = build_name_normalization_system_prompt(self._name_kind)
        mapping: dict[tuple[str, str], str] = {}
        for domain_name, names in sorted(by_domain.items()):
            payload = [
                {
                    "name": name,
                    "columns": self._columns_of(grouped[(domain_name, name)]),
                    "column_comments": self._comments_of(grouped[(domain_name, name)])[
                        :3
                    ],
                }
                for name in sorted(names)
            ]
            try:
                logger.info(
                    "LLM call: %s name_normalization domain=%s names=%s",
                    self.task.value,
                    domain_name or "(none)",
                    len(names),
                )
                response = self.llm.complete(
                    system_prompt,
                    build_name_normalization_user_prompt(
                        self._name_kind,
                        domain_name,
                        payload,
                    ),
                    0.2,
                )
                resolved = validate_name_normalization_items(
                    self._parse_json(response),
                    set(names),
                )
            except Exception as exc:
                logger.warning(
                    f"{self.task.value} normalization failed for domain "
                    f"{domain_name or '(none)'}, keep original names: {exc}"
                )
                resolved = {name: name for name in names}
            mapping.update(
                {(domain_name, name): canonical for name, canonical in resolved.items()}
            )
        return mapping

    def _regroup(
        self,
        grouped: dict[tuple[str, str], list[dict[str, Any]]],
        mapping: Mapping[tuple[str, str], str],
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """按归一后的名称重新聚合。"""
        regrouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for key, members in grouped.items():
            domain_name, name = key
            canonical = str(mapping.get(key) or name).strip() or name
            regrouped.setdefault((domain_name, canonical), []).extend(members)
        return {
            key: self._dedupe_members(members) for key, members in regrouped.items()
        }

    def _enums_of(self, members: list[dict[str, Any]]) -> list[str]:
        """合并全部成员列的枚举值，去重后截断到 :attr:`_MAX_ENUMS`。"""
        merged: list[str] = []
        for member in members:
            for enum in _as_enum_list(member.get("column_enums_1d")):
                if enum not in merged:
                    merged.append(enum)
                if len(merged) >= self._MAX_ENUMS:
                    return merged
        return merged

    def _prompt_enums(self, members: list[dict[str, Any]]) -> list[str]:
        """进 Prompt 的枚举样例：条数和单条长度都收紧。"""
        return [
            enum[: self._PROMPT_ENUM_CHARS]
            for enum in self._enums_of(members)[: self._PROMPT_ENUMS]
        ]

    @staticmethod
    def _columns_of(members: list[dict[str, Any]]) -> list[str]:
        """聚合到的物理列名（去重，保持首次出现顺序）。"""
        columns: list[str] = []
        for member in members:
            name = str(member.get("column_name") or "").strip()
            if name and name not in columns:
                columns.append(name)
        return columns

    @staticmethod
    def _comments_of(members: list[dict[str, Any]]) -> list[str]:
        comments: list[str] = []
        for member in members:
            for key in ("column_comment", "column_name_cn"):
                text = str(member.get(key) or "").strip()
                if text and text not in comments:
                    comments.append(text)
        return comments

    @staticmethod
    def _tables_of(members: list[dict[str, Any]]) -> list[str]:
        tables: list[str] = []
        for member in members:
            name = str(member.get("table_name") or "").strip()
            if name and name not in tables:
                tables.append(name)
        return tables

    def _payload_for(self, keys: list[tuple[str, str]]) -> list[dict[str, Any]]:
        """为一批条目构造 Prompt payload。"""
        payload: list[dict[str, Any]] = []
        for domain_name, name in keys:
            members = self._members[(domain_name, name)]
            item = {
                "domain_name": domain_name,
                self._name_field: name,
                "table_count": len(self._tables_of(members)),
                "total_query_count": sum(
                    _as_int(member.get("query_log_clause_count"))
                    + _as_int(member.get("query_log_aggregation_count"))
                    for member in members
                ),
                "columns": self._columns_of(members),
                "column_comments": self._comments_of(members),
            }
            item.update(self._extra_payload(members))
            payload.append(item)
        return payload

    def _detail_columns(
        self,
        members: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """聚合态与最终产物共用的明细列（描述之后的部分）。"""
        detail = {"synonyms": _join_unique(self._columns_of(members))}
        detail.update(self._extra_row(members))
        return detail

    def _build_agg_rows(self) -> list[dict[str, Any]]:
        """聚合态结果：最终列去掉描述，另带便于人工复核的统计字段。

        输出的结果示例（dimension）::

            domain_name,dimension_name,synonyms,enums,table_count,total_query_count,tables
            sales,行业,industry$$$industry_name,电力$$$渔业,2,41,sales_cust$$$sales_order
        """
        rows: list[dict[str, Any]] = []
        for (domain_name, name), members in sorted(self._members.items()):
            tables = self._tables_of(members)
            row = {
                # 产物列名与 ground truth 对齐：domain，而不是 domain_name。
                "domain": domain_name or None,
                self._name_field: name,
                **self._detail_columns(members),
            }
            row.update(
                {
                    "table_count": len(tables),
                    "total_query_count": sum(
                        _as_int(member.get("query_log_clause_count"))
                        + _as_int(member.get("query_log_aggregation_count"))
                        for member in members
                    ),
                    "tables": _join_unique(tables),
                }
            )
            rows.append(row)
        return rows

    def _build_final_rows(
        self,
        described: Mapping[tuple[str, str], str],
    ) -> list[dict[str, Any]]:
        """最终产物：聚合列 + LLM 描述；模型漏答的退回列注释。"""
        rows: list[dict[str, Any]] = []
        for (domain_name, name), members in sorted(self._members.items()):
            comments = self._comments_of(members)
            description = described.get((domain_name, name)) or (
                comments[0] if comments else None
            )
            # 列序固定为：域、业务名、描述、其余明细。
            rows.append(
                {
                    "domain": domain_name or None,
                    self._name_field: name,
                    self._description_field: description,
                    **self._detail_columns(members),
                }
            )
        return rows

    # -- LLM 阶段 --------------------------------------------------------

    def _describe_in_batches(self) -> dict[tuple[str, str], str]:
        """分批补描述，返回 ``{(domain, 业务名): 描述}``。

        条目可达数百个，单次调用会把 prompt 撑到十几万 token，因此按
        :attr:`_BATCH_SIZE` 切批；单批失败只影响该批，其余批次照常产出。
        """
        keys = sorted(self._members)
        described: dict[tuple[str, str], str] = {}
        system_prompt = self.build_system_prompt()
        total = (len(keys) + self._BATCH_SIZE - 1) // self._BATCH_SIZE

        logger.info(
            "%s: 分批补描述 total_items=%s batches=%s batch_size=%s",
            self.task.value,
            len(keys),
            total,
            self._BATCH_SIZE,
        )
        for start in range(0, len(keys), self._BATCH_SIZE):
            batch = keys[start : start + self._BATCH_SIZE]
            index = start // self._BATCH_SIZE + 1
            try:
                logger.info(
                    "LLM call: %s descriptions batch %s/%s size=%s",
                    self.task.value,
                    index,
                    total,
                    len(batch),
                )
                response = self.llm.complete(
                    system_prompt,
                    self._build_user_prompt(self._payload_for(batch)),
                    0.2,
                )
                described.update(self._validate(self._parse_json(response), set(batch)))
                logger.info(
                    f"{self.task.value} descriptions {index}/{total}: "
                    f"{len(batch)} items"
                )
            except Exception as exc:
                logger.warning(
                    f"{self.task.value} batch {index}/{total} failed, "
                    f"fall back to column comments: {exc}"
                )
        return described

    def _persist_aggregation(self, stage: str) -> None:
        """把当前聚合态落成 ``agg_*`` 的一个版本。

        规则归并后与 LLM 归一后各落一次，两个版本一 diff 就能看出归一合并了什么。
        """
        if self._source_tables is None:
            return
        self._source_tables.update(
            {self._agg_task.value: rows_to_dataframe(self._build_agg_rows())},
            self._agg_task,
        )
        logger.info(
            f"Persisted {self._agg_task.value} ({stage}): {len(self._members)} items"
        )

    def handle(
        self,
        table_evidence: SourceTableEvidence,
        log_evidence: LogEvidence | None = None,
    ) -> dict[str, pd.DataFrame]:
        """聚合 → 规则归并 → 落盘 → LLM 归一 → 重聚合 → 落盘 → 补描述。"""
        grouped = self._group_members(table_evidence)
        logger.info(
            f"{self.task.value} groups: {len(grouped)} items from "
            f"{sum(len(m) for m in grouped.values())} columns"
        )
        if not grouped:
            return {self.task.value: pd.DataFrame()}

        # 第一步：确定性聚合（含规则归并），先落盘，后面任何 LLM 阶段中断都不必重算。
        self._members = self._merge_identical_columns(grouped)
        self._persist_aggregation("rule-merged")

        # 第二步：跨表名称归一，然后按归一后的名字重新聚合。
        mapping = self._normalize_names(self._members)
        before = len(self._members)
        self._members = self._regroup(self._members, mapping)
        logger.info(
            f"{self.task.value}: normalized {before} -> {len(self._members)} items"
        )
        self._persist_aggregation("normalized")

        described = self._describe_in_batches()
        logger.info(
            f"{self.task.value} descriptions: {len(described)}/"
            f"{len(self._members)} filled by LLM, rest fall back to comments"
        )
        return {self.task.value: rows_to_dataframe(self._build_final_rows(described))}


class DimensionManageTaskHandler(_SemanticAggregationTaskHandler):
    """把所有表里的维度列按 ``(domain_name, dimension_name)`` 聚合成业务维度。

    只收 ``column_role == "dimension"`` 的列；日期列（``date`` / ``is_date``）
    不进入维度聚合。

    最终产物 ``{"dimension": DataFrame}``，输出的结果示例::

        domain_name,dimension_name,dimension_description,synonyms,enums
        sales,行业,客户所属行业,industry$$$industry_name,电力$$$渔业$$$农业
    """

    _agg_task = ProfileTaskName.AGG_DIMENSION
    _name_field = "dimension_name"
    _description_field = "dimension_description"
    _name_kind = "维度"

    @property
    def task(self) -> ProfileTaskName:
        return ProfileTaskName.DIMENSION_MANAGE

    def build_system_prompt(self) -> str:
        return build_dimension_meta_system_prompt()

    def _build_user_prompt(self, payload: list[dict[str, Any]]) -> str:
        return build_dimension_meta_user_prompt(payload)

    def _validate(self, parsed, keys):
        return validate_dimension_description_items(parsed, keys)

    def _accepts(self, column: Mapping[str, Any]) -> bool:
        """优先用列角色阶段的判定；日期列一律排除；缺失角色时退回日志标记。"""
        role = str(column.get("column_role") or "").strip().lower()
        if role == "date" or _resolve_boolean_value(
            column.get("is_date"), default=False
        ):
            return False
        if role:
            return role == "dimension"
        flag = column.get("is_dimension")
        if flag is not None:
            return _resolve_boolean_value(flag, default=False)
        return str(column.get("is_dimension_in_query_logs") or "").lower() == "yes"

    def _semantic_name(self, column: Mapping[str, Any]) -> str:
        return (
            str(column.get("dimension_name") or "").strip()
            or str(column.get("column_name") or "").strip()
        )

    def _extra_payload(self, members: list[dict[str, Any]]) -> dict[str, Any]:
        return {"enums_sample": self._prompt_enums(members)}

    def _extra_row(self, members: list[dict[str, Any]]) -> dict[str, Any]:
        return {"enums": _join_unique(self._enums_of(members))}


class MetricManageTaskHandler(_SemanticAggregationTaskHandler):
    """把所有表里的指标列按 ``(domain_name, metric_name)`` 聚合成业务指标。

    只收 ``column_role == "metric"`` 的列——被判为 ``discarded`` 的派生候选
    （``COUNT(*)``、``MAX(ds)`` 之类）在这里自然被排除。

    最终产物 ``{"metric": DataFrame}``，输出的结果示例::

        domain_name,metric_name,metric_description,synonyms,date_range,formula
        sales,成交额,当日成交总金额,gmv$$$total_gmv,1d,SUM(gmv)
    """

    _agg_task = ProfileTaskName.AGG_METRIC
    _name_field = "metric_name"
    _description_field = "metric_description"
    _name_kind = "指标"

    @property
    def task(self) -> ProfileTaskName:
        return ProfileTaskName.METRIC_MANAGE

    def build_system_prompt(self) -> str:
        return build_metric_system_prompt()

    def _build_user_prompt(self, payload: list[dict[str, Any]]) -> str:
        return build_metric_user_prompt(payload)

    def _validate(self, parsed, keys):
        return validate_metric_description_items(parsed, keys)

    def _accepts(self, column: Mapping[str, Any]) -> bool:
        """优先用列角色阶段的判定；缺失时退回日志证据标记。"""
        role = str(column.get("column_role") or "").strip().lower()
        if role:
            return role == "metric"
        flag = column.get("is_metric")
        if flag is not None:
            return _resolve_boolean_value(flag, default=False)
        return str(column.get("is_metric_in_query_logs") or "").lower() == "yes"

    def _semantic_name(self, column: Mapping[str, Any]) -> str:
        return (
            str(column.get("metric_name") or "").strip()
            or str(column.get("query_log_metric_name") or "").strip()
            or str(column.get("column_name") or "").strip()
        )

    def _date_ranges_of(self, members: list[dict[str, Any]]) -> list[str]:
        return [
            str(member.get("date_range") or "").strip()
            for member in members
            if str(member.get("date_range") or "").strip()
        ]

    def _formulas_of(self, members: list[dict[str, Any]]) -> list[str]:
        formulas: list[str] = []
        for member in members:
            text = str(member.get("metric_formula") or "").strip()
            if not text:
                text = str(member.get("column_name") or "").strip()
            if text and text not in formulas:
                formulas.append(text)
        return formulas[: self._MAX_ENUMS]

    def _extra_payload(self, members: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "date_ranges": sorted(set(self._date_ranges_of(members))),
            "formulas": self._formulas_of(members)[: self._PROMPT_ENUMS],
        }

    def _extra_row(self, members: list[dict[str, Any]]) -> dict[str, Any]:
        # date_range 只进 prompt 供模型写描述时参考，不进最终产物——
        # ground truth 的 metric_lib 里没有这一列。
        return {"formula": _join_unique(self._formulas_of(members))}
