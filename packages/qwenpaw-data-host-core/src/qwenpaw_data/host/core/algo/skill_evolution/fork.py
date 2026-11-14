# -*- coding: utf-8 -*-
"""Slim AgentScope ReAct fork for skill evolution (stage-only tools)."""

from __future__ import annotations

import asyncio
import logging

from agentscope.agent import Agent, ReActConfig
from qwenpaw_data.host.core.agent.middleware.tracing import TracingMiddleware
from agentscope.message import AssistantMsg, TextBlock, UserMsg
from agentscope.model import ChatModelBase
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState

from qwenpaw_data.host.core.algo.skill_evolution.generator import render_review_prompt
from qwenpaw_data.host.core.algo.skill_evolution.models import (
    EvolutionProposal,
    ProposedPatch,
)
from qwenpaw_data.host.core.algo.skill_evolution.settings import TIMEOUT_SEC
from qwenpaw_data.host.core.algo.skill_evolution.skills_io import SkillCatalog
from qwenpaw_data.host.core.algo.skill_evolution.stage_buffer import StageBuffer
from qwenpaw_data.host.core.algo.skill_evolution.tools import build_evolution_toolkit
from qwenpaw_data.host.core.algo.skill_evolution.transcript import HistoryTurn

logger = logging.getLogger(__name__)

FORK_MAX_ITERS = 32
_FORK_SYSTEM_PROMPT = (
    "你是 QwenPawData 的 Skill 自进化审阅器。"
    "只能使用提供的 skill 审阅工具；禁止创建或删除 skill；"
    "所有修改只暂存供人工审查，不要声称已写入磁盘。"
)


async def run_evolution_fork(
    *,
    model: ChatModelBase,
    catalog: SkillCatalog,
    history: list[HistoryTurn],
    feedback_texts: list[str],
    skills_used: list[str],
    plan_edit_note: str,
    incremental: bool = False,
    prior_pending: EvolutionProposal | None = None,
    max_patches: int,
    max_files: int,
    timeout_sec: float = TIMEOUT_SEC,
) -> tuple[str, list[ProposedPatch]]:
    """Run a tool-using fork and return ``(summary, staged_patches)``."""
    buffer = StageBuffer(max_patches=max_patches, max_files=max_files)
    toolkit = build_evolution_toolkit(catalog=catalog, buffer=buffer)
    agent = Agent(
        name="skill_evolution",
        system_prompt=_FORK_SYSTEM_PROMPT,
        model=model,
        toolkit=toolkit,
        middlewares=[TracingMiddleware()],
        state=AgentState(
            permission_context=PermissionContext(mode=PermissionMode.BYPASS),
            context=_history_to_msgs(history),
        ),
        react_config=ReActConfig(max_iters=FORK_MAX_ITERS),
    )
    prompt = render_review_prompt(
        feedback_texts=feedback_texts,
        skills_used=skills_used,
        plan_edit_note=plan_edit_note,
        incremental=incremental,
        prior_pending=prior_pending,
    )
    try:
        await asyncio.wait_for(
            agent.reply(UserMsg(name="user", content=prompt)),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.warning("skill evolution fork timed out after %.1fs", timeout_sec)
    except Exception:
        logger.exception("skill evolution fork failed")
        raise

    summary = buffer.summary or ("无需修改" if not buffer.patches else "Skill 修订提案")
    return summary, list(buffer.patches)


def _history_to_msgs(history: list[HistoryTurn]) -> list:
    msgs: list = []
    for turn in history:
        text = turn.content
        if not text:
            continue
        if turn.role == "user":
            msgs.append(UserMsg(name="user", content=text))
        elif turn.role == "assistant":
            msgs.append(
                AssistantMsg(
                    name="assistant",
                    content=[TextBlock(type="text", text=text)],
                )
            )
        else:
            # Tool / other → assistant-visible context as a user digest line.
            msgs.append(UserMsg(name="user", content=text))
    return msgs


__all__ = ["FORK_MAX_ITERS", "run_evolution_fork"]
