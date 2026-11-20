"""Expose settlement-card ingestion as a native Metadata operation."""

from typing import Literal

from fastapi import APIRouter, HTTPException

from ....auth import WriteOwner
from ....metadata.feedback import FeedbackError
from ....metadata.feedback.contracts import CardAck, FeedbackCard
from ...dependencies import AuthorizationDependency, RuntimeDependency

router = APIRouter(prefix="/api/v1/semantic", tags=["feedback"])


@router.post("/feedback_card", response_model=CardAck)
async def feedback_card(
    card: FeedbackCard,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    mode: Literal["test", "confirm"] = "test",
) -> CardAck:
    def prepare():
        scope = runtime.issue_graph_write_scope(
            context,
            owner=WriteOwner.USER,
            zone="metadata",
        )
        return runtime.metadata_feedback(scope), scope

    try:
        service, scope = await runtime.run_background(prepare)
        return await service.ingest(card, scope, mode=mode)
    except BlockingIOError as exc:
        raise HTTPException(503, "Metadata execution is busy") from exc
    except FeedbackError as exc:
        raise HTTPException(
            exc.status_code, {"code": exc.code, "message": str(exc)}
        ) from exc
