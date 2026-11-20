"""Verify shared execution claims, expiry and owner isolation."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from context_manager.db import close_database, initialize_database
from context_manager.schema.relational import user_ingest_claims
from context_manager.settings.db import DbSettings
from context_manager.user import repository
from context_manager.user.loader import UserHistoryScope
from context_manager.user.repository import UserIngestRepository

SCOPE = UserHistoryScope("tenant-1", "workspace-1", "user-1")


@pytest.fixture(params=["sqlite", "postgresql"])
def lease_database(request, tmp_path):
    url = f"sqlite:///{tmp_path / 'leases.db'}"
    if request.param == "postgresql":
        url = os.environ.get("CM_USER_LEASE_TEST_PG_URL")
        if not url:
            pytest.skip(
                "Set CM_USER_LEASE_TEST_PG_URL to an isolated PostgreSQL test database"
            )
    database = initialize_database(DbSettings(url=url))
    try:
        yield
    finally:
        with database.engine.begin() as connection:
            connection.execute(user_ingest_claims.delete())
        close_database()


def test_only_one_concurrent_claim_wins(lease_database):
    gate = Barrier(2)

    def claim():
        gate.wait(timeout=5)
        return UserIngestRepository().claim(SCOPE)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(claim) for _ in range(2)]
        tokens = [future.result() for future in futures]
    assert sum(token is not None for token in tokens) == 1


def test_stale_heartbeat_can_be_taken_and_the_old_epoch_cannot_refresh(
    lease_database, monkeypatch
):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(repository.time, "time", lambda: clock["now"])
    first, second = UserIngestRepository(), UserIngestRepository()
    old = first.claim(SCOPE)
    for field in ("tenant_id", "workspace_id", "user_id"):
        assert second.claim(replace(SCOPE, **{field: "another"}))
    assert first.renew(SCOPE, old)
    clock["now"] += 301
    new = second.claim(SCOPE)
    assert new and new != old
    assert not first.renew(SCOPE, old)
    first.release(SCOPE, old)
    assert first.claim(SCOPE) is None
    second.release(SCOPE, new)
    assert first.claim(SCOPE)
