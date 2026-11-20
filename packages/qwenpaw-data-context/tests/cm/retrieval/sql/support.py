"""Seed the existing semantic datasource table for connection-read tests."""

import json

from context_manager.db import close_database, initialize_database
from context_manager.settings.db import DbSettings
from semantic_config.tables import datasource


def create_catalog(tmp_path, *, config=None, **fields):
    dsn = f"sqlite:///{tmp_path / 'datasources.db'}"
    close_database()
    engine = initialize_database(DbSettings(url=dsn)).engine
    datasource.create(engine, checkfirst=True)
    values = {
        "tenant_id": "tenant-1",
        "workspace_id": "workspace-1",
        "datasource_id": "warehouse",
        "datasource_name": "Warehouse",
        "datasource_type": "postgresql",
        "config": json.dumps(
            config
            if config is not None
            else {
                "host": "postgres.internal",
                "port": 5432,
                "database": "analytics",
                "user": "reader",
                "password": "very-secret",
            }
        ),
        **fields,
    }
    with engine.begin() as connection:
        connection.execute(datasource.insert().values(**values))
    return dsn
