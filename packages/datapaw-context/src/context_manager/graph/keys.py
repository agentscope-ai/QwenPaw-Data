"""节点 ``key`` 构造器。

只做字符串拼接，不带任何 IO。约束：

- ``key`` 全局唯一，**MERGE 必须按 key 匹配**。
- 物理层 key 用 ``db.schema.table[.column]`` 形式（见 §2 表格）。
- 语义层 key 用 ``<domain>:<name>`` 形式；Caliber 用 ``<column_short>=<value>``。
- ``zone`` 是 §13 引入的多租户字段，本模块负责定义 4 个常量供其它阶段统一引用。
- v4: ``card_key`` 用于 ``Strategy`` 节点 key（``card:<digest>``）。

v3.1 通用化：``derive_layer`` / ``dataset_short`` 均接受 ``profile`` 注入，
不再硬编码 appdata 前缀。向后兼容：不传 profile 时行为与旧版完全一致。
"""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:
    from .profile import DatasetProfile

# ---------------------------------------------------------------------- #
# zone 常量（§13.1）
# ---------------------------------------------------------------------- #
METADATA_ZONE = "metadata"
TRACE_ZONE = "trace"
KNOWLEDGE_ZONE = "knowledge"
SHARED_ZONE = "_shared"

# ---------------------------------------------------------------------- #
# 默认值
# ---------------------------------------------------------------------- #
DEFAULT_DB_ID = "app_db"
DEFAULT_SCHEMA = "public"

# 向后兼容：旧 callers 可能仍用 _LAYER_PREFIXES（只读；勿写）
_LAYER_PREFIXES = (
    "ads",
    "dws",
    "dwd",
    "dim",
    "auto_eval",
    "eval_task",
)

# 公共数据集前缀，dataset_short 截断时用（§2 注脚）；仅在无 profile 时使用
_DATASET_PREFIX_RE = re.compile(
    r"^public\.(?:dws|dwd|ads|dim)_ty_(?P<domain>[a-z0-9_]+?)_(?P<rest>.+)$",
    re.IGNORECASE,
)


def derive_layer(
    table_name: str,
    profile: Optional["DatasetProfile"] = None,
) -> str:
    """从表名前缀推断 ``Table.layer``。

    ``profile`` 不为 None 时使用 ``profile.layer_prefixes``；
    否则使用内置 ``_LAYER_PREFIXES``（向后兼容）。

    例：``dws_ac_chat_overview_1d`` → ``dws``。
    """
    prefixes: Sequence[str] = profile.layer_prefixes if profile is not None else _LAYER_PREFIXES
    name = table_name.lower()
    for prefix in prefixes:
        if name.startswith(prefix + "_") or name == prefix:
            return prefix
    return "other"


# ---------------------------------------------------------------------- #
# 物理层 key
# ---------------------------------------------------------------------- #
def database_key(db: str, datasource_id: str = "") -> str:
    """``db:<name>``，``datasource_id`` 非空时为 ``db:<datasource_id>:<name>``。"""
    if datasource_id:
        return f"db:{datasource_id}:{db}"
    return f"db:{db}"


def schema_key(db: str, schema: str, datasource_id: str = "") -> str:
    """``sch:<db>.<name>``，``datasource_id`` 非空时为 ``sch:<datasource_id>:<db>.<name>``。"""
    if datasource_id:
        return f"sch:{datasource_id}:{db}.{schema}"
    return f"sch:{db}.{schema}"


def table_key(db: str, schema: str, table: str, datasource_id: str = "") -> str:
    """``tbl:<db>.<schema>.<name>``，``datasource_id`` 非空时前置为 ``tbl:<datasource_id>:...``。"""
    if datasource_id:
        return f"tbl:{datasource_id}:{db}.{schema}.{table}"
    return f"tbl:{db}.{schema}.{table}"


def column_key(db: str, schema: str, table: str, column: str, datasource_id: str = "") -> str:
    """``col:<db>.<schema>.<table>.<name>``，``datasource_id`` 非空时前置为 ``col:<datasource_id>:...``。"""
    if datasource_id:
        return f"col:{datasource_id}:{db}.{schema}.{table}.{column}"
    return f"col:{db}.{schema}.{table}.{column}"


# ---------------------------------------------------------------------- #
# 语义层 key
# ---------------------------------------------------------------------- #
def datasource_key(name: str) -> str:
    """``dsrc:<datasource_name>``。"""
    return f"dsrc:{name}"


def domain_key(name: str, datasource_id: str = "") -> str:
    """``dom:<name>``，``datasource_id`` 非空时为 ``dom:<datasource_id>:<name>``。"""
    if datasource_id:
        return f"dom:{datasource_id}:{name}"
    return f"dom:{name}"


def metric_key(domain: str, name: str, datasource_id: str = "") -> str:
    """``met:<domain>:<metric_name>``，``datasource_id`` 非空时前置。"""
    if datasource_id:
        return f"met:{datasource_id}:{domain}:{name}"
    return f"met:{domain}:{name}"


def dataset_short(
    table_qualified: str,
    domain: Optional[str] = None,
    profile: Optional["DatasetProfile"] = None,
) -> str:
    """计算 ``Formula`` 的 ``dataset_short``（§2 注脚）。

    ``profile`` 不为 None 且 ``profile.dataset_short_regex`` 不为 None 时，
    使用 profile 正则截断；否则使用内置 appdata 正则（向后兼容）。
    """
    regex_str = None
    if profile is not None and profile.dataset_short_regex is not None:
        regex_str = profile.dataset_short_regex
    if regex_str is None:
        # 向后兼容：使用内置 appdata 正则
        m = _DATASET_PREFIX_RE.match(table_qualified)
        if m:
            rest = m.group("rest")
            head = rest.split("_", 1)[0]
            return head or rest
    else:
        m = re.match(regex_str, table_qualified, re.IGNORECASE)
        if m:
            rest = m.group("rest") if "rest" in m.groupdict() else ""
            head = rest.split("_", 1)[0]
            return head or rest
    # 兜底：取最后一段表名
    parts = table_qualified.rsplit(".", 1)
    return parts[-1]


def logical_dataset_name(
    table_or_qualified: str,
    *,
    default_db: str = DEFAULT_DB_ID,
) -> str:
    """把物理表引用(qualified 或 bare)归一化为 Dataset 节点 name。

    接受 ``public.dws_ac_chat_overview_1d``、``app_db.public.view_xxx``
    或裸名 ``dws_*`` / ``view_*``。

    - 已带 ``view_`` 前缀(build 类 dataset)→ 原样返回
    - qualified 名 → 提取表名返回(不加 ``view_``)
    - 裸名 → 原样返回
    """
    raw = (table_or_qualified or "").strip()
    if not raw:
        return ""
    # Already has a view prefix — keep as-is (build-type filtered datasets)
    if raw.startswith("view_"):
        return raw
    if "." in raw:
        try:
            _, _, table = split_qualified_table(raw, default_db=default_db)
        except ValueError:
            table = raw.rsplit(".", 1)[-1]
    else:
        table = raw
    return table


def formula_key(
    domain: str,
    metric_name: str,
    dataset_short_value: str,
    date_range: str = "",
    datasource_id: str = "",
) -> str:
    """``fml:<domain>:<metric_name>:<dataset_short>[:<date_range>]``。

    ``date_range`` 区分同一 dataset 上 1d / 30d / 1m 等口径变体；为空时保持
    向后兼容（旧 key 形态）。``datasource_id`` 非空时前置。
    """
    suffix = f":{date_range}" if date_range else ""
    if datasource_id:
        return f"fml:{datasource_id}:{domain}:{metric_name}:{dataset_short_value}{suffix}"
    return f"fml:{domain}:{metric_name}:{dataset_short_value}{suffix}"


def dim_key(domain: str, name: str, datasource_id: str = "") -> str:
    """``dim:<domain>:<dim_name>``，``datasource_id`` 非空时前置。"""
    if datasource_id:
        return f"dim:{datasource_id}:{domain}:{name}"
    return f"dim:{domain}:{name}"


def dim_value_key(domain: str, dim_name: str, value: str, datasource_id: str = "") -> str:
    """``dimv:<domain>:<dim_name>:<value>``，``datasource_id`` 非空时前置。"""
    if datasource_id:
        return f"dimv:{datasource_id}:{domain}:{dim_name}:{value}"
    return f"dimv:{domain}:{dim_name}:{value}"


def operator_key(domain: Optional[str], name: str) -> str:
    """``op:<domain or _global>:<name>``。"""
    return f"op:{domain or '_global'}:{name}"


def dataset_key(domain: str, name: str, datasource_id: str = "") -> str:
    """``ds:<domain>:<name>``，``datasource_id`` 非空时为 ``ds:<datasource_id>:<domain>:<name>``。

    ``datasource_id`` 为空时保持旧格式 ``ds:<domain>:<name>``（向后兼容）。
    """
    if datasource_id:
        return f"ds:{datasource_id}:{domain}:{name}"
    return f"ds:{domain}:{name}"


def dataset_column_key(domain: str, dataset_name: str, col_name: str, datasource_id: str = "") -> str:
    """``dscol:<domain>.<dataset>.<col>`` — DatasetColumn 视图列节点 key。

    ``datasource_id`` 非空时为 ``dscol:<datasource_id>.<domain>.<dataset>.<col>``；
    为空时保持旧格式（向后兼容）。
    """
    if datasource_id:
        return f"dscol:{datasource_id}.{domain}.{dataset_name}.{col_name}"
    return f"dscol:{domain}.{dataset_name}.{col_name}"


def turn_key(session_id: str, ordinal: int) -> str:
    """``turn:<session_id>:<ordinal>``。"""
    return f"turn:{session_id}:{int(ordinal)}"


def experience_key(task_signature: str) -> str:
    """``exp:<sha1[:16]>`` — 由任务语义签名派生。"""
    h = hashlib.sha1((task_signature or "").encode()).hexdigest()[:16]
    return f"exp:{h}"


def tag_key(name: str) -> str:
    """``tag:<snake_case_name>`` — LLM-generated semantic tag."""
    safe = re.sub(r"[^a-z0-9_]+", "_", (name or "").lower()).strip("_")
    if not safe:
        safe = hashlib.sha1((name or "unknown").encode()).hexdigest()[:12]
    return f"tag:{safe}"


def caliber_key(domain: str, column_short: str, value: str, datasource_id: str = "") -> str:
    """``cal:<domain>:<column_short>=<value>``，``datasource_id`` 非空时前置。

    ``column_short`` 通常取 ``<table>.<column>`` 或 ``<column>``，由 caller 决定。
    """
    if datasource_id:
        return f"cal:{datasource_id}:{domain}:{column_short}={value}"
    return f"cal:{domain}:{column_short}={value}"


# ---------------------------------------------------------------------- #
# v4: Strategy key
# ---------------------------------------------------------------------- #
def card_key(
    task_signature: str,
    anchor_label_types: list[str],
    question_summary: str,
    *,
    graph_db_id: str = "",
    variant_suffix: str = "",
) -> str:
    """``card:<12-char digest>`` — v4 不再把 task_type 编入 key。

    ``task_signature`` 为任务语义签名（可为旧版 task_type 字符串以兼容调用方）。
    ``variant_suffix`` 用于区分 apply / avoid 等同签名下的不同卡（如 ``avoid``）。
    """
    anchor_str = ",".join(sorted(anchor_label_types))
    summary_str = question_summary[:120].strip()
    sig = (task_signature or "").strip()[:512]
    gdb = (graph_db_id or "").strip()
    vs = (variant_suffix or "").strip()
    parts = [gdb, sig, anchor_str, summary_str]
    if vs:
        parts.append(vs)
    payload = "|".join(parts)
    digest = hashlib.sha1(payload.encode()).hexdigest()[:12]
    return f"card:{digest}"


# ---------------------------------------------------------------------- #
# 工具
# ---------------------------------------------------------------------- #
def split_qualified_column(qualified: str, default_db: str = DEFAULT_DB_ID) -> tuple[str, str, str, str]:
    """把 ``<schema>.<table>.<column>`` 或 ``<db>.<schema>.<table>.<column>`` 拆开。

    metrics_dict.yaml 通常写 ``public.<table>.<column>``，缺省 db 时补 ``default_db``。
    返回 ``(db, schema, table, column)``，4 段。

    >>> split_qualified_column("public.dws_ac_chat_overview_1d.visit_usercnt_1d")
    ('app_db', 'public', 'dws_ac_chat_overview_1d', 'visit_usercnt_1d')
    """
    parts = qualified.split(".")
    if len(parts) == 4:
        db, schema, table, column = parts
    elif len(parts) == 3:
        schema, table, column = parts
        db = default_db
    elif len(parts) == 2:
        # 大概率是 dataset：<schema>.<table>，列名不在
        raise ValueError(f"missing column in qualified name: {qualified!r}")
    else:
        raise ValueError(f"unexpected qualified column: {qualified!r}")
    return db, schema, table, column


def split_qualified_table(qualified: str, default_db: str = DEFAULT_DB_ID) -> tuple[str, str, str]:
    """把 ``<schema>.<table>`` 或 ``<db>.<schema>.<table>`` 拆开。"""
    parts = qualified.split(".")
    if len(parts) == 3:
        db, schema, table = parts
    elif len(parts) == 2:
        schema, table = parts
        db = default_db
    elif len(parts) == 1:
        return default_db, DEFAULT_SCHEMA, parts[0]
    else:
        raise ValueError(f"unexpected qualified table: {qualified!r}")
    return db, schema, table


__all__ = [
    "DEFAULT_DB_ID",
    "DEFAULT_SCHEMA",
    "KNOWLEDGE_ZONE",
    "METADATA_ZONE",
    "SHARED_ZONE",
    "TRACE_ZONE",
    "caliber_key",
    "card_key",
    "column_key",
    "database_key",
    "datasource_key",
    "dataset_column_key",
    "dataset_key",
    "dataset_short",
    "logical_dataset_name",
    "derive_layer",
    "dim_key",
    "dim_value_key",
    "domain_key",
    "experience_key",
    "formula_key",
    "metric_key",
    "operator_key",
    "schema_key",
    "split_qualified_column",
    "split_qualified_table",
    "table_key",
    "tag_key",
    "turn_key",
]
