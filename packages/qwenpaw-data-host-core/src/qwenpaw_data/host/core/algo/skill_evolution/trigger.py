# -*- coding: utf-8 -*-
from __future__ import annotations

from qwenpaw_data.host.core.algo.skill_evolution.models import SignalState
from qwenpaw_data.host.core.algo.skill_evolution.settings import USER_FEEDBACK_THRESHOLD


def evaluate_trigger(
    state: SignalState,
    *,
    user_feedback_threshold: int = USER_FEEDBACK_THRESHOLD,
) -> bool:
    """Return whether evolution should run.

    ``user_feedback_count`` covers both steer hints and normal user messages.
    """
    feedback_hit = state.user_feedback_count >= max(1, int(user_feedback_threshold))
    return feedback_hit or bool(state.plan_edit_pending)


__all__ = ["evaluate_trigger"]
