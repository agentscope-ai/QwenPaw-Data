# -*- coding: utf-8 -*-
"""LLM orchestration behind the compare picker's recommended pairs.

Given the same-family candidates the picker would list and the document the
user is previewing, ask a light model for 2-3 reference/target pairs, then
validate them against the candidate list. The target is locked to the previewed
document, so the model really only chooses the reference. Any model failure
degrades to an empty list — recommendations are advisory, never fatal.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from qwenpaw_data.host.core.algo.compare.family import compare_family
from qwenpaw_data.host.core.algo.compare.prompts import RECOMMEND_SYSTEM_PROMPT
from qwenpaw_data.host.core.api.models.artifact_compare import (
    CompareRecommendationsRequest,
    RecommendationList,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from qwenpaw_data.host.core.domain.identity import Identity

logger = logging.getLogger("qwenpaw_data.compare")


def _build_user_prompt(body: CompareRecommendationsRequest) -> str:
    lines = ["候选文件清单："]
    for candidate in body.candidates:
        family = compare_family(candidate.path) or "unknown"
        lines.append(f"- path={candidate.path} | name={candidate.name} | type={family}")
    if body.current_path:
        lines.append(f"\n用户当前正在预览的文档：{body.current_path}")
    lines.append("\n请从上面的候选清单中选出 2-3 对适合比较的文档。")
    return "\n".join(lines)


async def recommend_compare_pairs(
    *,
    db: "AsyncSession",
    identity: "Identity",
    session_id: str,
    body: CompareRecommendationsRequest,
    timeout: float = 20.0,
    attempts: int = 2,
) -> list[dict[str, Any]]:
    """Return 0-3 validated recommendations as plain dicts (empty on any failure).

    ``timeout``/``attempts`` are passed in by the caller (from Host settings);
    the defaults here are only the fallback so this module never reads the
    environment on its own.
    """
    valid = {candidate.path for candidate in body.candidates}
    if not valid:
        return []

    # Imported lazily so this module stays light: the LLM stack pulls in the
    # agent runtime, which only an actual recommendation call needs.
    from agentscope.message import SystemMsg, UserMsg

    from qwenpaw_data.host.core.algo.settlement.llm import (
        for_structured_calls,
        structured_call,
    )
    from qwenpaw_data.host.core.domain.model_usage import (
        ModelUsageContext,
        ModelUsageSource,
    )
    from qwenpaw_data.host.core.providers.factory import build_model
    from qwenpaw_data.host.core.repositories import PreferencesRepository

    try:
        prefs = await PreferencesRepository(db, identity).load()
        runtime = prefs.resolve()
        # Prefer the light model for this advisory call; fall back to the
        # default when no light model is configured so recommendations still work.
        # ``build_model`` requires a usage context (added on develop); this call is
        # advisory and not tied to a chat, so an empty chat_id is used.
        model = for_structured_calls(
            build_model(
                runtime.light or runtime.default,
                usage=ModelUsageContext(
                    ModelUsageSource.COMPARE,
                    identity,
                    session_id,
                    "",
                    source_step="compare_recommendations",
                ),
            )
        )
    except Exception:
        logger.info("compare recommendations: no usable model", exc_info=True)
        return []

    messages = [
        SystemMsg(name="system", content=RECOMMEND_SYSTEM_PROMPT),
        UserMsg(name="user", content=_build_user_prompt(body)),
    ]
    try:
        result = await structured_call(
            model,
            messages,
            RecommendationList,
            attempts=attempts,
            timeout=timeout,
        )
    except Exception:
        logger.info("compare recommendations: model call failed", exc_info=True)
        return []

    recommendations: list[dict[str, Any]] = []
    seen: set[str] = set()
    # The compared document (target) is locked to whatever the user is previewing,
    # so the model really only picks the reference. Force the target back to the
    # previewed path and validate just the reference against the candidate list —
    # otherwise a model that echoes the current document as target_path (natural,
    # since it is not among the reference candidates) would have every suggestion
    # dropped and the picker would show nothing.
    target = body.current_path
    for item in result.recommendations[:3]:
        reference = item.reference_path
        if reference not in valid:
            continue
        resolved_target = target or item.target_path
        if reference == resolved_target:
            continue
        family = compare_family(reference)
        if family is None or family != compare_family(resolved_target):
            continue
        if reference in seen:
            continue
        seen.add(reference)
        payload = item.model_dump(mode="json")
        payload["target_path"] = resolved_target
        recommendations.append(payload)
    return recommendations


__all__ = ["recommend_compare_pairs"]
