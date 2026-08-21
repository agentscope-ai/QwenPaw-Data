"""语义层 post-pass 派生函数（从 :mod:`semantic` 拆出，原文件已 2k 行）。

这些函数都是 **独立的图后处理 pass**，不依赖 ``_write_*`` 内部状态：

- :func:`parse_topline_filters` —— 文本工具，从 ``partition_predicate`` 抽 ``{col: topline}``。
- :func:`derive_granularity_partitions` —— 用 YAML predicate 折射 Table.partition_columns。
- :func:`derive_partitions_from_data` —— 直接扫 PG 找漏标的 topline 列（PG 不可达时静默跳过）。
- :func:`derive_partitions_from_comments` —— 扫 Column.description 文本兜底。
- :func:`derive_view_alias_synonyms` —— 解析 ddl.txt 的 VIEW 块给源列加 synonyms。
- :func:`derive_column_time_grain` —— 按列名后缀（_1d / _30d / _acc / ...）标 time_grain。
- :func:`derive_calibers_from_formulas` —— 每个 Formula 派生 1 个 Caliber 节点。
- :func:`backfill_dimension_supplement` —— 从 dimension_values_supplement.yaml 补枚举值。

:mod:`semantic` 仍 re-export 全部名称，外部 import 路径不变。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from neo4j import Driver

from ..utils import get_logger, neo4j_session
from .keys import dim_value_key

log = get_logger("graph.semantic_derive")


# ---------------------------------------------------------------------- #
# Granularity partition 派生（图层"多粒度可见化"）
# ---------------------------------------------------------------------- #
TOPLINE_LITERALS_DEFAULT: frozenset[str] = frozenset({"全部"})

# 从 partition_predicate 文本里抓 `<col> = '<value>'` 对。
# 仅匹配等值字面量；BETWEEN / IN / LIKE / 占位符 (${...}) 全部跳过。
_TOPLINE_PAIR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'([^']*)'")
# 同时识别 `col = 'literal'` 和 `col = ${placeholder}` (后者无引号；split-axis 用)。
# 三个捕获组：(col, literal_value, placeholder_name)；后两个互斥，恰好一个非空。
_PREDICATE_PAIR_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:'([^']*)'|\$\{([^}]*)\})"
)
# 时间分区列名（这些列即使匹配上等值字面量也不视为粒度切分）
_TIME_PARTITION_COLS: frozenset[str] = frozenset({"ds", "dt", "pt", "stat_date", "biz_date"})


def parse_topline_filters(
    predicate: str,
    *,
    topline_literals: frozenset[str] = TOPLINE_LITERALS_DEFAULT,
) -> dict[str, str]:
    """从一段 ``partition_predicate`` 字符串里抓出 ``{col: topline_value}`` 对。

    例如 ``"ds = '${as_of_date}' AND terminal_type = '全部' AND models_name = '全部'"``
    返回 ``{'terminal_type': '全部', 'models_name': '全部'}``。

    规则：
    - 仅识别 ``col = 'literal'`` 形态；BETWEEN / IN / LIKE / 占位符 (``${...}``) 不算。
    - 时间分区列（``ds`` 等，见 :data:`_TIME_PARTITION_COLS`）即使匹配也跳过。
    - 仅当 value 命中 ``topline_literals``（默认 ``{'全部'}``）才视为粒度切分键。
    """
    if not predicate:
        return {}
    out: dict[str, str] = {}
    for col, val in _TOPLINE_PAIR_RE.findall(predicate):
        col_lc = col.lower()
        if col_lc in _TIME_PARTITION_COLS:
            continue
        if val.startswith("${"):
            continue
        if val in topline_literals:
            out[col] = val
    return out


def derive_granularity_partitions(
    driver: Driver,
    *,
    topline_literals: frozenset[str] = TOPLINE_LITERALS_DEFAULT,
) -> None:
    """汇总所有 ``Formula -[:OF_VIEW]-> Dataset -[:CONTAINS_TABLE]-> Table`` 的 partition_predicate，
    在 ``Table`` / ``Column`` 上把 **粒度切分语义** 显式落图。

    写出的属性：

    - ``Table.partition_columns: list[str]``  — 该表所有 topline 切分列名
    - ``Table.is_multidim: bool``              — 是否 ≥2 个 partition 列（多粒度聚合表）
    - ``Column.granularity_role = 'partition'`` 标记
    - ``Column.topline_value``                 — 取 topline 字面量（如 ``'全部'``）

    这样 :func:`context_manager.pipeline._topology_subgraph_to_schema` 拉子图时可以一并取回这些属性，
    在 SQL 生成 prompt 里直接告诉 LLM "这张表是多粒度的，请把所有 partition 列
    过滤到 topline 值"，而不是依赖某条 formula 的 partition_predicate 文本里
    凑巧写全了。

    .. note::
        本函数仅基于 YAML 里写好的 ``partition_predicate`` 文本回填。许多
        ``_overview_1d`` / ``_index_1d`` 表的 YAML 公式根本没写
        ``partition_predicate``（典型如 Wan DAU 域）。这种情况下应再调用
        :func:`derive_partitions_from_data` 直接探 PG 数据补齐。
    """
    with neo4j_session(driver) as s:
        rows = s.run(
            """
            MATCH (f:Formula)-[:OF_VIEW]->(:Dataset)-[:CONTAINS_TABLE]->(t:Table)
            WHERE coalesce(f.partition_predicate, '') <> ''
            RETURN t.key AS tkey, collect(coalesce(f.partition_predicate, '')) AS pps
            """
        ).data()

        per_table: dict[str, dict[str, str]] = {}
        for r in rows:
            tk = str(r.get("tkey") or "")
            if not tk:
                continue
            merged: dict[str, str] = {}
            for pp in (r.get("pps") or []):
                merged.update(parse_topline_filters(pp, topline_literals=topline_literals))
            if merged:
                per_table[tk] = merged

        if not per_table:
            log.info("granularity post-pass: no topline filters derived (skipped)")
            return

        log.info(
            "granularity post-pass: tagging %d table(s) with partition columns "
            "(largest=%d cols)",
            len(per_table),
            max(len(v) for v in per_table.values()),
        )

        rows_in = [
            {
                "tkey": tk,
                "cols": sorted(merged.keys()),
                "topline": [{"k": k, "v": v} for k, v in merged.items()],
            }
            for tk, merged in per_table.items()
        ]
        s.run(
            """
            UNWIND $rows AS row
            MATCH (t:Table {key: row.tkey})
            SET t.partition_columns = row.cols,
                t.is_multidim = (size(row.cols) > 1)
            WITH t, row
            UNWIND row.topline AS mc
            OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column {name: mc.k})
            FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [c] END |
                SET c.granularity_role = 'partition',
                    c.topline_value = mc.v
            )
            """,
            rows=rows_in,
        )


# ---------------------------------------------------------------------- #
# 数据驱动的 topline 探针：直接扫 PG 找 '全部' 桶
# ---------------------------------------------------------------------- #
# 全 schema 通用的 topline 字面量；中文表常见 '全部'，英文常见 'all' / 'overall' / 'TOTAL'。
# 注意：要保留大小写敏感的字面量集合，因为 ``terminal_type = 'all'`` 与
# ``terminal_type = 'ALL'`` 在落表时常常不是同一行。
TOPLINE_LITERALS_RICH: frozenset[str] = frozenset({
    "全部", "总计", "合计",
    "all", "ALL", "All",
    "overall", "Overall", "OVERALL",
    "total", "Total", "TOTAL",
})

# 数据探针不应跳过的字段（即使列名长得像 id/key，也可能是粒度切分键）。
_DATA_PROBE_SKIP_NAME_PATTERNS = (
    re.compile(r".*_id$", re.IGNORECASE),
    re.compile(r".*_uuid$", re.IGNORECASE),
    re.compile(r"^uuid$", re.IGNORECASE),
)


def derive_partitions_from_data(
    driver: Driver,
    *,
    schema: str = "public",
    topline_literals: frozenset[str] = TOPLINE_LITERALS_RICH,
    distinct_limit: int = 50,
    statement_timeout_ms: int = 5000,
    no_topline_max_cardinality: int = 12,
) -> dict[str, dict[str, str]]:
    """直接扫 PostgreSQL 的低基数 text 列，凡是出现 topline 字面量的列就标成
    ``Column.granularity_role='partition'``，并合并到 ``Table.partition_columns``。

    比 :func:`derive_granularity_partitions` 更鲁棒——不依赖 YAML 的
    ``partition_predicate`` 是否凑巧写全。覆盖典型遗漏：

    - ``dws_ac_imggen_dau_index_1d`` 的 ``terminal_type = '全部'``（YAML 公式没写）；
    - ``dws_ac_imggen_task_index_1d`` 的多个 topline 列；
    - 各类 ``*_multidim_*`` 表的多 topline 列。

    返回 ``{table_short_name: {col_name: topline_value}}``，调用方可用于打印 / 校验。
    若数据源不可达，记日志并返回 ``{}``。
    """
    # 1) 拉图里的 (table_key, schema, table_name, column_name, type) 列表
    with neo4j_session(driver) as s:
        rows = s.run(
            """
            MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
            WHERE coalesce(c.type, '') <> ''
            RETURN t.key AS tkey, t.schema AS tschema, t.name AS tname,
                   c.name AS cname, c.type AS ctype
            """
        ).data()

    # 2) 仅扫 schema=public（或调用方指定的 schema）+ text/varchar/char 类型
    text_type_re = re.compile(r"^(text|varchar|char|character|nvarchar)", re.IGNORECASE)
    candidates: dict[str, list[tuple[str, str, str]]] = {}  # table_key -> [(tname, cname, ctype)]
    for r in rows:
        tsch = (r.get("tschema") or "").strip()
        if tsch and tsch.lower() != schema.lower():
            continue
        cname = str(r.get("cname") or "")
        if not cname or cname.lower() in _TIME_PARTITION_COLS:
            continue
        if any(p.match(cname) for p in _DATA_PROBE_SKIP_NAME_PATTERNS):
            continue
        ctype = str(r.get("ctype") or "")
        if not text_type_re.match(ctype):
            continue
        tkey = str(r.get("tkey") or "")
        tname = str(r.get("tname") or "")
        if not (tkey and tname):
            continue
        candidates.setdefault(tkey, []).append((tname, cname, ctype))

    if not candidates:
        log.info("derive_partitions_from_data: no text-typed columns to probe (skipped)")
        return {}

    log.info(
        "derive_partitions_from_data: probing %d table(s), %d column(s) for topline literals %s",
        len(candidates),
        sum(len(v) for v in candidates.values()),
        sorted(topline_literals),
    )

    # 3) 一次性建连接，逐列 SELECT DISTINCT (LIMIT N) 并匹配 topline
    per_table: dict[str, dict[str, str]] = {}  # table_key -> {col: topline}
    per_table_short: dict[str, dict[str, str]] = {}  # tname -> {col: topline}
    # 同表上 "看起来是分桶切分但没 '全部' 桶" 的列：典型如 imagegen_dau_index_1d.region 只有 (国内, 海外)；
    # 这些列必须靠 SUM(...) GROUP BY ds 跨值聚合才能拿到全网总和，光 WHERE topline 是拿不到的。
    no_topline_candidates: dict[str, dict[str, list[str]]] = {}  # tkey -> {col: distinct_values}
    skipped_low_card: int = 0
    matched_cols: int = 0

    def _probe_distinct(tname: str, cname: str) -> set[str]:
        import psycopg
        from ..ingest import _pg_conn_kwargs

        sql = (
            f'SELECT DISTINCT "{cname}" FROM "{schema}"."{tname}" '
            f'WHERE "{cname}" IS NOT NULL LIMIT {int(distinct_limit)}'
        )
        conn_kw = _pg_conn_kwargs(schema=schema)
        with psycopg.connect(**conn_kw, connect_timeout=5) as conn:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            cur.execute(sql)
            return {str(r[0]) for r in cur.fetchall() if r[0] is not None}

    try:
        for tkey, cols in candidates.items():
            tname = cols[0][0]
            for _, cname, _ in cols:
                try:
                    vals = _probe_distinct(tname, cname)
                except Exception as exc:
                    log.debug("probe failed: %s.%s — %s", tname, cname, exc)
                    continue
                # 卡值数：超过 distinct_limit-1 视为高基数列（无意义切分），跳过；
                # 但只要命中 topline 字面量就保留——topline 列即使切分桶很多（如 function_name 全部+几十个具体功能）也合法。
                matched: Optional[str] = None
                for lit in topline_literals:
                    if lit in vals:
                        matched = lit
                        break
                if matched is not None:
                    per_table.setdefault(tkey, {})[cname] = matched
                    per_table_short.setdefault(tname, {})[cname] = matched
                    matched_cols += 1
                    continue
                # 没 topline，但卡值数低 → 候选 split_no_topline。
                # 只采纳 cardinality ∈ [2, no_topline_max_cardinality] 的列；
                # cardinality=1 没意义（恒等），太大则更可能是普通枚举数据列而非粒度切分。
                if 2 <= len(vals) <= no_topline_max_cardinality:
                    no_topline_candidates.setdefault(tkey, {})[cname] = sorted(vals)[:8]
                skipped_low_card += 1
    except Exception as exc:
        log.warning("derive_partitions_from_data: data probe aborted (%s)", exc)
        return {}

    log.info(
        "derive_partitions_from_data: matched %d topline column(s) on %d table(s); "
        "additionally %d split_no_topline column(s) found "
        "(scanned ~%d cols total, %d had no topline)",
        matched_cols,
        len(per_table),
        sum(len(v) for v in no_topline_candidates.values()),
        sum(len(v) for v in candidates.values()),
        skipped_low_card,
    )

    if not per_table and not no_topline_candidates:
        return {}

    # 4) 写回 Neo4j：合并到已有的 Table.partition_columns / Column.granularity_role
    rows_in = [
        {
            "tkey": tk,
            "topline": [{"k": k, "v": v} for k, v in merged.items()],
        }
        for tk, merged in per_table.items()
    ]
    with neo4j_session(driver) as s:
        if rows_in:
            s.run(
                """
                UNWIND $rows AS row
                MATCH (t:Table {key: row.tkey})
                WITH t, row,
                     [x IN coalesce(t.partition_columns, []) | x] AS existing,
                     [x IN row.topline | x.k] AS new_cols
                // 纯 Cypher 去重：list comprehension + range 过滤掉重复项
                WITH t, row, existing + new_cols AS combined_dup
                WITH t, row,
                     [i IN range(0, size(combined_dup) - 1)
                      WHERE NOT (combined_dup[i] IN combined_dup[0..i])
                      | combined_dup[i]] AS combined
                WITH t, row, [x IN combined WHERE x IS NOT NULL] AS combined
                SET t.partition_columns = combined,
                    t.is_multidim = (size(combined) > 1)
                WITH t, row
                UNWIND row.topline AS mc
                OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column {name: mc.k})
                FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [c] END |
                    SET c.granularity_role = 'partition',
                        c.topline_value = mc.v
                )
                """,
                rows=rows_in,
            )
        # split_no_topline columns: 单独写一波；不会进入 Table.partition_columns
        # （那个字段表示"必须 WHERE topline"），但 Column 上落 granularity_role 标记，
        # _partition_block 渲染时可以拿来给 LLM 提示"这列没有 topline，要 SUM/GROUP BY"。
        nt_rows = [
            {
                "tkey": tk,
                "split_no_topline": [
                    {"k": k, "samples": vals} for k, vals in cols.items()
                ],
            }
            for tk, cols in no_topline_candidates.items()
        ]
        if nt_rows:
            s.run(
                """
                UNWIND $rows AS row
                MATCH (t:Table {key: row.tkey})
                UNWIND row.split_no_topline AS nt
                OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column {name: nt.k})
                // 只在还没被标 partition 的列上盖 split_no_topline；一个列要么是
                // partition (含 topline)、要么是 split_no_topline (无 topline)，不能同时。
                FOREACH (_ IN CASE
                    WHEN c IS NULL OR coalesce(c.granularity_role, '') = 'partition'
                    THEN [] ELSE [c]
                END |
                    SET c.granularity_role = 'split_no_topline',
                        c.granularity_samples = nt.samples
                )
                """,
                rows=nt_rows,
            )
    return per_table_short


# ---------------------------------------------------------------------- #
# 注释驱动的 partition 派生（不依赖 PG 数据，只看 Column.description 文本）
# ---------------------------------------------------------------------- #
# 列注释里出现这些短语就视为 "本列含 topline 桶"
_COMMENT_TOPLINE_RE = re.compile(r"含\s*[『「'\"]?\s*全部|含\s*[『「'\"]?\s*汇总|包含\s*全部|聚合维度")


def derive_partitions_from_comments(driver: Driver) -> dict[str, dict[str, str]]:
    """扫所有 ``Column.description`` / ``Column.comment``，凡是字面写「含 全部 /
    含汇总 / 包含全部 / 聚合维度」的列就视为 ``granularity_role='partition'`` 列，
    ``topline_value='全部'``，并合并到所属 ``Table.partition_columns``。

    与 :func:`derive_partitions_from_data` 互补：

    - 数据探针只对**实际有数据**且 PG 可达时才能起作用；
    - 注释探针只看 Neo4j 上已经写好的 ``description`` 文本，**离线**也能跑，
      并且能盖住数据 distinct 后没命中字面量的边角情况（如 ``models_name`` 在
      `dws_ac_chat_multidim_index_1d` 上 distinct 太大被低基数过滤跳过，
      但注释里写了「模型name，含 全部」，仍然该被识别为粒度切分键）。

    特殊处理：
    - ``summary_dimension`` 列：注释通常写 "聚合维度，取值为：全部/端/..."。
      这种列本身就是粒度切分维度的元描述列，必须 ``WHERE summary_dimension='全部'``
      才能拿全表汇总。

    返回 ``{table_key: {col_name: topline_value}}``。
    """
    with neo4j_session(driver) as s:
        rows = s.run(
            """
            MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
            WHERE coalesce(c.description, '') <> '' OR coalesce(c.comment, '') <> ''
            RETURN t.key AS tkey,
                   c.name AS cname,
                   coalesce(c.description, '') AS desc,
                   coalesce(c.comment, '') AS comment
            """
        ).data()

        per_table: dict[str, dict[str, str]] = {}
        for r in rows:
            tk = str(r.get("tkey") or "")
            cname = str(r.get("cname") or "")
            if not (tk and cname):
                continue
            blob = " ".join([r.get("desc") or "", r.get("comment") or ""]).strip()
            if not blob:
                continue
            if _COMMENT_TOPLINE_RE.search(blob):
                # 跳过时间分区列（即使描述里凑巧出现 '全部' 也不算）
                if cname.lower() in _TIME_PARTITION_COLS:
                    continue
                per_table.setdefault(tk, {})[cname] = "全部"

        if not per_table:
            log.info("derive_partitions_from_comments: no '含 全部' / 聚合维度 columns")
            return {}

        log.info(
            "derive_partitions_from_comments: tagged %d table(s) with %d "
            "comment-derived partition column(s)",
            len(per_table),
            sum(len(v) for v in per_table.values()),
        )

        rows_in = [
            {"tkey": tk, "topline": [{"k": k, "v": v} for k, v in cols.items()]}
            for tk, cols in per_table.items()
        ]
        s.run(
            """
            UNWIND $rows AS row
            MATCH (t:Table {key: row.tkey})
            WITH t, row,
                 [x IN coalesce(t.partition_columns, []) WHERE x IS NOT NULL] AS existing,
                 [x IN row.topline | x.k] AS new_cols
            WITH t, row, existing + new_cols AS combined_dup
            WITH t, row,
                 [i IN range(0, size(combined_dup) - 1)
                  WHERE NOT (combined_dup[i] IN combined_dup[0..i])
                  | combined_dup[i]] AS combined
            SET t.partition_columns = combined,
                t.is_multidim = (size(combined) > 1)
            WITH t, row
            UNWIND row.topline AS mc
            OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column {name: mc.k})
            // 已经被 split_no_topline 标过的列升级到 partition；只跳过已经是 partition 的
            FOREACH (_ IN CASE
                WHEN c IS NULL OR coalesce(c.granularity_role, '') = 'partition'
                THEN [] ELSE [c]
            END |
                SET c.granularity_role = 'partition',
                    c.topline_value = mc.v,
                    c.granularity_samples = []
            )
            """,
            rows=rows_in,
        )
        return per_table


# ---------------------------------------------------------------------- #
# DDL 文本兜底：从 ddl.txt 抽取 VIEW 体里的 alias 映射
# ---------------------------------------------------------------------- #
# `<src_table>.<src_col> AS "<alias>"` —— Postgres view body 里最常见的列别名形态
_VIEW_ALIAS_RE = re.compile(r'(\w+)\.(\w+)\s+AS\s+"([^"]+)"')

# `CREATE OR REPLACE VIEW <name> AS <body>` —— body 一直读到下一个 END / ALTER /
# COMMENT / CREATE / BEGIN / 注释块 / 文件结束
_VIEW_BLOCK_RE = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+VIEW\s+(\S+)\s+AS\s+(.*?)(?=\n\s*(?:END|COMMENT|ALTER|CREATE|BEGIN|/\*|\Z))",
    re.DOTALL | re.IGNORECASE,
)


def derive_view_alias_synonyms(driver: Driver, ddl_path: Path) -> dict[str, int]:
    """解析 ``ddl.txt`` 里的 ``CREATE OR REPLACE VIEW`` 块，把 view 列别名（业务名
    /中文指标名）反挂到源 ``Column.aliases`` 列表，并给 view 自身的列补 ``description``。

    举例：``ds1`` 视图里有

    .. code-block:: sql

        SELECT ads_appdata_studio_dashboard_overview_1d.landingpagevisit_usercnt_1d AS "DAU_1d"

    本函数会：

    - 把 ``"DAU_1d"`` 加进 ``Column(landingpagevisit_usercnt_1d).aliases``。
      锚点检索（fulltext on Column.aliases / vector on embedding 之后再走）就能
      在用户问 "Studio DAU" 时直接命中物理列；
    - 给 ``view_..._ds1.DAU_1d`` 这一列填上 ``description = '业务别名 = DAU_1d；
      源列 = ads_appdata_studio_dashboard_overview_1d.landingpagevisit_usercnt_1d'``，
      让 schema renderer 不再出现一片 ``desc=NONE`` 的视图列。

    返回简单的统计字典，方便上层日志展示。
    """
    if not ddl_path or not Path(ddl_path).exists():
        log.info("derive_view_alias_synonyms: ddl_path missing, skipped: %s", ddl_path)
        return {"sources": 0, "links": 0, "views": 0}

    text = Path(ddl_path).read_text(encoding="utf-8")

    src_to_aliases: dict[tuple[str, str, str], set[str]] = {}
    view_to_source: dict[tuple[str, str, str], str] = {}
    view_count = 0

    for view_full, body in _VIEW_BLOCK_RE.findall(text):
        view_count += 1
        if "." in view_full:
            view_sch, view_name = view_full.split(".", 1)
        else:
            view_sch, view_name = "public", view_full
        view_sch = view_sch.strip().lower()
        view_name = view_name.strip()
        for src_t, src_c, alias in _VIEW_ALIAS_RE.findall(body):
            alias_clean = alias.strip()
            src_t_clean = src_t.strip()
            src_c_clean = src_c.strip()
            if not (alias_clean and src_t_clean and src_c_clean):
                continue
            if alias_clean == src_c_clean:
                # 平凡重命名（select foo as "foo"），跳过——没新增信息
                continue
            # 一个源列可被多个视图、多个别名引用
            src_to_aliases.setdefault(
                (view_sch, src_t_clean, src_c_clean), set()
            ).add(alias_clean)
            view_to_source[(view_sch, view_name, alias_clean)] = (
                f"{src_t_clean}.{src_c_clean}"
            )

    if not src_to_aliases and not view_to_source:
        log.info(
            "derive_view_alias_synonyms: scanned %d view block(s) but found no aliases",
            view_count,
        )
        return {"sources": 0, "links": 0, "views": view_count}

    log.info(
        "derive_view_alias_synonyms: %d views → %d source-col synonym set(s), "
        "%d view-alias→src links",
        view_count,
        len(src_to_aliases),
        len(view_to_source),
    )

    syn_rows = [
        {"sch": k[0], "tname": k[1], "cname": k[2], "syns": sorted(v)}
        for k, v in src_to_aliases.items()
    ]
    view_rows = [
        {"sch": k[0], "vname": k[1], "alias": k[2], "src_ref": v}
        for k, v in view_to_source.items()
    ]

    with neo4j_session(driver) as s:
        # 1) 把 alias 合并进源列的 c.aliases（去重，纯 Cypher，不依赖 APOC）
        s.run(
            """
            UNWIND $rows AS row
            MATCH (t:Table {schema: row.sch, name: row.tname})-[:HAS_COLUMN]->(c:Column {name: row.cname})
            WITH c, row,
                 [x IN coalesce(c.aliases, []) WHERE x IS NOT NULL] AS existing
            WITH c, existing + row.syns AS combined_dup
            WITH c,
                 [i IN range(0, size(combined_dup) - 1)
                  WHERE NOT (combined_dup[i] IN combined_dup[0..i])
                  | combined_dup[i]] AS combined
            SET c.aliases = combined
            """,
            rows=syn_rows,
        )

        # 2) 给视图列补 description（仅当原本为空），让 to_m_schema / 锚点 fulltext 更友好
        s.run(
            """
            UNWIND $rows AS row
            MATCH (t:Table {schema: row.sch, name: row.vname})-[:HAS_COLUMN]->(c:Column {name: row.alias})
            WHERE coalesce(c.description, '') = ''
            SET c.description = '业务别名 = ' + row.alias + '；源列 = ' + row.src_ref
            """,
            rows=view_rows,
        )

    return {
        "sources": len(src_to_aliases),
        "links": len(view_to_source),
        "views": view_count,
    }


# ---------------------------------------------------------------------- #
# 列名后缀 → 时间粒度
# ---------------------------------------------------------------------- #
# 顺序敏感：长后缀必须先匹配（``_fytd`` 比 ``_fy`` / ``_td`` 长，要先试）
_TIME_GRAIN_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_fytd", "fytd"),
    ("_finyeartm", "fytd"),
    ("_30d", "30d"),
    ("_curr", "curr"),
    ("_acc", "acc"),
    ("_his", "his"),
    ("_td", "td"),
    ("_fy", "fy"),
    ("_1d", "1d"),
    ("_7d", "7d"),
    ("_1m", "1m"),
)


def derive_column_time_grain(driver: Driver) -> int:
    """根据数据仓库列名约定（``cnt_1d`` / ``usercnt_30d`` / ``gaap_fy`` / ``gaap_fytd``
    / ``valid_app_cnt_acc`` ...）给 ``Column.time_grain`` 写时间粒度标签。

    粒度词汇：

    - ``1d`` / ``7d`` / ``30d``：日 / 周 / 月滚动窗口
    - ``acc`` / ``his`` / ``td``：截止当日累计
    - ``fy`` / ``fytd``：财年 / 财年截止当日
    - ``1m`` / ``curr``：月 / 当前快照

    这给 SQL LLM 一个明确的"这列是日粒度还是累计"标签，避免把 ``active_usercnt_1d``
    当成 30d 用；同时 Decision LLM 选 metric 时也可以按 time_grain 与问题里的时间
    跨度做匹配（"日均" → 优先 _1d 列；"月活" → 30d 列）。
    返回受影响的列数。
    """
    with neo4j_session(driver) as s:
        rows = s.run(
            """
            MATCH (c:Column)
            WHERE coalesce(c.time_grain, '') = ''
            RETURN c.key AS ckey, c.name AS cname
            """
        ).data()

        updates = []
        for r in rows:
            cname = (str(r.get("cname") or "")).lower()
            if not cname:
                continue
            for suf, grain in _TIME_GRAIN_SUFFIXES:
                if cname.endswith(suf):
                    updates.append({"ckey": str(r["ckey"]), "grain": grain})
                    break

        if not updates:
            log.info("derive_column_time_grain: no recognizable suffix on existing columns")
            return 0

        log.info(
            "derive_column_time_grain: tagging %d column(s) with time_grain", len(updates)
        )
        s.run(
            """
            UNWIND $rows AS row
            MATCH (c:Column {key: row.ckey})
            SET c.time_grain = row.grain
            """,
            rows=updates,
        )
        return len(updates)


# ---------------------------------------------------------------------- #
# Post-pass: 从 Formula 派生 Caliber 节点（多轴 rollup / filter 复合口径）
# ---------------------------------------------------------------------- #
def derive_calibers_from_formulas(
    driver: Driver,
    *,
    topline_literals: frozenset[str] = TOPLINE_LITERALS_RICH,
) -> int:
    """每个 Formula 派生 1 个 :Caliber 节点（1:1），把多轴口径收口到一处。

    **轴语义统一**：partition_predicate 里每一个 ``col=value`` 对都映射成一条
    ``:ON_AXIS`` 边连到对应 :Dimension，边属性 ``mode`` 区分三种语义：

    - ``mode='rollup'``：value ∈ ``topline_literals`` (``'全部'`` / ``'all'`` 等) ——
      该轴上压成 topline 桶（如 ``country='全部'``）
    - ``mode='filter'``：value 是具体字面量（如 ``terminal_type='APP'``）——
      在该轴上钉到具体取值
    - ``mode='split'``：value 是参数占位符 ``${...}``（如 ``country=${country}``）——
      该轴开放给 groupby / 按维度拆分

    三种模式语义平级；之前用 ``:ROLLED_UP_ON → :Dimension`` 和
    ``:FILTERED_BY → :DimensionValue`` 两种边表达，且 split 没有边（孤立），
    现在统一成一条 ``:ON_AXIS → :Dimension`` 边 + ``mode`` / ``value`` 属性。
    要走到 :DimensionValue 节点，用 ``(cal)-[:ON_AXIS]->(d)-[:HAS_VALUE]->(dv {value: r.value})``。

    Caliber 节点字段（与单值旧模型 ``cal:<dom>:<col>=<val>`` 共存，key 段不重叠）：

    - ``key``           : ``cal:<formula_key_suffix>`` —— 与对应 Formula 同签名
    - ``name``          : 人类可读，如 ``DAU·rollup(6轴)·1d`` / ``DAU·split(country)·numerator·1d``
    - ``metric_key``    , ``domain``, ``dataset_view``, ``time_grain``, ``role``
    - ``axis_cols``     : 所有轴的列名 (与下两个并列对齐)
    - ``axis_modes``    : 每个轴的模式 (``rollup`` / ``filter`` / ``split``)
    - ``axis_values``   : 每个轴的字面值 (split 时为 ``''``)
    - ``description``   : 空，留给 BI 后续补

    边：

    - ``:Metric -[:HAS_CALIBER]-> :Caliber``
    - ``:Caliber -[:REALIZED_BY]-> :Formula``
    - ``:Caliber -[:USES_DATASET]-> :Dataset``
    - ``:Caliber -[:ON_AXIS {mode, value}]-> :Dimension`` —— 列名经 MAPS_TO_COLUMN
      反查命中 Dimension 才建；rollup / filter / split 三种 mode 共用此边

    旧 ``_write_caliber`` 单值模型（``cal:<dom>:<col>=<val>`` + ``:FILTER_ON``）
    保留兼容 studio / semantic_layer_reference 等 yaml 数据源，与本派生节点
    在图中共存、key 不冲突。
    """
    with neo4j_session(driver) as s:
        rows = s.run(
            """
            MATCH (m:Metric)-[:HAS_FORMULA]->(f:Formula)
            OPTIONAL MATCH (f)-[:OF_VIEW]->(ds:Dataset)
            RETURN m.key AS metric_key, m.name AS metric_name, m.domain AS domain,
                   m.datasource_id AS metric_ds_id,
                   f.key AS formula_key, f.dataset AS dataset_qualified,
                   f.date_range AS date_range, f.partition_predicate AS predicate,
                   f.role AS role, f.datasource_id AS formula_ds_id,
                   ds.key AS dataset_key, ds.name AS dataset_name
            """
        ).data()

        if not rows:
            log.info("derive_calibers_from_formulas: no formulas in graph (skipped)")
            return 0

        cal_rows: list[dict] = []
        for r in rows:
            f_key = str(r.get("formula_key") or "")
            if not f_key:
                continue
            predicate = str(r.get("predicate") or "").strip()
            if not predicate:
                continue
            axis_cols: list[str] = []
            axis_modes: list[str] = []
            axis_values: list[str] = []
            for col, literal_val, placeholder in _PREDICATE_PAIR_RE.findall(predicate):
                if col.lower() in _TIME_PARTITION_COLS:
                    continue
                if placeholder:
                    mode = "split"
                    eff_val = ""
                elif literal_val in topline_literals:
                    mode = "rollup"
                    eff_val = literal_val
                else:
                    mode = "filter"
                    eff_val = literal_val
                axis_cols.append(col)
                axis_modes.append(mode)
                axis_values.append(eff_val)
            if not axis_cols:
                continue
            # Scope the derived caliber key by datasource_id (ds-first convention,
            # matching caliber_key / formula_key). Prefer the formula's own ds_id
            # (formula keys are already scoped at write time); fall back to the
            # metric node's datasource_id so legacy formulas without a prefixed
            # key still get scoped. f_key suffix after "fml:" already carries
            # "<ds>:<domain>:..." when scoped, so we only prefix when the suffix
            # itself is not already ds-prefixed.
            cal_ds = str(r.get("formula_ds_id") or r.get("metric_ds_id") or "").strip()
            f_suffix = f_key[len("fml:"):] if f_key.startswith("fml:") else f_key
            # If the suffix already starts with "<ds>:" (scoped formula key), keep
            # it as-is — re-prefixing would double the ds segment. Otherwise prefix.
            if cal_ds and not f_suffix.startswith(cal_ds + ":"):
                cal_key_str = f"cal:{cal_ds}:{f_suffix}"
            else:
                cal_key_str = f"cal:{f_suffix}"

            metric_name = str(r.get("metric_name") or "")
            time_grain = str(r.get("date_range") or "")
            dataset_view = str(r.get("dataset_name") or r.get("dataset_qualified") or "")
            role = str(r.get("role") or "").strip().lower()
            if role not in ("numerator", "denominator"):
                role = ""

            # 命名策略：按 mode 分桶汇总；rollup 计数、filter 拼 col=val、split 列名。
            # 复合指标在末尾追加 role。
            n_rollup = sum(1 for m in axis_modes if m == "rollup")
            split_cols = [c for c, m in zip(axis_cols, axis_modes) if m == "split"]
            filter_pairs = [
                f"{c}={v}"
                for c, m, v in zip(axis_cols, axis_modes, axis_values)
                if m == "filter"
            ]
            parts = [metric_name]
            if n_rollup:
                parts.append(f"rollup({n_rollup}轴)")
            if split_cols:
                parts.append(f"split({','.join(split_cols)})")
            if filter_pairs:
                parts.append("+".join(filter_pairs))
            if time_grain:
                parts.append(time_grain)
            if role:
                parts.append(role)
            cal_name = "·".join(p for p in parts if p)

            cal_rows.append({
                "cal_key": cal_key_str,
                "name": cal_name,
                "metric_key": str(r.get("metric_key") or ""),
                "formula_key": f_key,
                "domain": str(r.get("domain") or ""),
                "dataset_key": str(r.get("dataset_key") or ""),
                "dataset_view": dataset_view,
                "time_grain": time_grain,
                "role": role,
                "axis_cols": axis_cols,
                "axis_modes": axis_modes,
                "axis_values": axis_values,
                "datasource_id": cal_ds,
            })

        # Upsert Caliber + 三条主边 (Metric / Formula / Dataset)
        s.run(
            """
            UNWIND $rows AS row
            MERGE (cal:Caliber {key: row.cal_key})
              ON CREATE SET cal.name = row.name, cal.metric_key = row.metric_key,
                            cal.domain = row.domain, cal.dataset_view = row.dataset_view,
                            cal.time_grain = row.time_grain, cal.role = row.role,
                            cal.axis_cols = row.axis_cols,
                            cal.axis_modes = row.axis_modes,
                            cal.axis_values = row.axis_values,
                            cal.datasource_id = row.datasource_id,
                            cal.zone = 'metadata'
              ON MATCH  SET cal.name = row.name, cal.metric_key = row.metric_key,
                            cal.domain = row.domain, cal.dataset_view = row.dataset_view,
                            cal.time_grain = row.time_grain, cal.role = row.role,
                            cal.axis_cols = row.axis_cols,
                            cal.axis_modes = row.axis_modes,
                            cal.axis_values = row.axis_values,
                            cal.datasource_id = row.datasource_id,
                            cal.zone = 'metadata'
            WITH cal, row
            OPTIONAL MATCH (m:Metric {key: row.metric_key})
            FOREACH (_ IN CASE WHEN m IS NULL THEN [] ELSE [m] END |
                MERGE (m)-[:HAS_CALIBER]->(cal)
            )
            WITH cal, row
            OPTIONAL MATCH (f:Formula {key: row.formula_key})
            FOREACH (_ IN CASE WHEN f IS NULL THEN [] ELSE [f] END |
                MERGE (cal)-[:REALIZED_BY]->(f)
            )
            WITH cal, row
            OPTIONAL MATCH (ds:Dataset {key: row.dataset_key})
            FOREACH (_ IN CASE WHEN ds IS NULL THEN [] ELSE [ds] END |
                MERGE (cal)-[:USES_DATASET]->(ds)
            )
            """,
            rows=cal_rows,
        )

        # :ON_AXIS 边：rollup / filter / split 三种模式共用。
        # 列名经 MAPS_TO_COLUMN 反查命中 Dimension 才建（同列多 Dimension 时全部连）；
        # 模式与值挂在边属性上，下游遍历用 r.mode / r.value 分发即可。
        axis_edges = [
            {
                "cal_key": cr["cal_key"],
                "col_name": col,
                "domain": cr["domain"],
                "mode": mode,
                "value": val,
            }
            for cr in cal_rows
            for col, mode, val in zip(cr["axis_cols"], cr["axis_modes"], cr["axis_values"])
        ]
        if axis_edges:
            s.run(
                """
                UNWIND $rows AS row
                MATCH (cal:Caliber {key: row.cal_key})
                OPTIONAL MATCH (d:Dimension {domain: row.domain})-[:MAPS_TO_COLUMN|MAPS_TO_DATASET_COLUMN]->(target {name: row.col_name})
                FOREACH (_ IN CASE WHEN d IS NULL THEN [] ELSE [d] END |
                    MERGE (cal)-[r:ON_AXIS {mode: row.mode}]->(d)
                      ON CREATE SET r.value = row.value, r.col_name = row.col_name
                      ON MATCH  SET r.value = row.value, r.col_name = row.col_name
                )
                """,
                rows=axis_edges,
            )

        mode_counts: dict[str, int] = {}
        for e in axis_edges:
            mode_counts[e["mode"]] = mode_counts.get(e["mode"], 0) + 1
        log.info(
            "derive_calibers_from_formulas: upserted %d caliber(s); "
            "ON_AXIS edges total=%d (rollup=%d, filter=%d, split=%d)",
            len(cal_rows),
            len(axis_edges),
            mode_counts.get("rollup", 0),
            mode_counts.get("filter", 0),
            mode_counts.get("split", 0),
        )
        return len(cal_rows)


# ---------------------------------------------------------------------- #
# Post-pass: 从 dimension_values_supplement.yaml 补枚举值
# ---------------------------------------------------------------------- #
def backfill_dimension_supplement(
    driver: Driver,
    *,
    supplement_path: Optional[Path] = None,
) -> int:
    """读 ``dimension_values_supplement.yaml``，把缺失的枚举值（含 ``全部`` rollup 哨兵）
    补到 ``DimensionValue`` 节点上，同时写 ``is_rollup`` / ``occur_cnt`` / ``notes``。

    遍历 ``hand_curated_supplement`` 与 ``schema_auto_supplement`` 两个列表，
    把 ``discovered_values`` / ``derived_values`` / ``discovered_additional_values``
    里列出的值 MERGE 到图上。

    若 YAML 不存在或读取失败则静默返回 0；维度节点不存在时跳过该条。
    """
    if supplement_path is None:
        here = Path(__file__).resolve().parents[3]  # repo root
        supplement_path = here / "data" / "test" / "dimension_values_supplement.yaml"

    if not supplement_path.exists():
        log.debug("backfill_dimension_supplement: %s not found, skipped", supplement_path)
        return 0

    try:
        import yaml as _yaml  # type: ignore[import-not-found]
    except ImportError:
        log.warning("backfill_dimension_supplement: pyyaml not installed, skipped")
        return 0

    try:
        data = _yaml.safe_load(supplement_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("backfill_dimension_supplement: failed to load %s: %s", supplement_path, exc)
        return 0

    if not isinstance(data, dict):
        log.warning("backfill_dimension_supplement: invalid YAML (expected mapping)")
        return 0

    total_written = 0

    def _extract_values(entry: dict) -> list[dict]:
        """从单条 supplement 条目中收集所有待写值。"""
        vals: list[dict] = []
        for field in ("discovered_values", "derived_values", "discovered_additional_values"):
            block = entry.get(field) or []
            if isinstance(block, list):
                vals.extend(block)
        return vals

    with neo4j_session(driver) as s:
        for section_key in ("hand_curated_supplement", "schema_auto_supplement"):
            section = data.get(section_key) or []
            if not isinstance(section, list):
                continue

            for entry in section:
                if not isinstance(entry, dict):
                    continue
                dim_key_str = str(entry.get("dimension_key") or "").strip()
                domain = str(entry.get("domain") or "").strip()
                dim_name = str(entry.get("dimension_name") or "").strip()
                if not (dim_key_str and domain and dim_name):
                    continue

                # 维度节点必须存在才写值
                hit = s.run(
                    "MATCH (d:Dimension {key: $k}) RETURN d.key LIMIT 1", k=dim_key_str
                ).single()
                if not hit:
                    continue

                raw_vals = _extract_values(entry)
                if not raw_vals:
                    continue

                dv_rows = []
                for v in raw_vals:
                    if not isinstance(v, dict):
                        continue
                    val = v.get("value")
                    if val is None or str(val).strip() == "":
                        continue
                    val_str = str(val).strip()
                    dv_rows.append({
                        "dv_key": dim_value_key(domain, dim_name, val_str),
                        "value": val_str,
                        "label": str(v.get("label") or val_str),
                        "occur_cnt": int(v.get("occur_cnt") or 0),
                        "is_rollup": bool(v.get("is_rollup", False)),
                        "notes": str(v.get("notes") or ""),
                    })

                if not dv_rows:
                    continue

                s.run(
                    """
                    MATCH (d:Dimension {key: $dim_key})
                    UNWIND $rows AS r
                    MERGE (dv:DimensionValue {key: r.dv_key})
                      ON CREATE SET dv.value = r.value, dv.label = r.label,
                                    dv.occur_cnt = r.occur_cnt,
                                    dv.is_rollup = r.is_rollup, dv.notes = r.notes,
                                    dv.dimension_key = $dim_key, dv.zone = 'metadata'
                      ON MATCH  SET dv.value = r.value, dv.label = r.label,
                                    dv.occur_cnt = CASE WHEN r.occur_cnt > 0 THEN r.occur_cnt
                                                        ELSE coalesce(dv.occur_cnt, 0) END,
                                    dv.is_rollup = r.is_rollup,
                                    dv.notes = CASE WHEN r.notes <> '' THEN r.notes
                                                    ELSE coalesce(dv.notes, '') END,
                                    dv.zone = 'metadata'
                    MERGE (d)-[:HAS_VALUE]->(dv)
                    """,
                    dim_key=dim_key_str,
                    rows=dv_rows,
                )
                total_written += len(dv_rows)

    if total_written:
        log.info("backfill_dimension_supplement: wrote %d DimensionValue node(s) from %s", total_written, supplement_path.name)
    else:
        log.debug("backfill_dimension_supplement: no new values (supplement may be empty or all dims missing)")

    return total_written


def backfill_metric_units(
    driver: Driver,
    *,
    supplement_path: Optional[Path] = None,
) -> int:
    """从 ``metric_unit_patches`` 补 Metric.unit。"""
    if supplement_path is None:
        here = Path(__file__).resolve().parents[3]  # repo root
        supplement_path = here / "data" / "test"

    if not supplement_path.exists():
        return 0

    try:
        import yaml as _yaml
    except ImportError:
        log.warning("backfill_metric_units: pyyaml not installed, skipped")
        return 0

    try:
        data = _yaml.safe_load(supplement_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("backfill_metric_units: failed to load %s: %s", supplement_path, exc)
        return 0

    patches = (data or {}).get("metric_unit_patches") or []
    if not patches:
        return 0

    total = 0
    with neo4j_session(driver) as s:
        for p in patches:
            domain = str(p.get("domain") or "").strip()
            name = str(p.get("metric_name") or "").strip()
            unit = str(p.get("unit") or "").strip()
            if not (domain and name and unit):
                continue
            m_key = f"met:{domain}:{name}"
            result = s.run(
                "MATCH (m:Metric {key: $key}) SET m.unit = $unit RETURN count(m) AS c",
                key=m_key, unit=unit,
            ).single()
            total += (result["c"] if result else 0)

    if total:
        log.info("backfill_metric_units: patched unit on %d metric(s)", total)
    return total


def sync_column_to_dataset_column(driver: Driver) -> int:
    """Propagate granularity_role / topline_value from Column → DatasetColumn via DERIVED_FROM."""
    with neo4j_session(driver) as s:
        result = s.run(
            """
            MATCH (dc:DatasetColumn)-[:DERIVED_FROM]->(c:Column)
            WHERE c.granularity_role IS NOT NULL
            SET dc.granularity_role = c.granularity_role,
                dc.topline_value = c.topline_value
            RETURN count(dc) AS cnt
            """
        ).single()
        cnt = result["cnt"] if result else 0
    if cnt:
        log.info("sync_column_to_dataset_column: propagated granularity to %d node(s)", cnt)
    return cnt


def derive_analyzed_by_from_topology(driver: Driver) -> int:
    """Infer ``Metric -[:ANALYZED_BY]-> Dimension`` from the Dataset/DatasetColumn topology.

    Path: Metric → Formula.dataset (physical table) ~ Dataset.parents
    → Dataset -[:HAS_COLUMN]-> DatasetColumn ← [:MAPS_TO_DATASET_COLUMN]- Dimension.

    Only creates edges within the same domain. Uses MERGE so it's idempotent
    and won't conflict with explicit ``analyzed_by`` from YAML.
    """
    with neo4j_session(driver) as s:
        result = s.run(
            """
            MATCH (m:Metric)-[:HAS_FORMULA]->(f:Formula)
            MATCH (f)-[:OF_VIEW]->(ds:Dataset)
            WHERE ds.domain = m.domain
            MATCH (ds)-[:HAS_COLUMN]->(dc:DatasetColumn)
                  <-[:MAPS_TO_DATASET_COLUMN]-(dim:Dimension)
            WHERE dim.domain = m.domain
            WITH m, dim
            MERGE (m)-[:ANALYZED_BY]->(dim)
            RETURN count(*) AS cnt
            """
        ).single()
        cnt = result["cnt"] if result else 0
    if cnt:
        log.info("derive_analyzed_by_from_topology: wrote %d ANALYZED_BY edge(s)", cnt)
    return cnt


def backfill_dimension_values_from_columns(driver: Driver) -> int:
    """从 Dimension 连接的 Column.sample_values 自动创建缺失的 DimensionValue 节点。

    路径：Dimension -[:MAPS_TO_DATASET_COLUMN]-> DatasetColumn -[:DERIVED_FROM]-> Column
    或：  Dimension -[:MAPS_TO_COLUMN]-> Column

    仅对尚无任何 HAS_VALUE 边的 Dimension 执行（避免覆盖手工维护的枚举值）。
    """
    with neo4j_session(driver) as s:
        records = s.run(
            """
            MATCH (dim:Dimension)
            WHERE NOT exists { (dim)-[:HAS_VALUE]->(:DimensionValue) }
            OPTIONAL MATCH (dim)-[:MAPS_TO_DATASET_COLUMN]->(dc:DatasetColumn)
                           -[:DERIVED_FROM]->(c1:Column)
            OPTIONAL MATCH (dim)-[:MAPS_TO_COLUMN]->(c2:Column)
            WITH dim,
                 coalesce(c1.sample_values, []) + coalesce(c2.sample_values, []) AS all_vals
            WHERE size(all_vals) > 0
            RETURN dim.key AS dim_key, dim.domain AS domain, dim.name AS dim_name,
                   [x IN all_vals WHERE x IS NOT NULL AND trim(x) <> '' | trim(x)] AS vals
            """
        ).data()

        total = 0
        for rec in records:
            d_key = rec["dim_key"]
            domain = rec["domain"] or ""
            dim_name = rec["dim_name"] or ""
            vals = rec["vals"] or []
            if not (d_key and domain and dim_name and vals):
                continue

            seen: set[str] = set()
            rows: list[dict] = []
            for v in vals:
                if v in seen:
                    continue
                seen.add(v)
                rows.append({
                    "dv_key": dim_value_key(domain, dim_name, v),
                    "value": v,
                    "is_rollup": v.strip() in TOPLINE_LITERALS_RICH,
                })

            if not rows:
                continue

            s.run(
                """
                MATCH (d:Dimension {key: $dim_key})
                UNWIND $rows AS r
                MERGE (dv:DimensionValue {key: r.dv_key})
                  ON CREATE SET dv.dimension_key = $dim_key, dv.value = r.value,
                                dv.label = r.value, dv.is_rollup = r.is_rollup,
                                dv.zone = 'auto_backfill'
                  ON MATCH  SET dv.is_rollup = CASE WHEN r.is_rollup THEN true
                                                    ELSE dv.is_rollup END
                MERGE (d)-[:HAS_VALUE]->(dv)
                """,
                dim_key=d_key,
                rows=rows,
            )
            total += len(rows)

    if total:
        log.info("backfill_dimension_values_from_columns: created %d DimensionValue node(s)", total)
    return total


__all__ = [
    "TOPLINE_LITERALS_DEFAULT",
    "TOPLINE_LITERALS_RICH",
    "backfill_dimension_supplement",
    "backfill_dimension_values_from_columns",
    "backfill_metric_units",
    "derive_calibers_from_formulas",
    "derive_column_time_grain",
    "derive_granularity_partitions",
    "derive_partitions_from_comments",
    "derive_partitions_from_data",
    "derive_analyzed_by_from_topology",
    "derive_view_alias_synonyms",
    "parse_topline_filters",
    "sync_column_to_dataset_column",
]
