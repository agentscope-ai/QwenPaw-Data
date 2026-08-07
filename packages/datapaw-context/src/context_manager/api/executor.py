"""SQL 执行器：拿 LLM 生成的 SELECT 跑 PG，并做"启发式可疑列"扫描。

只做两件事：

1. :func:`execute_sql` — 在数仓/本地 PG 上跑只读 SELECT，返回结构化结果（columns + rows）。
   后端可选: data-api-hub HTTP / psycopg 直连数仓 / JDBC / 本地 PG（见 ``SQL_EXEC_BACKEND``）。
   - 拒绝 ``INSERT/UPDATE/DELETE/DDL``（把 SQL 丢到 sqlglot 的 CTE-stripped AST 里检查，
     这里走简单的关键字白名单，已经够用，因为 SQL 是 LLM 出的）；
   - ``statement_timeout`` + ``cur.fetchmany`` 双限流；
   - 错误统一封到 :class:`ExecResult.error`，不抛。

2. :func:`detect_sentinel_risks` — 给 SQL 找"潜在 sentinel 列"。算法：
   1) 从 SQL 里抽出主表和 FROM/WHERE；
   2) 在 Neo4j 元数据里查这张表的所有 Column；
   3) 对每个**没出现在 WHERE/GROUP BY** 的低基数 dim-like 列，去 PG 跑
      ``SELECT DISTINCT col, count(*)`` 拿 distinct values；
   4) 任意值匹配已知 sentinel 词典（``全部 / all / total / 合计 / overall ...``）→ 记一条
      :class:`SentinelRisk` 给 critic。

为什么不用 sqlglot：dependency 还没装，且这里只关心 ``SUM(x) FROM t WHERE ...``
这种简单聚合 SQL，正则就能扛。

输出契约（critic 那边消费）见 :class:`ExecResult` / :class:`SentinelRisk` docstring。
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

import psycopg
from neo4j import Driver

from datapaw.context.blocking_io import BlockingIOGovernor, BlockingPool

from ..config import CFG
from ..utils import get_logger, neo4j_session
from context_manager.view_fallthrough import (
    FallthroughGiveUp,
    extract_cm_view_refs,
    extract_missing_table_name,
    is_view_not_found_error,
    rewrite_cm_view_to_physical,
    rewrite_dataset_to_physical,
)

log = get_logger("api.executor")


# ---------------------------------------------------------------------- #
# 数据类
# ---------------------------------------------------------------------- #
@dataclass
class ExecResult:
    """SQL 执行结果。"""
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    error: Optional[str] = None
    logview_url: Optional[str] = None
    instance_id: Optional[str] = None
    task_status: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "sql": self.sql,
            "columns": self.columns,
            "rows": [[_jsonable(v) for v in r] for r in self.rows],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "error": self.error,
        }
        if self.logview_url:
            d["logview_url"] = self.logview_url
        if self.instance_id:
            d["instance_id"] = self.instance_id
        if self.task_status:
            d["task_status"] = self.task_status
        return d


@dataclass
class SentinelRisk:
    """一条疑点：``column`` 中出现 sentinel 值 ``sentinel_value``，但 SQL 没过滤。"""
    column_key: str
    column_name: str        # 列名（不带表前缀）
    table_key: str
    table_name: str
    distinct_values: list[Any]
    sentinel_value: Any     # 实际命中的可疑值（如 "全部"）
    sentinel_reason: str    # "rollup_word_match" / "outlier_cardinality"
    suggested_filter: str   # 形如 "terminal_type != '全部'"

    def to_dict(self) -> dict:
        return {
            "column_key": self.column_key,
            "column_name": self.column_name,
            "table_key": self.table_key,
            "table_name": self.table_name,
            "distinct_values": [_jsonable(v) for v in self.distinct_values],
            "sentinel_value": _jsonable(self.sentinel_value),
            "sentinel_reason": self.sentinel_reason,
            "suggested_filter": self.suggested_filter,
        }


# ---------------------------------------------------------------------- #
# 执行
# ---------------------------------------------------------------------- #
_FORBID_RE = re.compile(
    r"\b("
    r"insert|update|delete|drop|truncate|alter|create|"
    r"grant|revoke|vacuum|copy|comment\s+on|merge\s+into|"
    r"do|load|refresh|reindex|cluster|discard|lock\s+table|"
    r"set\s+role|set\s+session|"
    r"prepare\s+[a-z]|execute\s+[a-z]|deallocate"
    r")\b",
    flags=re.IGNORECASE,
)
_SELECT_RE = re.compile(r"^\s*(with\b|select\b)", flags=re.IGNORECASE | re.DOTALL)
_ALLOW_RE = re.compile(
    r"^\s*("
    r"with\b|"
    r"select\b|"
    r"explain\b|"
    r"show\b|"
    r"values\s*\("
    r")",
    flags=re.IGNORECASE | re.DOTALL,
)


def _strip_string_literals(sql: str) -> str:
    """用占位符替换 SQL 中的字符串字面量，避免关键字误判（如 ``WHERE name = 'SELECT'``）。"""
    return re.sub(r"'(?:[^'\\]|\\.)*'", "''", sql)


def _guard_sql(sql: str) -> Optional[str]:
    """SQL 安全守卫。返回 ``None`` 表示通过，否则返回拒绝原因。

    三层防护:
    1) 白名单 — 仅允许 SELECT / WITH / EXPLAIN / SHOW / VALUES 开头
    2) 黑名单 — 即使以 SELECT 开头，也不允许嵌套 admin 关键字
    3) 多语句检测 — 分号分隔的多条 SQL 一律拒绝
    """
    s = sql.strip().rstrip(";").strip()
    if not s:
        return "SQL 为空"

    if not _ALLOW_RE.match(s):
        head = s[:60].replace("\n", " ")
        return f"仅允许只读 SQL（SELECT / WITH / EXPLAIN / SHOW），拒绝: {head}"

    stripped = _strip_string_literals(s)
    if ";" in stripped:
        return "禁止多语句执行（检测到分号分隔的多条 SQL）"

    hit = _FORBID_RE.search(stripped)
    if hit:
        return f"SQL 包含禁止的 admin 操作关键字: {hit.group(0)!r}"

    return None


def _is_select_only(sql: str) -> bool:
    s = sql.strip().rstrip(";").strip()
    if not _SELECT_RE.match(s):
        return False
    if _FORBID_RE.search(s):
        return False
    return True


def classify_pg_exec_signal(
    res: ExecResult, slow_threshold_ms: float
) -> Tuple[Optional[str], str]:
    """Classify execution for strategy-card signals (matches ``/api/execute_sql`` semantics).

    Returns ``(kind, detail)`` where ``detail`` is only meaningful for ``sql_error``.
    Priority: error → empty rows → slow (elapsed ≥ threshold, non-empty rows).
    """
    if res.error:
        return "sql_error", str(res.error)
    if not res.rows:
        return "empty_result", ""
    if res.elapsed_ms >= slow_threshold_ms:
        return "slow_query", ""
    return None, ""


def _resolve_sql_exec_backend() -> str:
    """根据当前 active datasource 的 ``_datasource_type`` 解析后端。

    所有连接凭证一律来自 semantic_config.db，不再退化到 CFG / 环境变量。
    无 active datasource 时返回 ``"unknown"``（executor 据此拒绝执行）。
    """
    from .datasource_active_api import get_synced_default_datasource_id, load_datasource_config

    active = get_synced_default_datasource_id()
    if active:
        cfg = load_datasource_config(active)
        if cfg is not None:
            ds_type = cfg.get("_datasource_type", "")
            if ds_type in ("postgresql", "postgres", "hologres"):
                return "direct"
            if ds_type == "mysql":
                return "pymysql_direct"
            if ds_type == "odps":
                return "odps_akless"
    return "unknown"


def _resolve_backend_for_datasource(datasource_id: str) -> str:
    """根据 datasource_id 解析 SQL 执行后端。

    只从 semantic_config.db 的 config 字段读 ``_datasource_type``：
    - postgresql/postgres/hologres → direct（psycopg）
    - mysql → pymysql_direct（PyMySQL，config 来自 SQLite）
    - odps → odps_akless（PyODPS，config 来自 SQLite）
    - 查不到 config → ``"unknown"``（执行时直接失败，不退化到 CFG）
    - datasource_id 为空 → 走 ``_resolve_sql_exec_backend()``
    """
    ds_id = (datasource_id or "").strip()
    if ds_id:
        from .datasource_active_api import load_datasource_config
        cfg = load_datasource_config(ds_id)
        if cfg is not None:
            ds_type = cfg.get("_datasource_type", "")
            if ds_type in ("postgresql", "postgres", "hologres"):
                return "direct"
            if ds_type == "mysql":
                return "pymysql_direct"
            if ds_type == "odps":
                return "odps_akless"
        return "unknown"
    return _resolve_sql_exec_backend()


def _pg_connect_kwargs(*, datasource_id: str) -> dict[str, Any]:
    """取 PG 连接参数：从 semantic_config.db 按 datasource_id 读 config。"""
    from .datasource_active_api import resolve_pg_connect_kwargs

    return resolve_pg_connect_kwargs(datasource_id=datasource_id, search_path=False)


def _execute_sql_via_psycopg(
    sql: str,
    *,
    max_rows: int = 200,
    datasource_id: str = "",
) -> ExecResult:
    """Execute read-only SQL via psycopg (local PG or PG-compatible warehouse).

    datasource_id 非空时，_pg_connect_kwargs 从 semantic_config.db 的 config 字段
    读该数据源的连接凭证；匹配不到直接失败（不回退 CFG）。
    """
    t0 = time.time()
    try:
        kwargs = _pg_connect_kwargs(datasource_id=datasource_id)
    except (ValueError, RuntimeError) as exc:
        return ExecResult(sql=sql, error=str(exc), elapsed_ms=(time.time() - t0) * 1000)
    try:
        with psycopg.connect(
            **kwargs,
            autocommit=True,
            connect_timeout=5,
        ) as conn, conn.cursor() as cur:
            cur.execute(sql)
            cols = [d.name for d in (cur.description or [])]
            rows = cur.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            total = len(rows)
            rows = rows[:max_rows]
            row_count = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else total
        return ExecResult(
            sql=sql, columns=cols, rows=[list(r) for r in rows],
            row_count=row_count, truncated=truncated,
            elapsed_ms=(time.time() - t0) * 1000,
        )
    except Exception as exc:
        return ExecResult(
            sql=sql, error=str(exc),
            elapsed_ms=(time.time() - t0) * 1000,
        )


def _execute_sql_via_pymysql(
    sql: str,
    *,
    max_rows: int = 200,
    datasource_id: str = "",
) -> ExecResult:
    """通过 PyMySQL 直连 MySQL 执行只读 SELECT。

    连接凭证从 semantic_config.db 按 ``datasource_id`` 读 config。
    """
    from .datasource_active_api import resolve_mysql_connect_kwargs

    t0 = time.time()
    try:
        kwargs = resolve_mysql_connect_kwargs(datasource_id=datasource_id or None)
    except (ValueError, RuntimeError) as exc:
        return ExecResult(sql=sql, error=str(exc), elapsed_ms=(time.time() - t0) * 1000)
    try:
        import pymysql

        conn = pymysql.connect(
            **kwargs,
            charset="utf8mb4",
            connect_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d[0] for d in (cur.description or [])]
                rows = cur.fetchmany(max_rows + 1)
                truncated = len(rows) > max_rows
                total = len(rows)
                rows = rows[:max_rows]
                return ExecResult(
                    sql=sql, columns=cols, rows=[list(r) for r in rows],
                    row_count=total, truncated=truncated,
                    elapsed_ms=(time.time() - t0) * 1000,
                )
        finally:
            conn.close()
    except Exception as exc:
        return ExecResult(
            sql=sql, error=str(exc),
            elapsed_ms=(time.time() - t0) * 1000,
        )


def _execute_sql_via_odps_akless(
    sql: str,
    *,
    max_rows: int = 200,
    datasource_id: str = "",
) -> ExecResult:
    """通过 PyODPS 执行 SQL（MaxCompute 等 ODPS 数据源）。

    凭证来自 semantic_config.db（由 ``datasource_id`` 指定或 active default）。
    """
    from ..graph.odps_akless import get_odps_client

    t0 = time.time()
    logview_url: Optional[str] = None
    try:
        client = get_odps_client(datasource_id=datasource_id or None)
        inst = client.execute_sql(sql)
        try:
            logview_url = inst.get_logview_address()
            log.info("ODPS LogView: %s", logview_url)
        except Exception:
            pass
        inst.wait_for_success()
        with inst.open_reader() as reader:
            cols = [col.name for col in reader._schema.columns]
            all_rows: list[list[Any]] = []
            for i, record in enumerate(reader):
                if i >= max_rows:
                    break
                all_rows.append([record[c] for c in cols])
            truncated = reader.count > max_rows
            return ExecResult(
                sql=sql, columns=cols, rows=all_rows,
                row_count=reader.count, truncated=truncated,
                elapsed_ms=(time.time() - t0) * 1000,
                logview_url=logview_url,
                instance_id=inst.id,
                task_status=str(inst.status),
            )
    except Exception as exc:
        return ExecResult(
            sql=sql, error=str(exc),
            elapsed_ms=(time.time() - t0) * 1000,
            logview_url=logview_url,
        )


def _resolve_dataset_parents(names: set[str]) -> dict[str, list[tuple[str, str]]]:
    """查每个 dataset name 对应的 :Dataset)-[:CONTAINS_TABLE]->(:Table) 物理表列表。

    返回 {name: [(schema, table), ...]}; 找不到的 name 返回空 list,由 rewrite 端判断。
    """
    if not names:
        return {}
    from neo4j import GraphDatabase  # 局部 import,避免循环依赖与启动期开销

    out: dict[str, list[tuple[str, str]]] = {n: [] for n in names}
    driver = GraphDatabase.driver(
        CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password),
    )
    try:
        cypher = (
            "UNWIND $names AS n "
            "MATCH (d:Dataset {name: n})-[:CONTAINS_TABLE]->(t:Table) "
            "RETURN n AS name, t.schema AS schema, t.name AS table"
        )
        with driver.session(database=CFG.neo4j_database) as s:
            for rec in s.run(cypher, names=list(names)):
                out[rec["name"]].append((rec["schema"], rec["table"]))
    finally:
        driver.close()
    return out


def execute_sql(
    sql: str,
    *,
    max_rows: int = 200,
    datasource_id: str = "",
) -> ExecResult:
    """执行只读 SQL。后端由当前数据源 ``_datasource_type`` 决定，凭证一律来自 semantic_config.db。

    后端映射：
    - postgresql/postgres/hologres → direct（psycopg）
    - mysql → pymysql_direct（PyMySQL）
    - odps → odps_akless（PyODPS）

    无 active datasource 或 config 缺失 → 直接失败，不退化到环境变量。
    所有路径均由 :func:`_guard_sql` 前置拦截，仅允许只读 SQL。

    兜底:若错误是 view-not-found 且 SQL 含 ``cm_view.``,用 :Dataset)-[:CONTAINS_TABLE]->(:Table)
    把 ``cm_view.X`` 重写为物理表后重试一次(只重试 1 次,不递归)。
    Multi/zero parent 时放弃兜底，抛回原始错误。

    失败时把异常文字塞 ``error``，不抛。
    """
    sql_clean = sql.strip().rstrip(";")

    reject_reason = _guard_sql(sql_clean)
    if reject_reason:
        log.warning("SQL blocked by guard: %s | reason: %s", sql_clean[:200], reject_reason)
        return ExecResult(sql=sql, error=f"[安全拦截] {reject_reason}")

    # 当前生效的数据源：显式传的 datasource_id 优先，否则用用户切换的 synced default
    from .datasource_active_api import get_synced_default_datasource_id
    active_ds = (datasource_id or "").strip() or get_synced_default_datasource_id()

    backend = _resolve_backend_for_datasource(active_ds)
    log.debug("execute_sql backend=%s datasource_id=%s active_ds=%s", backend, datasource_id, active_ds)

    # 连库凭证只来自 semantic_config.db，没有选中的数据源一律失败
    if not active_ds:
        return ExecResult(
            sql=sql_clean,
            error="未指定当前数据源：请先通过 PUT /api/datasources/active 切换数据源",
        )

    def _dispatch(s: str) -> ExecResult:
        if backend == "unknown":
            return ExecResult(
                sql=s,
                error=f"datasource_id={active_ds!r} 无法解析执行后端："
                f"semantic_config.db 无该数据源的 config 或类型不支持",
            )
        if backend == "odps_akless":
            return _execute_sql_via_odps_akless(s, max_rows=max_rows, datasource_id=active_ds)
        if backend == "pymysql_direct":
            return _execute_sql_via_pymysql(s, max_rows=max_rows, datasource_id=active_ds)
        if backend == "direct":
            return _execute_sql_via_psycopg(
                s, max_rows=max_rows, datasource_id=active_ds,
            )
        return ExecResult(
            sql=s,
            error=f"不支持的 SQL 执行后端: {backend}（请检查数据源类型配置）",
        )

    first = _dispatch(sql_clean)

    # --- view-not-found fallthrough ---
    # 两条路径，互斥触发（按优先级）：
    #
    # 1) Legacy: 旧 prompt 仍引用 cm_view.<dataset>（view 已迁到 public schema）。
    #    只在 SQL 显式含 "cm_view." 前缀时触发改写。
    #
    # 2) View 未建: materialize 还没跑 / 跑失败 / 新 import 后还没触发，
    #    agent 写 SELECT ... FROM view_xxx 报 "does not exist"。
    #    从错误消息提取表名，查图匹配 :Dataset，单 parent 时改写为物理表重试。
    if first.error and is_view_not_found_error(first.error):
        rewritten: str | None = None

        # 路径 1: cm_view.<name> legacy 前缀
        _LEGACY_CM_VIEW_PREFIX = "cm_view."
        if _LEGACY_CM_VIEW_PREFIX in sql_clean.lower():
            try:
                refs = extract_cm_view_refs(sql_clean)
                parents = _resolve_dataset_parents(refs)
                rewritten = rewrite_cm_view_to_physical(sql_clean, parents)
            except FallthroughGiveUp as exc:
                log.info("view_fallthrough=skipped reason=%s", exc)
                return first

        # 路径 2: 裸名 view_xxx（view 未建）
        if rewritten is None:
            missing_name = extract_missing_table_name(first.error)
            if missing_name:
                parents = _resolve_dataset_parents({missing_name})
                tables = parents.get(missing_name) or []
                if len(tables) == 1:
                    rewritten = rewrite_dataset_to_physical(
                        sql_clean, missing_name, tables[0],
                    )
                else:
                    log.info(
                        "view_fallthrough=skipped reason=dataset '%s' has %d parents; cannot fallthrough",
                        missing_name, len(tables),
                    )
                    return first

        if rewritten is not None:
            log.info("view_fallthrough=true original_error=%s", first.error[:200])
            retry = _dispatch(rewritten)
            if retry.error:
                log.warning("view_fallthrough_failed=true rewritten_error=%s", retry.error[:200])
            return retry

    return first


# ---------------------------------------------------------------------- #
# 启发式：找可疑 sentinel 列
# ---------------------------------------------------------------------- #
SENTINEL_TOKENS = {
    "全部", "总计", "合计", "全网", "全行业", "全终端",
    "all", "total", "overall", "all_terminals", "_ALL_",
    "*", "全",
}

# Column 类型里被认为是"低基数维度列"的（数值/时间不算）。
_DIM_TYPE_RE = re.compile(
    r"^(varchar|char|text|bpchar|character|enum|boolean|smallint|int)",
    flags=re.IGNORECASE,
)


def _extract_main_table(sql: str) -> Optional[str]:
    """从 SQL 抽出 ``FROM <schema.table>``（取第一个）。"""
    m = re.search(
        r"\bfrom\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*|[a-zA-Z_][\w]*)",
        sql, flags=re.IGNORECASE,
    )
    return m.group(1) if m else None


def _filtered_columns(sql: str) -> set[str]:
    """SQL 的 WHERE / GROUP BY / HAVING 中出现的列名（粗粒度，足够过滤已 covered 的列）。"""
    s = sql.lower()
    # 取 WHERE … (group by / order by / having / limit) 之间的字段
    parts: list[str] = []
    where = re.search(r"\bwhere\b(.+?)(\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|$)",
                      s, flags=re.IGNORECASE | re.DOTALL)
    if where:
        parts.append(where.group(1))
    gb = re.search(r"\bgroup\s+by\b(.+?)(\border\s+by\b|\bhaving\b|\blimit\b|$)",
                   s, flags=re.IGNORECASE | re.DOTALL)
    if gb:
        parts.append(gb.group(1))
    text = " ".join(parts)
    cols = set(re.findall(r"\b([a-z_][\w]*)\b", text))
    # 剔除常见 SQL 关键字
    cols -= {
        "and", "or", "not", "in", "is", "null", "between", "like", "exists",
        "select", "from", "where", "group", "by", "order", "having", "limit",
        "case", "when", "then", "else", "end", "as", "on", "join", "left",
        "right", "inner", "full", "cross", "outer", "distinct",
    }
    return cols


def _is_dim_like(col_props: dict) -> bool:
    if col_props.get("is_partition"):
        # 分区列哪怕有 sentinel 我们也跳过：分区谓词通常已经显式锁定了
        return False
    dtype = (col_props.get("type") or "").lower()
    if not _DIM_TYPE_RE.match(dtype):
        return False
    return True


def _list_table_columns(driver: Driver, table_qualified: str) -> tuple[Optional[str], list[dict]]:
    """从 Neo4j 拿 ``schema.table`` 对应的 Column 清单与 ``Table.key``。

    优先按 ``schema/name`` 严格匹配，避免不同 db 撞上。
    """
    if "." in table_qualified:
        sch, tbl = table_qualified.split(".", 1)
    else:
        sch, tbl = "public", table_qualified
    cypher = """
    MATCH (t:Table {schema: $sch, name: $tbl})
    OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column)
    RETURN t.key AS table_key, collect(c {.*}) AS cols
    """
    with neo4j_session(driver) as s:
        rec = s.run(cypher, sch=sch, tbl=tbl).single()
    if not rec or rec["table_key"] is None:
        return None, []
    return rec["table_key"], list(rec["cols"] or [])


def _distinct_values(table_qualified: str, column: str, *, limit: int = 30) -> list[Any]:
    """拿一列的 distinct values（top ``limit``，按计数降序）。失败返回 []。

    连接使用当前 active datasource（semantic_config.db sync 的 config）。
    """
    from .datasource_active_api import get_synced_default_datasource_id

    active_ds = get_synced_default_datasource_id()
    if not active_ds:
        log.warning("distinct query skipped: no active datasource")
        return []

    sql = (
        f"SELECT {column}, count(*) AS c FROM {table_qualified} "
        f"GROUP BY {column} ORDER BY c DESC LIMIT {int(limit)}"
    )
    try:
        kwargs = _pg_connect_kwargs(datasource_id=active_ds)
        with psycopg.connect(
            **kwargs,
            autocommit=True,
            connect_timeout=5,
        ) as conn, conn.cursor() as cur:
            cur.execute("SET statement_timeout = 5000")
            cur.execute(sql)
            rows = cur.fetchall()
        return [r[0] for r in rows]
    except Exception as exc:
        log.warning("distinct query failed for %s.%s: %s", table_qualified, column, exc)
        return []


def _match_sentinel(values: list[Any]) -> Optional[tuple[Any, str]]:
    """如果 ``values`` 里有 sentinel 词，返回 ``(命中值, 原因)``。"""
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        # 1) 文本完全匹配
        if s in SENTINEL_TOKENS:
            return v, "rollup_word_match"
        # 2) 大小写不敏感的英文 token
        if s.lower() in {t.lower() for t in SENTINEL_TOKENS if t.isascii()}:
            return v, "rollup_word_match"
    return None


def detect_sentinel_risks(driver: Driver, sql: str) -> list[SentinelRisk]:
    """给一条 SQL 跑 sentinel 检查；返回 0..N 条 :class:`SentinelRisk`。"""
    main_table = _extract_main_table(sql)
    if not main_table:
        return []
    table_key, cols = _list_table_columns(driver, main_table)
    if not table_key or not cols:
        return []
    filtered = _filtered_columns(sql)
    risks: list[SentinelRisk] = []
    for col in cols:
        cname = (col.get("name") or "").lower()
        if not cname or cname in filtered:
            continue
        if not _is_dim_like(col):
            continue
        values = _distinct_values(main_table, col["name"], limit=30)
        if not values or len(values) > 50:
            # 高基数列不像有 rollup sentinel；跳过省时间
            continue
        hit = _match_sentinel(values)
        if not hit:
            continue
        sentinel_value, reason = hit
        # 构造建议：默认 != 'sentinel'。字符串值加单引号。
        if isinstance(sentinel_value, str):
            esc = sentinel_value.replace("'", "''")
            suggested = f"{col['name']} != '{esc}'"
        else:
            suggested = f"{col['name']} != {sentinel_value}"
        risks.append(SentinelRisk(
            column_key=col.get("key", ""),
            column_name=col.get("name", ""),
            table_key=table_key,
            table_name=col.get("table", main_table.split(".")[-1]),
            distinct_values=values,
            sentinel_value=sentinel_value,
            sentinel_reason=reason,
            suggested_filter=suggested,
        ))
    return risks


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #
def _jsonable(v: Any) -> Any:
    """把 PG 的 datetime / Decimal 等转成 JSON 友好的类型。"""
    import datetime as _dt
    from decimal import Decimal
    if v is None or isinstance(v, (str, int, float, bool, list, dict)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (_dt.date, _dt.datetime, _dt.time)):
        return v.isoformat()
    return str(v)


# ---------------------------------------------------------------------- #
# 并行 SQL 执行（避免 sync ODPS/JDBC 阻塞 FastAPI event loop）
# ---------------------------------------------------------------------- #
_sql_executor: ThreadPoolExecutor | None = None
_sql_executor_lock = threading.Lock()


def get_sql_executor() -> ThreadPoolExecutor:
    """Process-wide pool for blocking SQL backends (ODPS akless, JDBC, hub)."""
    global _sql_executor
    if _sql_executor is None:
        with _sql_executor_lock:
            if _sql_executor is None:
                workers = max(1, int(os.getenv("SQL_EXEC_WORKERS", "4")))
                _sql_executor = ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="cm-sql-exec",
                )
                log.info("SQL executor thread pool initialized: workers=%d", workers)
    return _sql_executor


async def execute_sql_async(
    sql: str,
    *,
    max_rows: int = 200,
    datasource_id: str = "",
    governor: BlockingIOGovernor | None = None,
) -> ExecResult:
    """Run :func:`execute_sql` in the SQL thread pool (non-blocking for asyncio)."""
    if governor is not None:
        return await governor.run(
            BlockingPool.SQL,
            "sql.execute",
            execute_sql,
            sql,
            max_rows=max_rows,
            datasource_id=datasource_id,
        )
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        get_sql_executor(),
        lambda: execute_sql(
            sql,
            max_rows=max_rows,
            datasource_id=datasource_id,
        ),
    )


__all__ = [
    "ExecResult",
    "SentinelRisk",
    "classify_pg_exec_signal",
    "execute_sql",
    "execute_sql_async",
    "get_sql_executor",
    "detect_sentinel_risks",
]
