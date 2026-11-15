"""Shared, connection-scoped DDL for PostgreSQL-compatible control-plane stores."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy.engine import URL
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateTable

HOLOGRES_ROW = {"holo_orientation": "row"}
_hologres: ContextVar[bool] = ContextVar("control_plane_ddl_hologres", default=False)


@contextmanager
def ddl_target(url: URL) -> Iterator[bool]:
    """Select DDL from the actual engine, isolated across tasks and nested calls."""
    selected = "hologres.aliyuncs.com" in (url.host or "").lower()
    token = _hologres.set(selected)
    try:
        yield selected
    finally:
        _hologres.reset(token)


@compiles(CreateTable, "postgresql")
def _create_table(element, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    # Register once for all owners; unscoped compilation stays standard PG.
    sql = compiler.visit_create_table(element, **kw)
    orientation = element.element.info.get("holo_orientation")
    if orientation and _hologres.get():
        sql = sql.rstrip() + f" WITH (orientation='{orientation}')\n\n"
    return sql
