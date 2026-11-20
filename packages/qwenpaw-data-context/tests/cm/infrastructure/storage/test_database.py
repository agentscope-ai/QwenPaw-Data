"""Verify CM database initialization and shared business writes."""

import pytest
from context_manager.db import (
    initialize_database,
)
from context_manager.schema.relational import metadata
from context_manager.settings.db import DbSettings
from sqlalchemy import inspect, text

from cm.infrastructure.lifecycle.support import create_external_tables


def test_initialize_is_idempotent_and_preserves_external_tables(tmp_path):
    database = initialize_database(
        DbSettings(url=f"sqlite:///{tmp_path / 'shared.db'}")
    )
    with database.engine.begin() as connection:
        connection.execute(text("CREATE TABLE host_data (value TEXT)"))
        connection.execute(text("INSERT INTO host_data VALUES ('keep')"))
    database.initialize()
    database.initialize()
    assert set(inspect(database.engine).get_table_names()) == set(metadata.tables) | {
        "host_data"
    }
    with database.engine.connect() as connection:
        assert (
            connection.execute(text("SELECT value FROM host_data")).scalar_one()
            == "keep"
        )


@pytest.mark.parametrize("missing", ["table", "column"])
def test_external_dependencies_are_checked_without_repair(tmp_path, missing):
    database = initialize_database(
        DbSettings(url=f"sqlite:///{tmp_path / 'dependencies.db'}")
    )
    create_external_tables(database.engine)
    database.validate_dependencies(history_source="sql")
    with database.engine.begin() as connection:
        connection.execute(text("DROP TABLE chat_events"))
        if missing == "column":
            connection.execute(text("CREATE TABLE chat_events (chat_id TEXT)"))

    database.initialize()
    with pytest.raises(RuntimeError, match="chat_events.*missing"):
        database.validate_dependencies(history_source="sql")
    inspector = inspect(database.engine)
    if missing == "table":
        assert not inspector.has_table("chat_events")
    else:
        assert [c["name"] for c in inspector.get_columns("chat_events")] == ["chat_id"]
