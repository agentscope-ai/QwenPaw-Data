"""Open existing DuckDB files read-only and bound queries with interrupts."""

from pathlib import Path
from threading import Timer

from ..config import DuckDbConfig

# Datasource SQL must not reach files, ATTACH targets or extensions beyond the
# configured database; DuckDB refuses to re-enable these while it is running.
_SETTINGS = {
    "enable_external_access": False,
    "autoload_known_extensions": False,
    "autoinstall_known_extensions": False,
    "allow_unsigned_extensions": False,
}


def connect(config: DuckDbConfig):
    """Open an existing file read-only so a missing path is never created."""
    import duckdb

    path = Path(config.path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("DuckDB datasource file does not exist")
    return duckdb.connect(str(path), read_only=True, config=dict(_SETTINGS))


def query(connection, sql: str, *, max_rows: int, timeout_ms: int):
    """Interrupt the statement at its deadline and fetch one extra row."""
    cursor = connection.cursor()
    timer = Timer(timeout_ms / 1000, cursor.interrupt)
    timer.daemon = True
    try:
        timer.start()
        cursor.execute(sql)
        rows = tuple(cursor.fetchmany(max_rows + 1))
        return cursor.description, rows, None
    finally:
        timer.cancel()
        cursor.close()
