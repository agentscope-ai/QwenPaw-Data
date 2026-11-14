"""Build isolated relational schemas and SQL-backed Glossaries for tests."""

from pathlib import Path

from context_manager.db import CMDatabase
from context_manager.schema import DEFAULT_GLOSSARY_FILE, Glossary, GraphSchema
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool


def relational_database() -> Engine:
    """Create an initialized, isolated CM database shared across test threads."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    CMDatabase(engine).initialize()
    return engine


def make_glossary(
    schema: GraphSchema,
    seed_file: str | Path | None = None,
    *,
    engine: Engine | None = None,
    tenant_id: str = "tenant-1",
    workspace_id: str = "workspace-1",
) -> Glossary:
    """Use an isolated SQLite database unless a test supplies shared persistence."""
    if engine is None:
        engine = relational_database()
    else:
        CMDatabase(engine).initialize()
    return Glossary(
        schema,
        engine,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        seed_file=DEFAULT_GLOSSARY_FILE if seed_file is None else seed_file,
    )
