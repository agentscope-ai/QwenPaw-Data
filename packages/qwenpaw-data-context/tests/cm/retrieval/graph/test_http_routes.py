"""Verify current graph HTTP endpoints remain thin Store adapters."""

from __future__ import annotations

from context_manager.schema import (
    DATA_SOURCE,
)
from context_manager.store import (
    NodeRecord,
)

from cm.retrieval.http_support import build_client


def test_node_response_never_exposes_never_display_properties() -> None:
    """Generic node reads omit vectors and have no connection config to expose."""
    client, store, _ = build_client()
    store.get_result = NodeRecord(
        DATA_SOURCE,
        {
            "key": "dsrc:workspace-1:warehouse",
            "datasource_id": "warehouse",
            "datasource_name": "Warehouse",
            "datasource_type": "postgresql",
            "embedding": (1.0, 0.0),
            "embedding_hash": "semantic-vector-hash",
            "workspace_id": "workspace-1",
            "owner_id": "owner-1",
            "zone": "metadata",
        },
    )

    response = client.post(
        "/api/v1/graph/get_nodes",
        json={
            "node": "DataSource",
            "keys": ["dsrc:workspace-1:warehouse"],
        },
    )

    assert response.status_code == 200
    properties = response.json()[0]["properties"]
    assert properties["datasource_id"] == "warehouse"
    assert properties["datasource_name"] == "Warehouse"
    assert properties["datasource_type"] == "postgresql"
    assert "config" not in properties
    assert "embedding" not in properties
    assert "embedding_hash" not in properties
