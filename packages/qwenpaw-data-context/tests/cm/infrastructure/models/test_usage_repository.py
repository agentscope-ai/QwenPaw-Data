"""Exercise real SQLite commits against a synthetic Host-owned table."""

from dataclasses import asdict, replace
from datetime import UTC, datetime

import pytest
from context_manager.db import initialize_database
from context_manager.model.repository import ModelUsageRepository
from context_manager.model.usage import ModelUsageEvent
from context_manager.schema.relational import model_usages
from context_manager.settings.db import DbSettings
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    select,
)
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def event():
    return ModelUsageEvent(
        id="usage-1",
        user_id="user-a",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        session_id="session-a",
        chat_id="chat-a",
        source="cm",
        source_step="tool-a",
        model_type="chat",
        provider_id="provider-a",
        model_id="actual-model",
        input_tokens=12,
        output_tokens=4,
        cache_read_tokens=3,
        cache_creation_tokens=2,
        duration_ms=125,
        created_at=datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC),
    )


@pytest.fixture
def database(tmp_path):
    return initialize_database(DbSettings(url=f"sqlite:///{tmp_path / 'usage.db'}"))


@pytest.fixture
def host_table(database):
    schema = MetaData()
    table = Table(
        "model_usages",
        schema,
        Column("id", Text, primary_key=True),
        *(
            Column(name, Text, nullable=False)
            for name in (
                "user_id",
                "tenant_id",
                "workspace_id",
                "session_id",
                "chat_id",
                "source",
                "source_step",
                "model_type",
                "provider_id",
                "model_id",
            )
        ),
        *(
            Column(name, Integer, nullable=False)
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
                "duration_ms",
            )
        ),
        Column("created_at", DateTime, nullable=False),
        CheckConstraint("input_tokens < 100"),
    )
    schema.create_all(database.engine)
    return table


def test_commits_typed_rows_and_preserves_first_write_on_duplicate_id(
    database, host_table, event
):
    repository = ModelUsageRepository()
    repository.probe()
    embedding = replace(
        event,
        id="usage-2",
        session_id="",
        chat_id="",
        source="知识抽取",
        source_step="knowledge_extract#doc",
        model_type="embedding",
        input_tokens=20,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )
    # A caller's rollback must not undo the recorder's independent transaction.
    with database.engine.connect() as caller:
        transaction = caller.begin()
        caller.execute(select(host_table))
        repository.write((event, embedding))
        transaction.rollback()
    repository.write((replace(event, input_tokens=99), embedding))
    with database.engine.connect() as connection:
        rows = (
            connection.execute(select(model_usages).order_by(model_usages.c.id))
            .mappings()
            .all()
        )
    expected = [asdict(event), asdict(embedding)]
    for row in expected:
        row["created_at"] = row["created_at"].replace(tzinfo=None)
    assert [dict(row) for row in rows] == expected
    assert isinstance(rows[0]["created_at"], datetime)


def test_failed_batch_rolls_back_all_rows(database, host_table, event):
    repository = ModelUsageRepository()
    with pytest.raises(IntegrityError):
        repository.write((event, replace(event, id="usage-invalid", input_tokens=100)))
    with database.engine.connect() as connection:
        assert connection.execute(select(model_usages)).all() == []
    repository.write((event,))
    with database.engine.connect() as connection:
        assert connection.execute(select(model_usages.c.id)).scalars().all() == [
            event.id
        ]
