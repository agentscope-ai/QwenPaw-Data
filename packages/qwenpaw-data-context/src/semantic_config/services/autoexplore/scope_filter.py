"""mg-088 自动探查：scope 配置的多值/模式语义（spec §1）。

命名锁（spec §1.4）：本卡一律 **explore_scope**——与 mgw6-scope 读侧过滤
（context_manager/api/datasource_scope.py）及身份 DatasourceScope
（datasource_active_api.py）严格区分。

本模块承载：
- $$$ 多值拆分/拼接（与 12-sheet 多值约定一致）；
- 默认排除规则常量（§1.3：``tmp%`` 前缀 + 日期后缀分片 token）；
- sample_mode 判定（on/off/auto；auto 对 odps/bigquery 判 off，裁决 Q1）。

LIKE 过滤 / 全名拼装 / table_limit 截断的纯函数面见本模块 :func:`filter_table_names`
等（spec §3.4/§3.3/§3.8，配测试 tests/test_mg088_autoexplore_pipeline.py）。
"""

from __future__ import annotations

MULTI_SEP = "$$$"

#: 保留排除 token：日期后缀分片表（正则 ``_\d{6,8}$``，§1.3-2）。
#: SQL LIKE 无法表达该正则，故以保留 token 存 exclude_patterns，过滤面特判。
DATE_SUFFIX_TOKEN = "@date_suffix"

#: 首次自动生成的默认排除规则（§1.3）：tmp% 前缀（profiling AlgoConfig 同构口径，
#: 仅引用不 import）+ 日期后缀分片族。
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = ("tmp%", DATE_SUFFIX_TOKEN)

#: sample_mode=auto 时默认关采样的数据源类型（registry 归一后键；裁决 Q1：
#: ODPS/BigQuery 真金白银扫描成本，默认 off）。其余类型 auto 判 on。
SAMPLING_OFF_TYPES = frozenset({"odps", "bigquery"})


def split_patterns(raw: str | None) -> list[str]:
    """$$$ 多值 → 去空白列表；空/None → []（= 该过滤面不生效）。"""
    if not raw:
        return []
    return [p.strip() for p in str(raw).split(MULTI_SEP) if p.strip()]


def join_patterns(values) -> str:
    """列表 → $$$ 多值串（空列表 → ""）。"""
    return MULTI_SEP.join(str(v) for v in values if str(v).strip())


def sampling_enabled(sample_mode: str | None, ds_type: str) -> bool:
    """sample_mode × 数据源类型 → 是否采样（§3.6 资格，Q1）。

    - on / off 直判；
    - auto：odps/bigquery → off，其余 on。
    """
    mode = (sample_mode or "auto").strip().lower()
    if mode == "on":
        return True
    if mode == "off":
        return False
    return ds_type not in SAMPLING_OFF_TYPES


# ---------------------------------------------------------------------- #
# 过滤纯函数（§3.4：schema 白名单 → include → exclude；内存面零额外 SQL）
# ---------------------------------------------------------------------- #
import re as _re  # noqa: E402

#: 日期后缀分片表（§1.3-2；与 profiling _PARTITION_SUFFIX 同形、独立实现，禁 import）
_DATE_SUFFIX_RE = _re.compile(r"_\d{6,8}$")

#: ODPS 族类型（registry 归一后键）：全名需探查侧补 project. 前缀（§3.3，Q2）
_ODPS_TYPES = frozenset({"odps"})


def like_to_regex(pattern: str) -> "_re.Pattern":
    """SQL LIKE → 正则（``%``→``.*``、``_``→``.``、其余字面量）。

    大小写不敏感——先例 profiling AlgoConfig「内存侧筛选，大小写不敏感」口径。
    """
    parts = []
    for ch in pattern:
        if ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(_re.escape(ch))
    return _re.compile("^" + "".join(parts) + "$", _re.IGNORECASE)


def _excluded(name: str, patterns: list[str]) -> bool:
    for p in patterns:
        if p == DATE_SUFFIX_TOKEN:
            if _DATE_SUFFIX_RE.search(name):
                return True
        elif like_to_regex(p).match(name):
            return True
    return False


def filter_manifest_tables(
    tables,
    *,
    schemas: list[str],
    include: list[str],
    exclude: list[str],
) -> list:
    """scope 过滤（顺序：schema 白名单 → include OR → exclude 任一命中）。

    空白名单/空 include = 该面不过滤；pattern 匹配**裸表名**（不带 schema），
    与 profiling AlgoConfig 先例同口径。
    """
    schema_set = {s.strip() for s in schemas if s and s.strip()}
    out = []
    for t in tables:
        if schema_set and t.schema not in schema_set:
            continue
        if include and not any(like_to_regex(p).match(t.name) for p in include):
            continue
        if exclude and _excluded(t.name, exclude):
            continue
        out.append(t)
    return out


# ---------------------------------------------------------------------- #
# 两段全名拼装（§3.3，W3 契约 Table.name = <ns>.<table>）
# ---------------------------------------------------------------------- #
def full_table_name(record, *, ds_type: str, project: str = "", db_id: str = "") -> str:
    """TableRecord → 两段全名。

    - universal（postgresql/mysql/sqlite）：connector 已产 ``schema.table``
      形态（record.schema + record.name）→ 直通；bigquery 同形（dataset.table）。
    - ODPS：connector 输出裸表名，探查侧补 ``project.`` 前缀（裁决 Q2；
      project 取连接配置单 project 字段，缺失回落 db_id，先例 ingest_odps
      ``ns = db_id or proj`` 的探查侧镜像）。
    """
    if ds_type in _ODPS_TYPES:
        ns = (project or "").strip() or (db_id or "").strip()
        return f"{ns}.{record.name}" if ns else record.name
    if record.schema:
        return f"{record.schema}.{record.name}"
    return record.name


# ---------------------------------------------------------------------- #
# table_limit 截断（§3.8：字典序稳定 + truncated 标记）
# ---------------------------------------------------------------------- #
def apply_table_limit(full_names: list[str], *, limit: int) -> tuple[list[str], bool]:
    """按字典序稳定排序后截断；返回 (保留列表, truncated)。"""
    ordered = sorted(full_names)
    if limit and limit > 0 and len(ordered) > limit:
        return ordered[:limit], True
    return ordered, False
