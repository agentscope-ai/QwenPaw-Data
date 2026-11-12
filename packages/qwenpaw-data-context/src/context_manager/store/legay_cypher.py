"""Execute trusted fixed Cypher reads with explicit scalar/map return columns.

Callers apply the supplied scope to every node and relationship. Unlike the
retired query adapter, this does not parse queries or decode graph entities.
"""

from psycopg import sql
from pydantic_core import to_json

from ..auth import GLOBAL_OWNER_ID, AccessMode


def execute(store, query, parameters, *, columns, scope, order_by=(), limit=None):
    store.access.require(scope, AccessMode.READ)
    store._require_target(scope)
    if not columns or any(not name.isidentifier() for name in columns):
        raise ValueError("explicit return column names are required")
    if any(name not in columns for name in order_by):
        raise ValueError("ordering requires a declared return column")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be positive")
    queries = (query,) if isinstance(query, str) else tuple(query)
    if not queries:
        return ()
    values = dict(parameters)
    values["scope"] = {
        "workspace": scope.workspace_id,
        "owners": list(scope.owner_ids),
        "zone": scope.zone,
        "datasource": scope.datasource_id,
    }
    if store.backend == "neo4j":
        query = (
            queries[0]
            if len(queries) == 1
            else (
                "CALL { "
                + " UNION ALL ".join(queries)
                + " } RETURN "
                + ", ".join(columns)
            )
        )
        if order_by:
            query += " ORDER BY " + ", ".join(order_by)
        if limit is not None:
            values["read_limit"] = limit
            query += " LIMIT $read_limit"
        return store.execute_query(query, values, scope=scope)
    if store.backend != "holoage":
        raise ValueError("Unsupported fixed Cypher backend")

    declaration = ", ".join(f"{name} ag_catalog.agtype" for name in columns)
    projection = ", ".join(f"'{name}', {name}" for name in columns)
    result = []
    # Resolve physical ownership afresh on every operation, as primitives do.
    for graph in store._read_graphs(scope):
        graph_values = {**values, "scope": dict(values["scope"])}
        if graph == store._runtime.target.graph_name:
            graph_values["scope"]["owners"] = [GLOBAL_OWNER_ID]
        bindings = {"params": to_json(graph_values).decode()}
        branches = []
        graph_literal = sql.Literal(graph).as_string().replace("%", "%%")
        for query in queries:
            if "$cm_read$" in query:
                raise ValueError("reserved Cypher delimiter")
            query_literal = "$cm_read$" + query.replace("%", "%%") + "$cm_read$"
            branches.append(
                f"SELECT * FROM ag_catalog.cypher({graph_literal}, "
                f"{query_literal}, %(params)s::ag_catalog.agtype) "
                f"AS result({declaration})"
            )
        statement = "SELECT * FROM (" + " UNION ALL ".join(branches) + ") records"
        if order_by:
            statement += " ORDER BY " + ", ".join(order_by)
        if limit is not None:
            bindings["read_limit"] = limit
            statement += " LIMIT %(read_limit)s"
        # Holo accepts maps, but not top-level integer counts, in this cast.
        rows = store.execute_query(
            f"SELECT ag_catalog.agtype_to_json(ag_catalog.agtype_build_map({projection})) AS row "
            f"FROM ({statement}) bounded",
            bindings,
            scope=scope,
        )
        result.extend(row["row"] for row in rows)
    # Each graph contributes at most limit rows. Avoid one oversized AGE plan
    # across every user's label tables, while retaining one global budget.
    if order_by:
        result.sort(key=lambda row: tuple(row[name] for name in order_by))
    return tuple(result if limit is None else result[:limit])
