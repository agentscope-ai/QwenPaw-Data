# -*- coding: utf-8 -*-
"""TaskEvent —— QwenPaw Data TaskGraph 的 SSE 事件模型。

设计原则：
- 在本包内维护稳定的 SSE wire model，不依赖已经并入 AgentScope 2.0 的旧
  ``agentscope-runtime`` 包
- 前端通过 ``object == "task_status"`` 识别并路由到 DAG 视图
- 每个事件始终携带完整 ``graph_snapshot``，前端全量替换、零合并逻辑

事件类型（``TaskEventType``）四类：
- ``graph_created``：新图创建或加载（``create_plan`` / ``load_graph``）
- ``graph_updated``：图内节点变更（``update_subtask`` /
  ``revise_current_plan``）
- ``graph_finished``：图正常结束（``finish_plan``）
- ``graph_archived``：活跃图被归档（被新图替换）
"""
from typing import Optional

from pydantic import BaseModel


class TaskEventError(BaseModel):
    """Stable wire representation for task-graph errors."""

    code: str
    message: str


class TaskEventType:
    """TaskGraph 状态变更事件类型常量。"""

    GRAPH_CREATED = "graph_created"
    GRAPH_UPDATED = "graph_updated"
    GRAPH_FINISHED = "graph_finished"
    GRAPH_ARCHIVED = "graph_archived"


class TaskEvent(BaseModel):
    """QwenPaw Data TaskGraph 状态变更事件。

    SSE 帧格式：``data: {task_event.model_dump_json()}\\n\\n``。

    与旧 Runtime ``Event`` 保持兼容的字段：
    - ``sequence_number``：SSE 流中的事件序号
    - ``status``：复用 ``RunStatus``，描述 TaskGraph 的宏观状态
      （``in_progress`` / ``completed`` / ``canceled``）
    - ``error``：复用 ``Error`` 模型，可用于传递图级错误
    """

    sequence_number: int | None = None
    object: str = "task_status"
    status: str | None = None
    error: TaskEventError | None = None

    event_type: str
    """变更类型，取值见 :class:`TaskEventType`。用于日志/调试。"""

    graph_snapshot: Optional[dict] = None
    """变更后的 TaskGraph 完整快照。前端用此字段全量替换 DAG 视图。"""
