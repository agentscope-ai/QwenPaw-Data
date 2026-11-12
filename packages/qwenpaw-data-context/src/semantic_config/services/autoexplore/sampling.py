"""mg-088 自动探查：sample_values 抽样（§3.6：条数/类型白名单/注入防护/静默降级）。

SQL 形态先例 ``graph/physical.py::_sample_pg_column_values``（每列一条
``SELECT DISTINCT ... IS NOT NULL LIMIT n``）；本卡经 connector.execute_sql
统一走（不新开连接），注入防护第一层 = 标识符引号包裹 + 数据值永不拼进 SQL 文本。

预算：整轮采样预算（SAMPLE_BUDGET_SECS=300，AlgoConfig enums 预算同值）在管线层
传入；单语句 statement_timeout 3s 仅 PG 族（随 SQL 前缀下发，先例同值；驱动不
支持多语句时 execute_sql 返回 error → 静默降级空样本，不伤元数据主链）。
"""

from __future__ import annotations

import logging
import time

from .sanitizer import sanitize_auto_samples

log = logging.getLogger("semantic_config.autoexplore.sampling")

#: 反引号标识符族（其余走 ANSI 双引号）
_BACKTICK_TYPES = frozenset({"mysql", "odps", "bigquery"})

#: 标识符中不可转义的字符（出现即拒绝采样该列——静默降级，绝不放行进 SQL）
_IDENT_FORBIDDEN = "\r\n\0"

#: 类型白名单外的阻断词：大对象/半结构化/时空/浮点精度族（防大值噪声/驱动转换炸）
_SAMPLE_TYPE_BLOCKLIST = (
    "blob",
    "binary",
    "bytea",
    "json",
    "array",
    "decimal",
    "numeric",
    "double",
    "float",
    "real",
    "geometry",
    "geography",
    "point",
    "date",
    "time",
    "interval",
    "map<",
    "struct<",
    "uuid",
    "money",
)

#: 类型白名单：text/varchar/char/enum 及 int 族小类型
_SAMPLE_TYPE_ALLOWLIST = ("text", "varchar", "char", "string", "enum", "int")


def connector_can_sample(connector) -> bool:
    """SqlExecutable 能力探测（callable 检查，Fake/降级场景安全）。"""
    return callable(getattr(connector, "execute_sql", None))


def is_samplable_type(data_type) -> bool:
    """只对 text/varchar/char/enum 及 int 族小类型抽样（§3.6 类型白名单）。"""
    tl = str(data_type or "").strip().lower()
    if not tl:
        return False
    if any(b in tl for b in _SAMPLE_TYPE_BLOCKLIST):
        return False
    return any(a in tl for a in _SAMPLE_TYPE_ALLOWLIST)


def quote_ident(name: str, ds_type: str) -> str:
    """标识符引号包裹（注入防护第一层；数据值永不拼进 SQL 文本）。

    ANSI 双引号族：``"`` → ``""``；反引号族：`` ` `` → ```` `` ````。
    含换行/NUL 的标识符无法转义 → ValueError（调用方静默跳过该列）。
    """
    if any(ch in name for ch in _IDENT_FORBIDDEN):
        raise ValueError(f"标识符含不可转义字符，拒绝采样: {name!r}")
    if ds_type in _BACKTICK_TYPES:
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def _statement_timeout_prefix(ds_type: str) -> str:
    """PG 族单语句 3s 超时（先例 physical.py:140 同值）；其余类型不加。"""
    if ds_type == "postgresql":
        return "SET statement_timeout = '3000'; "
    return ""


def build_sample_sql(full_name: str, col_name: str, *, limit: int, ds_type: str) -> str:
    """每列一条的采样 SQL（标识符来自源库自身元数据，逐段引号包裹）。"""
    segments = [quote_ident(seg, ds_type) for seg in full_name.split(".")]
    table_ref = ".".join(segments)
    col_q = quote_ident(col_name, ds_type)
    return (
        f"{_statement_timeout_prefix(ds_type)}"
        f"SELECT DISTINCT {col_q} FROM {table_ref} "
        f"WHERE {col_q} IS NOT NULL LIMIT {int(limit)}"
    )


def sample_columns(
    connector,
    full_name: str,
    columns,
    *,
    sample_limit: int,
    ds_type: str,
    budget_secs: float,
) -> dict[str, list[str]]:
    """逐列抽样 ≤sample_limit 个 distinct 非空值 → {列名: 净化后样本}。

    静默降级：任何失败（SQL 报错/超时/标识符异常）该列空列表，不抛；
    预算耗尽即停（已采的保留，未采的不再发 SQL）。非 SqlExecutable
    connector → 空 dict（元数据照常入图，§5.1-⑦）。
    """
    out: dict[str, list[str]] = {}
    if not connector_can_sample(connector):
        return out
    deadline = time.monotonic() + max(budget_secs, 0)
    for col in columns:
        if time.monotonic() >= deadline:
            log.info("autoexplore sampling budget exhausted at table=%s", full_name)
            break
        if not is_samplable_type(col.type):
            continue
        try:
            sql = build_sample_sql(
                full_name, col.name, limit=sample_limit, ds_type=ds_type
            )
        except ValueError:
            continue
        try:
            res = connector.execute_sql(sql, max_rows=sample_limit)
        except Exception as exc:  # noqa: BLE001 — 采样失败是预期结果（静默降级）
            log.debug(
                "autoexplore sample failed table=%s col=%s: %s",
                full_name,
                col.name,
                exc,
            )
            out[col.name] = []
            continue
        if getattr(res, "error", None):
            out[col.name] = []
            continue
        vals = [r[0] for r in (res.rows or []) if r and r[0] is not None]
        out[col.name] = sanitize_auto_samples(vals)
    return out
