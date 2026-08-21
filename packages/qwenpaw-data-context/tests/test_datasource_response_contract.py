import json

from semantic_config.models.datasource import DatasourceUpdate
from semantic_config.services import datasource_service
from semantic_config.services.connection_tester import _safe_error_message
from semantic_config.services.datasource_service import _to_response


def test_datasource_response_exposes_datasource_id_only() -> None:
    response = _to_response(
        {
            "datasource_id": "postgresql-example",
            "datasource_name": "Example",
            "datasource_type": "postgresql",
            "config": "{}",
        },
    )

    assert response.datasource_id == "postgresql-example"
    assert response.datasource_name == "Example"
    assert response.datasource_type == "postgresql"
    # datasource_code 已彻底移除，Response 不应再暴露该字段
    assert not hasattr(response, "datasource_code")


def test_datasource_response_never_returns_stored_secrets() -> None:
    response = _to_response(
        {
            "datasource_id": "postgresql-example",
            "datasource_name": "Example",
            "datasource_type": "postgresql",
            "config": json.dumps(
                {
                    "host": "db.internal",
                    "user": "alice",
                    "password": "must-not-leak",
                    "sts_token": "must-not-leak-either",
                }
            ),
        }
    )
    assert response.config == {"host": "db.internal", "user": "alice"}


async def test_empty_secret_on_update_preserves_existing_value(monkeypatch) -> None:
    row = {
        "id": 1,
        "datasource_id": "postgresql-example",
        "datasource_name": "Example",
        "datasource_type": "postgresql",
        "config": json.dumps(
            {
                "host": "old-host",
                "port": 5432,
                "dbname": "app",
                "user": "alice",
                "password": "stored-secret",
            }
        ),
    }
    saved: dict[str, str] = {}

    async def find_by_datasource_id(_db, _datasource_id):
        return row

    async def update(_db, _id, **kwargs):
        saved["config"] = kwargs["config_json"]
        return 1

    async def find_by_id(_db, _id):
        next_row = dict(row)
        next_row["config"] = saved["config"]
        return next_row

    monkeypatch.setattr(datasource_service.repo, "find_by_datasource_id", find_by_datasource_id)
    monkeypatch.setattr(datasource_service.repo, "update", update)
    monkeypatch.setattr(datasource_service.repo, "find_by_id", find_by_id)

    response = await datasource_service.update(
        object(),
        "postgresql-example",
        DatasourceUpdate(
            config={
                "host": "new-host",
                "port": 5432,
                "dbname": "app",
                "user": "alice",
                "password": "",
            }
        ),
    )
    assert json.loads(saved["config"])["password"] == "stored-secret"
    assert response.config == {
        "host": "new-host",
        "port": 5432,
        "dbname": "app",
        "user": "alice",
    }


def test_connection_error_redacts_raw_credential_values() -> None:
    message = _safe_error_message(
        RuntimeError("login failed for alice with super-secret-password"),
        {"password": "super-secret-password"},
    )
    assert "super-secret-password" not in message
    assert "***" in message
