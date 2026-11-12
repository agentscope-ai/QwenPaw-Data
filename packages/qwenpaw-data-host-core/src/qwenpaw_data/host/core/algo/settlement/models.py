# -*- coding: utf-8 -*-
"""Settlement 数据模型。"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)


class CardType(str, Enum):
    """可沉淀项的 4 种类型。"""

    metric_caliber = "metric_caliber"
    dimension_def = "dimension_def"
    column_meaning = "column_meaning"
    dataset_usage = "dataset_usage"


class DismissedFilterResult(BaseModel):
    """LLM dismissed 去重结果。"""

    dismissed_indices: list[int] = Field(default_factory=list)


class _NonEmpty(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class MetricCaliberFields(_NonEmpty):
    metric_name: str = Field(min_length=1)
    caliber: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    table: str = Field(min_length=1)
    formula_sql: str = Field(min_length=1)


class DimensionDefFields(_NonEmpty):
    dimension_name: str = Field(min_length=1)
    bind_column: str = Field(min_length=1)
    value_samples: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    table: str = Field(min_length=1)


class ColumnMeaningFields(_NonEmpty):
    column_name: str = Field(min_length=1)
    meaning: str = Field(min_length=1)
    table: str = Field(min_length=1)
    domain: str = Field(min_length=1)


class DatasetUsageFields(_NonEmpty):
    use_case: str = Field(min_length=1)
    recommended_dataset: str = Field(min_length=1)
    domain: str = Field(min_length=1)


_FIELDS_BY_TYPE: dict[CardType, type[BaseModel]] = {
    CardType.metric_caliber: MetricCaliberFields,
    CardType.dimension_def: DimensionDefFields,
    CardType.column_meaning: ColumnMeaningFields,
    CardType.dataset_usage: DatasetUsageFields,
}


class DetectedItem(BaseModel):
    """LLM 识别出的单个可沉淀项；fields 按 type 做必填校验。"""

    type: CardType
    fields: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_typed_fields(self) -> Self:
        schema = _FIELDS_BY_TYPE[self.type]
        validated = schema.model_validate(self.fields or {})
        dumped = validated.model_dump()
        self.fields = {str(k): str(v) for k, v in dumped.items()}
        return self


class DetectionResult(BaseModel):
    """LLM 检测调用的完整输出。字段不全的 item 在校验时丢弃，不导致整次失败。"""

    items: list[DetectedItem] = Field(default_factory=list)

    @field_validator("items", mode="before")
    @classmethod
    def _drop_invalid_items(cls, value: Any) -> list[Any]:
        if not isinstance(value, list):
            return []
        kept: list[Any] = []
        for raw in value:
            try:
                kept.append(DetectedItem.model_validate(raw))
            except ValidationError as exc:
                logger.info("Settlement: drop incomplete detected item: %s", exc)
                continue
        return kept
