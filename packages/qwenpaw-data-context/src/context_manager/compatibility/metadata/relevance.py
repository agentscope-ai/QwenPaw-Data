"""Own the retained Metadata relevance parameters from the deployment JSON."""

from dataclasses import dataclass
import json
import os
from pathlib import Path

from qwenpaw_data.context.env import load_qwenpaw_data_env


@dataclass(frozen=True)
class RelevanceConfig:
    text_weight: float = 0.4
    vec_weight: float = 0.6
    threshold: float = 0.4
    floor: float = 0.2

    @classmethod
    def load(cls) -> "RelevanceConfig":
        """Read only the four scoring options still consumed by Metadata."""
        load_qwenpaw_data_env(override=False)
        override = (os.getenv("AGENT_EXPLORER_CONFIG") or "").strip()
        path = (
            Path(override)
            if override
            else Path(__file__).resolve().parents[4] / "config" / "agent_explorer.json"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(value, dict):
            return cls()
        options = value.get("recall") or {}
        return cls(
            text_weight=max(0.0, float(options.get("relevance_text_weight", 0.4))),
            vec_weight=max(0.0, float(options.get("relevance_vec_weight", 0.6))),
            threshold=max(
                0.0, min(1.0, float(options.get("relevance_threshold", 0.4)))
            ),
            floor=max(0.0, min(1.0, float(options.get("relevance_floor", 0.2)))),
        )


RELEVANCE = RelevanceConfig.load()
