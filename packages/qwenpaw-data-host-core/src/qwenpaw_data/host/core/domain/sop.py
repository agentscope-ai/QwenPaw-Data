"""Portable SOP structure, independent of live task state and legacy DAG code."""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SOPNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str | None = None
    name: str
    description: str
    expected_outcome: str
    deps: list[str] = Field(default_factory=list)


class SOP(BaseModel):
    """OSS SOP wire contract; never accepts execution state or completed outputs."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    expected_outcome: str
    nodes: list[SOPNode] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dependencies(self) -> SOP:
        ids: set[str] = set()
        for index, node in enumerate(self.nodes):
            node.node_id = node.node_id or f"node_{index:03d}"
            if node.node_id in ids:
                raise ValueError(f"Duplicate node_id in SOP: {node.node_id!r}")
            ids.add(node.node_id)
        for node in self.nodes:
            unknown = set(node.deps) - ids
            if unknown:
                raise ValueError(
                    f"Node {node.node_id!r} has unknown deps: {sorted(unknown)}"
                )
        try:
            TopologicalSorter(
                {node.node_id: node.deps for node in self.nodes}
            ).prepare()
        except CycleError:
            raise ValueError("SOP contains a cycle") from None
        return self

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SOP:
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, text: str) -> SOP:
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError("SOP YAML root must be a mapping")
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False)
