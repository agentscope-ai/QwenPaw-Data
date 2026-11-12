"""Domain profiling 的确定性领域逻辑。

本模块只负责 JOIN 证据、Domain 输入、LLM 响应校验和结果记录转换：

    SqlAnalysis -> JoinEvidence -> DomainInput -> DomainResolution -> result rows

LLM 调用与 80% 覆盖率重试由 :class:`handler.BizDomainTaskHandler` 编排；缓存和落盘由
``SourceTablesContext`` 负责。本模块不直接访问 LLM、文件系统或 adapters。
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

import pandas as pd
from sqlglot import exp

from .base import SourceTableEvidence
from .llm import extract_json_payload
from .utils_ast import SqlAnalysis

JOIN_EDGE_COLUMNS = (
    "left_table",
    "right_table",
    "join_type",
    "on_columns",
    "count",
)
MIN_DOMAIN_COVERAGE = 0.8


@dataclass(frozen=True)
class JoinEdge:
    """单条 SQL 中两个物理表之间的一条 JOIN 证据。"""

    left_table: str
    right_table: str
    join_type: str
    on_columns: str


class JoinEvidence(TypedDict):
    """全部 Query Logs 聚合后的 Domain JOIN 证据。"""

    join_edges: pd.DataFrame
    clusters: dict[str, list[str]]
    table_to_cluster_id: dict[str, str]
    query_table_names: list[str]
    table_frequencies: dict[str, int]


class DomainRequest(TypedDict):
    """交给 Domain Prompt 的结构化数据。"""

    cluster_items: list[dict[str, Any]]
    unclustered_items: list[dict[str, str]]


class DomainAssignment(TypedDict):
    """一个 Domain 及其最终覆盖的表。"""

    domain: str
    display_name: str
    description: str
    aliases: list[str]
    tables: list[str]


@dataclass(frozen=True)
class DomainInput:
    """一次 Domain LLM 请求所需的完整只读上下文。"""

    request: DomainRequest
    join_evidence: JoinEvidence
    expected_tables: frozenset[str]
    table_comments: dict[str, str]


@dataclass(frozen=True)
class DomainResolution:
    """一次 LLM 响应完成校验后的 Domain 解析结果。"""

    assignments: tuple[DomainAssignment, ...]
    resolver_source: str
    coverage: float
    unmapped_tables: tuple[str, ...]


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, node: str) -> None:
        self.parent.setdefault(node, node)

    def find(self, node: str) -> str:
        self.parent.setdefault(node, node)
        if self.parent[node] != node:
            self.parent[node] = self.find(self.parent[node])
        return self.parent[node]

    def union(self, left: str, right: str) -> None:
        self.add(left)
        self.add(right)
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left != root_right:
            self.parent[root_right] = root_left

    def clusters(self) -> tuple[tuple[str, ...], ...]:
        grouped: dict[str, list[str]] = {}
        for node in self.parent:
            grouped.setdefault(self.find(node), []).append(node)
        groups = [tuple(sorted(tables)) for tables in grouped.values()]
        return tuple(sorted(groups, key=lambda tables: tables[0]))


def _normalize_table_name(name: str) -> str:
    """全链路只保留最后一段 ``table_name``，不保留 schema/catalog。"""
    return str(name or "").strip().rsplit(".", 1)[-1]


def extract_join_edges(analysis: SqlAnalysis) -> list[JoinEdge]:
    """从单个 SQL 分析上下文提取 JOIN 边，表标识统一为无 schema 的 ``table_name``。

    输出的结果示例（``list[JoinEdge]``）::

        [JoinEdge(left_table="order_items", right_table="orders",
                  join_type="INNER JOIN", on_columns="order_id=id"),
         JoinEdge(left_table="orders", right_table="users",
                  join_type="LEFT OUTER JOIN", on_columns="user_id=id")]

    Corner cases：
        - 只拆 ON 的顶层 AND 等值条件，OR 子树整体跳过；
        - 自连接（左右同表）与无法定位来源表的条件被丢弃；
        - 同一对表的多个 ON 列会合并成 ``"a=b,c=d"`` 一条边。
    """
    edges: list[JoinEdge] = []
    seen: set[tuple[str, str, str, str]] = set()

    for join in analysis.join_exprs:
        on_expr = join.args.get("on")
        if on_expr is None:
            continue
        parent_select = analysis.resolve_owning_select_expr(join)
        if parent_select is None:
            continue

        join_type_parts: list[str] = []
        if side := join.args.get("side"):
            join_type_parts.append(str(side).upper())
        if kind := join.args.get("kind"):
            join_type_parts.append(str(kind).replace("_", " ").upper())
        join_type = f"{' '.join(join_type_parts)} JOIN" if join_type_parts else "JOIN"
        table_pairs: dict[tuple[str, str], list[str]] = {}

        # 顶层 AND 中的等值条件分别贡献 JOIN 证据；OR 子树不做不安全拆分。
        for part in analysis.extract_conjunct_exprs(on_expr):
            if not isinstance(part, exp.EQ):
                continue
            left_col, right_col = part.left, part.right
            if not isinstance(left_col, exp.Column) or not isinstance(
                right_col,
                exp.Column,
            ):
                continue

            left_source = analysis.resolve_unique_column_source_table_name(
                parent_select,
                left_col,
            )
            right_source = analysis.resolve_unique_column_source_table_name(
                parent_select,
                right_col,
            )
            if left_source is None or right_source is None:
                continue

            left_table = _normalize_table_name(left_source)
            right_table = _normalize_table_name(right_source)
            if not left_table or not right_table or left_table == right_table:
                continue

            pair = tuple(sorted((left_table, right_table)))
            on_column = (
                f"{analysis.extract_column_name(left_col)}="
                f"{analysis.extract_column_name(right_col)}"
            )
            table_pairs.setdefault(pair, []).append(on_column)

        for (left_table, right_table), on_columns in table_pairs.items():
            columns_text = ",".join(sorted(set(on_columns)))
            key = (left_table, right_table, join_type, columns_text)
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                JoinEdge(
                    left_table=left_table,
                    right_table=right_table,
                    join_type=join_type,
                    on_columns=columns_text,
                )
            )
    return edges


def aggregate_join_evidence(
    analyses: Sequence[SqlAnalysis],
) -> JoinEvidence:
    """聚合全部 SQL 的 JOIN 边，并用并查集构造连通分量与表索引。

    输出 :class:`JoinEvidence`，其中 ``join_edges`` 的结果示例::

        left_table,right_table,join_type,on_columns,count
        order_items,orders,INNER JOIN,order_id=id,37
        orders,users,LEFT OUTER JOIN,user_id=id,12

    其余键::

        clusters            {"cluster_0": ["order_items", "orders", "users"]}
        table_to_cluster_id {"orders": "cluster_0", "users": "cluster_0"}
        query_table_names   ["items", "order_items", "orders", "users"]
        table_frequencies   {"orders": 12, "users": 5, ...}  # 每条日志计一次

    Corner cases：
        - 没有任何 JOIN 时 ``join_edges`` 是带列空表、``clusters`` 为 ``{}``，
          此时所有表都会进入 Prompt 的 ``unclustered_items``。
    """
    edge_rows: list[dict[str, str]] = []
    table_frequencies: Counter[str] = Counter()

    for analysis in analyses:
        names_in_query = {
            table_name
            for name in analysis.physical_table_names
            if (table_name := _normalize_table_name(name))
        }
        table_frequencies.update(names_in_query)
        edge_rows.extend(
            {
                "left_table": edge.left_table,
                "right_table": edge.right_table,
                "join_type": edge.join_type,
                "on_columns": edge.on_columns,
            }
            for edge in extract_join_edges(analysis)
        )

    if edge_rows:
        join_edges = (
            pd.DataFrame(edge_rows)
            .groupby(
                ["left_table", "right_table", "join_type", "on_columns"],
                as_index=False,
            )
            .size()
            .rename(columns={"size": "count"})
        )[list(JOIN_EDGE_COLUMNS)]
    else:
        join_edges = pd.DataFrame(columns=list(JOIN_EDGE_COLUMNS))

    union_find = _UnionFind()
    for row in join_edges.itertuples(index=False):
        union_find.union(row.left_table, row.right_table)

    clusters: dict[str, list[str]] = {}
    table_to_cluster_id: dict[str, str] = {}
    for index, tables in enumerate(union_find.clusters()):
        cluster_id = f"cluster_{index}"
        clusters[cluster_id] = list(tables)
        for table in tables:
            table_to_cluster_id[table] = cluster_id

    return {
        "join_edges": join_edges,
        "clusters": clusters,
        "table_to_cluster_id": table_to_cluster_id,
        "query_table_names": sorted(table_frequencies),
        "table_frequencies": dict(table_frequencies),
    }


def _extract_table_comments(
    table_evidence: SourceTableEvidence,
) -> dict[str, str]:
    """读取源表注释；主键始终是 overview 中的 ``table_name``。"""
    comments: dict[str, str] = {}
    overview = table_evidence.table_overview
    if isinstance(overview, pd.DataFrame) and not overview.empty:
        if "table_name" not in overview.columns:
            raise ValueError("Domain table evidence requires table_name")
        for row in overview.itertuples(index=False):
            table_name = _normalize_table_name(getattr(row, "table_name", ""))
            if not table_name:
                continue
            raw_comment = getattr(row, "table_comment", None)
            comment = (
                ""
                if raw_comment is None
                or (isinstance(raw_comment, float) and pd.isna(raw_comment))
                else str(raw_comment).strip()
            )
            comments[table_name] = comment
        return comments

    # sources 仅用于旧值对象上下文兼容，同样只读取 table_name。
    for source in table_evidence.sources:
        table_name = _normalize_table_name(source.table_name)
        if table_name:
            comments[table_name] = source.table_comment or ""
    return comments


def _create_empty_join_evidence() -> JoinEvidence:
    """创建无 Query Logs 时使用的空 Domain 日志证据。"""
    return {
        "join_edges": pd.DataFrame(columns=list(JOIN_EDGE_COLUMNS)),
        "clusters": {},
        "table_to_cluster_id": {},
        "query_table_names": [],
        "table_frequencies": {},
    }


def _seen_tables_from_join_evidence(join_evidence: JoinEvidence) -> set[str]:
    """日志里出现过的表名集合；兼容缺少 ``table_frequencies`` 的旧证据。"""
    frequencies = join_evidence.get("table_frequencies") or {}
    if frequencies:
        return {name for name, count in frequencies.items() if count > 0}
    return set(join_evidence.get("query_table_names") or ())


def _build_domain_request(
    join_evidence: JoinEvidence,
    expected_tables: frozenset[str],
    table_comments: dict[str, str],
) -> DomainRequest:
    """组装 cluster 与未聚类表的结构化 Prompt 输入。"""
    cluster_items: list[dict[str, Any]] = []
    for cluster_id, tables in join_evidence["clusters"].items():
        table_set = set(tables)
        sample_joins = [
            {
                "left": row.left_table,
                "right": row.right_table,
                "on": row.on_columns,
                "count": int(row.count),
            }
            for row in join_evidence["join_edges"].itertuples(index=False)
            if row.left_table in table_set and row.right_table in table_set
        ]
        cluster_items.append(
            {
                "cluster_id": cluster_id,
                "tables": [
                    {
                        "table_name": table,
                        "table_comment": table_comments.get(table, ""),
                    }
                    for table in tables
                ],
                "cluster_size": len(tables),
                "sample_joins": sample_joins[:10],
            }
        )

    clustered_tables = {
        table for tables in join_evidence["clusters"].values() for table in tables
    }
    return {
        "cluster_items": cluster_items,
        "unclustered_items": [
            {
                "table_name": table,
                "table_comment": table_comments.get(table, ""),
            }
            for table in sorted(expected_tables)
            if table not in clustered_tables
        ],
    }


def prepare_domain_input(
    table_evidence: SourceTableEvidence,
    join_evidence: JoinEvidence | None,
    *,
    filter_unused_tables: bool = True,
) -> DomainInput:
    """合并源表证据和已抽取的 Domain 日志证据，生成 LLM 输入。

    ``join_evidence`` 应由 :func:`aggregate_join_evidence` 在
    ``QueryLogContext.extract_evidence`` 中一次性生成。无 Query Logs 时传
    ``None``，此时 Domain 仅根据源表名称和注释生成。

    ``filter_unused_tables`` 为 True 且日志里有出现过的表时，只保留
    「源表 ∩ 日志出现表」参与 Domain（从未进日志的源表不会进入
    ``unclustered_items``）。无日志 / 频次为空时自动不筛，保持原行为。
    """
    resolved_join_evidence = (
        join_evidence if join_evidence is not None else _create_empty_join_evidence()
    )
    table_comments = _extract_table_comments(table_evidence)
    seen = _seen_tables_from_join_evidence(resolved_join_evidence)
    if filter_unused_tables and seen:
        expected_tables = frozenset(table_comments) & seen
    else:
        expected_tables = frozenset(set(table_comments) | seen)
    return DomainInput(
        request=_build_domain_request(
            resolved_join_evidence,
            expected_tables,
            table_comments,
        ),
        join_evidence=resolved_join_evidence,
        expected_tables=expected_tables,
        table_comments=table_comments,
    )


def _extract_domain_of_source(code: str) -> str:
    """从 JSON 字符串字段中抽出唯一的 ``domain_of`` 函数源码。"""
    text = (code or "").strip()
    fenced = re.search(
        r"```(?:python|py)?\s*([\s\S]*?)```",
        text,
        flags=re.IGNORECASE,
    )
    if fenced:
        text = fenced.group(1).strip()
    match = re.search(
        r"(def\s+domain_of\s*\([\s\S]*?)(?=\ndef\s+\w+\s*\(|\Z)",
        text,
    )
    if match:
        return match.group(1).strip()
    if "def domain_of" in text:
        return text
    raise ValueError("LLM 输出中未找到 domain_of 函数定义")


def _validate_domain_of_ast(source: str) -> None:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("domain_of 禁止 import")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError(f"domain_of 禁止访问 {node.attr}")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {
                "eval",
                "exec",
                "open",
                "__import__",
                "compile",
                "input",
                "print",
            }:
                raise ValueError(f"domain_of 禁止调用 {func.id}")
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if len(functions) != 1 or functions[0].name != "domain_of":
        raise ValueError("domain_of_code 必须且只能定义 domain_of 一个函数")


def compile_domain_of(code: str) -> tuple[Callable[[str], str | None], str]:
    """把 LLM 生成的 ``domain_of`` 代码编译成可调用函数。

    ``code`` 可以是 LLM 原始输出（允许带 markdown 围栏），也可以是已经抽取过的
    纯源码——黑板重新加载 ``domain_of.py`` 时走的就是后一条路径。

    返回 ``(函数, 规范化后的源码)``；函数签名为
    ``domain_of(table_name: str) -> str | None``，例如
    ``domain_of("ads_example_revenue_index_1d") -> "revenue"``。

    Corner cases：
        - 找不到 ``domain_of`` 定义、含 import / eval 等禁用语法、或定义了多个
          函数时抛 ``ValueError``；
        - 执行沙箱只暴露 ``str/len/isinstance/bool`` 等少数内建。
    """
    source = _extract_domain_of_source(code)
    _validate_domain_of_ast(source)
    namespace: dict[str, Any] = {
        "__builtins__": {
            "None": None,
            "True": True,
            "False": False,
            "str": str,
            "len": len,
            "isinstance": isinstance,
            "bool": bool,
        }
    }
    exec(compile(source, "<domain_of>", "exec"), namespace, namespace)
    function = namespace.get("domain_of")
    if not callable(function):
        raise ValueError("domain_of 编译后不可调用")
    return function, source  # type: ignore[return-value]


def _normalize_aliases(raw: Any) -> list[str]:
    if isinstance(raw, list):
        aliases = [str(alias).strip() for alias in raw]
    elif isinstance(raw, str):
        aliases = re.split(r"[,，、]", raw)
    else:
        aliases = []
    return sorted({alias for alias in aliases if alias})


def _parse_domain_payload(
    response: str,
) -> tuple[str, dict[str, dict[str, Any]]]:
    payload = extract_json_payload(response)
    if not isinstance(payload, dict):
        raise ValueError("LLM 输出必须是 JSON 对象")

    code = str(payload.get("domain_of_code") or "").strip()
    if not code:
        raise ValueError("缺少 domain_of_code")
    raw_domains = payload.get("domains")
    if not isinstance(raw_domains, list):
        raise ValueError("domains 必须是数组")

    metadata: dict[str, dict[str, Any]] = {}
    for item in raw_domains:
        if not isinstance(item, dict):
            raise ValueError("domains 数组元素必须是对象")
        domain = str(item.get("domain", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9_]+", domain):
            raise ValueError(f"非法 domain 名称: {domain!r}")
        if domain in metadata:
            raise ValueError(f"domain 重复: {domain}")
        metadata[domain] = {
            "display_name": str(item.get("display_name", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "aliases": _normalize_aliases(item.get("alias", item.get("aliases"))),
        }
    return code, metadata


def _apply_domain_of(
    domain_of: Callable[[str], str | None],
    domain_input: DomainInput,
    metadata: dict[str, dict[str, Any]],
) -> tuple[DomainAssignment, ...]:
    table_to_domain: dict[str, str] = {}
    for table in sorted(domain_input.expected_tables):
        try:
            raw_domain = domain_of(table)
        except Exception as exc:  # pragma: no cover - generated code edge
            raise ValueError(f"domain_of({table!r}) 执行失败: {exc}") from exc
        if raw_domain is None:
            continue
        domain = str(raw_domain).strip().lower()
        if not re.fullmatch(r"[a-z0-9_]+", domain):
            raise ValueError(f"domain_of({table!r}) 返回非法 domain: {raw_domain!r}")
        table_to_domain[table] = domain

    for tables in domain_input.join_evidence["clusters"].values():
        assigned_domains = {
            table_to_domain[table] for table in tables if table in table_to_domain
        }
        if len(assigned_domains) > 1:
            raise ValueError(
                f"同一 cluster 被映射到多个 domain: "
                f"{tables} -> {sorted(assigned_domains)}"
            )

    tables_by_domain: dict[str, list[str]] = {}
    for table, domain in table_to_domain.items():
        tables_by_domain.setdefault(domain, []).append(table)

    missing_metadata = sorted(set(tables_by_domain) - set(metadata))
    if missing_metadata:
        raise ValueError(f"domains 元信息缺少: {missing_metadata}")
    unused_metadata = sorted(set(metadata) - set(tables_by_domain))
    if unused_metadata:
        raise ValueError(f"domains 包含 resolver 不会返回的项: {unused_metadata}")

    return tuple(
        {
            "domain": domain,
            "display_name": str(metadata[domain]["display_name"]),
            "description": str(metadata[domain]["description"]),
            "aliases": list(metadata[domain]["aliases"]),
            "tables": sorted(tables),
        }
        for domain, tables in sorted(tables_by_domain.items())
    )


def parse_domain_resolution(
    response: str,
    domain_input: DomainInput,
) -> DomainResolution:
    """解析并校验一次 LLM 响应，同时计算覆盖率和未覆盖表。

    返回 :class:`DomainResolution`::

        assignments     ({"domain": "revenue", "display_name": "收入域",
                          "description": "...", "aliases": ["收入", "营收"],
                          "tables": ["ads_..._1d", ...]}, ...)
        resolver_source "def domain_of(table_name: str) -> str | None: ..."
        coverage        0.93          # 被映射到 domain 的表 / expected_tables
        unmapped_tables ("tmp_x", ...)

    Corner cases：
        - 响应缺 ``domain_of_code``、domain 名非法、同一 cluster 被映射到多个
          domain、domains 元信息与 resolver 返回值对不上时，一律抛 ``ValueError``，
          由 handler 兜底成 coverage=0 的空结果并重试一次。
    """
    code, metadata = _parse_domain_payload(response)
    domain_of, resolver_source = compile_domain_of(code)
    assignments = _apply_domain_of(domain_of, domain_input, metadata)

    mapped_tables = {
        table for assignment in assignments for table in assignment["tables"]
    }
    unmapped_tables = tuple(sorted(domain_input.expected_tables - mapped_tables))
    coverage = (
        len(mapped_tables) / len(domain_input.expected_tables)
        if domain_input.expected_tables
        else 1.0
    )
    return DomainResolution(
        assignments=assignments,
        resolver_source=resolver_source,
        coverage=coverage,
        unmapped_tables=unmapped_tables,
    )


def build_domain_result_rows(
    resolution: DomainResolution,
) -> list[dict[str, Any]]:
    """把最终解析结果转换为黑板 DataFrame 所需的稳定记录。

    输出的结果示例（``list[dict]``，装进 DataFrame 后即 domain 任务的结果表）::

        domain_name,display_name,description,aliases,member_tables
        revenue,收入域,收入与毛利相关事实表,收入、营收,"[{""source_table"": ""ads_..._1d"", ""dataset_name"": ""ads_..._1d""}]"

    ``member_tables`` 刻意存成 JSON 字符串，保证能稳定经过
    ``DataFrame -> Excel -> DataFrame`` 往返。

    Corner cases：
        - 没有覆盖任何表的 domain 会被整条丢弃。
    """
    return [
        {
            "domain_name": assignment["domain"],
            "display_name": assignment["display_name"] or None,
            "description": assignment["description"] or None,
            "aliases": "、".join(assignment["aliases"]) or None,
            # JSON 字符串可稳定经过 DataFrame -> Excel -> DataFrame。
            "member_tables": json.dumps(
                [
                    {
                        "source_table": table,
                        "dataset_name": table,
                    }
                    for table in assignment["tables"]
                ],
                ensure_ascii=False,
            ),
        }
        for assignment in resolution.assignments
        if assignment["tables"]
    ]


__all__ = [
    "DomainInput",
    "DomainResolution",
    "JoinEdge",
    "JoinEvidence",
    "aggregate_join_evidence",
    "build_domain_result_rows",
    "compile_domain_of",
    "extract_join_edges",
    "parse_domain_resolution",
    "prepare_domain_input",
]
