# -*- coding: utf-8 -*-
"""Materialize execute_sql CSV into the session artifact directory."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from qwenpaw_data.host.core.cm_sql_artifact import (
    is_execute_sql_tool,
    materialize_execute_sql_result,
)

if TYPE_CHECKING:
    from agentscope.agent import Agent


class SqlArtifactMiddleware(MiddlewareBase):
    """Copy CM execute_sql CSV into artifact_dir before the result hits context."""

    def __init__(self, *, artifact_dir: Path) -> None:
        self._artifact_dir = artifact_dir

    async def on_acting(  # type: ignore[override]
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator[Any, None]:
        name = getattr(input_kwargs["tool_call"], "name", "")
        prefixes = agent._cm_mcp_tool_prefixes
        token = getattr(agent, "_request_context", {}).get("access_token")
        async for chunk in next_handler(**input_kwargs):
            yield await self._rewrite(name, chunk, prefixes, token)

    async def _rewrite(
        self,
        name: str,
        chunk: Any,
        prefixes: set[str],
        token: str | None,
    ) -> Any:
        if not isinstance(chunk, ToolResponse) or not is_execute_sql_tool(
            name, prefixes
        ):
            return chunk
        if not chunk.content:
            return chunk
        first = chunk.content[0]
        text = getattr(first, "text", None)
        if not isinstance(text, str):
            return chunk
        rewritten = await materialize_execute_sql_result(
            text,
            artifact_dir=self._artifact_dir,
            access_token=token,
        )
        if rewritten != text:
            first.text = rewritten
        return chunk
