"""Verify relational target, driver mapping, transaction, and probe behavior."""

from __future__ import annotations

from typing import Self

import pytest
from context_manager.sql import (
    MySqlConfig,
    PostgresConfig,
    RdbBackend,
    RdbConnector,
    RdbError,
    RdbTarget,
)


class FakeCursor:
    """Capture health queries and expose one deterministic row."""

    def __init__(self, row: tuple[int, ...] | None = (1,)) -> None:
        """Initialize one cursor result and lifecycle counters."""
        self.row = row
        self.queries: list[tuple[str, object | None]] = []
        self.close_count = 0

    def execute(self, query: str, parameters: object | None = None) -> None:
        """Record one DB-API execute call."""
        self.queries.append((query, parameters))

    def fetchone(self) -> tuple[int, ...] | None:
        """Return the configured single row."""
        return self.row

    def close(self) -> None:
        """Count physical cursor closes."""
        self.close_count += 1

    def __enter__(self) -> Self:
        """Enter the fake cursor lifetime."""
        return self

    def __exit__(self, *args: object) -> None:
        """Close the fake cursor lifetime."""
        self.close()


class FakeDriverConnection:
    """Provide the DB-API subset owned by RdbConnection."""

    def __init__(self, cursor: FakeCursor | None = None) -> None:
        """Initialize transaction and close counters."""
        self.managed_cursor = cursor or FakeCursor()
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self) -> FakeCursor:
        """Return the single managed cursor."""
        return self.managed_cursor

    def commit(self) -> None:
        """Count commits."""
        self.commit_count += 1

    def rollback(self) -> None:
        """Count rollbacks."""
        self.rollback_count += 1

    def close(self) -> None:
        """Count closes."""
        self.close_count += 1


class CapturingFactory:
    """Capture driver keyword arguments and return a fake connection."""

    def __init__(self, connection: FakeDriverConnection | None = None) -> None:
        """Initialize one factory result and empty call log."""
        self.connection = connection or FakeDriverConnection()
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> FakeDriverConnection:
        """Record one physical connection attempt."""
        self.calls.append(dict(kwargs))
        return self.connection


def _target(config: PostgresConfig | MySqlConfig) -> RdbTarget:
    """Bind a connection config to one trusted datasource route."""
    return RdbTarget("tenant-1", "workspace-1", "warehouse", config)


def test_transaction_commits_success_and_rolls_back_failure() -> None:
    """A transaction performs one terminal driver action for each outcome."""
    raw = FakeDriverConnection()
    connector = RdbConnector(postgres_factory=CapturingFactory(raw))
    connection = connector.connect(
        _target(PostgresConfig("host", "db", "user", "secret"))
    )

    with connection.transaction() as active:
        assert active is connection
    assert raw.commit_count == 1
    assert raw.rollback_count == 0

    with pytest.raises(LookupError, match="business failure"), connection.transaction():
        raise LookupError("business failure")
    assert raw.commit_count == 1
    assert raw.rollback_count == 1


def test_driver_failure_is_sanitized() -> None:
    """Connection errors expose stable routing metadata but no driver or secret text."""

    def failing_factory(**kwargs: object) -> None:
        """Raise one driver-like exception containing sensitive text."""
        raise RuntimeError(f"password={kwargs['password']} host={kwargs['host']}")

    target = _target(PostgresConfig("host", "db", "user", "very-secret"))

    with pytest.raises(RdbError) as excinfo:
        RdbConnector(postgres_factory=failing_factory).connect(target)

    error = excinfo.value
    assert error.operation == "connect"
    assert error.backend is RdbBackend.POSTGRES
    assert error.datasource_id == "warehouse"
    assert "very-secret" not in str(error)
    assert "host" not in str(error)
