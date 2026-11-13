# -*- coding: utf-8 -*-
"""Score rows -> Score, and matching objects by their Scores."""

from __future__ import annotations

from qwenpaw_data.tracing.query.backend.contract import ScoreRow
from qwenpaw_data.tracing.query.models import LabelFilter, Score


def check_label_filters(
    label_filters: list[LabelFilter] | None, label_match: str
) -> list[LabelFilter]:
    if label_match not in {"all", "any"}:
        raise ValueError("label_match must be all or any")
    for item in label_filters or ():
        if not item.name or not item.values:
            raise ValueError("label filter name and values are required")
    return list(label_filters or ())


def to_score(row: ScoreRow) -> Score:
    return Score(
        id=row["id"],
        name=row["name"],
        value=row["value"],
        data_type=row["data_type"],
        comment=row["comment"],
        metadata=row["metadata"],
    )


def group_scores(rows: list[ScoreRow]) -> dict[str, list[Score]]:
    grouped: dict[str, list[Score]] = {}
    for row in rows:
        grouped.setdefault(row["trace_id"], []).append(to_score(row))
    return grouped


def _value_hit(value: str | float, wanted: list[str]) -> bool:
    if isinstance(value, float):
        for item in wanted:
            try:
                if float(item) == value:
                    return True
            except ValueError:
                continue
        return False
    return value in wanted


def match_labels(
    scores: list[Score], label_filters: list[LabelFilter], label_match: str
) -> bool:
    if not label_filters:
        return True
    hits = [
        any(
            score.name == item.name and _value_hit(score.value, item.values)
            for score in scores
        )
        for item in label_filters
    ]
    return all(hits) if label_match == "all" else any(hits)
