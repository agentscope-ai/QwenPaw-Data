# -*- coding: utf-8 -*-
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402
from qwenpaw_data.host.core.api.routers.datasources import (  # noqa: E402
    get_context_manager_client,
)
from qwenpaw_data.host.core.integrations.cm import (  # noqa: E402
    CMClientError,
    CMDataset,
    CMDatasetList,
    CMDatasource,
    CMDatasourceList,
)


class FakeCMClient:
    def __init__(self, items=None, error: str | None = None) -> None:
        self._items = items or []
        self._error = error

    def list_datasource_metadata(self) -> CMDatasourceList:
        if self._error:
            raise CMClientError(self._error)
        return CMDatasourceList(items=self._items, total=len(self._items))

    def get_active_datasource_id(self) -> None:
        return None


def test_context_client_uses_local_service_token_only_in_local_mode(monkeypatch):
    from qwenpaw_data.host.core.api.routers import datasources
    from qwenpaw_data.host.core.utils import auth

    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "machine-token")
    monkeypatch.setenv("QWENPAW_DATA_CONTEXT_API_TOKEN", "context-token")
    monkeypatch.setenv("QWENPAW_DATA_CLIENT_API_TOKEN", "client-token")
    monkeypatch.setattr(datasources, "get_access_token", lambda: None)
    monkeypatch.setattr(auth, "auth_enabled", lambda: False)
    assert datasources.get_context_manager_client().access_token == "client-token"
    monkeypatch.delenv("QWENPAW_DATA_CLIENT_API_TOKEN")
    assert datasources.get_context_manager_client().access_token == "context-token"
    monkeypatch.delenv("QWENPAW_DATA_CONTEXT_API_TOKEN")
    assert datasources.get_context_manager_client().access_token == "machine-token"
    monkeypatch.setattr(auth, "auth_enabled", lambda: True)
    assert datasources.get_context_manager_client().access_token is None
    monkeypatch.setattr(datasources, "get_access_token", lambda: "delegated-user-token")
    assert datasources.get_context_manager_client().access_token == "delegated-user-token"


@asynccontextmanager
async def service_client(tmp_path: Path, monkeypatch, fake: FakeCMClient):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "local")
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("QWENPAW_DATA_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'host.db'}")
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    app = create_app()
    app.dependency_overrides[get_context_manager_client] = lambda: fake
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
        ) as http:
            yield http


async def test_datasource_list_proxies_cm(tmp_path, monkeypatch) -> None:
    fake = FakeCMClient(
        items=[
            CMDatasource(
                datasource_id="ds_pg",
                datasource_name="prod-postgres",
                datasource_type="postgres",
                config=None,
            ),
            CMDatasource(
                datasource_id="ds_csv",
                datasource_name="",
                datasource_type=None,
                config=None,
            ),
        ],
    )
    async with service_client(tmp_path, monkeypatch, fake) as http:
        response = await http.get("/api/v1/datasources")
        assert response.status_code == 200
        items = response.json()["items"]
        assert items[0] == {
            "id": "ds_pg",
            "name": "prod-postgres",
            "status": "ready",
            "description": "postgres",
            "recommended": False,
        }
        assert items[1]["name"] == "ds_csv"  # falls back to id


async def test_datasource_list_maps_cm_failure_to_502(tmp_path, monkeypatch) -> None:
    async with service_client(
        tmp_path, monkeypatch, FakeCMClient(error="connection refused")
    ) as http:
        response = await http.get("/api/v1/datasources")
        assert response.status_code == 502
        assert "connection refused" in response.json()["message"]


async def test_dataset_list_reload_and_datasource_scope(tmp_path, monkeypatch):
    class Catalog:
        calls = []

        def list_datasets(self, datasource_id):
            self.calls.append(datasource_id)
            return CMDatasetList(
                (
                    [CMDataset(1, "dws_gaap_di", "Daily GAAP")]
                    if datasource_id == "gaap"
                    else []
                ),
                1 if datasource_id == "gaap" else 0,
            )

    catalog = Catalog()
    async with service_client(tmp_path, monkeypatch, catalog) as http:
        for _ in range(2):
            response = await http.get(
                "/api/v1/datasets", params={"datasource_id": "gaap"}
            )
            assert response.status_code == 200
            assert response.json() == {
                "items": [
                    {
                        "id": "dws_gaap_di",
                        "name": "dws_gaap_di",
                        "description": "Daily GAAP",
                    }
                ]
            }
        response = await http.get("/api/v1/datasets", params={"datasource_id": "other"})
        assert response.json() == {"items": []}
        assert (await http.get("/api/v1/datasets")).status_code == 400
        assert (
            await http.get("/api/v1/datasets", params={"datasource_id": " "})
        ).status_code == 400
    assert catalog.calls == ["gaap", "gaap", "other"]


async def test_dataset_failure_maps_to_502(tmp_path, monkeypatch):
    class Unavailable:
        def list_datasets(self, datasource_id):
            raise CMClientError("CM dataset request returned HTTP 503")

    async with service_client(tmp_path, monkeypatch, Unavailable()) as http:
        response = await http.get("/api/v1/datasets", params={"datasource_id": "gaap"})
        assert response.status_code == 502
        assert "Unable to load datasets" in response.json()["message"]
