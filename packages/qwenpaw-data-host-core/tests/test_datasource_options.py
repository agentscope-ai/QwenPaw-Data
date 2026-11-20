from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.api.routers.datasources import get_context_manager_client
from qwenpaw_data.host.core.integrations.cm import (
    CMClientError,
    CMLockedError,
    CMDataset,
    CMDatasetList,
    CMDatasource,
    CMDatasourceList,
)


class StubContextManagerClient:
    def __init__(
        self,
        *,
        result: CMDatasourceList | None = None,
        error: CMClientError | None = None,
        active_id: str | None = None,
        active_error: CMClientError | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.active_id = active_id
        self.active_error = active_error
        self.datasets = None
        self.dataset_error = None
        self.list_datasets_calls: list[str] = []

    def list_datasource_metadata(self) -> CMDatasourceList:
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def get_active_datasource_id(self) -> str | None:
        if self.active_error is not None:
            raise self.active_error
        return self.active_id

    def list_datasets(self, datasource_id: str) -> CMDatasetList:
        self.list_datasets_calls.append(datasource_id)
        if self.dataset_error is not None:
            raise self.dataset_error
        assert self.datasets is not None
        return self.datasets


def _headers() -> dict[str, str]:
    return {}


def test_datasource_options_are_loaded_from_context_manager(tmp_path: Path) -> None:
    upstream = StubContextManagerClient(
        result=CMDatasourceList(
            items=[
                CMDatasource(
                    datasource_id="postgresql-primary",
                    datasource_name="Primary Warehouse",
                    datasource_type="postgresql",
                    config={"password": "must-not-leak"},
                ),
                CMDatasource(
                    datasource_id="odps-fallback",
                    datasource_name=None,
                    datasource_type="odps",
                    config=None,
                ),
            ],
            total=2,
        ),
    )
    app = create_app()
    app.dependency_overrides[get_context_manager_client] = lambda: upstream

    with TestClient(app) as client:
        response = client.get("/api/datasources", headers=_headers())

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": "postgresql-primary",
                "name": "Primary Warehouse",
                "status": "ready",
                "description": "postgresql",
                "recommended": False,
            },
            {
                "id": "odps-fallback",
                "name": "odps-fallback",
                "status": "ready",
                "description": "odps",
                "recommended": False,
            },
        ],
    }
    assert "must-not-leak" not in response.text


def test_datasource_options_mark_databridge_active_as_recommended(
    tmp_path: Path,
) -> None:
    upstream = StubContextManagerClient(
        result=CMDatasourceList(
            items=[
                CMDatasource(
                    datasource_id="postgresql-primary",
                    datasource_name="Primary Warehouse",
                    datasource_type="postgresql",
                    config=None,
                ),
                CMDatasource(
                    datasource_id="odps-fallback",
                    datasource_name="Warehouse",
                    datasource_type="odps",
                    config=None,
                ),
            ],
            total=2,
        ),
        active_id="odps-fallback",
    )
    app = create_app()
    app.dependency_overrides[get_context_manager_client] = lambda: upstream

    with TestClient(app) as client:
        response = client.get("/api/datasources", headers=_headers())

    assert response.status_code == 200
    assert response.json()["items"][0]["recommended"] is False
    assert response.json()["items"][1]["recommended"] is True


def test_datasource_options_keep_list_when_active_lookup_fails(tmp_path: Path) -> None:
    upstream = StubContextManagerClient(
        result=CMDatasourceList(
            items=[
                CMDatasource(
                    datasource_id="postgresql-primary",
                    datasource_name="Primary Warehouse",
                    datasource_type="postgresql",
                    config=None,
                ),
            ],
            total=1,
        ),
        active_error=CMClientError("active lookup failed"),
    )
    app = create_app()
    app.dependency_overrides[get_context_manager_client] = lambda: upstream

    with TestClient(app) as client:
        response = client.get("/api/datasources", headers=_headers())

    assert response.status_code == 200
    assert response.json()["items"][0]["recommended"] is False


def test_datasource_options_pass_through_rebuild_lock(tmp_path: Path) -> None:
    upstream = StubContextManagerClient(error=CMLockedError())
    app = create_app()
    app.dependency_overrides[get_context_manager_client] = lambda: upstream

    with TestClient(app) as client:
        response = client.get("/api/datasources", headers=_headers())

    assert response.status_code == 423
    assert response.json() == {
        "code": "VALIDATION",
        "message": "图谱正在重建，其他功能暂时不可用，请稍后刷新重试",
        "details": None,
    }


def test_datasource_options_report_databridge_failure(tmp_path: Path) -> None:
    upstream = StubContextManagerClient(error=CMClientError("connection failed"))
    app = create_app()
    app.dependency_overrides[get_context_manager_client] = lambda: upstream

    with TestClient(app) as client:
        response = client.get("/api/datasources", headers=_headers())

    assert response.status_code == 502
    assert response.json() == {
        "code": "VALIDATION",
        "message": ("Unable to load data sources from DataBridge: connection failed"),
        "details": None,
    }


def test_dataset_options_require_datasource_id(tmp_path: Path) -> None:
    app = create_app()
    app.dependency_overrides[get_context_manager_client] = lambda: (
        StubContextManagerClient()
    )

    with TestClient(app) as client:
        response = client.get("/api/datasets", headers=_headers())

    assert response.status_code == 400
    assert response.json()["message"] == "datasource_id is required"


def test_dataset_options_use_dataset_name_and_omit_source_sql(tmp_path: Path) -> None:
    upstream = StubContextManagerClient()
    upstream.datasets = CMDatasetList(
        items=[
            CMDataset(
                dataset_id=11,
                dataset_name="orders",
                description="订单表",
            ),
            CMDataset(
                dataset_id=12,
                dataset_name="users",
                description=None,
            ),
        ],
        total=2,
    )
    app = create_app()
    app.dependency_overrides[get_context_manager_client] = lambda: upstream

    with TestClient(app) as client:
        response = client.get(
            "/api/datasets",
            params={"datasource_id": "postgresql-primary"},
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": "orders",
                "name": "orders",
                "description": "订单表",
            },
            {
                "id": "users",
                "name": "users",
                "description": "",
            },
        ],
    }
    assert upstream.list_datasets_calls == ["postgresql-primary"]
    assert "source" not in response.text
    assert "11" not in response.text


def test_dataset_options_report_databridge_failure(tmp_path: Path) -> None:
    upstream = StubContextManagerClient()
    upstream.dataset_error = CMClientError("connection failed")
    app = create_app()
    app.dependency_overrides[get_context_manager_client] = lambda: upstream

    with TestClient(app) as client:
        response = client.get(
            "/api/datasets",
            params={"datasource_id": "postgresql-primary"},
            headers=_headers(),
        )

    assert response.status_code == 502
    assert response.json() == {
        "code": "VALIDATION",
        "message": ("Unable to load datasets from DataBridge: connection failed"),
        "details": None,
    }
