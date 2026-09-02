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
from qwenpaw_data.host.core.cm_client import (  # noqa: E402
    CMClientError,
    CMDatasource,
    CMDatasourceList,
)


class FakeCMClient:
    def __init__(self, items=None, error: str | None = None) -> None:
        self._items = items or []
        self._error = error

    def list_datasources(self) -> CMDatasourceList:
        if self._error:
            raise CMClientError(self._error)
        return CMDatasourceList(items=self._items, total=len(self._items))


@asynccontextmanager
async def service_client(tmp_path: Path, monkeypatch, fake: FakeCMClient):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    app = create_app(home=tmp_path, model=object())
    app.dependency_overrides[get_context_manager_client] = lambda: fake
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
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
