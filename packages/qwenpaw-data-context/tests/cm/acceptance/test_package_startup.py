"""Exercise the minimal semantic-config/profiling bridge to current CM ports."""

import sqlite3
from unittest.mock import Mock

import pytest
from context_manager.auth import bind_authorization
from profiling.cm_adapter import resolve_profile_out_dir
from profiling.sql_executor import execute_read
from semantic_config.cm_runtime import bind_runtime

from cm.infrastructure.access.support import authorization
from cm.retrieval.mcp_support import build_app
from cm.retrieval.sql.support import create_catalog


@pytest.mark.parametrize("user_id", ["user-1", "user-2"])
def test_profiling_queries_a_new_catalog_datasource_before_weaving(
    tmp_path, monkeypatch, user_id
):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (amount INTEGER)")
        db.executemany("INSERT INTO orders VALUES (?)", [(10,), (20,)])
    dsn = create_catalog(tmp_path, datasource_type="sqlite", config={"path": str(path)})
    _, store, runtime, _ = build_app(model_config_dsn=dsn)
    assert not store.nodes
    graph_lookup = Mock(side_effect=AssertionError("SQL must not resolve a graph key"))
    monkeypatch.setattr(runtime, "graph_store", graph_lookup)
    try:
        with bind_runtime(runtime), bind_authorization(authorization(user_id)):
            result = execute_read(
                "SELECT sum(amount) AS total FROM orders", datasource_id="warehouse"
            )
        assert result.columns == ("total",)
        assert result.rows == ((30,),)
        graph_lookup.assert_not_called()
        assert not store.nodes
    finally:
        runtime.close()


def test_profile_paths_reject_symlink_escape(tmp_path, monkeypatch):
    home, outside = tmp_path / "home", tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / "tenant").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(home))
    with pytest.raises(ValueError, match="symlinks"):
        resolve_profile_out_dir("tenant", "workspace", "datasource")
    assert not list(outside.iterdir())
