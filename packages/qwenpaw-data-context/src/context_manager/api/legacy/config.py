"""Forward the frontend model-config routes to current handlers."""

from __future__ import annotations

from fastapi import APIRouter, Response

from ..native.config.views import (
    EmbeddingConfigPayload,
    EmbeddingUpdateResponse,
    LlmConfigPayload,
    LlmUpdateResponse,
    ModelConfigDependency,
    ModelConfigResponse,
    ModelConnectionTestResponse,
    get_model_config,
    delete_model_config,
    test_embedding_config,
    test_llm_config,
    update_embedding_config,
    update_llm_config,
)
from ..dependencies import AuthorizationDependency

# TODO(cm-api-compat): remove these forwarders after the context frontend
# migrates from /api/system/model-config/* to /api/v1/config/*.

router = APIRouter(
    prefix="/api/system/model-config",
    tags=["legacy-model-config"],
)


def _deprecate(response: Response, successor: str) -> None:
    """Identify the canonical successor of one legacy frontend route."""
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = f'<{successor}>; rel="successor-version"'


@router.get("/", response_model=ModelConfigResponse)
def legacy_get_model_config(
    response: Response,
    config: ModelConfigDependency,
) -> ModelConfigResponse:
    """Forward the legacy root read without another config implementation."""
    _deprecate(response, "/api/v1/config")
    return get_model_config(config)


@router.delete("/")
def legacy_delete_model_config(
    response: Response,
    config: ModelConfigDependency,
) -> dict[str, bool]:
    """Forward clearing personal model selections without deleting Providers."""
    _deprecate(response, "/api/v1/config")
    return delete_model_config(config)


@router.put("/llm", response_model=LlmUpdateResponse)
def legacy_update_llm_config(
    payload: LlmConfigPayload,
    response: Response,
    config: ModelConfigDependency,
) -> LlmUpdateResponse:
    """Forward the legacy LLM update to the canonical handler."""
    _deprecate(response, "/api/v1/config/llm")
    return update_llm_config(payload, config)


@router.post("/llm/test", response_model=ModelConnectionTestResponse)
async def legacy_test_llm_config(
    payload: LlmConfigPayload,
    response: Response,
    config: ModelConfigDependency,
) -> ModelConnectionTestResponse:
    """Forward the legacy LLM probe to the canonical handler."""
    _deprecate(response, "/api/v1/config/llm/test")
    return await test_llm_config(payload, config)


@router.put("/embedding", response_model=EmbeddingUpdateResponse)
def legacy_update_embedding_config(
    payload: EmbeddingConfigPayload,
    response: Response,
    config: ModelConfigDependency,
    context: AuthorizationDependency,
) -> EmbeddingUpdateResponse:
    """Map the legacy embedding path to the canonical embed path."""
    _deprecate(response, "/api/v1/config/embed")
    return update_embedding_config(payload, config, context)


@router.post("/embedding/test", response_model=ModelConnectionTestResponse)
async def legacy_test_embedding_config(
    payload: EmbeddingConfigPayload,
    response: Response,
    config: ModelConfigDependency,
) -> ModelConnectionTestResponse:
    """Map the legacy embedding probe to the canonical embed path."""
    _deprecate(response, "/api/v1/config/embed/test")
    return await test_embedding_config(payload, config)


__all__ = [
    "legacy_get_model_config",
    "legacy_delete_model_config",
    "legacy_test_embedding_config",
    "legacy_test_llm_config",
    "legacy_update_embedding_config",
    "legacy_update_llm_config",
    "router",
]
