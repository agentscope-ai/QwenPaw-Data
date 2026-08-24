# -*- coding: utf-8 -*-
"""Artifact 模型 —— session 级文件产物索引项。

每次 ``update_subtask(state='done')`` 记录文件产出时，RuntimeStateManager 会把
``NodeOutput.files`` 中的每个 ``FileRef`` 投影成一条 ``ArtifactItem``，
追加到 session 级 ``artifacts`` 列表中，作为后续 files / 下载接口的数据源。
"""

from datetime import datetime

from pydantic import BaseModel, Field


def _get_timestamp() -> str:
    """Return the stable millisecond timestamp used in persisted artifacts."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class ArtifactItem(BaseModel):
    """Session 级文件产物索引项（append-only）。"""

    graph_id: str = Field(description="产出文件所属 graph id")
    node_id: str = Field(description="产出文件所属 node id")
    name: str = Field(description="文件名，如 ``dau_trend.png``")
    path: str = Field(description="沙箱视角相对路径，与 ``FileRef.path`` 一致")
    mime_type: str = Field(description="MIME 类型，如 ``image/png``")
    size_bytes: int = Field(
        default=0,
        description="文件大小（字节），由后端 stat 自动填充",
    )
    created_at: str = Field(
        default_factory=_get_timestamp,
        description="创建时间戳",
    )
