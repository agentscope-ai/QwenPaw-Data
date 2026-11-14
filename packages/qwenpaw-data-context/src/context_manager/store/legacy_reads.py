"""Preserve legacy native scores and persisted Core projections."""

from ..utils.validation import require_nonempty_text
from .base import GraphStore


def search_fulltext_scores(store, spec, text, *, limit, scope, metric_domain=None):
    """Retain native scores and the legacy Metric validity/trust ordering."""
    # TODO(transition:anchor-score-contract): use search_fulltext after the
    # caller no longer exposes and compares backend-native index scores.
    store._prepare_read(spec, scope)
    if spec.internal or not spec.fulltext_fields:
        raise ValueError("anchor search requires a public searchable node type")
    text = require_nonempty_text(text, "full-text query")
    limit, _ = GraphStore._page(limit, 0)
    values = {
        "q": text,
        "k": limit,
        "label": spec.name,
        "scope_workspace_id": scope.workspace_id,
        "scope_owner_ids": list(scope.owner_ids),
        "scope_zone": spec.zone.value,
        "scope_datasource_id": scope.datasource_id,
        "metric_domain": metric_domain,
    }
    if metric_domain is not None and spec.name != "Metric":
        raise ValueError("metric_domain requires Metric")
    if store.backend == "neo4j":
        from .neo4j.catalog import Neo4jIndexKind
        from .neo4j.physical_schema import NEO4J_INDEXES

        values["idx"] = next(
            item.name
            for item in NEO4J_INDEXES
            if item.node is spec and item.kind is Neo4jIndexKind.FULLTEXT
        )
        return store.execute_query(
            "CALL db.index.fulltext.queryNodes($idx, $q) YIELD node, score "
            "WHERE $label IN labels(node) "
            "AND node.workspace_id = $scope_workspace_id "
            "AND node.zone = $scope_zone "
            "AND (size($scope_owner_ids) = 0 OR node.owner_id IN $scope_owner_ids) "
            "AND ($scope_datasource_id IS NULL OR node.datasource_id = $scope_datasource_id) "
            + (
                "AND node.domain = $metric_domain "
                "AND (node.valid_to IS NULL OR node.valid_to > datetime()) "
                "RETURN node, score ORDER BY score DESC, coalesce(node.source_trust, 1.0) DESC LIMIT $k"
                if metric_domain is not None
                else "RETURN node, score ORDER BY score DESC LIMIT $k"
            ),
            values,
            scope=scope,
        )
    if store.backend != "holoage":
        raise ValueError("Unsupported legacy graph backend")
    from psycopg import sql

    branches = []
    for graph in store._read_graphs(scope):
        table = sql.Identifier(graph, spec.name).as_string().replace("%", "%%")
        global_filter = (
            "AND ag_catalog.agtype_object_field_text(n.properties, 'owner_id') = '__global__' "
            if graph == store._runtime.target.graph_name
            else ""
        )
        branches.append(
            f"SELECT ag_catalog.agtype_to_json(n.properties) AS node, n.id, "
            f"{sql.Literal(graph).as_string().replace('%', '%%')}::text AS graph_name, "
            "COALESCE(ag_catalog.agtype_object_field_text(n.properties, 'source_trust')::float8, 1.0) AS trust, "
            f"text_search(n.content, %(q)s) AS score FROM {table} n "
            "WHERE text_search(n.content, %(q)s) > 0 "
            "AND ag_catalog.agtype_object_field_text(n.properties, 'workspace_id') = %(scope_workspace_id)s "
            "AND ag_catalog.agtype_object_field_text(n.properties, 'zone') = %(scope_zone)s "
            + global_filter
            + "AND (%(scope_datasource_id)s::text IS NULL OR "
            "ag_catalog.agtype_object_field_text(n.properties, 'datasource_id') = %(scope_datasource_id)s) "
            + (
                "AND ag_catalog.agtype_object_field_text(n.properties, 'domain') = %(metric_domain)s "
                "AND (ag_catalog.agtype_object_field_text(n.properties, 'valid_to') IS NULL OR "
                "ag_catalog.agtype_object_field_text(n.properties, 'valid_to')::timestamptz > CURRENT_TIMESTAMP) "
                if metric_domain is not None
                else ""
            )
        )
    order = (
        "score DESC, trust DESC, id, graph_name"
        if metric_domain is not None
        else "score DESC, id, graph_name"
    )
    return store.execute_query(
        f"SELECT node, score FROM ({' UNION ALL '.join(branches)}) hits ORDER BY {order} LIMIT %(k)s",
        values,
        scope=scope,
    )


def count_core_entities(store, *, scope, key=None):
    """Read the persisted Core projection missing from canonical NodeRecord."""
    # TODO(transition:core-label-projection): remove with the replacement label
    # read contract. Do not recompute labels from a possibly changed Glossary.
    from ..schema import ENTITY

    store._prepare_read(ENTITY, scope)
    values = {
        "workspace": scope.workspace_id,
        "owners": list(scope.owner_ids),
        "datasource": scope.datasource_id,
        "key": key,
    }
    if store.backend == "neo4j":
        statement = (
            "MATCH (n:Entity:Core) WHERE n.workspace_id = $workspace "
            "AND n.zone = 'knowledge' "
            "AND (size($owners) = 0 OR n.owner_id IN $owners) "
            "AND ($datasource IS NULL OR n.datasource_id = $datasource) "
            "AND ($key IS NULL OR n.key = $key) RETURN count(n) AS count"
        )
    elif store.backend == "holoage":
        from psycopg import sql

        branches = []
        for graph in store._read_graphs(scope):
            table = sql.Identifier(graph, "Entity").as_string().replace("%", "%%")
            global_filter = (
                "AND ag_catalog.agtype_object_field_text(n.properties, 'owner_id') = '__global__' "
                if graph == store._runtime.target.graph_name
                else ""
            )
            branches.append(
                f"SELECT count(*) AS count FROM {table} n WHERE n.is_core "
                "AND ag_catalog.agtype_object_field_text(n.properties, 'workspace_id') = %(workspace)s "
                "AND ag_catalog.agtype_object_field_text(n.properties, 'zone') = 'knowledge' "
                + global_filter
                + "AND (%(datasource)s::text IS NULL OR "
                "ag_catalog.agtype_object_field_text(n.properties, 'datasource_id') = %(datasource)s) "
                "AND (%(key)s::text IS NULL OR "
                "ag_catalog.agtype_object_field_text(n.properties, 'key') = %(key)s)"
            )
        statement = (
            f"SELECT sum(count) AS count FROM ({' UNION ALL '.join(branches)}) graphs"
        )
    else:
        raise ValueError("Unsupported legacy graph backend")
    return int(store.execute_query(statement, values, scope=scope)[0]["count"])
