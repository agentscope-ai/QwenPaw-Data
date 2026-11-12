#!/usr/bin/env python3
"""对齐本地 SQLite 语义库与 semantic_config/tables.py 的结构（老库自对齐）。

背景：SQLAlchemy ``create_all(checkfirst=True)`` 只按表名判存——表一旦存在，代码
后续新增的列/索引永远不会落到老库，查询命中不存在的列时只在请求期炸 500
（如 biz_domain.import_id）。Hologres 侧需人工迁移；
本脚本给本地 SQLite 库提供等价的一键对齐，可手工执行。

处理规则（全部幂等，可重复执行）：
- 缺可空列 / 带 server_default 的列 → ``ALTER TABLE ADD COLUMN``；
- 缺 NOT NULL 且无默认列（SQLite 无法 ADD）→ 空表 DROP 按新结构重建；非空表
  报错退出（exit 2），请人工迁移，不擅自丢数据；
- 缺失索引 → ``CREATE INDEX IF NOT EXISTS``（唯一索引遇历史重复数据仅告警，不中断）；
- 残留列（库里有、代码已删）→ 仅 WARNING，不处理（不挡查询，由人工迁移清理）。

默认 dry-run（只打印计划）；``--apply`` 实际执行。库文件不存在时视为全新环境
直接跳过（建表是应用启动时 init_db 的职责）。

用法：
    export QWENPAW_DATA_DB_URL=sqlite+aiosqlite:////path/to/qwenpaw_data.db
    packages/qwenpaw-data-context/.venv/bin/python scripts/migrate/sc_align_sqlite_schema.py [--apply]
    # 也可显式指定库文件（覆盖 QWENPAW_DATA_DB_URL 解析）：
    packages/qwenpaw-data-context/.venv/bin/python scripts/migrate/sc_align_sqlite_schema.py --db /path/to/qwenpaw_data.db --apply

非 SQLite 后端（QWENPAW_DATA_DB_URL=postgresql/...）自动跳过：Hologres 不支持任意
ALTER/CREATE INDEX，须人工迁移。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_CONTEXT_SRC = (
    Path(__file__).resolve().parents[2] / "packages" / "qwenpaw-data-context" / "src"
)
sys.path.insert(0, str(_CONTEXT_SRC))

from sqlalchemy.dialects import sqlite as sa_sqlite  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from semantic_config.tables import SC_TABLE_LIST  # noqa: E402

_DIALECT = sa_sqlite.dialect()


def _resolve_db_path(explicit: str | None) -> str | None:
    """解析目标 SQLite 文件路径；非 SQLite 后端返回 None（调用方跳过）。"""
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    # 与 semantic_config 同源解析：QWENPAW_DATA_DB_URL（可为裸路径），
    # 缺省 <QWENPAW_DATA_HOME>/control_plane.db。
    from qwenpaw_data_auth.database import shared_database_url
    from sqlalchemy.engine import make_url

    url = make_url(shared_database_url())
    if url.get_backend_name() != "sqlite":
        return None
    return url.database


def _add_column_ddl(table_name: str, column) -> str:
    """编译 ADD COLUMN DDL；表名/列名/默认值均来自 tables.py 常量，可安全拼接。"""
    ddl = (
        f"ALTER TABLE {table_name} ADD COLUMN {column.name} "
        f"{column.type.compile(dialect=_DIALECT)}"
    )
    if not column.nullable and column.server_default is not None:
        ddl += f" NOT NULL DEFAULT {column.server_default.arg}"
    return ddl


def align_schema(conn: sqlite3.Connection, apply: bool) -> int:
    """对 SC_TABLE_LIST 逐表对齐。返回需人工迁移的表数量（>0 时调用方应非零退出）。"""
    human_required = 0
    for table in SC_TABLE_LIST:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table.name})")}
        if not existing:
            continue  # 表不存在：建表是 init_db/create_all 的职责，这里跳过
        expected = set(table.c.keys())
        missing = [table.c[name] for name in sorted(expected - existing)]
        extra = sorted(existing - expected)
        if extra:
            print(
                f"[warn] {table.name}: 残留列 {extra}（代码已删除；不挡查询，可手工清理）"
            )
        if not missing:
            continue
        unaddable = [
            c.name for c in missing if not c.nullable and c.server_default is None
        ]
        if unaddable:
            row_count = conn.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()[0]
            if row_count == 0:
                print(
                    f"[plan] {table.name}: 缺 NOT NULL 列 {unaddable}，SQLite 无法 ADD COLUMN；表为 0 行，DROP 重建"
                )
                if apply:
                    conn.execute(f"DROP TABLE {table.name}")
                    conn.execute(str(CreateTable(table).compile(dialect=_DIALECT)))
                    for idx in table.indexes:
                        conn.execute(
                            str(
                                CreateIndex(idx, if_not_exists=True).compile(
                                    dialect=_DIALECT
                                )
                            )
                        )
                    print(f"[done] {table.name}: 已按新结构重建（索引随表重建）")
            else:
                human_required += 1
                print(
                    f"[ERROR] {table.name}: 缺 NOT NULL 列 {unaddable} 且表内有 {row_count} 行，"
                    "无法自动对齐——请人工迁移，相关接口会持续 500"
                )
            continue
        for column in missing:
            ddl = _add_column_ddl(table.name, column)
            print(f"[plan] {ddl}")
            if apply:
                conn.execute(ddl)
                print(f"[done] {table.name}: 补列 {column.name}")

    existing_idx = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    for table in SC_TABLE_LIST:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table.name,)
        ).fetchone():
            continue
        for idx in table.indexes:
            if idx.name in existing_idx:
                continue
            ddl = str(CreateIndex(idx, if_not_exists=True).compile(dialect=_DIALECT))
            print(f"[plan] {ddl}")
            if apply:
                try:
                    conn.execute(ddl)
                    print(f"[done] 补建索引 {idx.name}")
                except sqlite3.Error as exc:
                    print(f"[warn] 索引 {idx.name} 补建失败（不中断）: {exc}")
    return human_required


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db", default=None, help="SQLite 库文件路径（缺省按 QWENPAW_DATA_DB_URL 解析）"
    )
    parser.add_argument(
        "--apply", action="store_true", help="实际执行（缺省 dry-run 只打印计划）"
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db)
    if db_path is None:
        print(
            "非 SQLite 后端（QWENPAW_DATA_DB_URL 非 sqlite），跳过；Hologres 请人工迁移"
        )
        return 0
    if not Path(db_path).exists():
        print(f"库文件不存在：{db_path}（全新环境，由应用启动时建表），跳过")
        return 0

    conn = sqlite3.connect(db_path)
    try:
        human_required = align_schema(conn, apply=args.apply)
        if args.apply:
            conn.commit()
            print(
                "对齐完成。"
                if human_required == 0
                else "存在需人工迁移的表，见上方 [ERROR]。"
            )
        else:
            conn.rollback()
            print("dry-run（未改动）；加 --apply 实际执行。")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 2 if human_required else 0


if __name__ == "__main__":
    sys.exit(main())
