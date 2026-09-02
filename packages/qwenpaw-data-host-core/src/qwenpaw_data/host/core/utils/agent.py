# -*- coding: utf-8 -*-
from __future__ import annotations

from agentscope.agent import Agent
from agentscope.message import ToolCallState, ToolResultBlock, ToolResultState


def close_interrupted_tool_calls(agent: Agent) -> None:
    """Close open tool calls so persisted model context stays valid after cancel."""
    if not agent.state.context:
        return
    last_message = agent.state.context[-1]
    if last_message.role != "assistant" or last_message.name != agent.name:
        return

    result_ids = {
        block.id for block in last_message.get_content_blocks("tool_result")
    }
    interrupted_results: list[ToolResultBlock] = []
    for tool_call in last_message.get_content_blocks("tool_call"):
        if tool_call.id in result_ids:
            continue
        tool_call.state = ToolCallState.FINISHED
        interrupted_results.append(
            ToolResultBlock(
                id=tool_call.id,
                name=tool_call.name,
                output="Interrupted by a runtime control request.",
                state=ToolResultState.ERROR,
            ),
        )

    if interrupted_results:
        agent.state.append_context(agent.name, interrupted_results)
