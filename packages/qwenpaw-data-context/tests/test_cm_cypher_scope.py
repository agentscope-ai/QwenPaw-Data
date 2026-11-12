"""Port AST scope security cases and native AGE SQL admission restrictions."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from context_manager import schema


@pytest.fixture(scope="module")
def cypher_modules():
    # Load the real parser/admission modules without eagerly importing the
    # adjacent ModelService. No packages, parser code or checks are stubbed.
    modules = []
    for leaf in ("cypher_ast", "admission"):
        name = f"context_manager._focused_cypher.{leaf}"
        path = Path(schema.__file__).resolve().parents[1] / "cypher" / f"{leaf}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except ModuleNotFoundError as exc:
            sys.modules.pop(name, None)
            pytest.skip(f"Real Cypher dependency unavailable: {exc}")
        modules.append(module)
    return SimpleNamespace(ast=modules[0], admission=modules[1])


def _builder(modules, role="member", *, global_mg_only=False):
    is_admin = role in {"ws_admin", "tenant_admin"}
    return SimpleNamespace(
        global_mg_only=global_mg_only and is_admin,
        scope_cypher=lambda query: modules.ast.scope_cypher(
            query, workspace_id="w-1", user_id="u-1", is_admin=is_admin,
            global_mg_only=global_mg_only,
        ),
    )


@pytest.mark.parametrize("query", [
    "CALL db.labels()",
    "MATCH (n) FOREACH (x IN [n] | DELETE x)",
    "MATCH (n) DELETE n", "MATCH (n) SET n.x = 1",
    "MATCH (n) RETURN n; MATCH (x) RETURN x", "MATCH () RETURN 1",
    "MATCH (:Metric) RETURN 1",
    "MATCH (n) WHERE EXISTS { MATCH (x) RETURN x } RETURN n",
    "MATCH (n) WHERE exists((n)-->(x)) RETURN n",
    "MATCH (n) RETURN [(n)-->(x) | x]", "MATCH (n:$($label)) RETURN n",
    "USE other MATCH (n) RETURN n",
    "LOAD CSV FROM 'https://example.invalid/x' AS row RETURN row",
])
def test_complex_or_dangerous_syntax_is_rejected(cypher_modules, query):
    with pytest.raises(cypher_modules.ast.CypherAstError):
        _builder(cypher_modules).scope_cypher(query)


def test_every_node_and_relationship_in_match_is_scoped(cypher_modules):
    query, params = _builder(cypher_modules).scope_cypher(
        "MATCH (a:Entity)-[r:ASSOCIATION]->(b:Entity) RETURN a, b, r"
    )
    for variable in ("a", "b", "r"):
        assert f"{variable}.workspace_id = $scoped_ws_id" in query
        assert f"{variable}.owner_id = $scoped_uid" in query
        assert f"{variable}.owner_id = '__global__'" in query
    assert params == {"scoped_ws_id": "w-1", "scoped_uid": "u-1"}


def test_admin_keeps_workspace_scope_without_owner_scope(cypher_modules):
    query, params = _builder(cypher_modules, "ws_admin").scope_cypher(
        "MATCH (a:Entity)-[r:PARTICIPATION]->(b:Event) RETURN a, b"
    )
    for variable in ("a", "b", "r"):
        assert f"{variable}.workspace_id = $scoped_ws_id" in query
    assert "owner_id" not in query
    assert params == {"scoped_ws_id": "w-1"}


def test_optional_match_existing_where_and_with_alias_are_scoped_independently(cypher_modules):
    query, params = _builder(cypher_modules).scope_cypher(
        "MATCH (a:Entity) WHERE a.enabled = true WITH a AS source "
        "OPTIONAL MATCH (source)-[r:ASSOCIATION]->(b:Entity) "
        "WHERE b.enabled = true RETURN source, b ORDER BY b.name SKIP 1 LIMIT 10"
    )
    for variable in ("a", "source", "b", "r"):
        assert query.count(f"{variable}.workspace_id = $scoped_ws_id") == 1
    assert "AND (a.enabled = true)" in query
    assert "AND (b.enabled = true)" in query
    assert "ORDER BY b.name SKIP 1 LIMIT 10" in query
    assert params == {"scoped_ws_id": "w-1", "scoped_uid": "u-1"}


def test_with_dropped_variable_is_a_new_scoped_node(cypher_modules):
    query, _ = _builder(cypher_modules).scope_cypher(
        "MATCH (n:Entity) WITH n AS kept MATCH (n:Entity) RETURN kept, n"
    )
    assert query.count("n.workspace_id = $scoped_ws_id") == 2


def test_constants_and_literal_values_cannot_inject_patterns(cypher_modules):
    builder = _builder(cypher_modules)
    assert builder.scope_cypher("RETURN 1") == ("RETURN 1", {})
    query, _ = builder.scope_cypher(
        'MATCH (n:Entity {text: "MATCH () DELETE n; CALL db.labels()"}) RETURN n'
    )
    assert query.count("n.workspace_id = $scoped_ws_id") == 1


def test_escaped_identifier_is_scoped_from_parse_tree(cypher_modules):
    query, _ = _builder(cypher_modules).scope_cypher(
        "MATCH (`order item`:Entity) RETURN `order item`"
    )
    assert "`order item`.workspace_id = $scoped_ws_id" in query


@pytest.mark.parametrize("query", [
    "MATCH (a)-[rels:ASSOCIATION*1..2]->(b) RETURN a, b",
    "MATCH (a)-[:ASSOCIATION*1..2]->(b) RETURN a, b",
    "MATCH (a)-->(b) RETURN *", "MATCH (a)-->(b) WITH * RETURN a",
])
def test_unscopable_relationships_are_rejected(cypher_modules, query):
    with pytest.raises(cypher_modules.ast.CypherAstError):
        _builder(cypher_modules).scope_cypher(query)


@pytest.mark.parametrize("edge", ["-->", "<--", "--", "-[]->", "-[:ASSOCIATION {weight: 1}]->"])
def test_anonymous_relationships_are_scoped_without_variable_collisions(cypher_modules, edge):
    query, _ = _builder(cypher_modules).scope_cypher(
        f"MATCH (`__scoped_rel_0`){edge}(b) OPTIONAL MATCH (b)-->(c) "
        "WHERE c.enabled OR true RETURN b, c, count(*)"
    )
    for variable in ("__scoped_rel_1", "__scoped_rel_2"):
        assert f"[{variable}" in query
        assert f"{variable}.workspace_id = $scoped_ws_id" in query
        assert f"{variable}.owner_id = $scoped_uid" in query
    assert "AND (c.enabled OR true)" in query
    assert query.endswith("RETURN b, c, count(*)")
    cypher_modules.ast.parse_scoped_query(query)


def test_parse_failure_is_closed(cypher_modules):
    with pytest.raises(cypher_modules.ast.CypherAstError, match="Invalid or unsupported"):
        _builder(cypher_modules).scope_cypher("MATCH (n RETURN n")


@pytest.mark.parametrize("role", ["ws_admin", "tenant_admin"])
def test_global_metadata_scope_pins_admin_nodes_and_relationships(cypher_modules, role):
    builder = _builder(cypher_modules, role, global_mg_only=True)
    query, params = builder.scope_cypher(
        "MATCH (a:Dataset)-[r:HAS]->(b:DatasetField) RETURN a, b, r"
    )
    assert builder.global_mg_only
    for variable in ("a", "b", "r"):
        assert f"{variable}.workspace_id = $scoped_ws_id" in query
        assert f"{variable}.owner_id = '__global__'" in query
    assert "$scoped_uid" not in query
    assert params == {"scoped_ws_id": "w-1"}


def test_global_metadata_scope_is_ignored_for_members(cypher_modules):
    builder = _builder(cypher_modules, global_mg_only=True)
    query, params = builder.scope_cypher("MATCH (n:Metric) RETURN n")
    assert not builder.global_mg_only
    assert "n.owner_id = $scoped_uid" in query
    assert "n.owner_id = '__global__'" in query
    assert params == {"scoped_ws_id": "w-1", "scoped_uid": "u-1"}


def test_global_scope_covers_union_and_parenthesizes_existing_or(cypher_modules):
    builder = _builder(cypher_modules, "ws_admin", global_mg_only=True)
    query, _ = builder.scope_cypher(
        "MATCH (n) RETURN n AS item UNION MATCH (x) RETURN x AS item"
    )
    for variable in ("n", "x"):
        assert f"{variable}.workspace_id = $scoped_ws_id" in query
        assert f"{variable}.owner_id = '__global__'" in query
    query, _ = builder.scope_cypher("MATCH (n:Metric) WHERE n.name = 'DAU' OR true RETURN n")
    assert "WHERE (n.workspace_id = $scoped_ws_id AND " in query
    assert "n.owner_id = '__global__') AND (n.name = 'DAU' OR true)" in query


def test_personal_scope_excludes_global_and_internal_nodes(cypher_modules):
    query, params = cypher_modules.ast.scope_cypher(
        "MATCH (n) RETURN n", workspace_id="w-1", user_id="u-1",
        is_admin=False, include_global=False,
    )
    assert "'__global__'" not in query
    assert "n.owner_id = $scoped_uid" in query
    for node in schema.GRAPH_SCHEMA.nodes:
        if node.internal:
            assert node.name in query
    assert params == {"scoped_ws_id": "w-1", "scoped_uid": "u-1"}


@pytest.mark.parametrize("query", [
    'SELECT * FROM "bob_graph"."Metric"',
    'WITH x AS (SELECT * FROM "bob_graph"."Metric") SELECT * FROM x',
    "SELECT * FROM pg_authid WHERE EXISTS (WITH pg_authid AS (SELECT 1) SELECT * FROM pg_authid)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT CASE WHEN true THEN pg_read_file('/etc/passwd') ELSE '' END",
    "SELECT json_agg(pg_read_file('/etc/passwd'))",
    "SELECT * FROM ag_catalog.cypher('alice_graph', $$MATCH (n) DELETE n$$) AS (n ag_catalog.agtype)",
    "SELECT * FROM ag_catalog.cypher('bob_graph', $$RETURN 1$$) AS (n ag_catalog.agtype)",
    "SELECT 1; SELECT 2", "SELECT 1 INTO stolen", "SELECT 1 FOR UPDATE",
    'SELECT * FROM "alice_graph"."Metric"; DELETE FROM "alice_graph"."Metric"',
])
def test_age_sql_subqueries_functions_and_writes_cannot_escape_scope(cypher_modules, query):
    with pytest.raises(cypher_modules.admission.GraphQueryError):
        cypher_modules.admission.admit_age_sql(query, graph="alice_graph")


@pytest.mark.parametrize("query", [
    'SELECT * FROM "alice_graph"."Metric"',
    'WITH x AS (SELECT * FROM "alice_graph"."Metric") SELECT count(*) FROM x',
    "SELECT * FROM ag_catalog.cypher('alice_graph', $$MATCH (n:UnknownLabel) RETURN n$$) AS (n ag_catalog.agtype)",
    'SELECT json_agg(CASE WHEN array_length(ARRAY[1,2],1)>0 THEN ag_catalog.agtype_object_field_text(properties, \'name\') ELSE \'\' END) FROM "alice_graph"."Metric"',
])
def test_native_age_reads_accept_authorized_tables_ctes_and_aggregation(cypher_modules, query):
    cypher_modules.admission.admit_age_sql(query, graph="alice_graph")


def test_fulltext_admission_requires_declared_index_and_explicit_node_yield(cypher_modules):
    admission = cypher_modules.admission
    indexes = {"cm_metric_fulltext": "Metric"}
    query = "CALL db.index.fulltext.queryNodes($index,$text) YIELD node AS n,score RETURN n"
    params = {"index": "cm_metric_fulltext", "text": "revenue"}
    admission.admit_cypher(query, params, fulltext_indexes=indexes)
    for invalid in (
        query.replace("$index,$text", "$index,$text,{limit:1}"),
        query.replace("$index", "'unknown_index'"),
        query.replace("YIELD node AS n,score", "YIELD score"),
        query.replace("db.index.fulltext.queryNodes", "db.labels"),
    ):
        with pytest.raises(admission.GraphQueryError):
            admission.admit_cypher(invalid, params, fulltext_indexes=indexes)


def test_normal_cypher_public_entrypoint_imports_without_substitutes():
    try:
        module = importlib.import_module("context_manager.cypher")
    except ModuleNotFoundError as exc:
        pytest.skip(f"Adjacent Context integration dependency unavailable: {exc}")
    assert module.GraphQueryService is not None
