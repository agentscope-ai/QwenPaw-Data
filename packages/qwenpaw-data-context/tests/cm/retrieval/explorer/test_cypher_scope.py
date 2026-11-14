"""Develop scoped Cypher security cases, ported to the Explorer application."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from context_manager.cypher.cypher_ast import (
    CypherAstError as ScopedCypherError,
)
from context_manager.cypher.cypher_ast import parse_scoped_query, scope_cypher


def _builder(role="member", *, global_mg_only=False):
    is_admin = role in {"ws_admin", "tenant_admin"}
    return SimpleNamespace(
        global_mg_only=global_mg_only and is_admin,
        scope_cypher=lambda query: scope_cypher(
            query,
            workspace_id="w-1",
            user_id="u-1",
            is_admin=is_admin,
            global_mg_only=global_mg_only,
        ),
    )


@pytest.mark.parametrize(
    "query",
    [
        "CALL db.labels()",
        "MATCH (n) FOREACH (x IN [n] | DELETE x)",
        "MATCH (n) DELETE n",
        "MATCH (n) SET n.x = 1",
        "MATCH (n) RETURN n; MATCH (x) RETURN x",
        "MATCH () RETURN 1",
        "MATCH (:Metric) RETURN 1",
        "MATCH (n) WHERE EXISTS { MATCH (x) RETURN x } RETURN n",
        "MATCH (n) WHERE exists((n)-->(x)) RETURN n",
        "MATCH (n) RETURN [(n)-->(x) | x]",
        "MATCH (n:$($label)) RETURN n",
        "USE other MATCH (n) RETURN n",
        "LOAD CSV FROM 'https://example.invalid/x' AS row RETURN row",
    ],
)
def test_complex_or_dangerous_syntax_is_rejected(query: str) -> None:
    with pytest.raises(ScopedCypherError):
        _builder().scope_cypher(query)


def test_every_node_in_multi_node_match_is_scoped() -> None:
    query, params = _builder().scope_cypher(
        "MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity) RETURN a, b, r"
    )
    assert "a.workspace_id = $scoped_ws_id" in query
    assert "b.workspace_id = $scoped_ws_id" in query
    assert "a.owner_id = $scoped_uid" in query
    assert "b.owner_id = $scoped_uid" in query
    assert "r.workspace_id = $scoped_ws_id" in query
    assert "r.owner_id = $scoped_uid" in query
    assert "r.owner_id = '__global__'" in query
    assert params == {"scoped_ws_id": "w-1", "scoped_uid": "u-1"}


def test_admin_keeps_only_active_workspace_scope_without_owner_scope() -> None:
    query, params = _builder("ws_admin").scope_cypher(
        "MATCH (a:Event)-[r:PARTICIPATION]->(b:Entity) RETURN a, b"
    )
    assert "a.workspace_id = $scoped_ws_id" in query
    assert "b.workspace_id = $scoped_ws_id" in query
    assert "r.workspace_id = $scoped_ws_id" in query
    assert "owner_id" not in query
    assert params == {"scoped_ws_id": "w-1"}


def test_member_scope_includes_owner_and_global_rows() -> None:
    query, _ = _builder().scope_cypher("MATCH (n:Metric) RETURN n")
    assert "n.owner_id = $scoped_uid" in query
    assert "n.owner_id = '__global__'" in query


def test_optional_match_and_existing_where_are_scoped_independently() -> None:
    query, params = _builder().scope_cypher(
        "MATCH (a:Entity) WHERE a.enabled = true "
        "WITH a AS source "
        "OPTIONAL MATCH (source)-[r:RELATED_TO]->(b:Entity) "
        "WHERE b.enabled = true "
        "RETURN source, b ORDER BY b.name SKIP 1 LIMIT 10"
    )
    assert query.count("source.workspace_id = $scoped_ws_id") == 1
    assert query.count("b.workspace_id = $scoped_ws_id") == 1
    assert query.count("a.workspace_id = $scoped_ws_id") == 1
    assert query.count("r.workspace_id = $scoped_ws_id") == 1
    assert "AND (a.enabled = true)" in query
    assert "AND (b.enabled = true)" in query
    assert "ORDER BY b.name SKIP 1 LIMIT 10" in query
    assert params == {"scoped_ws_id": "w-1", "scoped_uid": "u-1"}


def test_with_dropped_variable_is_treated_as_new_scoped_node() -> None:
    query, _ = _builder().scope_cypher(
        "MATCH (n:Entity) WITH n AS kept MATCH (n:Entity) RETURN kept, n"
    )
    assert query.count("n.workspace_id = $scoped_ws_id") == 2


def test_constant_query_needs_no_scope_predicates() -> None:
    assert _builder().scope_cypher("RETURN 1") == ("RETURN 1", {})


def test_property_values_cannot_create_fake_clauses_or_patterns() -> None:
    query, _ = _builder().scope_cypher(
        'MATCH (n:Entity {text: "MATCH () DELETE n; CALL db.labels()"}) RETURN n'
    )
    assert query.count("n.workspace_id = $scoped_ws_id") == 1


def test_escaped_node_identifier_is_scoped_from_parse_tree() -> None:
    query, _ = _builder().scope_cypher(
        "MATCH (`order item`:Entity) RETURN `order item`"
    )
    assert "`order item`.workspace_id = $scoped_ws_id" in query


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (a)-[rels:RELATED_TO*1..2]->(b) RETURN a, b",
        "MATCH (a)-[:RELATED_TO*1..2]->(b) RETURN a, b",
        "MATCH (a)-->(b) RETURN *",
        "MATCH (a)-->(b) WITH * RETURN a",
    ],
)
def test_unscopable_relationships_are_rejected(query: str) -> None:
    with pytest.raises(ScopedCypherError):
        _builder().scope_cypher(query)


@pytest.mark.parametrize(
    "edge", ["-->", "<--", "--", "-[]->", "-[:RELATED_TO {weight: 1}]->"]
)
def test_anonymous_relationships_are_scoped_without_variable_collisions(edge) -> None:
    query, _ = _builder().scope_cypher(
        f"MATCH (`__scoped_rel_0`){edge}(b) "
        "OPTIONAL MATCH (b)-->(c) WHERE c.enabled OR true "
        "RETURN b, c, count(*)"
    )
    for variable in ("__scoped_rel_1", "__scoped_rel_2"):
        assert f"[{variable}" in query
        assert f"{variable}.workspace_id = $scoped_ws_id" in query
        assert f"{variable}.owner_id = $scoped_uid" in query
    assert "AND (c.enabled OR true)" in query
    assert query.endswith("RETURN b, c, count(*)")
    parse_scoped_query(query)


def test_parse_failure_is_closed() -> None:
    with pytest.raises(ScopedCypherError, match="Invalid or unsupported"):
        _builder().scope_cypher("MATCH (n RETURN n")


# --- global MG narrowing (scope=global_mg from the global MG view) -----------


@pytest.mark.parametrize("role", ["ws_admin", "tenant_admin"])
def test_global_mg_scope_pins_admin_nodes_and_relationships(role: str) -> None:
    """Both endpoints and the relationship itself must carry the global owner."""
    builder = _builder(role, global_mg_only=True)
    query, params = builder.scope_cypher(
        "MATCH (a:Dataset)-[r:HAS]->(b:DatasetColumn) RETURN a, b, r"
    )
    assert builder.global_mg_only is True
    for var in ("a", "b", "r"):
        assert f"{var}.workspace_id = $scoped_ws_id" in query
        assert f"{var}.owner_id = '__global__'" in query
    # The owner is pinned, never widened to the member disjunction.
    assert "$scoped_uid" not in query
    assert params == {"scoped_ws_id": "w-1"}


def test_global_mg_scope_excludes_member_owned_rows() -> None:
    """A member's userspace MG mirror must not leak into the global MG view."""
    query, _ = _builder("ws_admin", global_mg_only=True).scope_cypher(
        "MATCH (n:Metric) RETURN n"
    )
    assert "n.owner_id = '__global__'" in query
    assert "OR" not in query


def test_global_mg_scope_is_ignored_for_members() -> None:
    """Members keep their mirror view: the hint must not reach the originals."""
    builder = _builder("member", global_mg_only=True)
    query, params = builder.scope_cypher("MATCH (n:Metric) RETURN n")
    assert builder.global_mg_only is False
    assert "n.owner_id = $scoped_uid" in query
    assert "n.owner_id = '__global__'" in query
    assert params == {"scoped_ws_id": "w-1", "scoped_uid": "u-1"}


def test_admin_scope_is_unchanged_without_the_hint() -> None:
    """The narrowing is opt-in; the default admin scope keeps no owner filter."""
    query, params = _builder("ws_admin").scope_cypher("MATCH (n:Metric) RETURN n")
    assert "owner_id" not in query
    assert params == {"scoped_ws_id": "w-1"}


def test_global_mg_scope_covers_each_union_branch() -> None:
    query, _ = _builder("ws_admin", global_mg_only=True).scope_cypher(
        "MATCH (n) RETURN n AS item UNION MATCH (x) RETURN x AS item"
    )
    for variable in ("n", "x"):
        assert f"{variable}.workspace_id = $scoped_ws_id" in query
        assert f"{variable}.owner_id = '__global__'" in query


def test_global_mg_scope_wraps_an_existing_where_clause() -> None:
    """A user-supplied WHERE cannot escape the pinned owner predicate."""
    query, _ = _builder("ws_admin", global_mg_only=True).scope_cypher(
        "MATCH (n:Metric) WHERE n.name = 'DAU' OR true RETURN n"
    )
    # The scope predicate is ANDed in front and the caller's expression is
    # parenthesised, so a trailing ``OR true`` cannot widen the owner filter.
    assert "WHERE (n.workspace_id = $scoped_ws_id AND " in query
    assert "n.owner_id = '__global__') AND (n.name = 'DAU' OR true)" in query
