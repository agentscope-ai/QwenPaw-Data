"""Read Host history through a short-lived, read-only database connection."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import get_database


class HistoryDataError(ValueError):
    """Identify invalid source fields within one session."""

    def __init__(self, reason: str, *fields: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fields = fields


def parse_timestamp(value: object, field: str) -> datetime:
    """Interpret Host's naive timestamps as UTC and normalize aware values."""
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).strip().replace("z", "Z"))
        )
    except ValueError as exc:
        raise HistoryDataError("invalid_source_timestamp", field) from exc
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _at_or_before(value: object, cutoff: object) -> bool | None:
    try:
        return parse_timestamp(value, "timestamp") <= parse_timestamp(cutoff, "cutoff")
    except HistoryDataError:
        return None  # Retain malformed rows for scoped diagnostics.


class HistoryConnection:
    """Provide named SQL parameters and bounded row reads for both backends."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.sqlite = connection.dialect.name == "sqlite"

    def rows(
        self, query: str, parameters: Mapping[str, object] | None = None
    ) -> Iterator[dict]:
        result = self.connection.execute(text(query), parameters or {}).mappings()
        try:
            while batch := result.fetchmany(256):
                yield from (dict(row) for row in batch)
        finally:
            result.close()

    def before(self, column: str) -> str:
        return (
            f"cm_ug_at_or_before({column}, :cutoff)"
            if self.sqlite
            else f"({column} <= :cutoff)"
        )


@contextmanager
def history_connection() -> Iterator[HistoryConnection]:
    """Read one consistent Host snapshot through CM's shared connection pool."""
    with get_database().read_connection() as connection:
        if connection.dialect.name == "sqlite":
            connection.connection.driver_connection.create_function(
                "cm_ug_at_or_before", 2, _at_or_before
            )
        yield HistoryConnection(connection)
