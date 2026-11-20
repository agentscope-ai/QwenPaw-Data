"""Expose thin authenticated model-configuration APIs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from ....model import EmbeddingSpaceConflict, ModelConfigError, ModelService
from ....runtime import ContextRuntimeUnavailable
from ...dependencies import AuthorizationDependency, RuntimeDependency


class LlmConfigPayload(BaseModel):
    """Accept one partial LLM configuration update or probe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str | None = None
    model: str | None = None
    provider_id: str | None = None


class EmbeddingConfigPayload(BaseModel):
    """Accept one partial embedding configuration update or probe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str | None = None
    model: str | None = None
    provider_id: str | None = None
    dim: int | None = Field(default=None, ge=1)


class LlmConfigResponse(BaseModel):
    """Return only the masked LLM configuration fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str | None = None
    model: str | None = None
    provider_id: str | None = None
    api_key: str


class EmbeddingConfigResponse(BaseModel):
    """Return only the masked embedding configuration fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str | None = None
    model: str | None = None
    provider_id: str | None = None
    api_key: str
    dim: int | None = None


class EmbeddingSpaceResponse(BaseModel):
    """Report configuration agreement, not whether graph vectors were rebuilt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adopted: bool = False
    aligned: bool | None = None
    model: str | None = None
    base_url: str | None = None
    dim: int | None = None
    fingerprint: str | None = None


class ModelConfigResponse(BaseModel):
    """Return the complete frontend-safe model configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    llm: LlmConfigResponse
    embedding: EmbeddingConfigResponse
    embedding_space: EmbeddingSpaceResponse
    configured: bool
    key_status: str
    seeded: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class LlmUpdateResponse(BaseModel):
    """Return the updated, masked LLM document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    llm: LlmConfigResponse


class EmbeddingUpdateResponse(BaseModel):
    """Return the updated embedding document and invalidation signal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    embedding: EmbeddingConfigResponse
    rebuild_required: bool


class ModelConnectionTestResponse(BaseModel):
    """Return a sanitized provider connection-test result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    success: bool
    message: str
    detected_dim: int | None = None


def require_model_config(
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> ModelService:
    """Select model configuration through the authenticated runtime target."""
    try:
        return runtime.models(context)
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except (ContextRuntimeUnavailable, ModelConfigError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


ModelConfigDependency = Annotated[
    ModelService,
    Depends(require_model_config),
]

router = APIRouter(prefix="/config", tags=["context-manager-config"])


def _payload(payload: BaseModel) -> dict[str, object]:
    """Detach only fields supplied by this request for domain-level merging."""
    return payload.model_dump(exclude_unset=True)


def _invalid_config(exc: TypeError | ValueError) -> HTTPException:
    """Map domain validation without leaking provider or secret details."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=str(exc),
    )


def _unavailable_config(exc: ModelConfigError) -> HTTPException:
    """Map private persistence failures to a stable service response."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=str(exc),
    )


@router.get("", response_model=ModelConfigResponse)
def get_model_config(
    config: ModelConfigDependency,
) -> ModelConfigResponse:
    """Read one masked configuration from the selected runtime."""
    try:
        return ModelConfigResponse.model_validate(config.read())
    except ModelConfigError as exc:
        raise _unavailable_config(exc) from exc


@router.delete("")
def delete_model_config(config: ModelConfigDependency) -> dict[str, bool]:
    """Remove only the authenticated user's model selections and references."""
    try:
        config.delete()
        return {"deleted": True}
    except ModelConfigError as exc:
        raise _unavailable_config(exc) from exc


@router.put("/llm", response_model=LlmUpdateResponse)
def update_llm_config(
    payload: LlmConfigPayload,
    config: ModelConfigDependency,
) -> LlmUpdateResponse:
    """Save one partial model selection for the authenticated user."""
    try:
        llm, _ = config.save("llm", _payload(payload))
        return LlmUpdateResponse(llm=llm)
    except (TypeError, ValueError) as exc:
        raise _invalid_config(exc) from exc
    except ModelConfigError as exc:
        raise _unavailable_config(exc) from exc


@router.post("/llm/test", response_model=ModelConnectionTestResponse)
async def test_llm_config(
    payload: LlmConfigPayload,
    config: ModelConfigDependency,
) -> ModelConnectionTestResponse:
    """Probe an LLM candidate without persisting or activating it."""
    try:
        result = await config.probe("llm", _payload(payload))
        return ModelConnectionTestResponse.model_validate(result)
    except (TypeError, ValueError) as exc:
        raise _invalid_config(exc) from exc
    except ModelConfigError as exc:
        raise _unavailable_config(exc) from exc


@router.put("/embed", response_model=EmbeddingUpdateResponse)
def update_embedding_config(
    payload: EmbeddingConfigPayload,
    config: ModelConfigDependency,
    context: AuthorizationDependency,
) -> EmbeddingUpdateResponse:
    """Delegate an embedding update without owning rebuild-job policy."""
    try:
        embedding, rebuild_required = config.save(
            "embedding",
            _payload(payload),
            can_override_space=(
                context.role == "tenant_admin"
                or context.active_workspace_role == "ws_admin"
            ),
        )
        return EmbeddingUpdateResponse(
            embedding=embedding,
            rebuild_required=rebuild_required,
        )
    except EmbeddingSpaceConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "code": "embedding_space_conflict",
                    "message": str(exc),
                    "details": exc.details,
                }
            },
        ) from exc
    except (TypeError, ValueError) as exc:
        raise _invalid_config(exc) from exc
    except ModelConfigError as exc:
        raise _unavailable_config(exc) from exc


@router.post("/embed/test", response_model=ModelConnectionTestResponse)
async def test_embedding_config(
    payload: EmbeddingConfigPayload,
    config: ModelConfigDependency,
) -> ModelConnectionTestResponse:
    """Probe an embedding candidate without persisting or activating it."""
    try:
        result = await config.probe("embedding", _payload(payload))
        return ModelConnectionTestResponse.model_validate(result)
    except (TypeError, ValueError) as exc:
        raise _invalid_config(exc) from exc
    except ModelConfigError as exc:
        raise _unavailable_config(exc) from exc


__all__ = [
    "EmbeddingConfigPayload",
    "EmbeddingConfigResponse",
    "EmbeddingSpaceResponse",
    "EmbeddingUpdateResponse",
    "LlmConfigPayload",
    "LlmConfigResponse",
    "LlmUpdateResponse",
    "ModelConfigDependency",
    "ModelConfigResponse",
    "ModelConnectionTestResponse",
    "get_model_config",
    "delete_model_config",
    "require_model_config",
    "router",
    "test_embedding_config",
    "test_llm_config",
    "update_embedding_config",
    "update_llm_config",
]
