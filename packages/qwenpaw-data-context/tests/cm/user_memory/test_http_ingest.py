"""Verify the authenticated manual User Graph ingest adapter."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from context_manager.api.dependencies import require_context_runtime
from context_manager.user.loader import UserHistoryScope

from cm.retrieval.http_support import build_client


def test_manual_ingest_targets_only_the_authenticated_user() -> None:
    """Request payload cannot select another tenant, Workspace, or owner."""
    client, _, _ = build_client()
    calls: list[tuple[datetime, UserHistoryScope]] = []

    async def run(
        cutoff_at: datetime,
        target_scope: UserHistoryScope,
    ) -> dict[str, object]:
        calls.append((cutoff_at, target_scope))
        return {
            "status": "success",
            "users_discovered": 1,
            "users_succeeded": 1,
            "messages_processed": 3,
            "messages_failed": 0,
        }

    client.app.dependency_overrides[require_context_runtime] = lambda: SimpleNamespace(
        user_ingest=SimpleNamespace(ingest=run),
    )

    response = client.post(
        "/api/cm/user/ingest",
        json={
            "tenant_id": "other-tenant",
            "workspace_id": "other-workspace",
            "user_id": "other-user",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["messages_processed"] == 3
    assert response.json()["messages_failed"] == 0
    assert len(calls) == 1
    cutoff_at, target_scope = calls[0]
    assert cutoff_at.tzinfo is not None
    assert target_scope == UserHistoryScope(
        "tenant-1",
        "workspace-1",
        "user-1",
    )


def test_manual_ingest_sanitizes_job_failure() -> None:
    """Model, database, and graph errors do not leak through the debug route."""
    client, _, _ = build_client()

    async def fail(
        _cutoff_at: datetime,
        _target_scope: UserHistoryScope,
    ) -> dict[str, object]:
        raise RuntimeError("secret backend detail")

    client.app.dependency_overrides[require_context_runtime] = lambda: SimpleNamespace(
        user_ingest=SimpleNamespace(ingest=fail),
    )

    response = client.post("/api/cm/user/ingest")

    assert response.status_code == 503
    assert response.json() == {"detail": "manual User Graph ingest failed"}
