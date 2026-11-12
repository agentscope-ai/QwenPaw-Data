"""Start independent editing/profiling processes against an empty PostgreSQL DB."""

import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url


def test_postgresql_concurrent_initialization(tmp_path):
    configured = os.environ.get("SC_SCHEMA_TEST_PG_URL")
    if not configured:
        pytest.skip("Set SC_SCHEMA_TEST_PG_URL to an isolated PostgreSQL test server")
    admin_url = make_url(configured).set(drivername="postgresql+psycopg")
    database = "schema_startup_" + uuid4().hex
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
    url = admin_url.set(database=database)
    empty_env = tmp_path / "empty.env"
    empty_env.touch()
    gate = tmp_path / "start"
    source = Path(__file__).resolve().parents[2] / "src"
    environment = {
        **os.environ,
        "QWENPAW_DATA_DB_URL": url.render_as_string(hide_password=False),
        "QWENPAW_DATA_ENV_FILE": str(empty_env),
        "PYTHONPATH": os.pathsep.join((str(source), os.environ.get("PYTHONPATH", ""))),
    }
    script = """
import asyncio, sys, time
from pathlib import Path
from semantic_config.db import init_db
from semantic_config.engine import reset_engine
from profiling.db import init_probe_db
while not Path(sys.argv[1]).exists():
    time.sleep(.01)
async def start():
    try:
        await init_db()
        await init_probe_db()
    finally:
        await reset_engine()
asyncio.run(start())
"""
    workers = []
    engine = create_engine(url)
    try:
        for _ in range(4):
            workers.append(
                subprocess.Popen(
                    [sys.executable, "-c", script, str(gate)],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
        gate.touch()
        outcomes = [
            (worker.communicate(timeout=60)[0], worker.returncode) for worker in workers
        ]
        assert all(code == 0 for _, code in outcomes), outcomes
        from profiling.tables import PROFILING_TABLE_LIST
        from semantic_config.tables import SC_TABLE_LIST

        actual = set(inspect(engine).get_table_names())
        assert {
            table.name for table in (*SC_TABLE_LIST, *PROFILING_TABLE_LIST)
        } <= actual
        assert {"dimension_type", "child_dimensions", "priority"} <= {
            column["name"]
            for column in inspect(engine).get_columns("staging_dimension")
        }
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
        engine.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database}"')
        admin.dispose()
