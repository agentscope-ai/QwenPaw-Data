"""Verify temporary datasource selection with real shared-table persistence."""

from dataclasses import replace

import pytest
from context_manager.api.app import create_service_app
from context_manager.cron import CronSettings
from qwenpaw_data_auth.config import AuthConfig
from fastapi import HTTPException
from fastapi.testclient import TestClient
from semantic_config.tables import active_datasource_selection, datasource
from sqlalchemy import create_engine, select

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import create_external_tables

_PATHS = ("/api/datasources/active", "/api/v1/cm/datasources/active")


@pytest.fixture
def selection_client(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    dsn = f"sqlite:///{tmp_path / 'catalog.db'}"
    monkeypatch.setenv("QWENPAW_DATA_DB_URL", dsn)
    engine = create_engine(dsn)
    datasource.create(engine)
    active_datasource_selection.create(engine)
    with engine.begin() as connection:
        connection.execute(
            datasource.insert(),
            [
                {
                    "id": index,
                    "datasource_id": code,
                    "tenant_id": tenant,
                    "workspace_id": workspace,
                    "is_deleted": deleted,
                }
                for index, code, tenant, workspace, deleted in (
                    (11, "sales", "tenant-1", "workspace-1", 0),
                    (12, "finance", "tenant-1", "workspace-1", 0),
                    (13, "deleted", "tenant-1", "workspace-1", 1),
                    (21, "other-workspace", "tenant-1", "workspace-2", 0),
                    (31, "other-tenant", "tenant-2", "workspace-1", 0),
                )
            ],
        )

    identities = {
        "user-1": authorization(),
        "user-2": authorization(user_id="user-2"),
        "workspace-2": authorization(workspace_id="workspace-2"),
        "tenant-2": authorization(tenant_id="tenant-2"),
        "forbidden": replace(authorization(), workspace_ids=frozenset()),
    }

    def authorize(request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in identities:
            raise HTTPException(401, "Not authenticated")
        return identities[token]

    async def initialized_tables():
        create_external_tables(engine)

    app = create_service_app(
        auth_config=AuthConfig(secret_keys=["test-secret"], control_plane_dsn=dsn),
        authorizer=authorize,
        semantic_initializer=initialized_tables,
        cron_settings=CronSettings(enabled=False),
    )
    try:
        with TestClient(app, headers={"Authorization": "Bearer user-1"}) as client:
            yield client, engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("path", _PATHS)
def test_selection_roundtrip_updates_one_existing_table_row(selection_client, path):
    client, engine = selection_client
    assert client.get(path).json() == {"datasource_id": ""}
    for value, selected in (
        (" sales ", "sales"),
        ("12", "finance"),
        ("finance", "finance"),
    ):
        response = client.put(path, json={"datasource_id": value})
        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "datasource_id": selected,
            "message": "switched",
        }
        assert response.headers["Deprecation"] == "true"
        for read_path in _PATHS:
            result = client.get(read_path)
            assert result.json() == {"datasource_id": selected}
            assert result.headers["Deprecation"] == "true"
    with engine.connect() as connection:
        rows = connection.execute(select(active_datasource_selection)).mappings().all()
    assert len(rows) == 1
    assert dict(rows[0]) == {
        "tenant_id": "tenant-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "datasource_id": "finance",
        "updated_at": rows[0]["updated_at"],
    }
    assert rows[0]["updated_at"]


@pytest.mark.parametrize(
    ("identity", "selected"),
    [
        ("user-2", "finance"),
        ("workspace-2", "other-workspace"),
        ("tenant-2", "other-tenant"),
    ],
)
def test_selection_isolated_by_tenant_workspace_and_user(
    selection_client, identity, selected
):
    client, _ = selection_client
    path = _PATHS[0]
    assert client.put(path, json={"datasource_id": "sales"}).status_code == 200
    headers = {"Authorization": f"Bearer {identity}"}
    assert client.get(path, headers=headers).json() == {"datasource_id": ""}
    assert (
        client.put(path, headers=headers, json={"datasource_id": selected}).status_code
        == 200
    )
    assert client.get(path, headers=headers).json() == {"datasource_id": selected}
    assert client.get(path).json() == {"datasource_id": "sales"}
