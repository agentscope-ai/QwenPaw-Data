"""Partial updates must keep the fields the caller did not send.

Regression tests for the semantic-config repositories: the UPDATE statements
used to overwrite every column, so a partial payload (e.g. from the CLI)
nulled out the remaining fields — hitting NOT NULL constraints or silently
erasing data. The repositories now use COALESCE to preserve omitted fields.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from semantic_config.models.biz_domain import BizDomainCreate, BizDomainUpdate
from semantic_config.models.datasource import DatasourceCreate
from semantic_config.models.dimension import DimensionCreate, DimensionUpdate
from semantic_config.models.metric_lib import MetricCreate, MetricUpdate
from semantic_config.services import biz_domain_service, dimension_service, metric_lib_service
from semantic_config.services import datasource_service

_SCHEMA = (
    Path(__file__).resolve().parents[1] / "src" / "semantic_config" / "schema.sql"
)


@pytest.fixture()
async def db(tmp_path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(tmp_path / "semantic_config.db")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    await conn.commit()
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_domain(db: aiosqlite.Connection) -> tuple[str, int]:
    datasource = await datasource_service.create(
        db,
        DatasourceCreate(
            datasource_name="ds",
            datasource_type="postgresql",
            config={
                "host": "127.0.0.1",
                "port": 5432,
                "dbname": "db",
                "user": "u",
                "password": "p",
            },
        ),
    )
    domain = await biz_domain_service.create(
        db,
        BizDomainCreate(
            datasource_id=datasource.datasource_id,
            domain_name="d1",
            display_name="Domain 1",
            description="desc",
            aliases="a1",
        ),
    )
    return datasource.datasource_id, domain.domain_id


async def test_metric_partial_update_keeps_other_fields(db: aiosqlite.Connection) -> None:
    datasource_id, domain_id = await _seed_domain(db)
    created = await metric_lib_service.create(
        db,
        MetricCreate(
            datasource_id=datasource_id,
            domain_id=domain_id,
            metric_name="m1",
            description="original",
            unit="CNY",
            is_polaris=True,
            synonyms="s1",
        ),
    )

    updated = await metric_lib_service.update(
        db, created.id, MetricUpdate(description="changed")
    )

    assert updated.description == "changed"
    assert updated.metric_name == "m1"
    assert updated.unit == "CNY"
    assert updated.is_polaris is True
    assert updated.synonyms == "s1"


async def test_dimension_partial_update_keeps_other_fields(db: aiosqlite.Connection) -> None:
    datasource_id, domain_id = await _seed_domain(db)
    created = await dimension_service.create(
        db,
        DimensionCreate(
            datasource_id=datasource_id,
            domain_id=domain_id,
            dimension_name="dim1",
            description="original",
            synonyms="s1",
            enums="a,b",
        ),
    )

    updated = await dimension_service.update(
        db, created.id, DimensionUpdate(description="changed")
    )

    assert updated.description == "changed"
    assert updated.dimension_name == "dim1"
    assert updated.synonyms == "s1"
    assert updated.enums == "a,b"


async def test_domain_partial_update_keeps_other_fields(db: aiosqlite.Connection) -> None:
    _, domain_id = await _seed_domain(db)

    updated = await biz_domain_service.update(
        db, domain_id, BizDomainUpdate(description="changed")
    )

    assert updated.description == "changed"
    assert updated.domain_name == "d1"
    assert updated.display_name == "Domain 1"
    assert updated.aliases == "a1"
