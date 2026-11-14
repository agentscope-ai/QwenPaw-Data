"""BigQuery 的物理表来源：一条走 SQL，一条走 spider 的 schema JSON。

- :class:`BigQuerySourceTableInput` —— 真连，只提供 SQL 文本与引用规则；
  并发、预算、分区识别在 :class:`~.table_input_sql.SqlTableInput`。
- :class:`BigQueryJsonTableInput` —— spider2 发行版为每张表附带 schema
  JSON（``resource/databases/bigquery/<db>/<project.dataset>/<table>.json``），
  快照可以完全由文件构造，**零 SQL、零凭证、零费用**。

------------------------------------------------------------------------
作用域：一个来源 = 一个 dataset
------------------------------------------------------------------------
BigQuery 的层级是 project → dataset → table。本来源对齐 PG 的「一个来源
一个库」：dataset 就是「库」，从 ``config.input_query_log_dbname``（可写 ``project.dataset``）
或 ``connection`` 的 :data:`BQ_PROJECT_KEYS` / :data:`BQ_DATASET_KEYS` 解析
（方言知识，同 PG 认 dbname/database）。跨 dataset 的画像 = 多个来源。
（JSON 来源例外：spider 按 ``<db>`` 打包，一个 db 可含多个 dataset，
作用域因此是一个 db 目录，见 :class:`BigQueryJsonTableInput`。）

------------------------------------------------------------------------
分片表（sharded tables）→ 通配表名
------------------------------------------------------------------------
spider2 的 ga4 / ga360 / firebase 全是 ``events_20201101`` 这类**日期分片表**
—— 物理上几百张同构表，逻辑上一张。:func:`merge_shard_families` 把它们归并成
``events_*``：这不是本包发明的记号，而是 BigQuery 自己的通配表名，可以直接
``FROM `p.d.events_*``` 查询（数据集 ``interface/bigquery.py`` 的示例就这么写）。
列结构取**最新分片**为代表，与 BigQuery 通配查询「取最近创建的分片的
schema」语义一致。两条来源共用这一套归并。

真正的分区表（按列分区）走另一条路：``is_partitioning_column`` 是列级事实，
:meth:`~BigQuerySourceTableInput._partition_column` 优先用它，探枚举时只扫
最新分区。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from ..config import AlgoConfig
from ..snapshots.base import as_str_list
from .table_input_sql import quote_literal
from .table_input_base import TableInput
from .table_input_sql import SqlTableInput


#: connection 里 project / dataset 的候选键。**方言规则**，同
#: ``query_log_postgres.PG_DBNAME_KEYS``。
BQ_PROJECT_KEYS: tuple[str, ...] = ("project", "project_id")
BQ_DATASET_KEYS: tuple[str, ...] = ("dataset", "dataset_id", "schema")

#: ``table_comment`` 取自 TABLE_OPTIONS 的 description —— 那里的
#: ``option_value`` 是带引号的字面量表示（``"..."``），TRIM 剥掉外层引号。
#: ``object_type`` / ``view_definition`` 与 PG 版对齐，供 dataset 直通；
#: 物化视图的定义不在 VIEWS 里，为 NULL，同 PG 版的处理。
_SQL_TABLES = """
    SELECT t.table_name,
           TRIM(o.option_value, '"') AS table_comment,
           CASE t.table_type
               WHEN 'BASE TABLE' THEN 'TABLE'
               WHEN 'VIEW' THEN 'VIEW'
               WHEN 'MATERIALIZED VIEW' THEN 'MATERIALIZED_VIEW'
               ELSE t.table_type
           END AS object_type,
           v.view_definition
    FROM {ds}.INFORMATION_SCHEMA.TABLES AS t
    LEFT JOIN {ds}.INFORMATION_SCHEMA.TABLE_OPTIONS AS o
      ON o.table_name = t.table_name AND o.option_name = 'description'
    LEFT JOIN {ds}.INFORMATION_SCHEMA.VIEWS AS v
      ON v.table_name = t.table_name
    WHERE t.table_type IN ('BASE TABLE', 'VIEW', 'MATERIALIZED VIEW', 'EXTERNAL')
      {exclude_tables}
    ORDER BY t.table_name
"""

#: **SQL 直接产出契约要求的列名与取值**，不靠事后改名（同 PG 版）。
#:
#: - 列注释在 COLUMN_FIELD_PATHS（COLUMNS 里没有）；按 field_path =
#:   column_name 只取顶层列，STRUCT 的嵌套子字段不展开 —— 顶层列的
#:   ``data_type`` 已含完整 ``STRUCT<...>`` 签名。
#: - 主键是 BigQuery 的非强制约束（2023 GA），公开数据集里罕见但真实存在，
#:   按 TABLE_CONSTRAINTS + KEY_COLUMN_USAGE 如实回答，不猜。
#: - ``is_hidden = 'NO'`` 排除 ``_PARTITIONTIME`` 这类伪列，对应 PG 版的
#:   ``attnum > 0``。
#: - ``is_partitioning_column`` / ``clustering_ordinal_position`` 是额外列：
#:   契约不要求，但前者是 :meth:`_partition_column` 的依据。
_SQL_COLUMNS = """
    WITH pk AS (
        SELECT k.table_name, k.column_name
        FROM {ds}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS k
        JOIN {ds}.INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS tc
          ON tc.constraint_name = k.constraint_name
         AND tc.table_name = k.table_name
        WHERE tc.constraint_type = 'PRIMARY KEY'
    )
    SELECT c.column_name,
           c.data_type,
           CASE c.is_nullable WHEN 'NO' THEN 'N' ELSE 'Y' END AS is_nullable,
           p.description AS column_comment,
           CAST(NULL AS STRING) AS column_name_cn,
           CASE WHEN pk.column_name IS NOT NULL THEN 'Y' ELSE 'N' END AS is_primary,
           c.ordinal_position,
           c.is_partitioning_column,
           c.clustering_ordinal_position
    FROM {ds}.INFORMATION_SCHEMA.COLUMNS AS c
    LEFT JOIN {ds}.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS AS p
      ON p.table_name = c.table_name AND p.field_path = c.column_name
    LEFT JOIN pk
      ON pk.table_name = c.table_name AND pk.column_name = c.column_name
    WHERE c.table_name = {target}
      AND c.is_hidden = 'NO'
    ORDER BY c.ordinal_position
"""


class BigQuerySourceTableInput(SqlTableInput):
    """BigQuery 单 dataset 的物理表 + 视图 + 分片表族。

    ``object_type`` / ``view_definition`` / ``ordinal_position`` /
    ``is_partitioning_column`` / ``clustering_ordinal_position`` 作为额外列
    保留。表不存在时 INFORMATION_SCHEMA 查询返回空集而非报错，落进
    「缺失的表可以不出现在返回值里」的约定。
    """

    dialect_keys = ("bigquery", "bq")
    dialect = "bigquery"

    # -- 作用域解析 ------------------------------------------------------

    def dataset_path(self) -> str:
        """``project.dataset``（或裸 ``dataset``，走执行器的默认 project）。

        显式 ``config.input_query_log_dbname`` 优先，否则按候选键从 ``connection`` 拼。
        解析不出 dataset 直接抛 —— 表层是硬依赖，早失败早知道。
        """
        if self.config.input_query_log_dbname:
            return str(self.config.input_query_log_dbname)
        conn = self.config.datasource_connection
        dataset = next((str(conn[k]) for k in BQ_DATASET_KEYS if conn.get(k)), "")
        if not dataset:
            raise ValueError(
                "BigQuery 来源解析不出 dataset：config.input_query_log_dbname 为空，"
                f"connection 里也没有 {'/'.join(BQ_DATASET_KEYS)}"
            )
        if "." in dataset:
            return dataset
        project = next((str(conn[k]) for k in BQ_PROJECT_KEYS if conn.get(k)), "")
        return f"{project}.{dataset}" if project else dataset

    def _ds(self) -> str:
        """INFORMATION_SCHEMA 的限定前缀：```project.dataset```。"""
        return self.quote_ident(self.dataset_path())

    def _table_path(self, table_name: str) -> str:
        """数据查询用的完整表路径；``events_*`` 这类通配名同样适用。"""
        return self.quote_ident(f"{self.dataset_path()}.{table_name}")

    # -- 方言 SQL --------------------------------------------------------

    def sql_tables(self) -> str:
        exclude_tables = "\n      ".join(
            f"AND t.table_name NOT LIKE {quote_literal(p)}"
            for p in self.config.input_table_exclude_patterns
        )
        return _SQL_TABLES.format(ds=self._ds(), exclude_tables=exclude_tables)

    def sql_columns(self, table_name: str) -> str:
        name = str(table_name)
        if name.endswith("_*"):
            # 分片表族：取最新分片为代表。前缀不进正则（避免转义问题），
            # 用 STARTS_WITH + 「剩余部分全是 6~8 位数字」双条件锁定分片。
            prefix = name[:-1]
            target = (
                f"(SELECT MAX(table_name)"
                f" FROM {self._ds()}.INFORMATION_SCHEMA.TABLES"
                f" WHERE STARTS_WITH(table_name, {quote_literal(prefix)})"
                f" AND REGEXP_CONTAINS(SUBSTR(table_name, {len(prefix) + 1}),"
                f" r'^[0-9]{{6,8}}$'))"
            )
        else:
            target = quote_literal(name)
        return _SQL_COLUMNS.format(ds=self._ds(), target=target)

    def sql_distinct(
        self,
        table_name: str,
        column_name: str,
        partition: str | None,
    ) -> str:
        source = self._table_path(table_name)
        where = ""
        if partition:
            part = self.quote_ident(partition)
            # 只探最新分区。分片表族不加 _TABLE_SUFFIX 过滤：子查询过滤在
            # BigQuery 上不裁剪分片，扫描量不变，不如让 DISTINCT 覆盖全族。
            where = f" WHERE {part} = (SELECT MAX({part}) FROM {source})"
        return (
            f"SELECT DISTINCT {self.quote_ident(column_name)} AS v "
            f"FROM {source}{where} LIMIT {self.config.input_table_enums_max_distinct}"
        )

    def quote_ident(self, name: str) -> str:
        return "`" + str(name).replace("`", "\\`") + "`"

    # -- 分片 / 分区 ------------------------------------------------------

    def merge_partitioned(self, frame: pd.DataFrame) -> pd.DataFrame:
        """分片族归并；sql_tables 的 ORDER BY 保证 keep="last" 取最新分片。"""
        return merge_shard_families(frame)

    def _partition_column(self, frame: pd.DataFrame) -> str | None:  # type: ignore[override]
        """优先用声明的分区列（列级事实），再退回基类的名字启发式。"""
        if "is_partitioning_column" in frame.columns:
            hit = frame.loc[
                frame["is_partitioning_column"].astype(str) == "YES", "column_name"
            ]
            if len(hit):
                return str(hit.iloc[0])
        return SqlTableInput._partition_column(frame)


# ===========================================================================
# spider schema JSON 来源
# ===========================================================================

#: connection 里 schema 目录的候选键（方言规则，同 BQ_DATASET_KEYS）。
BQ_SCHEMA_DIR_KEYS: tuple[str, ...] = ("schema_dir", "schema_path")


class BigQueryJsonTableInput(TableInput):
    """从 spider2 的 schema JSON 目录构造快照，**不执行任何 SQL**。

    ``schema_dir`` 指向一个 db 目录（含若干 ``<project.dataset>`` 子目录）
    或单个 dataset 目录（直接含 ``*.json``）。每个 JSON 一张物理表::

        table_name / table_fullname / column_names / column_types /
        nested_column_names / nested_column_types / description / sample_rows

    snowflake 的 schema JSON 是同一格式的子集（没有 ``nested_*``），
    本类原样可读 —— 将来接 snowflake 时直接复用或起个语义别名。

    ------------------------------------------------------------------
    转换规则（外部 schema 的适配器，转换发生在 fetch_* 内）
    ------------------------------------------------------------------
    - **列面 = nested 的叶子路径**：BigQuery 文件的真实语义列在
      ``nested_column_names`` / ``nested_column_types``（``totals.visits``
      这类叶子路径）—— 顶层 ``column_names`` 里的 ``totals`` / ``hits``
      只是 STRUCT/ARRAY 容器签名，指标与维度全在叶子层（GA360 的
      visits / pageviews / transactionRevenue 均如此）。nested 列表同时
      枚举了容器行（``totals`` 与 ``totals.visits`` 并存），容器不是可
      取值的列，滤掉只留叶子；没有 ``nested_*`` 的文件（snowflake、
      平铺表）回落顶层，行为不变。
    - **列注释**：``description`` 与 nested（BigQuery）或顶层（snowflake）
      按长度对齐，叶子路径直接命中各自的注释。
    - **枚举 = sample_rows 里的真实取值**：值是真的、计数未知，正合契约
      「``column_distinct_num=None`` 表示不止这些」的语义；嵌套路径按段
      下钻取叶子值（数组逐元素展开），仍为 dict/list 的值不是枚举，跳过。
    - **is_nullable="Y" / is_primary="N" 是弱断言**：JSON 没记录这两项；
      BigQuery/Snowflake 列默认可空、主键非强制且极罕见，按默认值给而不是
      留 None —— ``as_yn(None)`` 会归成 "N"，那等于宣称全表 NOT NULL。
    - **跨 dataset 重名**（69 个 bq db 里有 5 个）：重名表一律限定为
      ``<dataset>.<table>``，不重名的保持裸名 —— 表名是快照主键，静默去重
      等于丢表。
    - **分片族**：与真连路径共用 :func:`merge_shard_families`，受
      ``config.input_table_merge_partitions_enabled`` 控制；列结构取最新分片为代表。

    零 SQL 零执行器 —— 分派按 ``impl(config)`` 统一构造（``_registry``
    查表），schema 从 JSON 目录读。
    """

    # 一个类认领两个**方言**（同格式 JSON）—— 规则约束的是同一方言的别名数
    dialect_keys = ("bigquery_json", "snowflake_json")
    dialect = "bigquery_json"

    def __init__(
        self,
        config: AlgoConfig,
        *,
        schema_dir: str | Path | None = None,
    ) -> None:
        super().__init__(config)
        resolved = schema_dir or next(
            (
                self.config.datasource_connection[k]
                for k in BQ_SCHEMA_DIR_KEYS
                if self.config.datasource_connection.get(k)
            ),
            None,
        )
        if not resolved:
            raise ValueError(
                "BigQueryJsonTableInput 需要 schema 目录：构造参数 schema_dir "
                f"或 connection 里的 {'/'.join(BQ_SCHEMA_DIR_KEYS)}"
            )
        self._dir = Path(resolved)
        if not self._dir.is_dir():
            raise FileNotFoundError(f"schema 目录不存在: {self._dir}")
        self._records: dict[str, Mapping[str, Any]] | None = None

    # -- 读取与命名 ------------------------------------------------------

    def _load(self) -> dict[str, Mapping[str, Any]]:
        """{逻辑物理名: 记录}。读一次缓存；重名表限定 dataset 前缀。"""
        if self._records is not None:
            return self._records
        dataset_dirs = (
            [self._dir]
            if any(self._dir.glob("*.json"))
            else sorted(d for d in self._dir.iterdir() if d.is_dir())
        )
        entries: list[tuple[str, str, Mapping[str, Any]]] = []
        seen_in: dict[str, set[str]] = {}
        for directory in dataset_dirs:
            dataset = directory.name.rsplit(".", 1)[-1]
            for path in sorted(directory.glob("*.json")):
                record = json.loads(path.read_text(encoding="utf-8"))
                name = str(record.get("table_name") or path.stem)
                entries.append((dataset, name, record))
                seen_in.setdefault(name, set()).add(dataset)
        duplicated = {n for n, ds in seen_in.items() if len(ds) > 1}
        self._records = {
            (f"{dataset}.{name}" if name in duplicated else name): record
            for dataset, name, record in entries
        }
        return self._records

    # -- TableInput 实现 -------------------------------------------------

    async def fetch_table_meta(self) -> pd.DataFrame:
        records = self._load()
        frame = pd.DataFrame(
            [
                {
                    "table_name": name,
                    "table_comment": None,  # JSON 只有列级 description
                    "table_fullname": record.get("table_fullname"),
                }
                for name, record in records.items()
            ]
        )
        if len(frame) == 0:
            return frame
        frame = frame.sort_values("table_name").reset_index(drop=True)
        if self.config.input_table_merge_partitions_enabled:
            frame = merge_shard_families(frame)
        return frame

    async def fetch_column_meta(
        self,
        table_names: Sequence[str],
    ) -> dict[str, pd.DataFrame]:
        records = self._load()
        out: dict[str, pd.DataFrame] = {}
        for name in table_names:
            record = records.get(str(name)) or self._family_representative(str(name))
            if record is not None:
                out[str(name)] = self._columns_frame(str(name), record)
        return out

    def _family_representative(self, name: str) -> Mapping[str, Any] | None:
        """``events_*`` → 最新分片的记录（与真连路径的 MAX(table_name) 同义）。"""
        if not name.endswith("_*"):
            return None
        prefix = name[:-1]
        records = self._load()
        shards = [
            k
            for k in records
            if k.startswith(prefix) and re.fullmatch(r"\d{6,8}", k[len(prefix) :])
        ]
        return records[max(shards)] if shards else None

    @staticmethod
    def _columns_frame(table_name: str, record: Mapping[str, Any]) -> pd.DataFrame:
        names, types = _effective_columns(record)
        comments = _column_comments(record)
        samples = _sample_values(record, names)
        return pd.DataFrame(
            [
                {
                    "table_name": table_name,
                    "column_name": column,
                    "data_type": types[index] if index < len(types) else None,
                    "is_nullable": "Y",
                    "column_comment": comments.get(column),
                    "column_name_cn": None,
                    "is_primary": "N",
                    "column_enums": samples.get(column, []),
                    "column_distinct_num": None,
                    "ordinal_position": index + 1,
                }
                for index, column in enumerate(names)
            ]
        )


def _effective_columns(
    record: Mapping[str, Any],
) -> tuple[list[str], list[Any]]:
    """真实列面：``nested_*`` 的叶子路径优先，缺失则回落顶层。

    nested 列表把每一层都枚举成一行（``totals`` 与 ``totals.visits`` 并存）
    —— 容器行不是可取值的列，按「是任一路径的祖先前缀」滤掉。平铺表的
    nested 与顶层一致，滤后行为不变；snowflake 文件没有 nested_* → 顶层。
    """
    names = [str(n) for n in record.get("nested_column_names") or []]
    if names:
        types = list(record.get("nested_column_types") or [])
        containers: set[str] = set()
        for name in names:
            parts = name.split(".")
            for depth in range(1, len(parts)):
                containers.add(".".join(parts[:depth]))
        kept = [
            (name, types[index] if index < len(types) else None)
            for index, name in enumerate(names)
            if name not in containers
        ]
        return [n for n, _ in kept], [t for _, t in kept]
    return (
        [str(n) for n in record.get("column_names") or []],
        list(record.get("column_types") or []),
    )


def _column_comments(record: Mapping[str, Any]) -> dict[str, str]:
    """description 按长度对齐到 nested（BQ）或顶层（snowflake）。
    列面就是对齐面的子集，叶子路径直接命中各自的注释。"""
    descriptions = list(record.get("description") or [])
    nested = [str(n) for n in record.get("nested_column_names") or []]
    columns = [str(n) for n in record.get("column_names") or []]
    if nested and len(descriptions) == len(nested):
        pairs = zip(nested, descriptions)
    elif len(descriptions) == len(columns):
        pairs = zip(columns, descriptions)
    else:
        return {}
    return {
        name: str(text)
        for name, text in pairs
        if text is not None and str(text).strip()
    }


def _leaf_values(value: Any, parts: Sequence[str]) -> list[Any]:
    """按路径段下钻取叶子标量；数组逐元素展开（REPEATED STRUCT 的语义）。"""
    if value is None:
        return []
    if isinstance(value, list):
        out: list[Any] = []
        for item in value:
            out.extend(_leaf_values(item, parts))
        return out
    if not parts:
        return [] if isinstance(value, dict) else [value]
    if isinstance(value, dict):
        return _leaf_values(value.get(parts[0]), parts[1:])
    return []


def _sample_values(
    record: Mapping[str, Any], columns: Sequence[str]
) -> dict[str, list[str]]:
    """sample_rows 里各列的标量取值，嵌套路径按段下钻。"""
    rows = record.get("sample_rows")
    if not isinstance(rows, list):
        return {}
    out: dict[str, list[str]] = {}
    for column in columns:
        parts = str(column).split(".")
        collected = [
            value
            for row in rows
            if isinstance(row, dict)
            for value in _leaf_values(row, parts)
        ]
        if collected:
            # as_str_list：去重保序、丢 nan、截断 —— 规范化只有这一处定义
            out[str(column)] = as_str_list(collected)
    return out


#: 日期分片后缀（``_YYYYMM`` / ``_YYYYMMDD``）。
_SHARD_SUFFIX_RE = re.compile(r"_\d{6,8}$")


# TODO: 当前这个函数和pg的merged操作是基本等价的，看后面找个机会统一起来管理。
def merge_shard_families(frame: pd.DataFrame) -> pd.DataFrame:
    """``events_20201101`` → ``events_*``（BigQuery 通配表名）。

    与 PG 基类的差别只在归并后的名字：那边留基表名（PG 的分区裁剪写法），
    这里留通配名——它才是 BigQuery 里可直接 FROM 的逻辑表。调用方保证
    frame 已按 ``table_name`` 排序，``keep="last"`` 留下的因此是最新分片的
    元数据，与通配查询取最近分片 schema 的语义一致。

    ``table_fullname``（JSON 来源的额外列）同步换成通配路径 —— 它就是
    真实查询里写的 ```p.d.events_*```。
    """
    if frame is None or len(frame) == 0:
        return frame
    out = frame.copy()
    for column in ("table_name", "table_fullname"):
        if column in out.columns:
            out[column] = (
                out[column].astype(str).str.replace(_SHARD_SUFFIX_RE, "_*", regex=True)
            )
    return out.drop_duplicates(subset=["table_name"], keep="last").reset_index(
        drop=True
    )


__all__ = [
    "BQ_DATASET_KEYS",
    "BQ_PROJECT_KEYS",
    "BQ_SCHEMA_DIR_KEYS",
    "BigQueryJsonTableInput",
    "BigQuerySourceTableInput",
    "merge_shard_families",
]
