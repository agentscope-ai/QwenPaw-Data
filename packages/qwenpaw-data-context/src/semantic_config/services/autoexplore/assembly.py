"""mg-088 自动探查：行组装 + payload 校验补位 + 人工优先列级跳过（§3.5/§3.7）。

- 行键对齐 ``upsert_rows`` 消费面（semantic_import_service tables 段行构造同款）；
- 组装后使用本模块行契约校验，不依赖图服务的包；
- 人工优先：编辑器层 table_column 存在的 (table_name, column_name) = 人工数据在场
  → 该列自动探查不写图（编辑器层行本身即标记，零 schema 变更）。
"""

from __future__ import annotations

from pydantic import BaseModel


class _TableRow(BaseModel):
    datasource_id: str
    table_name: str


class _ColumnRow(_TableRow):
    column_name: str
    data_type: str
    is_primary: bool
    is_nullable: bool
    column_comment: str
    sample_values: list[str]


def split_manual_columns(full_name: str, columns, manual_keys: set) -> tuple[list, int]:
    """人工优先列级跳过：返回 (自动列, 人工在场列数)。

    ``manual_keys`` = 编辑器层 table_column 的 (两段全名, 列名) 集。
    自动列为空且人工数>0 → 整表 skipped_manual（管线层判定）。
    """
    auto = [c for c in columns if (full_name, c.name) not in manual_keys]
    return auto, len(columns) - len(auto)


def assemble_table_rows(
    datasource_id: str,
    full_name: str,
    columns,
    samples_by_col: dict[str, list[str]],
) -> tuple[dict, list[dict]]:
    """一组列 → (table 行, table_column 行列表)，先过 payload 校验再产出。

    table 行 = ``{"datasource_id", "table_name"}``；table_column 行字段逐一对齐
    正名行词汇（data_type/is_primary/is_nullable/column_comment/sample_values）。
    """
    table_row = _TableRow(
        datasource_id=datasource_id, table_name=full_name
    ).model_dump()
    col_rows = [
        _ColumnRow(
            **table_row,
            column_name=c.name,
            data_type=c.type or "",
            is_primary=bool(c.pk),
            is_nullable=bool(c.nullable),
            column_comment=c.comment or "",
            sample_values=list(samples_by_col.get(c.name, [])),
        ).model_dump()
        for c in columns
    ]
    return table_row, col_rows
