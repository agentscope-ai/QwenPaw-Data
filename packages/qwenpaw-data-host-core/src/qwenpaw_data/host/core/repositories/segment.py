# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import ChatRow, SegmentRow
from qwenpaw_data.host.core.domain.identity import Identity


class SegmentRepository:
    """Segment 持久化：segment_id 为 upsert 键（算法侧生成、会话内唯一）。"""

    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    @staticmethod
    def _to_dict(row: SegmentRow) -> dict[str, Any]:
        return {
            "segment_id": row.id,
            "chat_id": row.chat_id,
            "title": row.title,
            "input": row.input,
            "behavior": row.behavior,
            "conclusion": row.conclusion,
            "artifact": row.artifact_json,
            "confidence": row.confidence_json,
            "coverage": {"start_seq": row.start_seq, "end_seq": row.end_seq},
            "started_at": row.started_at,
            "ended_at": row.ended_at,
        }

    async def upsert(
        self,
        *,
        chat_id: str,
        segment_id: str,
        title: str,
        coverage: dict[str, int],
        behavior: str = "",
        conclusion: str = "",
        input: str | None = None,
        artifact: list[dict[str, Any]] | None = None,
        confidence: dict[str, Any] | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> dict[str, Any]:
        start_seq = int(coverage["start_seq"])
        end_seq = int(coverage["end_seq"])
        if start_seq > end_seq:
            raise ValueError("segment coverage start_seq must be <= end_seq")
        row = await self.db.get(SegmentRow, (chat_id, segment_id))
        if row is None:
            row = SegmentRow(
                id=segment_id,
                chat_id=chat_id,
                user_id=self.identity.user_id,
                tenant_id=self.identity.tenant_id,
                workspace_id=self.identity.workspace_id,
            )
            self.db.add(row)
        row.title = title
        row.input = input
        row.behavior = behavior
        row.conclusion = conclusion
        row.artifact_json = artifact
        row.confidence_json = confidence
        row.start_seq = start_seq
        row.end_seq = end_seq
        row.started_at = started_at
        row.ended_at = ended_at
        await self.db.flush()
        return self._to_dict(row)

    async def list_for_chats(
        self, chat_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        if not chat_ids:
            return {}
        rows = (
            await self.db.scalars(
                select(SegmentRow)
                .where(SegmentRow.chat_id.in_(chat_ids))
                .order_by(SegmentRow.start_seq, SegmentRow.end_seq)
            )
        ).all()
        out: dict[str, list[dict[str, Any]]] = {cid: [] for cid in chat_ids}
        for row in rows:
            out.setdefault(row.chat_id, []).append(self._to_dict(row))
        return out

    async def list_artifacts_for_session(self, session_id: str) -> list[dict[str, Any]]:
        if not session_id:
            raise ValueError("session_id is required")
        rows = (
            await self.db.execute(
                select(SegmentRow, ChatRow.sequence)
                .join(ChatRow, ChatRow.id == SegmentRow.chat_id)
                .where(ChatRow.session_id == session_id)
                .order_by(ChatRow.sequence, SegmentRow.start_seq, SegmentRow.end_seq)
            )
        ).all()
        latest: dict[str, dict[str, Any]] = {}
        for row, _sequence in rows:
            for item in row.artifact_json or []:
                path = item.get("relative_path")
                if not path:
                    raise ValueError("segment artifact relative_path is required")
                name = item.get("name")
                if not name:
                    raise ValueError("segment artifact name is required")
                ref = {
                    "segment_id": row.id,
                    "chat_id": row.chat_id,
                    "title": row.title,
                }
                current = latest.get(path)
                if current is None:
                    latest[path] = {
                        "name": name,
                        "description": item.get("description") or "",
                        "relative_path": path,
                        "chat_id": row.chat_id,
                        "segments": [ref],
                    }
                    continue
                if not any(
                    existing["segment_id"] == ref["segment_id"]
                    and existing["chat_id"] == ref["chat_id"]
                    for existing in current["segments"]
                ):
                    current["segments"].append(ref)
        return list(latest.values())
