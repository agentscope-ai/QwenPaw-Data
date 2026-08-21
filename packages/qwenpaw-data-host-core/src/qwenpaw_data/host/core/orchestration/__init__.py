# -*- coding: utf-8 -*-
"""QwenPaw Data 编排层 —— TaskNode-based DAG 数据模型与运行时状态管理器。

暴露：
- ``TaskNode`` / ``NodeOutput``：独立 DAG 节点模型
- ``FileRef``：DAG 文件引用
- ``ArtifactItem``：session 级文件产物索引项
- ``TaskEvent`` / ``TaskEventType``：SSE 事件模型
- ``DefaultGraphToHint``：提示生成器
- ``RuntimeStateManager``：运行时状态管理器
"""

from .artifact import ArtifactItem
from .dag_store import DAGBroadcaster, DAGStore
from .events import TaskEvent, TaskEventType
from .hint import DefaultGraphToHint
from .state import RuntimeStateManager
from .task_graph import FileRef, NodeOutput, PlanNodeChange, TaskNode

__all__ = [
    "ArtifactItem",
    "DAGBroadcaster",
    "DAGStore",
    "DefaultGraphToHint",
    "FileRef",
    "NodeOutput",
    "PlanNodeChange",
    "RuntimeStateManager",
    "TaskEvent",
    "TaskEventType",
    "TaskNode",
]
