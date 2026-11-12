"""导出某个 datasource 的 CM 导入载荷到 JSON 文件（联调用，不写图、不发请求）。

用法：
    uv run python scripts/dump_weave_payload.py <datasource_id> <out.json> [--incremental]

载荷与 POST /api/semantic-config/weave-task/submit 实际发给 CM 的完全一致，
调用的是同一个 ``weave_assembler.assemble``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "packages" / "qwenpaw-data-context" / "src"))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402


async def main(datasource_id: str, out: Path, task_id: str, drop_first: bool) -> None:
    load_dotenv(_ROOT / ".env")

    from semantic_config.services import weave_assembler
    from semantic_config.tables import datasource

    engine = create_async_engine(os.environ["QWENPAW_DATA_DB_URL"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            ds = (
                (
                    await db.execute(
                        select(datasource).where(
                            datasource.c.datasource_id == datasource_id,
                            datasource.c.is_deleted == 0,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if ds is None:
                raise SystemExit(f"数据源不存在: {datasource_id}")
            tenant_id, workspace_id = ds["tenant_id"], ds["workspace_id"]
            print(
                f"数据源 {datasource_id} → tenant={tenant_id} workspace={workspace_id}"
            )

            payload = await weave_assembler.assemble(
                db,
                datasource_id,
                task_id,
                tenant_id,
                workspace_id,
                drop_semantic_first=drop_first,
            )
    finally:
        await engine.dispose()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    doms = payload.get("semantic", {}).get("domains") or []
    print(f"已写出: {out}  ({out.stat().st_size / 1024:.1f} KB)")
    print(f"  顶层键: {list(payload)}")
    print(f"  tables: {len(payload.get('tables') or [])}")
    print(f"  semantic.domains: {len(doms)}")
    for d in doms:
        dsets = d.get("datasets") or []
        print(
            f"    - {d.get('name')}: datasets={len(dsets)}"
            f" fields={sum(len(x.get('dataset_fields') or []) for x in dsets)}"
            f" dimensions={len(d.get('dimensions') or [])}"
            f" bindings={sum(len(x.get('dimension_bindings') or []) for x in (d.get('dimensions') or []))}"
            f" metrics={len(d.get('metrics') or [])}"
            f" calibers={sum(len(x.get('metric_calibers') or []) for x in (d.get('metrics') or []))}"
            f" verified_queries={len(d.get('verified_queries') or [])}"
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("datasource_id")
    p.add_argument("out", type=Path)
    p.add_argument("--task-id", default="DUMP_LOCAL")
    p.add_argument(
        "--incremental", action="store_true", help="drop_semantic_first=false"
    )
    a = p.parse_args()
    asyncio.run(main(a.datasource_id, a.out, a.task_id, not a.incremental))
