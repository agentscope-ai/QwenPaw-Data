"""CLI：反射物理层元数据并灌入 Neo4j。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证 `import context_manager` 指向本仓库的 context_manager 包（脚本在 scripts/ 下运行）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from context_manager.ingest import ingest_postgres, ingest_odps, ingest_holo  # noqa: E402


def main() -> int:
    """解析 --wipe / --postgres / --odps / --holo，写入 Neo4j。

    默认行为：从远端 Holo (HTTP API) 反射物理层元数据。
    """
    p = argparse.ArgumentParser(
        description="反射物理层元数据 → Neo4j。默认从远端 Holo 反射；也可选本地 Postgres / ODPS。"
    )
    p.add_argument("--wipe", action="store_true", help="DETACH DELETE existing graph first")
    p.add_argument("--force", action="store_true", help="强制重新反射，即使物理层已存在")

    # 物理层反射选项
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--holo",
        action="store_true",
        default=True,
        help="从远端 Holo (HTTP API) 反射表元数据（默认行为）",
    )
    group.add_argument(
        "--postgres",
        action="store_true",
        help="从当前数据源（PostgreSQL/Hologres，凭证来自 semantic_config.db）反射表元数据",
    )
    group.add_argument(
        "--odps",
        action="store_true",
        help="从 ODPS (MaxCompute) 反射表元数据",
    )

    # Holo 选项
    p.add_argument(
        "--holo-graph-id",
        default=None,
        help="写入 Neo4j 时 :Database{name} 用的 id（默认用 active datasource 的 dbname）",
    )

    # Postgres 选项
    p.add_argument(
        "--pg-graph-id",
        default=None,
        help="写入 Neo4j 时 :Database{name} 用的 id（默认用 active datasource 的 dbname）",
    )

    # ODPS 选项
    p.add_argument(
        "--odps-graph-id",
        default=None,
        help="写入 Neo4j 时 :Database{name} 用的 id（默认与 odps_project 相同，例如 analytics_dw）",
    )
    p.add_argument(
        "--odps-project",
        default=None,
        help="ODPS project 名（默认读 CFG.odps_project）",
    )
    p.add_argument(
        "--odps-table-prefix",
        default=None,
        help="只反射前缀匹配的 ODPS 表（project 可能上万张表，建议限制）",
    )

    # 通用选项:只反射语义层涉及的相关表(白名单),避免全量反射产生孤立节点
    p.add_argument(
        "--only-tables",
        default=None,
        help="只反射这些表(逗号分隔),避免全量反射产生孤立物理节点",
    )
    args = p.parse_args()

    # skip_if_exists 逻辑：默认跳过已存在的物理层，--force 强制重新反射
    skip_if_exists = not args.force

    # 派生 only_tables 白名单
    only_tables: list[str] | None = None
    if args.only_tables:
        only_tables = [t.strip() for t in args.only_tables.split(",") if t.strip()]

    # 显式指定了 --postgres 或 --odps
    if args.postgres:
        ingest_postgres(db_id=args.pg_graph_id, wipe=args.wipe, skip_if_exists=skip_if_exists, only_tables=only_tables)
    elif args.odps:
        ingest_odps(
            db_id=args.odps_graph_id,
            wipe=args.wipe,
            project=args.odps_project,
            table_prefix=args.odps_table_prefix,
            skip_if_exists=skip_if_exists,
            only_tables=only_tables,
        )
    # 默认：远端 Holo
    else:
        ingest_holo(db_id=args.holo_graph_id, wipe=args.wipe, skip_if_exists=skip_if_exists, only_tables=only_tables)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
