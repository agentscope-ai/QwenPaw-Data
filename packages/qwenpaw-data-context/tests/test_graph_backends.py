# -*- coding: utf-8 -*-
"""Graph backend abstraction: manager registry, Neo4j session primitives, dispatch."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from context_manager.graph.backends import GraphBackend, get_backend
from context_manager.graph.backends.base import GraphSession
from context_manager.graph.backends.neo4j_backend import Neo4jBackend, Neo4jSession
from context_manager.graph.backends.registry import (
    BackendManager,
    get_manager,
    init_backend,
    reset_manager,
)
from context_manager.utils import graph_session, neo4j_database_ctx


@pytest.fixture(autouse=True)
def _clean_manager():
    reset_manager()
    yield
    reset_manager()


# --- fakes --------------------------------------------------------------------


class FakeResult:
    def __init__(self, record: Any = None) -> None:
        self._record = record

    def single(self) -> Any:
        return self._record


class FakeNeo4jSession:
    """Stands in for neo4j.Session: records run() calls."""

    def __init__(self, single_record: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.single_record = single_record

    def run(self, cypher: str, **params: Any) -> FakeResult:
        self.calls.append((" ".join(cypher.split()), params))
        return FakeResult(self.single_record)

    def execute_write(self, fn, **kwargs):
        return fn(self, **kwargs)

    def close(self) -> None:
        self.closed = True


class FakeDriver:
    def __init__(self) -> None:
        self.session_kwargs: list[dict[str, Any]] = []
        self.sessions: list[FakeNeo4jSession] = []
        self.closed = False

    def session(self, **kwargs: Any) -> FakeNeo4jSession:
        self.session_kwargs.append(kwargs)
        sess = FakeNeo4jSession()
        self.sessions.append(sess)
        return sess

    def close(self) -> None:
        self.closed = True


class FakeBackend(GraphBackend):
    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.closed = False
        self.databases: list[Optional[str]] = []

    @contextmanager
    def session(self, *, database: Optional[str] = None):
        self.databases.append(database)
        yield SimpleNamespace(backend=self.name, database=database)

    def close(self) -> None:
        self.closed = True


def _cfg(**overrides: Any) -> SimpleNamespace:
    base = dict(
        graph_backend="neo4j",
        neo4j_uri="bolt://example:7687",
        neo4j_user="neo4j",
        neo4j_password="pw",
        neo4j_database=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --- BackendManager ------------------------------------------------------------


def test_first_register_becomes_active_and_switch() -> None:
    mgr = BackendManager()
    a, b = FakeBackend("a"), FakeBackend("b")
    mgr.register("a", a)
    mgr.register("b", b)
    assert mgr.active_name() == "a"
    assert mgr.active() is a
    assert mgr.switch("b") == "b"
    assert mgr.active() is b
    assert set(mgr.names()) == {"a", "b"}
    assert mgr.info() == {"active": "b", "registered": ["a", "b"]}
    with pytest.raises(KeyError):
        mgr.switch("missing")


def test_register_override_closes_old_instance() -> None:
    mgr = BackendManager()
    old, new = FakeBackend("old"), FakeBackend("new")
    mgr.register("x", old)
    mgr.register("x", new)
    assert old.closed is True
    assert mgr.active() is new


def test_unregister_falls_back_and_close_all() -> None:
    mgr = BackendManager()
    a, b = FakeBackend("a"), FakeBackend("b")
    mgr.register("a", a)
    mgr.register("b", b)
    mgr.unregister("a")
    assert a.closed is True
    assert mgr.active_name() == "b"
    mgr.close_all()
    assert b.closed is True
    assert mgr.active_or_none() is None
    with pytest.raises(RuntimeError):
        mgr.active()


def test_manager_singleton_reset() -> None:
    mgr = get_manager()
    assert get_manager() is mgr
    backend = FakeBackend()
    mgr.register("fake", backend)
    reset_manager()
    assert backend.closed is True
    assert get_manager() is not mgr


# --- factory / init_backend ----------------------------------------------------


def test_get_backend_neo4j_with_injected_driver() -> None:
    driver = FakeDriver()
    backend = get_backend(_cfg(), neo4j_driver=driver)
    assert isinstance(backend, Neo4jBackend)
    backend.close()
    assert driver.closed is False  # injected driver is externally owned


def test_get_backend_unknown_type() -> None:
    with pytest.raises(ValueError, match="Unknown graph_backend"):
        get_backend(_cfg(graph_backend="mystery"))


def test_init_backend_registers_and_activates() -> None:
    driver = FakeDriver()
    name = init_backend(_cfg(), neo4j_driver=driver)
    assert name == "neo4j"
    mgr = get_manager()
    assert mgr.active_name() == "neo4j"
    assert isinstance(mgr.active(), Neo4jBackend)


# --- Neo4jBackend sessions -------------------------------------------------------


def test_backend_session_database_resolution() -> None:
    driver = FakeDriver()
    backend = Neo4jBackend(
        uri="bolt://x", user="u", password="p",
        default_database="defaultdb", driver=driver,
    )
    with backend.session() as sess:
        assert isinstance(sess, Neo4jSession)
    with backend.session(database="explicit"):
        pass
    assert driver.session_kwargs == [
        {"database": "defaultdb"},
        {"database": "explicit"},
    ]
    assert all(s.closed for s in driver.sessions)


# --- Neo4jSession cypher generation ---------------------------------------------


def _session() -> tuple[Neo4jSession, FakeNeo4jSession]:
    raw = FakeNeo4jSession()
    return Neo4jSession(raw), raw


def test_upsert_node_create_and_update_clauses() -> None:
    sess, raw = _session()
    sess.upsert_node(
        "Table",
        "t1",
        {"name": "orders"},
        update_props={"name": "orders_v2"},
        match={"ds": "d1"},
    )
    cypher, params = raw.calls[0]
    assert "MERGE (n:Table {key: $key, ds: $ds})" in cypher
    assert "ON CREATE SET n.name = $name, n.ds = $ds" in cypher
    assert "ON MATCH SET n.name = $upd_name" in cypher
    assert params == {"key": "t1", "ds": "d1", "name": "orders", "upd_name": "orders_v2"}


def test_batch_upsert_nodes_unwind() -> None:
    sess, raw = _session()
    sess.batch_upsert_nodes("Col", [{"key": "c1", "typ": "int"}], match_fields=["ws"])
    cypher, params = raw.calls[0]
    assert "UNWIND $nodes AS row" in cypher
    assert "MERGE (n:Col {key: row.key, ws: row.ws})" in cypher
    assert params["nodes"][0]["key"] == "c1"
    # empty batch is a no-op
    sess.batch_upsert_nodes("Col", [])
    assert len(raw.calls) == 1


def test_update_node_variants() -> None:
    sess, raw = _session()
    sess.update_node("Table", "t1", {"a": 1})
    sess.update_node_merge_props("Table", "t1", {"b": 2})
    sess.update_node_conditional(
        "Table", "t1", {"emb": [0.1], "emb_hash": "h2"},
        condition_field="emb_hash", condition_value="h2",
    )
    assert "SET n.a = $a" in raw.calls[0][0]
    assert "SET n += $props" in raw.calls[1][0]
    cond_cypher = raw.calls[2][0]
    assert "CASE WHEN n.emb_hash <> $emb_hash THEN $emb ELSE n.emb END" in cond_cypher


def test_delete_node_detach() -> None:
    sess, raw = _session()
    sess.delete_node("Table", "t1", match={"ws": "w"})
    cypher, params = raw.calls[0]
    assert "MATCH (n:Table {key: $key, ws: $ws})" in cypher
    assert "DETACH DELETE n" in cypher
    assert params == {"key": "t1", "ws": "w"}


def test_upsert_edge_and_guarded() -> None:
    sess, raw = _session()
    sess.upsert_edge("HAS_COLUMN", "Table", "t1", "Col", "c1", props={"idx": 1})
    plain = raw.calls[0][0]
    assert "MATCH (a:Table {key: $start_key})" in plain
    assert "MERGE (a)-[r:HAS_COLUMN]->(b)" in plain
    assert "SET r.idx = $idx" in plain

    sess.upsert_edge_guarded("REL", "A", "a1", "B", "b1")
    guarded = raw.calls[1][0]
    assert "OPTIONAL MATCH (b:B {key: $end_key})" in guarded
    assert "FOREACH (_ IN CASE WHEN b IS NULL THEN [] ELSE [b] END |" in guarded


def test_batch_edges_and_ordered_chain() -> None:
    sess, raw = _session()
    sess.batch_upsert_edges(
        "NEXT", [{"start_key": "s1", "end_key": "e1", "w": 2}], "Step", "Step"
    )
    assert "UNWIND $edges AS row" in raw.calls[0][0]
    assert "SET r.w = row.w" in raw.calls[0][0]

    sess.batch_upsert_edges_guarded(
        "NEXT", [{"start_key": "s1", "end_key": "e1"}], "Step", "Step"
    )
    assert "FOREACH" in raw.calls[1][0]

    raw.calls.clear()
    nodes = [
        {"key": "n2", "ts": 2},
        {"key": "n1", "ts": 1},
        {"key": "n3", "ts": 3},
    ]
    sess.batch_upsert_edges_ordered("NEXT", nodes, "Event", order_field="ts")
    # first deletes the existing chain, then rebuilds in ts order
    assert "DELETE r" in raw.calls[0][0]
    rebuilt = raw.calls[1][1]["edges"]
    assert [(e["start_key"], e["end_key"]) for e in rebuilt] == [
        ("n1", "n2"),
        ("n2", "n3"),
    ]
    # short chains are a no-op
    raw.calls.clear()
    sess.batch_upsert_edges_ordered("NEXT", [{"key": "only"}], "Event")
    assert raw.calls == []


def test_exists_and_find_label_helpers() -> None:
    hit = Neo4jSession(FakeNeo4jSession(single_record={"lb": "Table"}))
    assert hit.node_exists("Table", "t1") is True
    assert hit.edge_exists("REL", "A", "a1", "B", "b1") is True
    assert hit.find_node_label("t1", labels=["Table", "Col"]) == "Table"

    miss = Neo4jSession(FakeNeo4jSession(single_record=None))
    assert miss.node_exists("Table", "t1") is False
    assert miss.edge_exists("REL", "A", "a1", "B", "b1") is False
    assert miss.find_node_label("t1") is None


def test_base_defaults_reject_unimplemented() -> None:
    class Minimal(GraphSession):
        def run(self, cypher: str, **params: Any) -> Any:
            return None

        def execute_write(self, fn, **kwargs):
            return None

        def close(self) -> None:
            pass

    with pytest.raises(NotImplementedError):
        Minimal().upsert_node("L", "k", {})


# --- graph_session dispatch -------------------------------------------------------


def test_graph_session_with_backend_instance() -> None:
    backend = FakeBackend()
    with graph_session(backend, database="db1") as sess:
        assert sess.database == "db1"
    assert backend.databases == ["db1"]


def test_graph_session_backend_respects_database_ctx() -> None:
    backend = FakeBackend()
    token = neo4j_database_ctx.set("neo4j")
    try:
        with graph_session(backend) as sess:
            assert sess.database == "neo4j"
    finally:
        neo4j_database_ctx.reset(token)


def test_graph_session_driver_back_compat() -> None:
    driver = FakeDriver()
    with graph_session(driver, database="neo4j") as sess:
        assert isinstance(sess, FakeNeo4jSession)
    assert driver.session_kwargs == [{"database": "neo4j"}]
    assert driver.sessions[0].closed is True


def test_graph_session_none_routes_to_active_backend() -> None:
    backend = FakeBackend()
    get_manager().register("fake", backend)
    with graph_session(None, database="db2") as sess:
        assert sess.backend == "fake"
        assert sess.database == "db2"


def test_graph_session_none_without_manager_raises() -> None:
    with pytest.raises(RuntimeError, match="BackendManager"):
        with graph_session(None):
            pass
