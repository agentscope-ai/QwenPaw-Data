# -*- coding: utf-8 -*-
"""The action space of a recommendation: every skill a question can route to.

Built once per process from the shipped skills, so a candidate naming a skill
that does not exist can be dropped without asking the model twice.
"""

from __future__ import annotations

import logging

from qwenpaw_data.host.core.utils.skill import discover_builtin_skills

logger = logging.getLogger(__name__)

# Only these two groups are analyses a user can ask for; the rest are guides
# and routers the agent reads on its own.
ROUTABLE_GROUPS = frozenset({"atomic", "workflows"})
DESCRIPTION_CHAR_CAP = 40


def load_skill_index() -> dict[str, str]:
    """Map every routable skill name to a one-line capability description."""
    index: dict[str, str] = {}
    for skill in discover_builtin_skills():
        if skill.group not in ROUTABLE_GROUPS:
            continue
        description = _short_description(skill.description)
        if description:
            index[skill.name] = description
    if not index:
        logger.warning("No routable skills found; follow-up recommendation is blind")
    return index


def format_skill_index() -> str:
    """Render the index as the prompt's list of routable capabilities."""
    return "\n".join(f"- {name}: {text}" for name, text in SKILL_INDEX.items())


def _short_description(text: str) -> str:
    """Take the first sentence of a SKILL.md frontmatter description."""
    if not text.strip():
        return ""
    # Every skill spends a paragraph on when to invoke it, which the model does
    # not need to pick between them: one sentence keeps the prompt in budget.
    sentence = " ".join(text.split()).split("。")[0]
    return sentence[:DESCRIPTION_CHAR_CAP]


SKILL_INDEX: dict[str, str] = load_skill_index()


__all__ = [
    "DESCRIPTION_CHAR_CAP",
    "ROUTABLE_GROUPS",
    "SKILL_INDEX",
    "format_skill_index",
    "load_skill_index",
]
