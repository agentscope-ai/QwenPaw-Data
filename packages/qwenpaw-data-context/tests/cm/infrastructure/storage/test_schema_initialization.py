"""Explicit schema preparation must surface index population failures."""

from types import SimpleNamespace

import pytest
from context_manager.store.neo4j.physical_schema import ensure_schema


def test_neo4j_schema_initialization_waits_and_propagates_index_failure():
    writes = []

    def write(query, parameters, *, operation):
        writes.append(query)

    def read(query, parameters, *, operation):
        assert writes
        assert all(query.startswith("CREATE ") for query in writes)
        assert "db.awaitIndex" in query
        assert "cm_entity_key" in parameters["index_names"]
        raise RuntimeError("index population failed")

    with pytest.raises(RuntimeError, match="index population failed"):
        ensure_schema(SimpleNamespace(write=write, read=read))
