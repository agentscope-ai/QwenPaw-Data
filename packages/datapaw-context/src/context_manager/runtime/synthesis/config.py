"""Load the `synthesis` config block from config/agent_explorer.json.

Back-compat: a legacy top-level `explore_synthesis` block is merged onto
`synthesis.explore_entity` so existing deployments keep working. An explicit
`synthesis.*` value always wins over the legacy block.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG: dict[str, Any] = {
    "path_hint": {
        "enabled": True, "model": None, "max_tokens": 500,
        "temperature": 0.2, "timeout_sec": 12.0,
    },
    "explore_entity": {
        "enabled": True,
        "fields": {
            "summary": True, "usage_guidance": True,
            "related_metrics_nl": True, "experience_hints": True,
        },
        "hop2": {
            "enabled": True, "max_targets": 4,
            "candidate_sources": ["related_metrics", "source_columns", "related_cards"],
        },
        "llm": {
            "model": None, "synthesis_max_tokens": 1200, "picker_max_tokens": 200,
            "temperature": 0.2, "picker_timeout_sec": 8.0, "synthesis_timeout_sec": 15.0,
        },
        "context": {"include_original_query": True, "max_neighbors_per_node": 3},
    },
}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _default_config_path() -> Path:
    """Locate ``config/agent_explorer.json`` by walking up from this module.

    This module lives at ``runtime/synthesis/config.py`` (depth varies), so a
    fixed ``parents[N]`` index is brittle. Walk up until we find the real file;
    fall back to the repo-root guess if none is found.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "config" / "agent_explorer.json"
        if cand.is_file():
            return cand
    return here.parents[4] / "config" / "agent_explorer.json"


def load_synthesis_config(path: Optional[Path] = None) -> dict[str, Any]:
    if path is None:
        path = _default_config_path()
    try:
        raw = json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULT_CONFIG)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    legacy = raw.get("explore_synthesis")
    if isinstance(legacy, dict):
        cfg["explore_entity"] = _deep_merge(cfg["explore_entity"], legacy)
    section = raw.get("synthesis") or {}
    return _deep_merge(cfg, section)
