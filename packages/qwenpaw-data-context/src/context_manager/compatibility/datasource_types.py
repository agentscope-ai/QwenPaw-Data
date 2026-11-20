"""Project canonical datasource config declarations into the legacy catalog."""

from __future__ import annotations

from copy import deepcopy

from semantic_config.models.datasource_config import (
    CONFIG_MODEL_BY_TYPE,
    DatasourceType,
)

# TODO(cm-api-compat): remove this projection after the frontend no longer calls
# GET /api/datasource-types and the Datasource capability owns its final public
# discovery contract. Do not move this legacy UI metadata into semantic_config.

_LEGACY_LABELS: dict[DatasourceType, str] = {
    DatasourceType.BIGQUERY: "BigQuery",
    DatasourceType.DORIS: "Apache Doris",
    DatasourceType.DUCKDB: "DuckDB",
    DatasourceType.MYSQL: "MySQL",
    DatasourceType.ODPS: "ODPS (MaxCompute)",
    DatasourceType.POSTGRESQL: "PostgreSQL",
    DatasourceType.SQLITE: "SQLite",
    DatasourceType.STARROCKS: "StarRocks",
    DatasourceType.TIDB: "TiDB",
}

_LEGACY_PASSWORD_FIELDS = frozenset(
    {
        "access_key_id",
        "access_key_secret",
        "password",
        "sts_token",
    }
)


def _legacy_form_schema(datasource_type: DatasourceType) -> dict[str, object]:
    """Decorate one detached canonical JSON Schema for the retired form renderer."""
    model = CONFIG_MODEL_BY_TYPE[datasource_type]
    schema = deepcopy(model.model_json_schema())
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError(
            f"datasource config schema has no properties: {datasource_type.value}"
        )

    for order, field_name in enumerate(model.model_fields, start=1):
        field_schema = properties.get(field_name)
        if not isinstance(field_schema, dict):
            raise RuntimeError(
                "datasource config schema is missing field "
                f"{datasource_type.value}.{field_name}"
            )
        credential = (
            field_name in _LEGACY_PASSWORD_FIELDS or field_name == "credentials_json"
        )
        field_schema["x-ui-group"] = "credentials" if credential else "connection"
        field_schema["x-ui-order"] = order
        if field_name in _LEGACY_PASSWORD_FIELDS:
            field_schema["format"] = "password"
            field_schema["writeOnly"] = True
        elif field_name == "credentials_json":
            field_schema["writeOnly"] = True

    return schema


def legacy_datasource_type_catalog() -> list[dict[str, object]]:
    """Return the deterministic legacy type catalog without provider operations."""
    configured_types = set(CONFIG_MODEL_BY_TYPE)
    labelled_types = set(_LEGACY_LABELS)
    if configured_types != labelled_types:
        missing = sorted(item.value for item in configured_types - labelled_types)
        stale = sorted(item.value for item in labelled_types - configured_types)
        raise RuntimeError(
            "legacy datasource labels do not match canonical config models: "
            f"missing={missing}, stale={stale}"
        )
    return [
        {
            "type": datasource_type.value,
            "label": _LEGACY_LABELS[datasource_type],
            "category": "sql",
            "form_schema": _legacy_form_schema(datasource_type),
        }
        for datasource_type in sorted(configured_types, key=lambda item: item.value)
    ]


__all__ = ["legacy_datasource_type_catalog"]
