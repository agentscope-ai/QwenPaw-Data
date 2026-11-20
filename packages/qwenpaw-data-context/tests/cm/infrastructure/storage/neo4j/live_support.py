"""Opt-in real Neo4j checks; URI must name a disposable local test instance."""

import os
from urllib.parse import urlparse

import pytest
from context_manager.auth import GraphScopeAuthority
from context_manager.schema import (
    GRAPH_SCHEMA,
)
from context_manager.store.neo4j import GraphTarget, Neo4jGraphStore, Neo4jRuntime
from context_manager.store.neo4j.physical_schema import ensure_schema
from neo4j import GraphDatabase

from cm.infrastructure.storage.schema_support import make_glossary


@pytest.fixture
def backend():
    uri = os.environ.get("CM_FEEDBACK_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("set CM_FEEDBACK_TEST_NEO4J_URI to a disposable local Neo4j")
    assert urlparse(uri).hostname in {"localhost", "127.0.0.1", "::1"}
    driver = GraphDatabase.driver(
        uri, auth=("neo4j", os.environ["CM_FEEDBACK_TEST_NEO4J_PASSWORD"])
    )
    driver.verify_connectivity()
    runtime = Neo4jRuntime(driver, GraphTarget("t1", "w1", database="neo4j"))
    ensure_schema(runtime)
    store = Neo4jGraphStore(
        runtime,
        GRAPH_SCHEMA,
        GraphScopeAuthority(),
        make_glossary(GRAPH_SCHEMA, tenant_id="t1", workspace_id="w1"),
    )
    try:
        yield store, driver
    finally:
        with driver.session(database="neo4j") as session:
            session.run(
                "MATCH (n) WHERE n.workspace_id IN ['w1', 'feedback-foreign'] DETACH DELETE n"
            ).consume()
        store.close()
