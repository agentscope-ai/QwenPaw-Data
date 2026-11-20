"""Keep frontend connection tests on the new CM connector dispatch."""

import sqlite3

import pytest
from semantic_config.services import connection_tester


@pytest.mark.asyncio
async def test_frontend_sqlite_probe_uses_real_driver(tmp_path):
    path = tmp_path / "source.db"
    sqlite3.connect(path).close()
    result = await connection_tester.test_connection("sqlite", {"path": str(path)})
    assert result.success
    missing = await connection_tester.test_connection(
        "sqlite", {"path": str(tmp_path / "missing.db")}
    )
    assert not missing.success
    assert not (tmp_path / "missing.db").exists()
