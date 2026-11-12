# -*- coding: utf-8 -*-
"""Host-free one-shot: signal → fork → materialized session proposal."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from qwenpaw_data.host.core.algo.skill_evolution.fork import run_evolution_fork
from qwenpaw_data.host.core.algo.skill_evolution.models import EvolutionProposal, SignalState
from qwenpaw_data.host.core.algo.skill_evolution.settings import (
    MAX_FILES,
    MAX_PATCHES,
    TIMEOUT_SEC,
)
from qwenpaw_data.host.core.algo.skill_evolution.skills_io import (
    SkillCatalog,
    snapshot_skills,
)
from qwenpaw_data.host.core.algo.skill_evolution.skills_used import extract_skills_used
from qwenpaw_data.host.core.algo.skill_evolution.transcript import messages_to_history
from qwenpaw_data.host.core.algo.skill_evolution.trigger import evaluate_trigger
from qwenpaw_data.host.core.algo.skill_evolution.validate import validate_and_materialize

if TYPE_CHECKING:
    from agentscope.model import ChatModelBase

logger = logging.getLogger(__name__)


async def propose_evolution(
    *,
    model: ChatModelBase,
    catalog: SkillCatalog,
    messages: list[dict[str, Any]],
    signal: SignalState,
    feedback_texts: list[str],
    chat_user_inputs: list[str] | None = None,
    plan_edit_note: str = "",
    incremental: bool = False,
    prior_pending: EvolutionProposal | None = None,
    max_patches: int = MAX_PATCHES,
    max_files: int = MAX_FILES,
    timeout_sec: float = TIMEOUT_SEC,
) -> EvolutionProposal | None:
    """Run evolution when ``signal`` fires; return one reviewable proposal or None."""
    if not evaluate_trigger(signal):
        return None

    existing = set(catalog.names)
    if not existing:
        return None

    skills_used = [name for name in extract_skills_used(messages) if name in existing]
    history = messages_to_history(messages, chat_user_inputs=chat_user_inputs)
    snaps = snapshot_skills(catalog, preferred=skills_used, max_files=max_files)
    snap_map = {(item.skill_name, item.relative_path): item for item in snaps}

    try:
        summary, staged = await run_evolution_fork(
            model=model,
            catalog=catalog,
            history=history,
            feedback_texts=feedback_texts,
            skills_used=skills_used,
            plan_edit_note=plan_edit_note,
            incremental=incremental,
            prior_pending=prior_pending,
            max_patches=max_patches,
            max_files=max_files,
            timeout_sec=timeout_sec,
        )
    except Exception:
        logger.exception("skill evolution fork failed")
        return None

    if not staged:
        return None

    for item in staged:
        key = (item.skill_name, item.relative_path)
        if key in snap_map:
            continue
        loaded = catalog.get(item.skill_name, item.relative_path)
        if loaded is not None:
            snap_map[key] = loaded

    try:
        patches = validate_and_materialize(
            staged,
            snapshots=snap_map,
            existing_skills=existing,
            max_patches=max_patches,
            max_files=max_files,
        )
    except ValueError:
        logger.warning(
            "skill evolution validate rejected staged patches", exc_info=True
        )
        return None

    if not patches:
        return None

    skills = list(dict.fromkeys(str(item["skill_name"]) for item in patches))
    return EvolutionProposal(
        summary=summary,
        skills=skills,
        patch_count=len(patches),
        patches=patches,
    )


__all__ = ["propose_evolution"]
