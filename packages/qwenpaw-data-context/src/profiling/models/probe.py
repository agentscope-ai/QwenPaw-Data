"""探查子系统的枚举、常量与响应 DTO（批次 1：状态机 + 总览/状态）。

阶段/状态定义对齐 SPEC §3.1、§附录 A。
"""

from __future__ import annotations

from pydantic import BaseModel

# ---- 7 个阶段（严格串行，顺序即前端导航自上而下）----
STAGE_BIZ_DOMAIN = "BIZ_DOMAIN"
STAGE_DATASET = "DATASET"
STAGE_DATASET_FIELDS = "DATASET_FIELDS"
STAGE_DIMENSION_CALIBER = "DIMENSION_CALIBER"
STAGE_DIMENSION_MANAGE = "DIMENSION_MANAGE"
STAGE_METRIC_CALIBER = "METRIC_CALIBER"
STAGE_METRIC_MANAGE = "METRIC_MANAGE"

STAGES: tuple[str, ...] = (
    STAGE_BIZ_DOMAIN,
    STAGE_DATASET,
    STAGE_DATASET_FIELDS,
    STAGE_DIMENSION_CALIBER,
    STAGE_DIMENSION_MANAGE,
    STAGE_METRIC_CALIBER,
    STAGE_METRIC_MANAGE,
)

# ---- 阶段状态（probe_stage.status）----
STATUS_NOT_STARTED = "NOT_STARTED"
STATUS_PROBING = "PROBING"
STATUS_REVIEWING = "REVIEWING"
STATUS_COMPLETED = "COMPLETED"
STATUS_SKIPPED = "SKIPPED"

# ---- 数据源总状态（推导，不落库）----
OVERALL_NOT_STARTED = "NOT_STARTED"
OVERALL_IN_PROGRESS = "IN_PROGRESS"
OVERALL_COMPLETED = "COMPLETED"

# ---- 探查任务状态（probe_task.status）----
TASK_RUNNING = "RUNNING"
TASK_SUCCESS = "SUCCESS"
TASK_STOPPED = "STOPPED"
TASK_FAILED = "FAILED"

# ---- staging 行级状态（probe_status）----
ROW_PENDING = "PENDING"
ROW_CONFIRMED = "CONFIRMED"
ROW_SKIPPED = "SKIPPED"

# ---- 行来源 ----
SOURCE_ALGO = "ALGO"
SOURCE_HUMAN = "HUMAN"

# 终态：阶段视为「已落地」的集合（用于总状态推导、前置校验、跳过判定）
_TERMINAL = frozenset({STATUS_COMPLETED, STATUS_SKIPPED})
STAGE_TERMINAL = _TERMINAL


def derive_overall(statuses: list[str]) -> str:
    """由各阶段状态推导数据源总状态（SPEC §3.1 / §4.2）。

    - COMPLETED：全部 ∈ {COMPLETED, SKIPPED}
    - NOT_STARTED：全部 = NOT_STARTED
    - IN_PROGRESS：其余中间态
    """
    if statuses and all(s in _TERMINAL for s in statuses):
        return OVERALL_COMPLETED
    if statuses and all(s == STATUS_NOT_STARTED for s in statuses):
        return OVERALL_NOT_STARTED
    return OVERALL_IN_PROGRESS


class StageStatusItem(BaseModel):
    """单阶段状态项（GET /probe/{ds}/status 的 stages 元素）。"""

    stage: str
    status: str
    current_task_id: str | None = None


class DatasourceStatusResponse(BaseModel):
    """GET /probe/{datasource_id}/status 响应。"""

    datasource_id: str
    overall_status: str
    stages: list[StageStatusItem]


class OverviewRecord(BaseModel):
    """GET /probe/overview 分页记录：总状态 + 各阶段状态（对齐 SPEC §7.1）。"""

    datasource_id: str
    datasource_name: str | None = None
    datasource_type: str | None = None
    overall_status: str
    biz_domain_status: str
    dataset_status: str
    dataset_fields_status: str
    dimension_caliber_status: str
    dimension_manage_status: str
    metric_caliber_status: str
    metric_manage_status: str


class ProbeTaskResponse(BaseModel):
    """探查任务响应（start/rerun/task 轮询）。"""

    task_id: str | None = None
    datasource_id: str
    stage: str
    status: str
    progress: int = 0
    message: str | None = None
    error_msg: str | None = None
    cancel_requested: bool = False


class RowIdsRequest(BaseModel):
    """批量确定 / 跳过 / 删除入参。"""

    row_ids: list[int] = []


def task_row_to_response(row, datasource_id: str, stage: str) -> ProbeTaskResponse:
    """probe_task 行 → ProbeTaskResponse；row 为空时返回一个 NOT_STARTED 占位（无任务）。"""
    if row is None:
        return ProbeTaskResponse(
            task_id=None,
            datasource_id=datasource_id,
            stage=stage,
            status=STATUS_NOT_STARTED,
        )
    return ProbeTaskResponse(
        task_id=row["task_id"],
        datasource_id=row["datasource_id"],
        stage=row["stage"],
        status=row["status"],
        progress=row["progress"] or 0,
        message=row["message"],
        error_msg=row["error_msg"],
    )
