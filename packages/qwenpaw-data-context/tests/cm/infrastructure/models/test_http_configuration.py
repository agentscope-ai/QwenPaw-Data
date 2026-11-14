"""Verify the canonical model-configuration HTTP contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cm.infrastructure.access.support import authorization
from cm.infrastructure.models.http_support import build_config_client


def test_config_paths_work_without_graph_access(tmp_path, monkeypatch) -> None:
    """Configuration uses SQL and model transports even when the graph is offline."""
    client = build_config_client(tmp_path, monkeypatch, role="ws_admin")
    try:
        read = client.get("/api/v1/config")
        llm_update = client.put(
            "/api/v1/config/llm",
            json={"model": "qwen-max", "provider_id": "chat"},
        )
        llm_test = client.post(
            "/api/v1/config/llm/test",
            json={"model": "qwen-test", "provider_id": "chat"},
        )
        embedding_update = client.put(
            "/api/v1/config/embed",
            json={"model": "embedding-v4", "dim": 1536},
        )
        embedding_test = client.post(
            "/api/v1/config/embed/test",
            json={"model": "embedding-test"},
        )

        assert read.status_code == 200
        assert read.json()["llm"]["api_key"] == "****"
        assert read.json()["configured"] is True
        assert read.json()["key_status"] == "ok"
        assert llm_update.json()["llm"]["model"] == "qwen-max"
        assert client.get("/api/v1/config").json()["llm"]["model"] == "qwen-max"
        assert llm_test.json() == {
            "success": True,
            "message": "Connected. Model: qwen-test",
            "detected_dim": None,
        }
        assert embedding_update.json()["embedding"]["model"] == "embedding-v4"
        assert embedding_update.json()["rebuild_required"] is True
        assert "job_id" not in embedding_update.json()
        assert embedding_test.json()["detected_dim"] == 1536
        assert client.get("/api/system/model-config/").status_code == 200
        assert client.delete("/api/v1/config").status_code == 200
        runtime = client.app.state.context_runtime
        for context in (
            authorization(tenant_id="other-tenant"),
            authorization(workspace_id="other-workspace"),
            replace(authorization(), workspace_ids=frozenset()),
        ):
            with pytest.raises(PermissionError):
                runtime.models(context)
        client.app.state.graph_driver.assert_not_called()
    finally:
        client.close()
        client.app.state.context_runtime.close()
