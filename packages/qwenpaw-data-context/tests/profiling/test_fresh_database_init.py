"""空库必须经过真实启动建表路径，不用 metadata.create_all 绕过建表名单。"""

from sqlalchemy import insert, inspect, select

from profiling.db import init_probe_db
from profiling.tables import PROFILING_TABLE_LIST, staging_biz_domain
from semantic_config.config import get_settings
from semantic_config.db import init_db
from semantic_config.engine import get_engine, reset_engine
from semantic_config.tables import dataset, dimension_dict, domain, metric_dict


async def test_fresh_startup_creates_formal_and_staging_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.db"
    monkeypatch.setattr(get_settings(), "db_url", f"sqlite+aiosqlite:///{db_path}")
    formal_tables = (domain, dataset, dimension_dict, metric_dict)
    assert not db_path.exists()

    try:
        # 与 server lifespan 一致：先 semantic_config，再 profiling。
        await init_db()
        async with get_engine().connect() as connection:
            names = await connection.run_sync(lambda c: inspect(c).get_table_names())
            assert {table.name for table in formal_tables} <= set(names)
            for table in formal_tables:
                indexes = await connection.run_sync(
                    lambda c: inspect(c).get_indexes(table.name)
                )
                assert {index.name for index in table.indexes} <= {
                    index["name"] for index in indexes
                }

        await init_probe_db()
        async with get_engine().begin() as connection:
            names = await connection.run_sync(lambda c: inspect(c).get_table_names())
            assert {table.name for table in PROFILING_TABLE_LIST} <= set(names)
            for table in formal_tables:
                assert (await connection.execute(select(table))).first() is None
            await connection.execute(insert(domain).values(domain_name="sales"))
            await connection.execute(
                insert(staging_biz_domain).values(
                    datasource_id="source-a",
                    domain_name="sales",
                    probe_status="CONFIRMED",
                )
            )

        # 再次启动必须幂等，不能删除正式资产或审核记录。
        await init_db()
        await init_probe_db()
        async with get_engine().connect() as connection:
            assert (
                await connection.execute(select(domain.c.domain_name))
            ).scalar_one() == "sales"
            assert (
                await connection.execute(select(staging_biz_domain.c.probe_status))
            ).scalar_one() == "CONFIRMED"
    finally:
        await reset_engine()
