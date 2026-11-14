# -*- coding: utf-8 -*-
"""Review tools: artifact inventory and content of the reviewed session."""

from __future__ import annotations

from itertools import islice
from pathlib import Path
from typing import Any

from agentscope.tool import ToolChunk
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw_data.host.core.utils.workspace import list_session_files

from .base import ReviewToolBase, page_meta

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Line-window semantics mirrors the agentscope Read tool so the model can
# reuse the paging habits it already knows.
_DEFAULT_LINES = 2000
_MAX_LINES = 2000
_MAX_LINE_CHARS = 2000


class _GetArtifactsParams(BaseModel):
    # Reject non-expected tool input parameters
    model_config = ConfigDict(extra="forbid")

    offset: int = Field(
        default=0,
        ge=0,
        description="文件列表起始位置（0 起）；默认自首个文件。",
    )
    limit: int = Field(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description=f"本窗口条数，默认前 {DEFAULT_LIMIT} 条，最大 {MAX_LIMIT}。",
    )


class _ReadArtifactParams(BaseModel):
    # Reject non-expected tool input parameters
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        description="产物文件相对路径，取自 get_artifacts 返回的文件列表。",
    )
    offset: int = Field(
        default=1,
        ge=1,
        description="起始行号（1 起）；默认自文件首行。",
    )
    limit: int = Field(
        default=_DEFAULT_LINES,
        ge=1,
        le=_MAX_LINES,
        description=f"读取行数，默认 {_DEFAULT_LINES} 行，最大 {_MAX_LINES} 行。",
    )


class GetArtifactsTool(ReviewToolBase):
    name = "get_artifacts"
    description = (
        "读取被审核会话的产物文件列表：全部产物文件，"
        "每项含 path、mime_type、size_bytes、modified_at。"
        "产物数量多时以 offset/limit 分页；has_more 为 true 表示产物未列全，"
        "以 next_offset 继续翻页。"
    )

    def __init__(self, run_context: Any) -> None:
        super().__init__(run_context)
        self.input_schema = _GetArtifactsParams.model_json_schema()

    async def call(self, **kwargs: Any) -> ToolChunk:
        try:
            params = _GetArtifactsParams.model_validate(kwargs)
            async with self._session_factory()() as db:
                result = await self._run(params, db)
            return self._ok(result)
        except Exception as exc:
            return self._err(exc)

    async def _run(self, params: _GetArtifactsParams, db: Any) -> dict[str, Any]:
        reviewed = await self._reviewed_session(db)
        source = self._source_paths(reviewed)
        # Registration samples the artifact directory only when a tool
        # call succeeds and never forgets, so late writes can be missed
        # and deleted files linger; the filesystem is the source of truth:
        # a file is listable exactly when it is on disk.
        entries = [
            {
                "path": item["rel_path"],
                "size_bytes": item["size_bytes"],
                "mime_type": item["mime_type"],
                "modified_at": item["modified_at"],
            }
            for item in list_session_files(source.artifact_dir)
        ]
        window = entries[params.offset : params.offset + params.limit]
        return {
            "reviewed_session_id": reviewed.id,
            "total": len(entries),
            "offset": params.offset,
            "limit": params.limit,
            **page_meta(len(entries), params.offset, len(window)),
            "artifacts": window,
        }


class ReadArtifactTool(ReviewToolBase):
    name = "read_artifact"
    description = (
        "读取被审核会话某个产物文件的内容。path 必须取自 get_artifacts 返回的文件列表。"
        "按行分页：offset 为 1 起始行号，limit 为行数（默认 2000，最大 2000）；"
        "超长行截断并标注，has_more 为 true 表示文件未读完，以 next_offset 继续翻页。"
        "二进制文件不支持读取。"
    )

    def __init__(self, run_context: Any) -> None:
        super().__init__(run_context)
        self.input_schema = _ReadArtifactParams.model_json_schema()

    async def call(self, **kwargs: Any) -> ToolChunk:
        try:
            params = _ReadArtifactParams.model_validate(kwargs)
            async with self._session_factory()() as db:
                result = await self._run(params, db)
            return self._ok(result)
        except Exception as exc:
            return self._err(exc)

    async def _run(self, params: _ReadArtifactParams, db: Any) -> dict[str, Any]:
        reviewed = await self._reviewed_session(db)
        source = self._source_paths(reviewed)
        target = self._resolve_artifact(source.artifact_dir, params.path)
        size_bytes = target.stat().st_size
        lines, has_more = self._read_window(target, params.offset, params.limit)
        content = "\n".join(lines)
        return {
            "path": params.path,
            "size_bytes": size_bytes,
            "offset": params.offset,
            "limit": params.limit,
            "has_more": has_more,
            "next_offset": params.offset + len(lines) if has_more else None,
            "content": content,
        }

    def _read_window(
        self, target: Path, offset: int, limit: int
    ) -> tuple[list[str], bool]:
        """Return 1-based lines ``[offset, offset + limit)`` plus has_more.

        Iteration stops once the window is full, so a huge artifact is
        never read past the requested window; one extra line is taken
        to detect ``has_more``.
        """
        try:
            with target.open("r", encoding="utf-8", newline="\n") as fh:
                window = list(islice(fh, offset - 1, offset + limit))
        except UnicodeDecodeError as exc:
            raise ValueError(
                "artifact is binary or not utf-8 text; reading is not supported"
            ) from exc
        has_more = len(window) > limit
        return [self._clip(line.rstrip("\r\n")) for line in window[:limit]], has_more

    @staticmethod
    def _clip(line: str) -> str:
        if len(line) > _MAX_LINE_CHARS:
            return line[:_MAX_LINE_CHARS] + "[truncated]"
        return line
