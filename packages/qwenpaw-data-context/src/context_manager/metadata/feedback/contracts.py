"""Host settlement card contracts mapped to the canonical Metadata schema."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CardType(str, Enum):
    metric_caliber = "metric_caliber"
    dimension_def = "dimension_def"
    column_meaning = "column_meaning"
    dataset_usage = "dataset_usage"


class CardBase(BaseModel):
    """四类卡片公共字段。workspace / owner 来自 Principal，不在 body 传。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    client_card_id: str = Field(
        min_length=1, max_length=128, description="客户端唯一 id，用于对账"
    )
    domain: str = Field(min_length=1, max_length=256, description="业务域名称")
    datasource_id: str = Field(
        min_length=1,
        max_length=128,
        description="数据源标识（必填，同名实体按数据源隔离）",
    )
    extra: dict[str, Any] = Field(
        default_factory=dict, description="扩展字段，服务端原样留存"
    )


class MetricCaliberCard(CardBase):
    """指标口径。落图：Metric + MetricCaliber + Dataset（口径链）。"""

    type: Literal[CardType.metric_caliber] = CardType.metric_caliber
    name: str = Field(
        min_length=1, max_length=256, description="指标名称 → Metric.name"
    )
    description: str = Field(
        min_length=1,
        max_length=4000,
        description="计算口径文字 → Metric.description",
    )
    dataset: str = Field(
        min_length=1,
        max_length=512,
        description="所属表（裸表名）→ Dataset（口径链 CALIBER_ON 端点）",
    )
    formula: str = Field(
        default="", max_length=16000, description="公式/SQL → MetricCaliber.expr"
    )
    unit: str = Field(default="", max_length=64, description="单位 → Metric.unit")


class DimensionDefCard(CardBase):
    """维度定义。落图：Dimension + MAPS_TO_FIELD + DatasetField。"""

    type: Literal[CardType.dimension_def] = CardType.dimension_def
    name: str = Field(
        min_length=1, max_length=256, description="维度名称 → Dimension.name"
    )
    dataset_name: str = Field(
        min_length=1,
        max_length=512,
        description="所属表（裸表名）→ Dataset.name",
    )
    maps_to_column: str = Field(
        min_length=1,
        max_length=256,
        description="绑定列 → MAPS_TO_FIELD（端点 DatasetField）",
    )
    values: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="维度值样例 → MAPS_TO_FIELD / DIMENSION_BINDING.value_example",
    )


class ColumnMeaningCard(CardBase):
    """列含义。落图：DatasetField.description（自由文本，不拆枚举值）。"""

    type: Literal[CardType.column_meaning] = CardType.column_meaning
    dataset_name: str = Field(
        min_length=1, max_length=512, description="所属表（裸表名）"
    )
    column: str = Field(min_length=1, max_length=256, description="列名")
    description: str = Field(
        min_length=1,
        max_length=4000,
        description="列含义文字 → DatasetField.description",
    )


class DatasetUsageCard(CardBase):
    """数据集用法。落图：使用场景追加到 Dataset.description。"""

    type: Literal[CardType.dataset_usage] = CardType.dataset_usage
    description: str = Field(
        min_length=1, max_length=4000, description="使用场景描述 → Dataset.description"
    )
    datasets: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        min_length=1, max_length=20, description="推荐数据集（裸表名）→ Dataset.name"
    )


FeedbackCard = Annotated[
    MetricCaliberCard | DimensionDefCard | ColumnMeaningCard | DatasetUsageCard,
    Field(discriminator="type"),
]


class FieldError(BaseModel):
    field: str
    message: str


class CardWarning(BaseModel):
    """回执警告：写入成功，但存在客户端应当知晓的情况。"""

    code: str
    message: str


class ScreenVerdict(BaseModel):
    """语义筛查结果（test / confirm 均按当前个人图计算）。

    verdict 取值：
    - ``CLEAR``：本人子图内无语义相似对象，可放心写入；
    - ``SIMILAR``：有相似对象但低于 LLM 裁决阈值，列出供参考；
    - ``CONFLICT``：LLM 判定同一对象但内容矛盾（反馈已确认，字段级覆盖）；
    - ``ADDITIVE``：LLM 判定互补，字段级合并（description 类追加去重）；
    - ``SKIPPED``：检索 / embedding / LLM 不可用，筛查未执行。

    ``matched_key`` / ``matched_label``：命中节点（可能是卡片自己的主节点——
    同 key 内容变化时 LLM 对照现有内容裁决；也可能是不同 key 的同一实体，
    confirm 时写入目标重定向到该节点）。
    """

    verdict: Literal["CLEAR", "SIMILAR", "CONFLICT", "ADDITIVE", "SKIPPED"]
    matched_key: str = ""
    matched_label: str = ""
    score: float = 0.0
    reason: str = ""
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class CardAck(BaseModel):
    """keys 返回实际目标；warnings 明确筛查或向量生成的降级。"""

    client_card_id: str
    type: CardType
    status: Literal["accepted", "updated", "duplicate", "rejected"]
    mode: Literal["confirm", "test"] = "test"
    keys: list[str] = Field(default_factory=list)
    errors: list[FieldError] = Field(default_factory=list)
    warnings: list[CardWarning] = Field(default_factory=list)
    screen: ScreenVerdict | None = None
