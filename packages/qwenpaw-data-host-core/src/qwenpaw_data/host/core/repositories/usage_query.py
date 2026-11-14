# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import ModelUsageRow, SessionRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.usage_window import (
    EmbeddingUsage,
    GenerationUsage,
    UsageWindow,
    background_group_id,
    build_usage_buckets,
    public_model_type,
)

_UNNAMED = "未命名 Session"
_row = ModelUsageRow
_GEN = _row.model_type.in_(("chat", "text"))
_EMB = _row.model_type == "embedding"


def _token_sums():
    return (
        func.coalesce(func.sum(case((_GEN, _row.input_tokens), else_=0)), 0),
        func.coalesce(func.sum(case((_GEN, _row.output_tokens), else_=0)), 0),
        func.coalesce(func.sum(case((_GEN, 1), else_=0)), 0),
        func.coalesce(func.sum(case((_EMB, _row.input_tokens), else_=0)), 0),
        func.coalesce(func.sum(case((_EMB, 1), else_=0)), 0),
    )


def _split(row: Any, offset: int = 0) -> tuple[GenerationUsage, EmbeddingUsage]:
    return (
        GenerationUsage.from_parts(row[offset], row[offset + 1], row[offset + 2]),
        EmbeddingUsage.from_parts(row[offset + 3], row[offset + 4]),
    )


def _totals(gen: GenerationUsage, emb: EmbeddingUsage, **extra: Any) -> dict[str, Any]:
    return {
        "generation": asdict(gen),
        "embedding": asdict(emb),
        "total_tokens": gen.total_tokens + emb.tokens,
        **extra,
    }


def _page(page: int, page_size: int, total: int) -> dict[str, Any]:
    page_count = (total + page_size - 1) // page_size if total else 0
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "page_count": page_count,
        "has_previous": page > 1 and total > 0,
        "has_next": page * page_size < total,
    }


def _unique_models(rows: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    models: dict[tuple[str, str, str], dict[str, str]] = {}
    for provider_id, model_id, raw_type in sorted(rows):
        mapped = public_model_type(raw_type)
        if mapped is None:
            continue
        models.setdefault(
            (provider_id, model_id, mapped),
            {"provider_id": provider_id, "model_id": model_id, "model_type": mapped},
        )
    return list(models.values())


class UsageQueryRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    def _scope(self, window: UsageWindow):
        return (
            _row.tenant_id == self.identity.tenant_id,
            _row.workspace_id == self.identity.workspace_id,
            _row.user_id == self.identity.user_id,
            _row.created_at >= window.start,
            _row.created_at < window.end,
        )

    async def summary(self, window: UsageWindow) -> dict[str, Any]:
        gen_in, gen_out, gen_calls, emb_tok, emb_calls = _token_sums()
        row = (
            await self.db.execute(
                select(
                    gen_in,
                    gen_out,
                    gen_calls,
                    emb_tok,
                    emb_calls,
                    func.count(
                        func.distinct(case((_row.session_id != "", _row.session_id)))
                    ),
                    func.count(
                        func.distinct(case((_row.session_id == "", _row.source)))
                    ),
                ).where(*self._scope(window))
            )
        ).one()
        gen, emb = _split(row)
        return {
            "window": asdict(window),
            **_totals(gen, emb),
            "sessions": {"count": int(row[5] or 0)},
            "background_tasks": {"count": int(row[6] or 0)},
        }

    async def timeseries(self, window: UsageWindow) -> dict[str, Any]:
        buckets = build_usage_buckets(window)
        by_index = {
            i: (GenerationUsage(), EmbeddingUsage()) for i in range(len(buckets))
        }
        if buckets:
            gen_in, gen_out, gen_calls, emb_tok, emb_calls = _token_sums()
            bucket_expr = case(
                *[
                    (
                        and_(
                            _row.created_at >= bucket.start,
                            _row.created_at < bucket.end,
                        ),
                        i,
                    )
                    for i, bucket in enumerate(buckets)
                ]
            )
            stmt = (
                select(bucket_expr, gen_in, gen_out, gen_calls, emb_tok, emb_calls)
                .where(*self._scope(window), bucket_expr.is_not(None))
                .group_by(bucket_expr)
            )
            for row in (await self.db.execute(stmt)).all():
                by_index[int(row[0])] = _split(row, 1)
        generation, embedding = GenerationUsage(), EmbeddingUsage()
        points = []
        for i, bucket in enumerate(buckets):
            gen, emb = by_index[i]
            generation = generation.add(gen)
            embedding = embedding.add(emb)
            points.append(
                {
                    "bucket_start": bucket.start,
                    "bucket_end": bucket.end,
                    "label": bucket.label,
                    "is_partial": bucket.is_partial,
                    "generation": asdict(gen),
                    "embedding": asdict(emb),
                }
            )
        return {
            "window": asdict(window),
            "granularity": "hour4" if window.period == "1d" else "day",
            "points": points,
            "totals": _totals(generation, embedding),
        }

    async def sessions(
        self, window: UsageWindow, *, page: int, page_size: int
    ) -> dict[str, Any]:
        gen_in, gen_out, gen_calls, emb_tok, emb_calls = _token_sums()
        grouped = (
            select(
                _row.session_id.label("session_id"),
                func.max(_row.created_at).label("last_activity_at"),
                gen_in.label("gen_in"),
                gen_out.label("gen_out"),
                gen_calls.label("gen_calls"),
                emb_tok.label("emb_tok"),
                emb_calls.label("emb_calls"),
            )
            .where(*self._scope(window), _row.session_id != "")
            .group_by(_row.session_id)
        ).subquery()
        total = int(
            (
                await self.db.execute(select(func.count()).select_from(grouped))
            ).scalar_one()
            or 0
        )
        totals_row = (
            await self.db.execute(
                select(
                    func.coalesce(func.sum(grouped.c.gen_in), 0),
                    func.coalesce(func.sum(grouped.c.gen_out), 0),
                    func.coalesce(func.sum(grouped.c.gen_calls), 0),
                    func.coalesce(func.sum(grouped.c.emb_tok), 0),
                    func.coalesce(func.sum(grouped.c.emb_calls), 0),
                )
            )
        ).one()
        page_rows = (
            await self.db.execute(
                select(grouped)
                .order_by(grouped.c.last_activity_at.desc(), grouped.c.session_id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        session_ids = [row.session_id for row in page_rows]
        titles = await self._session_titles(session_ids)
        models = await self._models_by_key(window, _row.session_id, session_ids)
        items = []
        for row in page_rows:
            gen, emb = _split(
                (row.gen_in, row.gen_out, row.gen_calls, row.emb_tok, row.emb_calls)
            )
            title, state = titles[row.session_id]
            items.append(
                {
                    "session_id": row.session_id,
                    "title": title,
                    "session_state": state,
                    "last_activity_at": row.last_activity_at,
                    "models": models.get(row.session_id, []),
                    **_totals(gen, emb),
                }
            )
        gen, emb = _split(totals_row)
        return {
            "window": asdict(window),
            "items": items,
            "totals": _totals(gen, emb, sessions=total),
            "pagination": _page(page, page_size, total),
        }

    async def background_tasks(
        self, window: UsageWindow, *, page: int, page_size: int
    ) -> dict[str, Any]:
        gen_in, gen_out, gen_calls, emb_tok, emb_calls = _token_sums()
        rows = (
            await self.db.execute(
                select(
                    _row.source,
                    func.max(_row.created_at),
                    gen_in,
                    gen_out,
                    gen_calls,
                    emb_tok,
                    emb_calls,
                )
                .where(*self._scope(window), _row.session_id == "")
                .group_by(_row.source)
            )
        ).all()
        models = await self._models_by_key(
            window, _row.source, [row[0] for row in rows]
        )
        items = []
        generation, embedding = GenerationUsage(), EmbeddingUsage()
        for row in rows:
            gen, emb = _split(row, 2)
            generation = generation.add(gen)
            embedding = embedding.add(emb)
            items.append(
                {
                    "group_id": background_group_id(row[0]),
                    "source": row[0],
                    "last_activity_at": row[1],
                    "models": models.get(row[0], []),
                    **_totals(gen, emb),
                }
            )
        items.sort(
            key=lambda item: (
                -item["total_tokens"],
                -item["last_activity_at"].timestamp(),
                item["group_id"],
            )
        )
        start = (page - 1) * page_size
        return {
            "window": asdict(window),
            "items": items[start : start + page_size],
            "totals": _totals(generation, embedding, groups=len(items)),
            "pagination": _page(page, page_size, len(items)),
        }

    async def _session_titles(
        self, session_ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        titles = {session_id: (_UNNAMED, "deleted") for session_id in session_ids}
        if not session_ids:
            return titles
        stmt = select(SessionRow.id, SessionRow.title, SessionRow.deleted_at).where(
            SessionRow.id.in_(session_ids)
        )
        for row in (await self.db.execute(stmt)).all():
            titles[row.id] = (
                (row.title or "").strip() or _UNNAMED,
                "deleted" if row.deleted_at is not None else "active",
            )
        return titles

    async def _models_by_key(
        self, window: UsageWindow, key_col: Any, keys: list[str]
    ) -> dict[str, list[dict[str, str]]]:
        if not keys:
            return {}
        grouped: dict[str, list[tuple[str, str, str]]] = {key: [] for key in keys}
        stmt = (
            select(key_col, _row.provider_id, _row.model_id, _row.model_type)
            .where(*self._scope(window), key_col.in_(keys))
            .distinct()
        )
        for row in (await self.db.execute(stmt)).all():
            grouped.setdefault(row[0], []).append((row[1], row[2], row[3]))
        return {key: _unique_models(values) for key, values in grouped.items()}
