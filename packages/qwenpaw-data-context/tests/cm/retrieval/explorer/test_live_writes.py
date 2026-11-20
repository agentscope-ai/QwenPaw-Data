"""Opt-in real Neo4j checks for the compatibility Raw executor."""

import pytest
from context_manager.compatibility.explorer.raw_cypher import (
    RawCypherService,
    RawQueryViolation,
)
from context_manager.store.neo4j import GraphTarget
from neo4j.exceptions import ConstraintError

from cm.infrastructure.storage.neo4j.live_support import backend  # noqa: F401
from cm.retrieval.explorer.test_raw_cypher import PROPS, raw  # noqa: F401


def test_raw_real_write_read_isolation_and_transaction_rollback(backend, raw):  # noqa: F811 - shared fixture
    _, driver = backend
    _, _, request, _ = raw
    service = RawCypherService(driver, GraphTarget("t1", "w1", database="neo4j"))
    create = "CREATE (n:Entity {workspace_id: $__raw_workspace_id, owner_id: $__raw_owner_id, key: 'raw-one', name: 'before'}) RETURN n.name AS name"
    assert service.execute(**request(create))["rows"] == [{"name": "before"}]
    with driver.session(database="neo4j") as session:
        session.run(
            "CREATE (:Entity {workspace_id:'feedback-foreign',owner_id:'u1',key:'foreign',name:'invisible'})"
        ).consume()
    read = "MATCH (n:Entity) RETURN n.name AS name"
    assert service.execute(**request(read))["rows"] == [{"name": "before"}]
    broken = f"MATCH (n:Entity {PROPS}) SET n.name='after' CREATE (m:Entity {{workspace_id:$__raw_workspace_id,owner_id:$__raw_owner_id,key:'raw-one'}}) RETURN n"
    args = request(broken)
    with pytest.raises(ConstraintError):
        service.execute(**args)
    assert service.execute(**request(read))["rows"] == [{"name": "before"}]
    with pytest.raises(RawQueryViolation, match="一次性"):
        service.execute(**args)


def test_raw_created_nodes_and_relationships_use_server_scope(backend, raw):  # noqa: F811 - shared fixture
    _, driver = backend
    _, _, request, _ = raw
    service = RawCypherService(driver, GraphTarget("t1", "w1", database="neo4j"))
    query = (
        "MERGE (a:Entity {workspace_id:$__raw_workspace_id,owner_id:$__raw_owner_id,key:'raw-a'}) "
        "CREATE (b:Entity {workspace_id:$__raw_workspace_id,owner_id:$__raw_owner_id,key:'raw-b'}) "
        f"CREATE (a)-[r:INFLUENCE {PROPS}]->(b) RETURN r.owner_id AS owner"
    )
    result = service.execute(
        **request(query, {"__raw_workspace_id": "foreign", "__raw_owner_id": "foreign"})
    )
    assert result["rows"] == [{"owner": "u1"}]
    with driver.session(database="neo4j") as session:
        rows = session.run(
            "MATCH (a:Entity {key:'raw-a'})-[r:INFLUENCE]->(b:Entity {key:'raw-b'}) "
            "RETURN a.workspace_id AS aws,a.owner_id AS ao,r.workspace_id AS rws,r.owner_id AS ro,b.workspace_id AS bws,b.owner_id AS bo"
        ).data()
    assert rows == [
        {"aws": "w1", "ao": "u1", "rws": "w1", "ro": "u1", "bws": "w1", "bo": "u1"}
    ]
