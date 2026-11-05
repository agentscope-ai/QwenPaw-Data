"""Opt-in SQL table scope, resolved from this datasource's semantic datasets.

Adapted from Cloud's dataset whitelist approach. A requested scope never falls
back to unrestricted execution. This is a correctness guard, not authorization;
the connector's read-only and resource controls still apply.
"""

from __future__ import annotations

import json

from fastapi import HTTPException
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from .semantic_store import neo4j_session
from .datasource_active_api import load_datasource_config

_DIALECTS = {
    "duckdb": "duckdb",
    "postgresql": "postgres",
    "postgres": "postgres",
    "hologres": "postgres",
    "mysql": "mysql",
    "sqlite": "sqlite",
    "odps": "hive",
    "maxcompute": "hive",
    "bigquery": "bigquery",
}


def _reject(code: str, message: str, *, status: int = 422, **details) -> None:
    raise HTTPException(
        status_code=status,
        detail={
            "code": code,
            "message": message,
            **details,
        },
    )


def _table_name(table: exp.Table) -> tuple[str, ...]:
    if not table.parts or any(not isinstance(p, exp.Identifier) for p in table.parts):
        _reject(
            "SQL_SCOPE_UNSUPPORTED",
            "Table functions are not supported in a dataset-scoped query.",
        )
    return tuple(p.name.casefold() for p in table.parts)


def extract_physical_tables(
    sql: str, *, dialect: str | None = None
) -> set[tuple[str, ...]]:
    """Resolve physical sources using SQL scopes, including nested/recursive CTEs."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            _reject(
                "SQL_SCOPE_UNSUPPORTED", "Dataset-scoped SQL must be one SELECT query."
            )
        scopes = traverse_scope(statements[0])
        if not scopes:
            _reject(
                "SQL_SCOPE_UNSUPPORTED",
                "Could not resolve SQL sources; query was not executed.",
            )
        tables = set()
        for scope in scopes:
            for _, source in scope.selected_sources.values():
                if isinstance(source, exp.Table):
                    tables.add(_table_name(source))
                elif not isinstance(source, Scope):
                    _reject(
                        "SQL_SCOPE_UNSUPPORTED",
                        "Unsupported SQL source; query was not executed.",
                    )
        # UDTFs can appear as their own Scope, with no selected_sources.
        if any(s.is_udtf for s in scopes):
            _reject(
                "SQL_SCOPE_UNSUPPORTED",
                "Table functions are not supported in a dataset-scoped query.",
            )
        return tables
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SQL_SCOPE_UNPARSED",
                "message": "Could not parse scoped SQL; confirm dataset names and query syntax before retrying.",
            },
        ) from exc


def _parents(value) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            value = json.loads(text)
        else:
            value = [text]
    if not isinstance(value, list) or any(
        not isinstance(v, str) or not v.strip() for v in value
    ):
        raise ValueError("Dataset parents must be a table name or list of table names")
    return value


def validate_dataset_sql(
    *,
    sql: str,
    dataset_names: list[str] | None,
    datasource_id: str,
    driver,
    dialect: str | None = None,
) -> None:
    if dataset_names is None:
        return
    names = list(dict.fromkeys(n.strip() for n in dataset_names if n.strip()))
    if not names or not datasource_id:
        _reject(
            "SQL_SCOPE_REQUIRED",
            "A scoped query needs a datasource and at least one dataset.",
        )
    if dialect is None:
        config = load_datasource_config(datasource_id) or {}
        dialect = _DIALECTS.get(config.get("_datasource_type", ""))
    try:
        with neo4j_session(driver) as session:
            records = session.run(
                """MATCH (ds:Dataset)
                WHERE ds.datasource_id = $datasource_id AND ds.name IN $names
                RETURN ds.name AS name, ds.parents AS parents""",
                datasource_id=datasource_id,
                names=names,
            ).data()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SQL_SCOPE_UNAVAILABLE",
                "message": "Dataset scope could not be verified; query was not executed.",
            },
        ) from exc
    missing = sorted(set(names) - {r["name"] for r in records})
    if missing:
        _reject(
            "SQL_DATASET_NOT_FOUND",
            "Confirm datasets on the selected datasource before querying.",
            datasets=missing,
        )
    allowed: set[tuple[str, ...]] = set()
    mappings: dict[str, set[tuple[str, ...]]] = {}
    try:
        for record in records:
            parents = _parents(record.get("parents") or []) or [record["name"]]
            targets = {
                _table_name(sqlglot.parse_one(p, into=exp.Table, read=dialect))
                for p in parents
            }
            old = mappings.setdefault(record["name"], targets)
            if old != targets:
                _reject(
                    "SQL_DATASET_AMBIGUOUS",
                    "Dataset has conflicting physical mappings.",
                    dataset=record["name"],
                )
            allowed.update(targets)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SQL_DATASET_MAPPING_INVALID",
                "message": "Dataset physical table mapping is invalid.",
            },
        ) from exc
    tables = extract_physical_tables(sql, dialect=dialect)
    # An unqualified source may refer to a qualified registered table. An
    # explicit schema must match; do not allow other_schema.same_table.
    bad = [
        ".".join(t)
        for t in tables
        if t not in allowed
        and not (len(t) == 1 and sum(a[-1] == t[0] for a in allowed) == 1)
    ]
    if bad:
        _reject(
            "SQL_TABLE_OUTSIDE_DATASETS",
            "SQL references tables outside the selected datasets. Use the confirmed physical names.",
            tables=sorted(bad),
            allowed_tables=sorted(".".join(t) for t in allowed),
        )
