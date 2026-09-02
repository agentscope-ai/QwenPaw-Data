# -*- coding: utf-8 -*-
"""Settlement card → CM ``feedback_card`` payload mapping & ingest."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from .client import SettlementCmClient

logger = logging.getLogger(__name__)

FeedbackMode = Literal["confirm", "test"]

# Only these two acks mean the dry-run would land new knowledge; ``duplicate``
# means it already exists, so the card is not worth recommending.
_RECOMMENDABLE_ACK = frozenset({"accepted", "updated"})

# CM ColumnMeaningCard.description max_length
_COLUMN_MEANING_MAX_LEN = 4000


def _split_samples(raw: str) -> list[str]:
    parts = re.split(r"[,，、;/]+", raw or "")
    return [p.strip() for p in parts if p.strip()][:20]


def _feedback_base(
    card: dict[str, Any],
    fields: dict[str, Any],
    *,
    datasource_id: str,
) -> dict[str, Any]:
    confirmed_at = card.get("confirmed_at")
    if hasattr(confirmed_at, "isoformat"):
        confirmed_at = confirmed_at.isoformat()
    client_card_id = str(card.get("id") or "").strip()
    if not client_card_id:
        raise ValueError("card.id is required for feedback_card client_card_id")
    domain = str(fields.get("domain") or "").strip()
    if not domain:
        raise ValueError("fields.domain is required for feedback_card")
    ds = (datasource_id or "").strip()
    if not ds:
        raise ValueError("datasource_id is required for feedback_card")
    session_id = str(card.get("session_id") or "").strip()
    source_chat_id = str(card.get("source_chat_id") or "").strip()
    return {
        "client_card_id": client_card_id,
        "domain": domain,
        "datasource_id": ds,
        "extra": {
            "confirmed_at": confirmed_at,
            "session_id": session_id or None,
            "source_chat_id": source_chat_id or None,
        },
    }


def card_to_feedback_payload(
    card: dict[str, Any],
    *,
    datasource_id: str | None = None,
) -> dict[str, Any]:
    """Map a settlement card to CM ``feedback_card`` body."""
    ds = (datasource_id or "").strip()
    if not ds:
        raise ValueError("datasource_id is required for feedback_card")

    card_type = str(card.get("type") or "")
    fields = dict(card.get("fields") or {})
    base = _feedback_base(card, fields, datasource_id=ds)

    if card_type == "metric_caliber":
        return {
            **base,
            "type": "metric_caliber",
            "name": str(fields.get("metric_name") or "").strip(),
            "description": str(fields.get("caliber") or "").strip(),
            "dataset": str(fields.get("table") or "").strip(),
            "formula": str(fields.get("formula_sql") or "").strip(),
        }

    if card_type == "dimension_def":
        return {
            **base,
            "type": "dimension_def",
            "name": str(fields.get("dimension_name") or "").strip(),
            "dataset_name": str(fields.get("table") or "").strip(),
            "maps_to_column": str(fields.get("bind_column") or "").strip(),
            "values": _split_samples(str(fields.get("value_samples") or "")),
        }

    if card_type == "column_meaning":
        meaning = str(fields.get("meaning") or "").strip()
        return {
            **base,
            "type": "column_meaning",
            "dataset_name": str(fields.get("table") or "").strip(),
            "column": str(fields.get("column_name") or "").strip(),
            "description": meaning[:_COLUMN_MEANING_MAX_LEN],
        }

    if card_type == "dataset_usage":
        dataset = str(fields.get("recommended_dataset") or "").strip()
        return {
            **base,
            "type": "dataset_usage",
            "description": str(fields.get("use_case") or "").strip(),
            "datasets": [dataset] if dataset else [],
        }

    raise ValueError(f"unsupported settlement card type: {card_type}")


def feedback_ack_status(record: dict[str, Any]) -> str | None:
    """Parse CardAck.status from a successful ``call`` record; else None."""
    if not str(record.get("status") or "").startswith("ok"):
        return None
    try:
        body = json.loads(record.get("result") or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    ack = str(body.get("status") or "").strip()
    return ack or None


def feedback_dry_run_recommendable(record: dict[str, Any]) -> bool:
    """True when dry-run ack is ``accepted`` or ``updated`` (not ``duplicate``)."""
    return feedback_ack_status(record) in _RECOMMENDABLE_ACK


async def settlement_ingest(
    card: dict[str, Any],
    *,
    datasource_id: str | None = None,
    access_token: str | None = None,
    cm: SettlementCmClient | None = None,
    mode: FeedbackMode = "confirm",
) -> dict[str, Any]:
    """POST card to CM ``/api/v1/semantic/feedback_card`` (``mode=test|confirm``)."""
    client = cm or SettlementCmClient(
        access_token=access_token,
        datasource_id=datasource_id,
    )
    ds = (datasource_id or client.datasource_id or "").strip()
    if not ds:
        logger.warning(
            "Settlement CM ingest skipped for card %s: missing datasource_id",
            card.get("id"),
        )
        return {
            "tool": "feedback_card",
            "kwargs": {},
            "status": "error: missing datasource_id",
            "result": "datasource_id is required for feedback_card",
        }
    payload = card_to_feedback_payload(card, datasource_id=ds)
    return await client.call("feedback_card", payload, max_len=0, mode=mode)
