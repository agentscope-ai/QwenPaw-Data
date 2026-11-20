# -*- coding: utf-8 -*-
"""用 Alembic 对比现库与 ``Base.metadata``，只应用加法 DDL。"""

from __future__ import annotations

import logging

from alembic.autogenerate import produce_migrations
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations.ops import (
    AddColumnOp,
    AlterColumnOp,
    CreateIndexOp,
    CreateTableOp,
    DropColumnOp,
    DropIndexOp,
    DropTableOp,
)
from sqlalchemy.engine import Connection

from qwenpaw_data.host.core.db.tables import Base

logger = logging.getLogger(__name__)


class SchemaAlignError(RuntimeError):
    pass


def _is_host_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    # 同库有 Auth/SC 表，不纳入对比，否则会被当成待删除。
    if type_ == "table":
        return name in Base.metadata.tables
    return obj.table.name in Base.metadata.tables


def align_host_schema(connection: Connection) -> None:
    # 启动只保证列在。类型 / default 不自动改；可空差异见下方跳过 AlterColumn。
    ctx = MigrationContext.configure(
        connection,
        opts={
            "compare_type": False,
            "compare_server_default": False,
            "include_object": _is_host_object,
        },
    )
    operations = Operations(ctx)
    applied: list[str] = []

    pending = list(produce_migrations(ctx, Base.metadata).upgrade_ops.ops)
    while pending:
        op = pending.pop(0)
        if getattr(op, "ops", None):
            pending.extend(op.ops)
            continue
        if isinstance(op, (DropColumnOp, DropIndexOp, DropTableOp, AlterColumnOp)):
            continue  # 残留对象、已有列的可空差异都不改
        if isinstance(op, CreateTableOp):
            operations.invoke(op)
            applied.append(op.table_name)
            continue
        if isinstance(op, AddColumnOp):
            column = op.column
            # 无 server_default 的 NOT NULL 无法 ALTER ADD。
            if not column.nullable and column.server_default is None:
                raise SchemaAlignError(
                    f"{op.table_name}.{column.name}: ADD COLUMN of a NOT NULL "
                    "column requires server_default"
                )
            operations.invoke(op)
            applied.append(f"{op.table_name}.{column.name}")
            continue
        if isinstance(op, CreateIndexOp):
            operations.invoke(op)
            applied.append(op.index_name or op.table_name)
            continue
        raise SchemaAlignError(
            f"host schema change is not additive: {type(op).__name__}"
        )

    if applied:
        logger.info("host schema aligned: %s", ", ".join(applied))
