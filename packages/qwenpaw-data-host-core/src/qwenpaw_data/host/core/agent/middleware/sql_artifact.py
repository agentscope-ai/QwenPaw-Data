# -*- coding: utf-8 -*-
"""Materialize execute_sql CSV into the session artifact directory."""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from qwenpaw_data.host.core.integrations.cm import (
    SqlArtifactError,
    is_execute_sql_tool,
    materialize_execute_sql_result,
)

if TYPE_CHECKING:
    from agentscope.agent import Agent
    from qwenpaw_data.host.core.runtime.context import RunContext

logger = logging.getLogger(__name__)

_MATERIALIZATION_ERROR = json.dumps(
    {
        "error": {
            "code": "SQL_ARTIFACT_MATERIALIZATION_FAILED",
            "message": (
                "SQL executed successfully, but the result CSV could not be "
                "saved into the session workspace. Do not retry the same query; "
                "report the failure or continue without the CSV."
            ),
        }
    },
    ensure_ascii=False,
)


class SqlArtifactMiddleware(MiddlewareBase):
    """Copy CM execute_sql CSV into artifact_dir before the result hits context."""

    def __init__(self, *, run_context: RunContext) -> None:
        self._run_context = run_context

    async def on_acting(  # type: ignore[override]
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator[Any, None]:
        name = getattr(input_kwargs["tool_call"], "name", "")
        token = getattr(agent, "_request_context", {}).get("access_token")
        async for chunk in next_handler(**input_kwargs):
            yield await self._rewrite(name, chunk, token)

    async def _rewrite(
        self,
        name: str,
        chunk: Any,
        token: str | None,
    ) -> Any:
        if not isinstance(chunk, ToolResponse) or not is_execute_sql_tool(name):
            return chunk
        if not chunk.content:
            return chunk
        first = chunk.content[0]
        text = getattr(first, "text", None)
        if not isinstance(text, str):
            return chunk
        try:
            rewritten = await materialize_execute_sql_result(
                text,
                artifact_dir=self._run_context.paths.artifact_dir,
                access_token=token,
            )
        except SqlArtifactError as exc:
            logger.error(
                "execute_sql artifact failed: error_type=%s",
                type(exc.__cause__ or exc).__name__,
            )
            chunk.content = [TextBlock(type="text", text=_MATERIALIZATION_ERROR)]
            chunk.state = ToolResultState.ERROR
            return chunk
        if rewritten != text:
            payload = json.loads(rewritten)
            paths = self._run_context.paths
            relative = (
                Path(payload["file_path"]).relative_to(paths.artifact_dir).as_posix()
            )
            payload["file_path"] = str(
                PurePosixPath(paths.agent_artifact_dir(self._run_context.workspace))
                / relative
            )
            first.text = json.dumps(payload, ensure_ascii=False)
        return chunk
