"""Excel 导入：解析 12-sheet xlsx（规范 v2.1）→ 按 sheet 批量 upsert 语义配置表。

列契约唯一权威源 = `semantic_config.template_contract`（Task A 交付）：本模块
不再维护任何 sheet/列定义，`IMPORT_ORDER` 驱动导入顺序与已知 sheet 集合，
`SHEET_SPECS` 驱动必填校验与表头校验。

写入以 sheet 为单位批量化：每个 sheet 一次 SELECT 取现存自然键、一条多行
INSERT、一条批量 UPDATE，父表写完后再一次 SELECT 回读「自然键 → id」映射供
子 sheet 引用。逐行版本每行要 4~6 次往返，在远端 Hologres 上是数量级差异。

方言约束：`tables.py` 中所有唯一索引都只在 SQLite 生成（Hologres 不支持
UNIQUE 与任意 CREATE INDEX），故不能用 ``INSERT ... ON CONFLICT DO UPDATE``；
改为「批量取现存行 → 内存分流 insert/update」，SQLite 与 Hologres 共用一套代码。
自然键含可空列时，比较发生在 Python 字典里，无需 SQL 侧的 NULL 安全比较。

sheet 名归一（应用层，不建别名表）：等价的历史/新命名在 ``_normalize_sheet_name``
收口对接到契约正名（domain→biz_domain、dataset_field→dataset_column、
dimension_binding→dataset_dimension、metric_caliber→metric_formula；datasource /
_meta 忽略）。同一逻辑表的两种写法在同一工作簿同时出现 → 冲突硬失败（防歧义）。

物理层（table / table_column / physical_join）现均落库，以 id 互相绑定：
- ``table``（physical_table）：upsert 键 (datasource_id, table_name)。
- ``table_column``：不存 table_name，按 table_name 解析 physical_table.id 落 ``table_id``；
  upsert 键 (table_id, column_name)。物理表改名不必刷列数据。
- ``physical_join``：left/right_table 名 → physical_table.id，left/right_columns 名
  （$$$ 多值）→ table_column.id（$$$ id 串），真正落库。

引用映射：
- domain：新协议子表以 ``domain_id``（= domain.import_id）引用，旧协议以
  ``domain_name`` 引用；两者都解析到 domain.id（``maps.domain`` 双键填充）。
  domain sheet 的 ``domain_id`` 写入 ``domain.import_id``，绝不写 id。
- dataset 父引用 = datasource_id（请求参数，存在性）+ domain；dataset_field 父引用 =
  dataset；dimension_binding 父引用 = dataset + dimension；metric_caliber 父引用 =
  metric + dataset；verified_query 父引用 = domain（upsert 键 (domain_id, question)）。

12 实体重构的导入侧口径（SPEC 00 §12.2）：
- 业务域是工作空间级对象（按 ``(ws, domain_name)`` upsert），维度/指标字典是域级
  （``(ws, domain_id, name)``），三者都**不写 datasource_id**；
- 数据集 upsert 键改为 ``(ws, datasource_id, dataset_name)``，数据源列统一命名
  ``datasource_id``（旧名 ``source_datasource`` 读入即忽略，归属一律取请求参数）；
- 口径行强制「数据集与所引业务域一致」，禁止跨域绑定（§9-Q7）；
- 导入末尾对本批涉及的业务域统一重算 ``datasource_scope``（§4.2）。

转换规则：模板布尔 1/0 → 存储 'Y'/'N'（④a，is_default 例外：1/0 → Integer，
空 → NULL 不兜底 ⑦a）；$$$ 多值与 value_example 原样存、保序不拆（D1/约束 4，
频次排序已在模板面完成）；expr 按模板内容原样存（D5：prose 行合法不校验形态）。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from qwenpaw_data_auth import Principal
from openpyxl import load_workbook
from sqlalchemy import bindparam, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.errors import BadRequestError
from semantic_config.principal import require_admin
from semantic_config.services.dimension_service import validate_priority
from semantic_config.services.domain_scope import recompute_domain_scopes
from semantic_config.tables import SC_TABLES
from semantic_config.template_contract import (
    IMPORT_ORDER,
    MULTI_VALUE_SEPARATOR,
    SHEET_SPECS,
    canonicalize_sheet_name,
)

MAX_BYTES = 20 * 1024 * 1024  # SPEC 01：20 MB

#: sheet 名归一（应用层收口，不建别名表）：历史/新命名 → 契约正名。
#: 只映射与正名不同的等价写法；正名本身直接命中 SHEET_SPECS，无需登记。
_SHEET_NAME_ALIASES = {
    "domain": "biz_domain",
    "dataset_field": "dataset_column",
    "dimension_binding": "dataset_dimension",
    "metric_caliber": "metric_formula",
}

#: 显式忽略的 sheet（数据源由请求参数绑定；_meta 为元信息）。
_IGNORED_SHEET_TITLES = {"datasource", "_meta"}


def _normalize_sheet_name(title: str) -> str | None:
    """把工作簿 sheet 名归一到契约正名；忽略/未知 → None。"""
    if title in _IGNORED_SHEET_TITLES:
        return None
    return canonicalize_sheet_name(_SHEET_NAME_ALIASES.get(title, title))


#: domain 引用列互认组：新协议子表用 domain_id（=import_id）、旧协议用 domain_name；
#: 契约按 domain_name 标必填，此处让组内任一列存在即视为满足（header 与逐行校验共用）。
_DOMAIN_REF_COLS = ("domain_name", "domain_id")


def _required_missing_in_header(spec, headers: list[str]) -> list[str]:
    """契约必填列在 header 中的缺失项；domain 引用列组内任一存在即满足。"""
    missing: list[str] = []
    for c in spec.columns:
        if not c.required or c.name in headers:
            continue
        if c.name in _DOMAIN_REF_COLS and any(x in headers for x in _DOMAIN_REF_COLS):
            continue
        missing.append(c.name)
    return missing


# 单条 DML 携带的行数上限，与 holo_age_backend 的批大小对齐；IN 列表分块避免
# 超长参数列表（asyncpg 单语句参数有上限）。
_WRITE_CHUNK = 300
_IN_CHUNK = 500

#: 已知 sheet = 契约 12 sheet（未知 sheet 如 _meta / 旧 8-sheet 名跳过，约束 1）。
KNOWN_SHEETS = list(IMPORT_ORDER)

#: summary 键 = 契约 sheet 名（S7）；保留常量供消费方引用。
SUMMARY_KEY = {name: name for name in IMPORT_ORDER}


@dataclass(frozen=True)
class _Ctx:
    """当前导入的租户归属与审计身份（提交时从 principal 捕获，贯穿整次导入）。"""

    tenant_id: str
    workspace_id: str
    user_id: str
    datasource_id: str | None = None


class _RowError(Exception):
    def __init__(self, sheet: str, row: int, message: str):
        self.sheet = sheet
        self.row = row
        self.message = message


def _norm(v) -> str | None:
    """空单元格 / '\\N' -> None；数值转字符串；其余去空白。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return None if s in ("", "\\N") else s
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _to_int(v) -> int | None:
    s = _norm(v)
    if s is None:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _to_yn(v, default: str) -> str:
    """is_primary/is_nullable：Excel 0/1 -> 'Y'/'N'；空/非法 -> 默认（④a）。"""
    s = _norm(v)
    if s is None:
        return default
    if s in ("1", "Y", "y"):
        return "Y"
    if s in ("0", "N", "n"):
        return "N"
    return default


def _dataset_source_is_sql(source: str | None) -> bool:
    """②b：dataset.source 内容自判——SELECT/WITH 起头 = SQL，否则 = 表名。"""
    if not source:
        return False
    head = source.lstrip().upper()
    return head.startswith("SELECT") or head.startswith("WITH")


def _read_sheets(content: bytes) -> tuple[dict[str, list[dict]], list[dict]]:
    """解析 xlsx -> ({canonical_sheet: [row_dict, ...]}, header 级错误)。

    仅保留 SPEC 01 产品 sheet（正名）；多余 sheet 忽略。header 缺必填列
    的 sheet 记一条 sheet 级错误（row=1），其行不再逐行解析。
    """
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise BadRequestError("解析失败 / 不是有效 xlsx") from e

    result: dict[str, list[dict]] = {}
    errors: list[dict] = []
    seen_titles: dict[str, str] = {}  # canonical -> 原始 sheet 名（冲突检测）
    for ws in wb.worksheets:
        canonical = _normalize_sheet_name(ws.title)
        if canonical is None:
            continue
        # 同一逻辑表的两种写法（如 dataset_field 与 dataset_column）同时出现 → 硬失败。
        if canonical in seen_titles and seen_titles[canonical] != ws.title:
            errors.append(
                {
                    "sheet": ws.title,
                    "row": 1,
                    "message": (
                        f"sheet 名冲突：{ws.title} 与 {seen_titles[canonical]} "
                        f"指向同一逻辑表 {canonical}，请只保留其一"
                    ),
                }
            )
            continue
        seen_titles[canonical] = ws.title
        spec = SHEET_SPECS[canonical]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = next(rows_iter)
        except StopIteration:
            continue
        headers = [(_norm(h) or "") for h in header]
        missing = _required_missing_in_header(spec, headers)
        if missing:
            errors.append(
                {
                    "sheet": canonical,
                    "row": 1,
                    "message": f"缺少必填列: {', '.join(missing)}",
                }
            )
            continue
        data = []
        for i, raw in enumerate(rows_iter, start=2):
            if raw is None or all(c is None for c in raw):
                continue
            row = {
                headers[j]: raw[j] if j < len(raw) else None
                for j in range(len(headers))
            }
            row["_row"] = i
            data.append(row)
        result[canonical] = data
    wb.close()
    return result, errors


# ---------------- 引用映射（一次查库建表，写完父 sheet 后刷新） ----------------
@dataclass
class _Maps:
    """自然键 → id 的内存映射，取代逐行查库解析外键。

    - ``referenced_ds`` / ``referenced_domains``：整份文件出现过的数据源编码 /
      模板 domain_id 值，作为各映射的查询范围（支持部分重导：父行只在库里也能解析）。
    - ``tables_declared``：table sheet（P2）声明集；None = 本次工作簿无 table
      sheet，跳过 P2 门控（S1，容忍部分导入）。
    - ``domain``：存储 domain_name 自然键 → id（①a，模板 domain_id 列映射于此）。
    """

    referenced_ds: list[str] = field(default_factory=list)
    referenced_domains: list[str] = field(default_factory=list)
    referenced_tables: list[str] = field(default_factory=list)
    datasource_id: str | None = None
    ds_codes: set[str] = field(default_factory=set)
    tables_declared: set[tuple] | None = None
    # domain：import_id 与 domain_name 双键 → biz_domain.id（新协议走 import_id，旧协议走名称）。
    domain: dict[str, int] = field(default_factory=dict)
    physical_table: dict[str, int] = field(
        default_factory=dict
    )  # table_name -> physical_table.id
    table_column: dict[tuple, int] = field(
        default_factory=dict
    )  # (table_id, column_name) -> id
    dataset: dict[tuple, int] = field(
        default_factory=dict
    )  # (datasource_id, dataset_name)
    dataset_domain: dict[int, int | None] = field(
        default_factory=dict
    )  # dataset_id -> domain_id
    prior_dataset_domains: set[int] = field(
        default_factory=set
    )  # 写前归属域，供 scope 重算
    dimension: dict[tuple, int] = field(
        default_factory=dict
    )  # (domain_id, dimension_name)
    metric: dict[tuple, int] = field(default_factory=dict)  # (domain_id, metric_name)


async def _load_keyed_ids(
    db: AsyncSession,
    table: str,
    nk: Sequence[str],
    first_values: Iterable[Any],
    ctx: _Ctx,
) -> dict[tuple, int]:
    """批量取「自然键元组 → id」。按自然键首列 IN 过滤，其余列在内存比较。

    既用于加载父表引用映射，也用于取目标表现存行以分流 insert / update。
    """
    t = SC_TABLES[table]
    values = [v for v in dict.fromkeys(first_values) if v is not None]
    out: dict[tuple, int] = {}
    if not values:
        return out
    key_cols = [t.c[c] for c in nk]
    # 带 datasource_id 列的表按请求数据源收窄；domain / 字典表无该列（已解耦），
    # 天然按工作空间 / 域解析（SPEC 00 §12.2）。
    extra = []
    if ctx.datasource_id and "datasource_id" in t.c:
        extra.append(t.c.datasource_id == ctx.datasource_id)
    for i in range(0, len(values), _IN_CHUNK):
        res = await db.execute(
            select(t.c.id, *key_cols).where(
                t.c[nk[0]].in_(values[i : i + _IN_CHUNK]),
                t.c.tenant_id == ctx.tenant_id,
                t.c.workspace_id == ctx.workspace_id,
                t.c.is_deleted == 0,
                *extra,
            )
        )
        for rid, *key in res:
            out[tuple(key)] = int(rid)
    return out


async def _refresh(db: AsyncSession, maps: _Maps, name: str, ctx: _Ctx) -> None:
    """重载单个映射，使后续 sheet 能看到本事务内刚写入的父行。"""
    if name == "ds_codes":
        found = await _load_keyed_ids(
            db, "datasource", ("datasource_id",), maps.referenced_ds, ctx
        )
        maps.ds_codes = {k[0] for k in found}
    elif name == "domain":
        # 双键：domain_name（旧协议引用）与 import_id（新协议引用）都指向同一 domain.id。
        by_name = await _load_keyed_ids(
            db, "domain", ("domain_name",), maps.referenced_domains, ctx
        )
        by_import = await _load_keyed_ids(
            db, "domain", ("import_id",), maps.referenced_domains, ctx
        )
        maps.domain = {k[0]: v for k, v in by_name.items()}
        maps.domain.update({k[0]: v for k, v in by_import.items()})
    elif name == "physical_table":
        found = await _load_keyed_ids(
            db, "physical_table", ("table_name",), maps.referenced_tables, ctx
        )
        maps.physical_table = {k[0]: v for k, v in found.items()}
    elif name == "table_column":
        maps.table_column = await _load_keyed_ids(
            db,
            "table_column",
            ("table_id", "column_name"),
            maps.physical_table.values(),
            ctx,
        )
    elif name == "dataset":
        # 数据集唯一键改为「同数据源内名唯一」，故按 (datasource_id, dataset_name) 建索引。
        maps.dataset = await _load_keyed_ids(
            db, "dataset", ("datasource_id", "dataset_name"), sorted(maps.ds_codes), ctx
        )
        maps.dataset_domain = await _load_dataset_domains(
            db, maps.dataset.values(), ctx
        )
    elif name == "dimension":
        maps.dimension = await _load_keyed_ids(
            db,
            "dimension_dict",
            ("domain_id", "dimension_name"),
            maps.domain.values(),
            ctx,
        )
    elif name == "metric":
        maps.metric = await _load_keyed_ids(
            db, "metric_dict", ("domain_id", "metric_name"), maps.domain.values(), ctx
        )


async def _load_dataset_domains(
    db: AsyncSession, dataset_ids: Iterable[int], ctx: _Ctx
) -> dict[int, int | None]:
    """dataset_id → domain_id，供口径 sheet 的「dataset 与字典同域」强校验（§9-Q7）。"""
    t = SC_TABLES["dataset"]
    ids = [i for i in dict.fromkeys(dataset_ids) if i is not None]
    out: dict[int, int | None] = {}
    for i in range(0, len(ids), _IN_CHUNK):
        res = await db.execute(
            select(t.c.id, t.c.domain_id).where(
                t.c.id.in_(ids[i : i + _IN_CHUNK]),
                t.c.tenant_id == ctx.tenant_id,
                t.c.workspace_id == ctx.workspace_id,
                t.c.is_deleted == 0,
            )
        )
        for rid, dom in res:
            out[int(rid)] = dom
    return out


async def _bootstrap_maps(
    db: AsyncSession, sheets: dict[str, list[dict]], ctx: _Ctx
) -> _Maps:
    referenced_ds: set[str] = set()
    referenced_domains: set[str] = set()
    referenced_tables: set[str] = set()
    for rows in sheets.values():
        for r in rows:
            if code := _norm(r.get("datasource_id")):
                referenced_ds.add(code)
            # domain 引用：domain_name 与 domain_id（import_id）都收集，供双键加载。
            if dn := _norm(r.get("domain_name")):
                referenced_domains.add(dn)
            if di := _norm(r.get("domain_id")):
                referenced_domains.add(di)
            # 物理表引用：table / table_column / physical_join 出现的表名。
            for key in ("table_name", "left_table", "right_table"):
                if tn := _norm(r.get(key)):
                    referenced_tables.add(tn)
    if ctx.datasource_id:
        referenced_ds.add(ctx.datasource_id)
    maps = _Maps(
        referenced_ds=sorted(referenced_ds),
        referenced_domains=sorted(referenced_domains),
        referenced_tables=sorted(referenced_tables),
        datasource_id=ctx.datasource_id,
    )
    # physical_table 先于 table_column（后者按 physical_table.id 加载）。
    for name in (
        "ds_codes",
        "domain",
        "physical_table",
        "table_column",
        "dataset",
        "dimension",
        "metric",
    ):
        await _refresh(db, maps, name, ctx)
    # 写前快照本数据源各数据集的归属域。数据集被改挂到别的域时，**原**域的
    # datasource_scope 也必须重算（原域可能因此不再含本数据源），而它可能已经不在
    # 本工作簿里、不进 maps.domain。dataset sheet 写完后 _refresh 会把
    # maps.dataset_domain 覆盖成迁移后的值，所以必须在这里存一份。
    maps.prior_dataset_domains = {
        d for d in maps.dataset_domain.values() if d is not None
    }
    return maps


# ---------------- 逐行校验 / 引用解析（纯内存，不查库） ----------------
def _check_required(sheet: str, r: dict) -> None:
    """契约驱动的必填校验（ColumnSpec.required）；首个缺失列即报。

    domain 引用列（domain_name/domain_id）互认：组内任一有值即视为满足。
    """
    for c in SHEET_SPECS[sheet].columns:
        if not c.required or _norm(r.get(c.name)) is not None:
            continue
        if c.name in _DOMAIN_REF_COLS and any(
            _norm(r.get(x)) is not None for x in _DOMAIN_REF_COLS
        ):
            continue
        raise _RowError(sheet, r["_row"], f"必填列不能为空: {c.name}")


def _need_ds(maps: _Maps, r: dict, sheet: str, column: str = "datasource_id") -> str:
    """校验数据源编码存在于当前 tenant/ws，返回其对外编码。"""
    code = maps.datasource_id or _norm(r.get(column))
    if not code:
        raise _RowError(sheet, r["_row"], f"{column} 不能为空")
    if code not in maps.ds_codes:
        raise _RowError(sheet, r["_row"], f"数据源不存在: {column}={code}")
    return code


def _need_domain(maps: _Maps, r: dict, sheet: str) -> int:
    """domain 引用 → biz_domain.id。新协议按 domain_id(=import_id)、旧协议按 domain_name。"""
    key = _norm(r.get("domain_id")) or _norm(r.get("domain_name"))
    if not key:
        raise _RowError(sheet, r["_row"], "domain_id 不能为空")
    dom_id = maps.domain.get(key)
    if dom_id is None:
        raise _RowError(sheet, r["_row"], f"业务域不存在: domain_id={key}")
    return dom_id


def _need_dataset(maps: _Maps, dom_id: int, r: dict, sheet: str) -> int:
    """数据集引用 → dataset.id。按 (请求数据源, dataset_name) 解析。

    数据集唯一键已改为「同数据源内名唯一」，不再按业务域解析；但仍强制该数据集
    与本行引用的业务域一致，否则口径会跨域绑定（§9-Q7 定为强制，Excel 单源导出
    的一致性依赖此约束）。
    """
    name = _norm(r.get("dataset_name"))
    if not name:
        raise _RowError(sheet, r["_row"], "dataset_name 不能为空")
    ds_id = maps.dataset.get((maps.datasource_id, name))
    if ds_id is None:
        raise _RowError(sheet, r["_row"], f"数据集不存在: dataset_name={name}")
    owner = maps.dataset_domain.get(ds_id)
    if owner is not None and owner != dom_id:
        raise _RowError(
            sheet,
            r["_row"],
            f"数据集与引用的业务域不一致，禁止跨域绑定: dataset_name={name}",
        )
    return ds_id


def _need_dimension(maps: _Maps, dom_id: int, r: dict, sheet: str) -> int:
    name = _norm(r.get("dimension_name"))
    if not name:
        raise _RowError(sheet, r["_row"], "dimension_name 不能为空")
    dim_id = maps.dimension.get((dom_id, name))
    if dim_id is None:
        raise _RowError(sheet, r["_row"], f"维度不存在: dimension_name={name}")
    return dim_id


def _need_metric(maps: _Maps, dom_id: int, r: dict, sheet: str) -> int:
    name = _norm(r.get("metric_name"))
    if not name:
        raise _RowError(sheet, r["_row"], "metric_name 不能为空")
    mid = maps.metric.get((dom_id, name))
    if mid is None:
        raise _RowError(sheet, r["_row"], f"指标不存在: metric_name={name}")
    return mid


# ---------------- 各 sheet 行 → (自然键, 待写列)；None = 校验通过但不落库 ----------------
def _prep_table(maps: _Maps, r: dict) -> tuple[tuple, dict] | None:
    """P2 原始表：upsert 键 (datasource_id, table_name)；同时记声明集门控 L2 表式 source。"""
    code = _need_ds(maps, r, "table")
    table_name = _norm(r.get("table_name"))
    if maps.tables_declared is None:
        maps.tables_declared = set()
    maps.tables_declared.add((code, table_name))
    return (code, table_name), {
        "datasource_id": code,
        "table_name": table_name,
        "table_comment": _norm(r.get("table_comment")),
        "partition_keys": _norm(r.get("partition_keys")),
    }


def _prep_table_column(maps: _Maps, r: dict) -> tuple[tuple, dict] | None:
    """P3 原始列：按 table_name 解析 physical_table.id → table_id（不存 table_name）。"""
    code = _need_ds(maps, r, "table_column")
    table_name = _norm(r.get("table_name"))
    column_name = _norm(r.get("column_name"))
    table_id = maps.physical_table.get(table_name)
    if table_id is None:
        raise _RowError(
            "table_column",
            r["_row"],
            f"原始表未声明（table sheet）: table_name={table_name}",
        )
    return (table_id, column_name), {
        "datasource_id": code,
        "table_id": table_id,
        "column_name": column_name,
        "data_type": _norm(r.get("data_type")),
        "is_primary": _to_yn(r.get("is_primary"), "N"),
        "is_nullable": _to_yn(r.get("is_nullable"), "Y"),
        "column_comment": _norm(r.get("column_comment")),
        "sample_values": _norm(r.get("sample_values")),
    }


def _resolve_col_ids(
    maps: _Maps, table_id: int, raw: str | None, sheet: str, r: dict, col: str
) -> str:
    """把 $$$ 列名串解析成 $$$ table_column.id 串（保序）；缺列即报错。"""
    if not raw:
        raise _RowError(sheet, r["_row"], f"{col} 不能为空")
    ids: list[str] = []
    for name in raw.split(MULTI_VALUE_SEPARATOR):
        name = name.strip()
        if not name:
            continue
        cid = maps.table_column.get((table_id, name))
        if cid is None:
            raise _RowError(sheet, r["_row"], f"连接列不存在: {col}={name}")
        ids.append(str(cid))
    if not ids:
        raise _RowError(sheet, r["_row"], f"{col} 不能为空")
    return MULTI_VALUE_SEPARATOR.join(ids)


def _prep_physical_join(maps: _Maps, r: dict) -> tuple[tuple, dict] | None:
    """物理连接：表名 → physical_table.id、列名（$$$ 多值）→ table_column.id。"""
    code = _need_ds(maps, r, "physical_join")
    left_table = _norm(r.get("left_table"))
    right_table = _norm(r.get("right_table"))
    left_tid = maps.physical_table.get(left_table)
    if left_tid is None:
        raise _RowError(
            "physical_join", r["_row"], f"左表未声明（table sheet）: {left_table}"
        )
    right_tid = maps.physical_table.get(right_table)
    if right_tid is None:
        raise _RowError(
            "physical_join", r["_row"], f"右表未声明（table sheet）: {right_table}"
        )
    left_col_ids = _resolve_col_ids(
        maps, left_tid, _norm(r.get("left_columns")), "physical_join", r, "left_columns"
    )
    right_col_ids = _resolve_col_ids(
        maps,
        right_tid,
        _norm(r.get("right_columns")),
        "physical_join",
        r,
        "right_columns",
    )
    return (code, left_tid, left_col_ids, right_tid, right_col_ids), {
        "datasource_id": code,
        "left_table_id": left_tid,
        "left_column_ids": left_col_ids,
        "right_table_id": right_tid,
        "right_column_ids": right_col_ids,
        "join_type": _norm(r.get("join_type")) or "inner",
    }


def _prep_domain(maps: _Maps, r: dict) -> tuple[tuple, dict] | None:
    """业务域：upsert 键 domain_name（工作空间内唯一）；domain sheet 的 domain_id 落
    import_id（不碰 id）。业务域已与数据源解耦，不写 datasource_id——其数据源范围由
    ``datasource_scope`` 在导入末尾按数据集统一重算（SPEC 00 §4.2、§12.2）。"""
    domain_name = _norm(r.get("domain_name"))
    return (domain_name,), {
        "domain_name": domain_name,
        "description": _norm(r.get("description")),
        "import_id": _norm(r.get("domain_id")),
    }


def _prep_dataset(maps: _Maps, r: dict) -> tuple[tuple, dict] | None:
    """数据集：upsert 键 (datasource_id, dataset_name)；数据源列统一为 datasource_id。"""
    dom_id = _need_domain(maps, r, "dataset")
    ds_code = _need_ds(maps, r, "dataset")
    name = _norm(r.get("dataset_name"))
    source = _norm(r.get("source"))
    # S1：表式 source（②b 判定）必须挂到 P2 声明（仅当本次含 table sheet）。
    if (
        maps.tables_declared is not None
        and source is not None
        and not _dataset_source_is_sql(source)
        and (ds_code, source) not in maps.tables_declared
    ):
        raise _RowError(
            "dataset", r["_row"], f"表式 source 未声明（table sheet）: source={source}"
        )
    return (ds_code, name), {
        "datasource_id": ds_code,
        "domain_id": dom_id,
        "dataset_name": name,
        "description": _norm(r.get("description")),
        "source": source,
    }


def _prep_dataset_field(maps: _Maps, r: dict) -> tuple[tuple, dict] | None:
    dom_id = _need_domain(maps, r, "dataset_field")
    dataset_id = _need_dataset(maps, dom_id, r, "dataset_field")
    field_name = _norm(r.get("field_name"))
    return (dataset_id, field_name), {
        "dataset_id": dataset_id,
        "domain_id": dom_id,
        "datasource_id": maps.datasource_id,
        "field_name": field_name,
        "description": _norm(r.get("description")),
        "data_type": _norm(r.get("data_type")),
        "is_primary": _to_yn(r.get("is_primary"), "N"),
        "is_nullable": _to_yn(r.get("is_nullable"), "Y"),
    }


def _prep_dimension(maps: _Maps, r: dict) -> tuple[tuple, dict] | None:
    dom_id = _need_domain(maps, r, "dimension")
    name = _norm(r.get("dimension_name"))
    priority = (_norm(r.get("priority")) or "MEDIUM").upper()
    try:
        validate_priority(priority)
    except BadRequestError as exc:
        raise _RowError("dimension", r["_row"], exc.message) from exc
    # 维度字典挂业务域、不挂数据源（12 实体重构），故不写 datasource_id。
    return (dom_id, name), {
        "domain_id": dom_id,
        "dimension_name": name,
        "description": _norm(r.get("description")),
        "synonyms": _norm(r.get("synonyms")),
        "dimension_type": _norm(r.get("dimension_type") or r.get("dimension_role")),
        "child_dimensions": _norm(r.get("child_dimensions")),
        "priority": priority,
    }


def _prep_dimension_binding(maps: _Maps, r: dict) -> tuple[tuple, dict] | None:
    dom_id = _need_domain(maps, r, "dimension_binding")
    dataset_id = _need_dataset(maps, dom_id, r, "dimension_binding")
    dim_id = _need_dimension(maps, dom_id, r, "dimension_binding")
    return (dataset_id, dim_id), {
        "dataset_id": dataset_id,
        "dimension_id": dim_id,
        "domain_id": dom_id,
        "datasource_id": maps.datasource_id,
        "expr": _norm(r.get("expr")),
        "value_example": _norm(r.get("value_example")),
    }


def _prep_metric(maps: _Maps, r: dict) -> tuple[tuple, dict] | None:
    dom_id = _need_domain(maps, r, "metric")
    name = _norm(r.get("metric_name"))
    priority = (_norm(r.get("priority")) or "MEDIUM").upper()
    # 指标字典同维度字典：挂业务域、不挂数据源。
    return (dom_id, name), {
        "domain_id": dom_id,
        "metric_name": name,
        "description": _norm(r.get("description")),
        "synonyms": _norm(r.get("synonyms")),
        "priority": priority,
    }


def _prep_metric_caliber(maps: _Maps, r: dict) -> tuple[tuple, dict] | None:
    dom_id = _need_domain(maps, r, "metric_caliber")
    metric_id = _need_metric(maps, dom_id, r, "metric_caliber")
    dataset_id = _need_dataset(maps, dom_id, r, "metric_caliber")
    # time_window 可空：契约不再必填；空单元格归一为空串，既满足列 NOT NULL，
    # 又让自然键 (metric_id, dataset_id, time_window) 保持稳定（无窗口口径归为同一行）。
    time_window = _norm(r.get("time_window")) or ""
    additivity = _norm(r.get("additivity"))
    return (metric_id, dataset_id, time_window), {
        "metric_id": metric_id,
        "dataset_id": dataset_id,
        "domain_id": dom_id,
        "datasource_id": maps.datasource_id,
        # D5：expr 按模板内容原样存（prose 行合法，不做形态校验）。
        "expr": _norm(r.get("expr")),
        "time_window": time_window,
        "additivity": additivity.upper() if additivity else None,
        "is_default": _to_int(r.get("is_default")),  # ⑦a：空 → NULL 不兜底
    }


def _prep_verified_query(maps: _Maps, r: dict) -> tuple[tuple, dict] | None:
    dom_id = _need_domain(maps, r, "verified_query")
    question = _norm(r.get("question"))
    return (dom_id, question), {
        "domain_id": dom_id,
        "question": question,
        "verified_sql": _norm(r.get("verified_sql")),
    }


@dataclass(frozen=True)
class _SheetSpec:
    """sheet → 目标表、自然键、行处理器，以及写完后需要刷新的映射名。

    ``table=None`` = 无存储表；``prep=None`` = 只计数不校验（当前无，保留兜底）。
    """

    table: str | None
    nk: tuple[str, ...]
    prep: Callable[[_Maps, dict], tuple[tuple, dict] | None] | None
    refreshes: str | None = None


_SPECS: dict[str, _SheetSpec] = {
    "biz_domain": _SheetSpec(
        "domain",
        ("domain_name",),
        _prep_domain,
        refreshes="domain",
    ),
    "table": _SheetSpec(
        "physical_table",
        ("datasource_id", "table_name"),
        _prep_table,
        refreshes="physical_table",
    ),
    "table_column": _SheetSpec(
        "table_column",
        ("table_id", "column_name"),
        _prep_table_column,
        refreshes="table_column",
    ),
    "physical_join": _SheetSpec(
        "physical_join",
        (
            "datasource_id",
            "left_table_id",
            "left_column_ids",
            "right_table_id",
            "right_column_ids",
        ),
        _prep_physical_join,
    ),
    "dataset": _SheetSpec(
        "dataset",
        ("datasource_id", "dataset_name"),
        _prep_dataset,
        refreshes="dataset",
    ),
    "dataset_column": _SheetSpec(
        "dataset_column_meta",
        ("dataset_id", "field_name"),
        _prep_dataset_field,
    ),
    "dimension": _SheetSpec(
        "dimension_dict",
        ("domain_id", "dimension_name"),
        _prep_dimension,
        refreshes="dimension",
    ),
    "dataset_dimension": _SheetSpec(
        "dataset_dimension",
        ("dataset_id", "dimension_id"),
        _prep_dimension_binding,
    ),
    "metric": _SheetSpec(
        "metric_dict",
        ("domain_id", "metric_name"),
        _prep_metric,
        refreshes="metric",
    ),
    "metric_formula": _SheetSpec(
        "metric_formula_lib",
        ("metric_id", "dataset_id", "time_window"),
        _prep_metric_caliber,
    ),
    "verified_query": _SheetSpec(
        "verified_query",
        ("domain_id", "question"),
        _prep_verified_query,
    ),
}


# ---------------- 批量写入 ----------------
async def _write_batch(
    db: AsyncSession, spec: _SheetSpec, prepared: dict[tuple, dict], ctx: _Ctx
) -> None:
    """按自然键分流 insert / update，各一条（分块）批量语句。"""
    t = SC_TABLES[spec.table]
    existing = await _load_keyed_ids(
        db, spec.table, spec.nk, (nk[0] for nk in prepared), ctx
    )
    ts = now_iso()
    inserts: list[dict] = []
    updates: list[dict] = []
    for nk, cols in prepared.items():
        found = existing.get(nk)
        if found is None:
            inserts.append(
                {
                    **cols,
                    "tenant_id": ctx.tenant_id,
                    "workspace_id": ctx.workspace_id,
                    "created_by": ctx.user_id,
                    "updated_by": ctx.user_id,
                    "created_at": ts,
                    "updated_at": ts,
                }
            )
        else:
            updates.append(
                {
                    "_row_id": found,
                    **cols,
                    "updated_by": ctx.user_id,
                    "updated_at": ts,
                }
            )

    for i in range(0, len(inserts), _WRITE_CHUNK):
        await db.execute(insert(t), inserts[i : i + _WRITE_CHUNK])
    if updates:
        # executemany UPDATE 要求 values 显式绑定参数；同 sheet 各行列集一致。
        stmt = (
            update(t)
            .where(t.c.id == bindparam("_row_id"))
            .values({c: bindparam(c) for c in updates[0] if c != "_row_id"})
        )
        for i in range(0, len(updates), _WRITE_CHUNK):
            await db.execute(stmt, updates[i : i + _WRITE_CHUNK])


async def import_excel(
    db: AsyncSession,
    filename: str,
    content: bytes,
    principal: Principal,
    *,
    datasource_id: str | None = None,
) -> dict:
    """导入语义工作簿。SPEC 01：必须带 datasource_id，Excel 内不写数据源列。"""
    require_admin(principal)
    if not datasource_id or not str(datasource_id).strip():
        raise BadRequestError("datasource_id 不能为空")
    datasource_id = str(datasource_id).strip()
    from semantic_config.repositories import datasource_repo

    ds = await datasource_repo.find_by_datasource_id(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    if ds is None:
        raise BadRequestError(f"数据源不存在: datasource_id={datasource_id}")
    ctx = _Ctx(
        tenant_id=principal.tenant_id,
        workspace_id=principal.active_workspace_id,
        user_id=principal.user_id,
        datasource_id=datasource_id,
    )
    # 文件级校验
    if not content:
        raise BadRequestError("上传文件不能为空")
    if not (filename or "").lower().endswith(".xlsx"):
        raise BadRequestError("仅支持 .xlsx 格式")
    if len(content) > MAX_BYTES:
        raise BadRequestError("文件大小不能超过 20MB")

    sheets, header_errors = _read_sheets(content)
    # 归属数据源一律由请求参数绑定：抹掉工作簿里可能残留的数据源列（含旧名
    # source_datasource）后统一改写，旧模板带这些列也不会影响归属（SPEC 00 §12.2）。
    for rows in sheets.values():
        for r in rows:
            r.pop("datasource_id", None)
            r.pop("source_datasource", None)
            r["datasource_id"] = datasource_id
    if not sheets and not header_errors:
        raise BadRequestError(
            "未找到可导入的 sheet（正名/兼容名：biz_domain(domain)、table、table_column、"
            "physical_join、dataset、dataset_column(dataset_field)、dimension、"
            "dataset_dimension(dimension_binding)、metric、metric_formula(metric_caliber)、"
            "verified_query）"
        )

    # 单事务：按 IMPORT_ORDER 逐 sheet 批量 upsert（AsyncSession 首次执行即自动开启事务）。
    # 有行错误时仍继续处理后续 sheet，以便一次报全所有问题行，最后整体回滚。
    summary: dict[str, int] = {}
    errors: list[dict] = list(header_errors)
    try:
        maps = await _bootstrap_maps(db, sheets, ctx)
        for sheet in IMPORT_ORDER:
            rows = sheets.get(sheet)
            if not rows:
                continue
            spec = _SPECS[sheet]
            # 同自然键在文件内重复出现时后写覆盖，与逐行 upsert 语义一致，
            # 同时避免批量 INSERT 产生重复行。
            prepared: dict[tuple, dict] = {}
            for r in rows:
                try:
                    if spec.prep is None:
                        continue  # 只计数不落库的 sheet（当前无，保留兜底）
                    _check_required(sheet, r)
                    item = spec.prep(maps, r)
                except _RowError as e:
                    errors.append(
                        {"sheet": e.sheet, "row": e.row, "message": e.message}
                    )
                    continue
                if item is not None:
                    nk, cols = item
                    prepared[nk] = cols
            summary[SUMMARY_KEY[sheet]] = len(rows)
            if not prepared or spec.table is None:
                continue
            await _write_batch(db, spec, prepared, ctx)
            if spec.refreshes:
                await _refresh(db, maps, spec.refreshes, ctx)
        # 导入批次涉及的业务域统一重算 datasource_scope（逐行重算会重复做无用功）。
        # 放在 commit 前、与本次写入同事务，保证 scope 与数据集强一致（§4.2）。
        # 并上 prior_dataset_domains：数据集改挂域时原域也要重算，否则原域会残留旧值。
        await recompute_domain_scopes(
            db,
            set(maps.domain.values()) | maps.prior_dataset_domains,
            tenant_id=ctx.tenant_id,
            workspace_id=ctx.workspace_id,
            updated_by=ctx.user_id,
        )
        if errors:
            await db.rollback()
            return {
                "success": False,
                "summary": {},
                "errors": errors,
            }
        await db.commit()
        return {
            "success": True,
            "summary": summary,
            "errors": [],
        }
    except Exception:
        await db.rollback()
        raise
