# -*- coding: utf-8 -*-
"""Load skill-create reference docs shipped with the writer."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


def _skill_creator_dir() -> Path:
    return Path(__file__).resolve().parent


@lru_cache(maxsize=16)
def reference(name: str) -> str:
    stem = name if name.endswith(".md") else f"{name}.md"
    path = _skill_creator_dir() / "references" / stem
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def evidence_guide_for_prompt() -> str:
    """Whole evidence-to-skill.md is the induct skill-card spec."""
    return reference("evidence-to-skill")


def checklist_for_prompt() -> str:
    """Whole authoring-checklist.md is the model-facing self-check."""
    return reference("authoring-checklist")
