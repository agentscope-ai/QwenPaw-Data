"""Forward the retired datasource-type catalog path to Compatibility."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict

from ...compatibility.datasource_types import legacy_datasource_type_catalog
from ..dependencies import AuthorizationDependency

# TODO(cm-api-compat): remove this forwarder after the context frontend stops
# calling GET /api/datasource-types. No successor Link is emitted until a real
# Datasource discovery API exists; do not point clients at a non-equivalent URL.


class LegacyDatasourceTypeInfo(BaseModel):
    """Validate the temporary catalog shape consumed by the existing frontend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    label: str
    category: Literal["sql"]
    form_schema: dict[str, object]


router = APIRouter(tags=["legacy-datasource-types"])


@router.get(
    "/api/datasource-types",
    response_model=list[LegacyDatasourceTypeInfo],
)
def legacy_list_datasource_types(
    response: Response,
    _authorization: AuthorizationDependency,
) -> list[LegacyDatasourceTypeInfo]:
    """Delegate catalog projection without adding datasource business logic."""
    response.headers["Deprecation"] = "true"
    return [
        LegacyDatasourceTypeInfo.model_validate(item)
        for item in legacy_datasource_type_catalog()
    ]


__all__ = [
    "LegacyDatasourceTypeInfo",
    "legacy_list_datasource_types",
    "router",
]
