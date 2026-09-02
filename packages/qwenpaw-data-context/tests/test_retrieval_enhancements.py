"""Roadmap #10 retrieval behaviors: subgraph search, snapshot cache, event search."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from context_manager.api import retrieval
from context_manager.utils import neo4j_database_ctx


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def data(self):
        return self.rows


class _ScriptedSession:
    """Return queued row batches and record every executed query."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls: list[tuple[str, dict]] = []

    def run(self, query, **params):
        self.calls.append((query, params))
        return _Rows(self.batches.pop(0) if self.batches else [])


@contextmanager
def _session_cm(session):
    yield session


def _patch_session(monkeypatch, session):
    monkeypatch.setattr(
        retrieval, "neo4j_session", lambda _driver: _session_cm(session)
    )


def _hit(key, label="Metric", zone="metadata", name=None):
    return {
        "key": key,
        "label": label,
        "zone": zone,
        "display_name": name or key,
    }


def _path_row(nodes, edges):
    return {"path_nodes": nodes, "path_edges": edges}


def _edge(src, dst, rel="RELATED_TO"):
    return {"source_key": src, "target_key": dst, "rel_type": rel}


@pytest.fixture(autouse=True)
def _fresh_cache():
    retrieval.invalidate_global_graph_snapshot_cache()
    yield
    retrieval.invalidate_global_graph_snapshot_cache()


# ---------------------------------------------------------------------- #
# search_explorer_subgraph
# ---------------------------------------------------------------------- #


def test_subgraph_empty_query_returns_empty_shape(monkeypatch):
    session = _ScriptedSession([])
    _patch_session(monkeypatch, session)

    result = retrieval.search_explorer_subgraph(object(), "   ")

    assert result == {"hit_nodes": [], "nodes": [], "edges": []}
    assert session.calls == []


def test_subgraph_rejects_invalid_labels():
    with pytest.raises(ValueError, match="Invalid Neo4j label"):
        retrieval.search_explorer_subgraph(
            object(), "gmv", allowed_labels=["Metric) DETACH DELETE"]
        )


def test_subgraph_rejects_unknown_match_mode():
    with pytest.raises(ValueError, match="match_mode"):
        retrieval.search_explorer_subgraph(object(), "gmv", match_mode="regex")


def test_subgraph_empty_scope_short_circuits(monkeypatch):
    session = _ScriptedSession([])
    _patch_session(monkeypatch, session)

    result = retrieval.search_explorer_subgraph(object(), "gmv", allowed_labels=[])

    assert result == {"hit_nodes": [], "nodes": [], "edges": []}
    assert session.calls == []


def test_subgraph_exact_mode_and_hop_clamp(monkeypatch):
    session = _ScriptedSession([[_hit("met:gmv")], []])
    _patch_session(monkeypatch, session)

    retrieval.search_explorer_subgraph(
        object(), "GMV", match_mode="exact", hops=9, limit=999
    )

    hit_query, hit_params = session.calls[0]
    assert "node.key = $search_query" in hit_query
    assert hit_params["limit"] == 200
    traversal_query, _ = session.calls[1]
    assert "*1..3" in traversal_query


def test_subgraph_dedupes_and_orders_deterministically(monkeypatch):
    session = _ScriptedSession(
        [
            [_hit("met:b"), _hit("met:a"), _hit("met:a")],
            [
                _path_row(
                    [_hit("met:b"), _hit("dim:z", label="Dimension")],
                    [_edge("met:b", "dim:z", "ANALYZED_BY")],
                ),
                _path_row(
                    [_hit("met:b"), _hit("dim:z", label="Dimension")],
                    [_edge("met:b", "dim:z", "ANALYZED_BY")],
                ),
            ],
        ]
    )
    _patch_session(monkeypatch, session)

    result = retrieval.search_explorer_subgraph(object(), "met")

    assert [node["key"] for node in result["hit_nodes"]] == ["met:a", "met:b"]
    assert [node["key"] for node in result["nodes"]] == ["dim:z", "met:a", "met:b"]
    assert result["edges"] == [
        {
            "source_key": "met:b",
            "target_key": "dim:z",
            "rel_type": "ANALYZED_BY",
            "properties": {},
        }
    ]


def test_subgraph_includes_intermediate_path_nodes(monkeypatch):
    middle = _hit("tbl:mid", label="Table")
    session = _ScriptedSession(
        [
            [_hit("met:gmv")],
            [
                _path_row(
                    [_hit("met:gmv"), middle, _hit("col:leaf", label="Column")],
                    [
                        _edge("met:gmv", "tbl:mid", "USES_TABLE"),
                        _edge("tbl:mid", "col:leaf", "HAS_COLUMN"),
                    ],
                )
            ],
        ]
    )
    _patch_session(monkeypatch, session)

    result = retrieval.search_explorer_subgraph(object(), "gmv", hops=2)

    keys = {node["key"] for node in result["nodes"]}
    assert keys == {"met:gmv", "tbl:mid", "col:leaf"}
    assert len(result["edges"]) == 2


def test_subgraph_node_cap_keeps_endpoint_closure(monkeypatch):
    session = _ScriptedSession(
        [
            [_hit("met:gmv")],
            [
                _path_row(
                    [_hit("met:gmv"), _hit("dim:a", label="Dimension")],
                    [_edge("met:gmv", "dim:a")],
                ),
                _path_row(
                    [
                        _hit("met:gmv"),
                        _hit("dim:b", label="Dimension"),
                        _hit("dim:c", label="Dimension"),
                    ],
                    [_edge("met:gmv", "dim:b"), _edge("dim:b", "dim:c")],
                ),
            ],
        ]
    )
    _patch_session(monkeypatch, session)

    result = retrieval.search_explorer_subgraph(object(), "gmv", hops=2, max_nodes=2)

    assert {node["key"] for node in result["nodes"]} == {"met:gmv", "dim:a"}
    for edge in result["edges"]:
        assert edge["source_key"] in {"met:gmv", "dim:a"}
        assert edge["target_key"] in {"met:gmv", "dim:a"}


def test_subgraph_edge_cap_is_enforced(monkeypatch):
    session = _ScriptedSession(
        [
            [_hit("met:gmv")],
            [
                _path_row(
                    [_hit("met:gmv"), _hit("dim:a", label="Dimension")],
                    [
                        _edge("met:gmv", "dim:a", "R1"),
                        _edge("met:gmv", "dim:a", "R2"),
                        _edge("met:gmv", "dim:a", "R3"),
                    ],
                )
            ],
        ]
    )
    _patch_session(monkeypatch, session)

    result = retrieval.search_explorer_subgraph(object(), "gmv", max_edges=2)

    assert len(result["edges"]) == 2


def test_subgraph_scope_excludes_other_label_nodes(monkeypatch):
    session = _ScriptedSession(
        [
            [_hit("met:gmv")],
            [
                _path_row(
                    [_hit("met:gmv"), _hit("task:t1", label="Task", zone="trace")],
                    [_edge("met:gmv", "task:t1")],
                )
            ],
        ]
    )
    _patch_session(monkeypatch, session)

    result = retrieval.search_explorer_subgraph(
        object(), "gmv", allowed_labels=["Metric", "Dimension"]
    )

    assert {node["key"] for node in result["nodes"]} == {"met:gmv"}
    assert result["edges"] == []


# ---------------------------------------------------------------------- #
# global_graph_snapshot cache
# ---------------------------------------------------------------------- #


def _patch_uncached(monkeypatch, payload=None):
    calls = {"n": 0}

    def fake_uncached(_driver, **_kwargs):
        calls["n"] += 1
        return {
            "center": None,
            "nodes": [dict(payload or {"id": "dom:a", "group": "Domain"})],
            "edges": [],
            "raw": {"fill": calls["n"]},
        }

    monkeypatch.setattr(retrieval, "_global_graph_snapshot_uncached", fake_uncached)
    return calls


def test_snapshot_cache_hit_skips_second_fill(monkeypatch):
    calls = _patch_uncached(monkeypatch)
    driver = object()

    first = retrieval.global_graph_snapshot(driver)
    second = retrieval.global_graph_snapshot(driver)

    assert calls["n"] == 1
    assert first == second


def test_snapshot_cache_returns_defensive_copies(monkeypatch):
    _patch_uncached(monkeypatch)
    driver = object()

    first = retrieval.global_graph_snapshot(driver)
    first["nodes"].clear()
    first["raw"]["fill"] = "mutated"

    second = retrieval.global_graph_snapshot(driver)
    assert second["nodes"] == [{"id": "dom:a", "group": "Domain"}]
    assert second["raw"]["fill"] == 1


def test_snapshot_cache_keys_include_shaping_args(monkeypatch):
    calls = _patch_uncached(monkeypatch)
    driver = object()

    retrieval.global_graph_snapshot(driver, zone_mode="all")
    retrieval.global_graph_snapshot(driver, zone_mode="knowledge")
    retrieval.global_graph_snapshot(driver, zone_mode="all", max_nodes=5)

    assert calls["n"] == 3


def test_snapshot_cache_keys_include_driver_and_database(monkeypatch):
    calls = _patch_uncached(monkeypatch)
    first_driver, second_driver = object(), object()

    retrieval.global_graph_snapshot(first_driver)
    retrieval.global_graph_snapshot(second_driver)
    token = neo4j_database_ctx.set("other_db")
    try:
        retrieval.global_graph_snapshot(second_driver)
        retrieval.global_graph_snapshot(second_driver)
    finally:
        neo4j_database_ctx.reset(token)

    assert calls["n"] == 3


def test_snapshot_cache_expires_after_ttl(monkeypatch):
    calls = _patch_uncached(monkeypatch)
    driver = object()
    clock = {"now": 100.0}
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: clock["now"])

    retrieval.global_graph_snapshot(driver)
    clock["now"] += retrieval._GLOBAL_GRAPH_SNAPSHOT_CACHE_TTL_SECONDS - 0.5
    retrieval.global_graph_snapshot(driver)
    clock["now"] += 1.0
    retrieval.global_graph_snapshot(driver)

    assert calls["n"] == 2


def test_snapshot_cache_explicit_invalidation_forces_refill(monkeypatch):
    calls = _patch_uncached(monkeypatch)
    driver = object()

    retrieval.global_graph_snapshot(driver)
    retrieval.invalidate_global_graph_snapshot_cache()
    retrieval.global_graph_snapshot(driver)

    assert calls["n"] == 2


def test_snapshot_cache_invalidation_during_fill_is_not_stored(monkeypatch):
    calls = {"n": 0}

    def racing_uncached(_driver, **_kwargs):
        calls["n"] += 1
        # A writer commits while this snapshot is being built.
        retrieval.invalidate_global_graph_snapshot_cache()
        return {"center": None, "nodes": [], "edges": [], "raw": {"fill": calls["n"]}}

    monkeypatch.setattr(retrieval, "_global_graph_snapshot_uncached", racing_uncached)
    driver = object()

    retrieval.global_graph_snapshot(driver)
    retrieval.global_graph_snapshot(driver)

    assert calls["n"] == 2


def test_snapshot_cache_is_bounded(monkeypatch):
    _patch_uncached(monkeypatch)

    for edge_budget in range(retrieval._GLOBAL_GRAPH_SNAPSHOT_CACHE_MAX_ENTRIES + 20):
        retrieval.global_graph_snapshot(object(), max_edges=edge_budget)

    assert (
        len(retrieval._GLOBAL_GRAPH_SNAPSHOT_CACHE)
        <= retrieval._GLOBAL_GRAPH_SNAPSHOT_CACHE_MAX_ENTRIES
    )


# ---------------------------------------------------------------------- #
# search_events hybrid retrieval
# ---------------------------------------------------------------------- #


def _event_row(key, score, name=None):
    return {
        "key": key,
        "name": name or key,
        "type": "signal",
        "scope": "",
        "description": "",
        "date_from": "",
        "date_to": "",
        "about_entity_key": "",
        "about_entity_name": "",
        "score": score,
    }


def test_search_events_empty_query_returns_empty():
    assert retrieval.search_events(object(), "   ") == []


def test_search_events_falls_back_to_fulltext_when_embedding_fails(monkeypatch):
    fulltext = [_event_row("ev:a", 3.0), _event_row("ev:b", 2.0)]
    monkeypatch.setattr(
        retrieval, "_fulltext_search_events", lambda *_a, **_k: list(fulltext)
    )
    monkeypatch.setattr(
        retrieval,
        "embed_one",
        lambda _q: (_ for _ in ()).throw(RuntimeError("no model")),
    )

    rows = retrieval.search_events(object(), "促销", limit=10)

    assert [row["key"] for row in rows] == ["ev:a", "ev:b"]
    assert all(row["vec_score"] == 0.0 for row in rows)


def test_search_events_fuses_rankings_and_keeps_vec_score(monkeypatch):
    monkeypatch.setattr(
        retrieval,
        "_fulltext_search_events",
        lambda *_a, **_k: [_event_row("ev:a", 3.0), _event_row("ev:b", 2.0)],
    )
    monkeypatch.setattr(
        retrieval,
        "_vector_search_events",
        lambda *_a, **_k: [_event_row("ev:b", 0.93), _event_row("ev:c", 0.91)],
    )
    monkeypatch.setattr(retrieval, "embed_one", lambda _q: [0.1, 0.2])

    rows = retrieval.search_events(object(), "促销", limit=10)

    assert [row["key"] for row in rows] == ["ev:b", "ev:a", "ev:c"]
    by_key = {row["key"]: row for row in rows}
    assert by_key["ev:b"]["vec_score"] == 0.93
    assert by_key["ev:a"]["vec_score"] == 0.0


def test_search_events_caps_limit_at_fifty(monkeypatch):
    pools = {}

    def fake_fulltext(_driver, _q, k):
        pools["k"] = k
        return [_event_row(f"ev:{i}", 100 - i) for i in range(60)]

    monkeypatch.setattr(retrieval, "_fulltext_search_events", fake_fulltext)
    monkeypatch.setattr(retrieval, "embed_one", lambda _q: None)

    rows = retrieval.search_events(object(), "促销", limit=500)

    assert len(rows) == 50
    assert pools["k"] == 150


def test_event_indexes_stay_declared_in_schema():
    from context_manager.graph import schema_init

    fulltext = {name: fields for name, _, fields, _ in schema_init._FULLTEXT_INDEXES}
    vectors = {name: (label, prop) for name, label, prop in schema_init._VECTOR_INDEXES}
    assert fulltext["event_text"] == ("name", "description")
    assert vectors["ev_vec"] == ("Event", "embedding")


# ---------------------------------------------------------------------- #
# mutation invalidation wiring
# ---------------------------------------------------------------------- #


def _spy_invalidator(monkeypatch):
    calls = {"n": 0}
    real = retrieval.invalidate_global_graph_snapshot_cache

    def spy():
        calls["n"] += 1
        real()

    monkeypatch.setattr(retrieval, "invalidate_global_graph_snapshot_cache", spy)
    return calls


def _fake_request(driver=None):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(driver=driver or object()))
    )


def test_kg_upsert_invalidates_snapshot_cache(monkeypatch):
    from context_manager.api import kg_admin_api

    calls = _spy_invalidator(monkeypatch)
    monkeypatch.setattr(
        kg_admin_api.kg_admin, "upsert_entity", lambda *_a, **_k: {"key": "ent:x"}
    )
    body = SimpleNamespace(
        canonical_name="X",
        type="org",
        aliases=[],
        description="",
        lifecycle_state="active",
    )

    kg_admin_api.upsert_entity("ent:x", body, _fake_request())

    assert calls["n"] == 1


def test_kg_validation_failure_keeps_snapshot_cache(monkeypatch):
    from context_manager.api import kg_admin_api

    calls = _spy_invalidator(monkeypatch)

    def explode(*_a, **_k):
        raise ValueError("bad key")

    monkeypatch.setattr(kg_admin_api.kg_admin, "delete_knowledge_node", explode)

    with pytest.raises(Exception):
        kg_admin_api.delete_entity("ent:x", _fake_request())

    assert calls["n"] == 0


def test_tg_task_status_update_invalidates_snapshot_cache(monkeypatch):
    from context_manager.api import tg_admin_api

    calls = _spy_invalidator(monkeypatch)
    monkeypatch.setattr(
        tg_admin_api.store,
        "update_task_status",
        lambda *_a, **_k: {"task_key": "task:1"},
    )
    body = SimpleNamespace(status="archived", reason="done")

    tg_admin_api.update_task_status("task:1", body, _fake_request())

    assert calls["n"] == 1


def test_doc_graph_delete_invalidates_only_on_success(monkeypatch):
    from context_manager.api import doc_api

    calls = _spy_invalidator(monkeypatch)
    monkeypatch.setattr(
        doc_api, "delete_kg_nodes_by_source", lambda *_a, **_k: {"deleted": 2}
    )
    doc_api._run_kg_delete_sync(object(), "report.pdf")
    assert calls["n"] == 1

    def explode(*_a, **_k):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(doc_api, "delete_kg_nodes_by_source", explode)
    doc_api._run_kg_delete_sync(object(), "report.pdf")
    assert calls["n"] == 1


def test_cypher_write_detection_matches_dml_only():
    from context_manager.api import cypher_api

    assert cypher_api._is_write_statement("MERGE (e:Entity {key: 'ent:x'})")
    assert cypher_api._is_write_statement(
        "MATCH (e:Entity) WHERE e.key = 'x' DETACH DELETE e"
    )
    assert not cypher_api._is_write_statement("MATCH (n:Metric) RETURN n LIMIT 5")
    assert not cypher_api._is_write_statement(
        "MATCH (n) WHERE n.name = 'create' RETURN n"
    )


def test_cypher_endpoint_guards_invalidation_with_write_check():
    import inspect

    from context_manager.api import cypher_api

    source = inspect.getsource(cypher_api.execute_cypher)
    assert "_is_write_statement" in source
    assert "invalidate_global_graph_snapshot_cache" in source
