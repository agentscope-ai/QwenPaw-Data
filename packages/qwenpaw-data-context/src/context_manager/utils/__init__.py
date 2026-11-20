"""Provide helpers shared across Context Manager.

The Cloud runtime introduced this package alongside the historical
``context_manager.utils`` module.  Python resolves the package first, while
the retained OSS compatibility APIs still import logging, graph-session and
SQL parsing helpers from ``context_manager.utils``.  Keep those helpers
available here until the compatibility APIs are retired or migrated.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

_logger = logging.getLogger("context_manager")


def get_logger(name: str = "context_manager") -> logging.Logger:
    """Return a consistently configured Context Manager logger."""
    if not _logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
                "%H:%M:%S",
            ),
        )
        _logger.addHandler(handler)
        _logger.setLevel(logging.INFO)
        _logger.propagate = False

    logger = logging.getLogger(name)
    if not logger.handlers:
        for handler in _logger.handlers:
            logger.addHandler(handler)
        logger.setLevel(_logger.level)
        logger.propagate = False
    return logger


ALLOWED_NEO4J_LOGICAL_DBS = frozenset({"neo4j"})

neo4j_database_ctx: ContextVar[Optional[str]] = ContextVar(
    "neo4j_database_ctx",
    default=None,
)


def pick_neo4j_database(header_value: Optional[str]) -> Optional[str]:
    """Resolve and validate the requested Neo4j logical database."""
    raw = (header_value or "").strip()
    if raw:
        if raw not in ALLOWED_NEO4J_LOGICAL_DBS:
            raise ValueError(f"Unknown Neo4j logical database: {raw!r}")
        return raw

    from ..config import CFG

    return CFG.neo4j_database


@contextmanager
def neo4j_session(
    driver: Any,
    *,
    database: Optional[str] = None,
) -> Iterator[Any]:
    """Open a Neo4j session using explicit, request or configured scope."""
    from ..config import CFG

    db = database
    if db is None:
        db = neo4j_database_ctx.get()
    if db is None:
        db = CFG.neo4j_database
    kwargs: dict[str, Any] = {}
    if db:
        kwargs["database"] = db
    session = driver.session(**kwargs)
    try:
        yield session
    finally:
        session.close()


@contextmanager
def graph_session(
    backend: Any,
    *,
    database: Optional[str] = None,
) -> Iterator[Any]:
    """Open a session for a graph backend or legacy Neo4j driver."""
    from ..graph.backends.base import GraphBackend

    if isinstance(backend, GraphBackend):
        from ..config import CFG

        db = database
        if db is None:
            db = neo4j_database_ctx.get()
        if db is None:
            db = CFG.neo4j_database
        with backend.session(database=db) as session:
            yield session
        return

    if backend is not None:
        with neo4j_session(backend, database=database) as session:
            yield session
        return

    from ..graph.backends.registry import get_manager

    active = get_manager().active_or_none()
    if active is None:
        raise RuntimeError(
            "graph_session(None): BackendManager is not initialized; pass a "
            "driver/backend or initialize the configured backend first",
        )
    with graph_session(active, database=database) as session:
        yield session


def strip_sql(sql: str) -> str:
    """Remove common model-response wrappers and normalize the terminator."""
    sql = (sql or "").strip()
    if not sql:
        return ""
    match = re.search(r"```(?:sql)?\s*(.*?)```", sql, flags=re.S | re.I)
    if match:
        sql = match.group(1).strip()
    sql = re.sub(
        r"^\s*(?:sql|answer|final\s*sql)\s*[:：]\s*",
        "",
        sql,
        flags=re.I,
    )
    return sql.strip().rstrip(";") + ";"


_SECTION_HEADER = re.compile(
    r"(?m)^\s{0,3}(#{1,4})\s*(Explanation|SQL|说明|解释)\s*$",
    re.I,
)


def parse_sql_and_explanation(raw: str) -> tuple[str, str]:
    """Split a model response into normalized SQL and its explanation."""
    text = (raw or "").strip()
    if not text:
        return "", ""

    matches = list(_SECTION_HEADER.finditer(text))
    if not matches:
        return strip_sql(text), ""

    by_label: dict[str, str] = {}
    for index, match in enumerate(matches):
        label = match.group(2).lower()
        if label in ("说明", "解释", "explanation"):
            key = "explanation"
        elif label == "sql":
            key = "sql"
        else:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        by_label[key] = text[start:end].strip()

    sql_raw = by_label.get("sql", "")
    explanation = by_label.get("explanation", "")
    if sql_raw:
        return strip_sql(sql_raw), explanation
    return strip_sql(text), ""


__all__ = [
    "ALLOWED_NEO4J_LOGICAL_DBS",
    "get_logger",
    "graph_session",
    "neo4j_database_ctx",
    "neo4j_session",
    "parse_sql_and_explanation",
    "pick_neo4j_database",
    "strip_sql",
]
