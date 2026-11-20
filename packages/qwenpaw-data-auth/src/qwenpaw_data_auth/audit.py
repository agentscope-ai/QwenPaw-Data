"""Security audit logging — append-only event log."""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import insert

from qwenpaw_data_auth.database import audit_logs, get_db, new_id, utcnow_iso

logger = logging.getLogger("qwenpaw_data_auth.audit")

EVENT_TYPES = {
    "login_success",
    "login_failed",
    "logout",
    "register",
    "password_changed",
    "password_reset",
    "user_disabled",
    "user_enabled",
    "role_binding_created",
    "role_binding_deleted",
    "tenant_created",
    "workspace_created",
    "workspace_archived",
    "raw_cypher_grant_issued",
    "raw_cypher_grant_revoked",
    "raw_cypher_grant_consumed",
    "raw_cypher_executed",
    "token_revoked",
    "account_locked",
    # Server-to-server token co-signing for Host cron / IM workers
    "service_access_issued",
    "service_access_denied",
    # Join open tenant/workspace requests
    "access_request_submitted",
    "access_request_approved",
    "access_request_rejected",
    "access_request_canceled",
    "join_open_changed",
}


def log_event(
    event_type: str,
    actor_id: Optional[str] = None,
    actor_ip: str = "",
    target_type: str = "",
    target_id: str = "",
    detail: Optional[dict] = None,
    dsn: str = "",
) -> None:
    if event_type not in EVENT_TYPES:
        logger.warning("Unknown audit event type: %s", event_type)

    detail_json = json.dumps(detail, ensure_ascii=False) if detail else None

    try:
        with get_db(dsn) as conn:
            conn.execute(
                insert(audit_logs).values(
                    id=new_id(),
                    timestamp=utcnow_iso(),
                    event_type=event_type,
                    actor_id=actor_id,
                    actor_ip=actor_ip,
                    target_type=target_type,
                    target_id=target_id,
                    detail=detail_json,
                )
            )
            conn.commit()
    except Exception:
        # Driver exceptions may embed DB passwords/SQL parameters. Do not log them.
        logger.error("Failed to write audit log for event=%s", event_type)
