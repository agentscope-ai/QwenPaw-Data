"""Open existing SQLite files and enforce read-query execution deadlines."""

import sqlite3
from pathlib import Path
from time import monotonic

from ..config import SqliteConfig


def connect(config: SqliteConfig) -> sqlite3.Connection:
    """Use URI open modes so a missing datasource file is never created."""
    path = Path(config.path).expanduser().resolve()
    mode = "ro" if config.read_only else "rw"
    return sqlite3.connect(
        f"{path.as_uri()}?mode={mode}",
        uri=True,
        timeout=config.connect_timeout,
    )


def configure_read(connection: sqlite3.Connection, cursor, timeout_ms: int) -> None:
    """Protect the query even when the datasource permits writable connections."""
    cursor.execute("PRAGMA query_only = ON")
    configure_timeout(connection, cursor, timeout_ms)


def configure_timeout(connection: sqlite3.Connection, cursor, timeout_ms: int) -> None:
    """Apply the same deadline to reads and explicitly authorized writes."""
    cursor.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    deadline = monotonic() + timeout_ms / 1000
    connection.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
