"""Partial updates must keep the fields the caller did not send.

Regression tests for the semantic-config repositories: the UPDATE statements
used to overwrite every column, so a partial payload (e.g. from the CLI)
nulled out the remaining fields — hitting NOT NULL constraints or silently
erasing data. The repositories now use COALESCE to preserve omitted fields.
"""

from __future__ import annotations

import pytest
from _sc_dbutil import new_memory_session
from semantic_config.models.biz_domain import BizDomainCreate, BizDomainUpdate
from semantic_config.models.dimension import DimensionCreate, DimensionUpdate
from semantic_config.models.metric_lib import MetricCreate, MetricUpdate
from semantic_config.principal import LOCAL_DEV_PRINCIPAL
from semantic_config.services import (
    biz_domain_service,
    dimension_service,
    metric_lib_service,
)
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture()
async def db() -> AsyncSession:
    session = await new_memory_session()
    try:
        yield session
    finally:
        await session.close()


async def _seed_domain(db: AsyncSession) -> int:
    domain = await biz_domain_service.create(
        db,
        BizDomainCreate(
            domain_name="d1",
            description="desc",
        ),
        LOCAL_DEV_PRINCIPAL,
    )
    return domain.domain_id


async def test_metric_partial_update_keeps_other_fields(db: AsyncSession) -> None:
    domain_id = await _seed_domain(db)
    created = await metric_lib_service.create(
        db,
        MetricCreate(
            domain_id=domain_id,
            metric_name="m1",
            description="original",
            synonyms="s1",
            priority="HIGH",
        ),
        LOCAL_DEV_PRINCIPAL,
    )

    updated = await metric_lib_service.update(
        db,
        created.metric_id,
        MetricUpdate(description="changed"),
        LOCAL_DEV_PRINCIPAL,
    )

    assert updated.description == "changed"
    assert updated.metric_name == "m1"
    assert updated.synonyms == "s1"
    assert updated.priority == "HIGH"


async def test_dimension_partial_update_keeps_other_fields(db: AsyncSession) -> None:
    domain_id = await _seed_domain(db)
    created = await dimension_service.create(
        db,
        DimensionCreate(
            domain_id=domain_id,
            dimension_name="dim1",
            description="original",
            synonyms="s1",
            dimension_type="categorical",
            child_dimensions="child1$$$child2",
            priority="LOW",
        ),
        LOCAL_DEV_PRINCIPAL,
    )

    updated = await dimension_service.update(
        db,
        created.dimension_id,
        DimensionUpdate(description="changed"),
        LOCAL_DEV_PRINCIPAL,
    )

    assert updated.description == "changed"
    assert updated.dimension_name == "dim1"
    assert updated.synonyms == "s1"
    assert updated.dimension_type == "categorical"
    assert updated.child_dimensions == "child1$$$child2"
    assert updated.priority == "LOW"


async def test_domain_partial_update_keeps_other_fields(db: AsyncSession) -> None:
    domain_id = await _seed_domain(db)

    updated = await biz_domain_service.update(
        db, domain_id, BizDomainUpdate(description="changed"), LOCAL_DEV_PRINCIPAL
    )

    assert updated.description == "changed"
    assert updated.domain_name == "d1"
