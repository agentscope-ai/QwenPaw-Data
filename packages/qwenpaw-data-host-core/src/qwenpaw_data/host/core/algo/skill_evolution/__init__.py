# -*- coding: utf-8 -*-
"""Skill self-evolution algorithm: fork, stage patches, validate hunks."""

from qwenpaw_data.host.core.algo.skill_evolution.apply import select_accepted_patches
from qwenpaw_data.host.core.algo.skill_evolution.fork import run_evolution_fork
from qwenpaw_data.host.core.algo.skill_evolution.manager import SkillEvolutionManager
from qwenpaw_data.host.core.algo.skill_evolution.propose import propose_evolution

__all__ = [
    "SkillEvolutionManager",
    "propose_evolution",
    "run_evolution_fork",
    "select_accepted_patches",
]
