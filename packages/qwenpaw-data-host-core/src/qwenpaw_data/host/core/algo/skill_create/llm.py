# -*- coding: utf-8 -*-
"""Structured LLM adapter for skill induction and authoring."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from qwenpaw_data.host.core.algo.skill_create.files import FLAG_RE, is_skill_payload_path

INDUCT_TIMEOUT = 90.0
AUTHOR_SKILL_MD_TIMEOUT = 90.0
AUTHOR_BUNDLE_TIMEOUT = 120.0
AUTHOR_TIMEOUT = AUTHOR_BUNDLE_TIMEOUT
MAX_SCRIPTS = 3
MAX_REFERENCES = 2
VALID_COVERAGE = {
    "none",
    "partial_caliber",
    "partial_domain",
    "business_only",
    "full",
    "",
}


class FileSpec(BaseModel):
    path: str = ""
    purpose: str = ""
    contract: str = ""


class InductSchema(BaseModel):
    reason: str = ""
    skill_name: str
    coverage: str = "none"
    existing_overlap: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    other_candidates: list[str] = Field(default_factory=list)
    skill_card: str
    files: list[FileSpec] = Field(default_factory=list)


class AuthorFileSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str = ""
    content: str = ""
    files: Any = None


@dataclass
class Decision:
    reason: str = ""
    skill_name: str = ""
    coverage: str = ""
    existing_overlap: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    other_candidates: list[str] = field(default_factory=list)
    files: list[FileSpec] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "skill_name": self.skill_name,
            "coverage": self.coverage,
            "existing_overlap": list(self.existing_overlap),
            "open_questions": list(self.open_questions),
            "other_candidates": list(self.other_candidates),
            "files": [item.model_dump() for item in self.files],
        }

    @classmethod
    def from_schema(cls, payload: InductSchema) -> Decision:
        coverage = payload.coverage if payload.coverage in VALID_COVERAGE else ""
        return cls(
            reason=payload.reason,
            skill_name=payload.skill_name.strip(),
            coverage=coverage,
            existing_overlap=[str(item) for item in payload.existing_overlap],
            open_questions=[str(item) for item in payload.open_questions],
            other_candidates=[str(item) for item in payload.other_candidates],
            files=list(payload.files or []),
        )


class SkillWriter(Protocol):
    async def induct(self, prompt: str) -> tuple[Decision, str]: ...

    async def author_file(
        self,
        prompt: str,
        *,
        expected_path: str,
        timeout: float | None = None,
    ) -> dict[str, str]: ...


def normalize_file_plan(raw: list[Any] | None) -> list[FileSpec]:
    skill: FileSpec | None = None
    scripts: list[FileSpec] = []
    references: list[FileSpec] = []
    seen: set[str] = set()
    for item in raw or []:
        spec = _as_file_spec(item)
        if spec is None:
            continue
        path = spec.path.strip().replace("\\", "/")
        if not is_skill_payload_path(path) or path in seen:
            continue
        cleaned = FileSpec(
            path=path,
            purpose=spec.purpose.strip(),
            contract=spec.contract.strip(),
        )
        if path == "SKILL.md":
            skill = cleaned
            seen.add(path)
            continue
        if path.startswith("scripts/"):
            if not FLAG_RE.search(cleaned.contract) or len(scripts) >= MAX_SCRIPTS:
                continue
            scripts.append(cleaned)
            seen.add(path)
            continue
        if path.startswith("references/") and len(references) < MAX_REFERENCES:
            references.append(cleaned)
            seen.add(path)
    if skill is None:
        skill = FileSpec(path="SKILL.md", purpose="技能正文")
    return [skill, *scripts, *references]


def coerce_author_file(payload: Any, expected_path: str) -> dict[str, str]:
    data = _as_dict(payload)
    path = str(data.get("path") or "").strip().replace("\\", "/")
    content = data.get("content")
    nested = _first_file_item(data.get("files"))
    if nested:
        if not path:
            path = str(nested.get("path") or "").strip().replace("\\", "/")
        if not isinstance(content, str) or not content.strip():
            content = nested.get("content")
    if not isinstance(content, str):
        raise ValueError("author file content is missing")
    path = (expected_path or path).strip().replace("\\", "/")
    if not is_skill_payload_path(path):
        raise ValueError(f"illegal author path: {path!r}")
    return {path: content}


def render_file_plan(plan: list[FileSpec]) -> str:
    if not plan:
        return "- `SKILL.md`"
    lines: list[str] = []
    for spec in plan:
        line = f"- `{spec.path}`：{spec.purpose or '（无说明）'}"
        if spec.contract:
            line += f"\n  contract: {spec.contract}"
        lines.append(line)
    return "\n".join(lines)


def _as_file_spec(item: Any) -> FileSpec | None:
    if isinstance(item, FileSpec):
        return item
    if isinstance(item, dict):
        return FileSpec(
            path=str(item.get("path") or ""),
            purpose=str(item.get("purpose") or ""),
            contract=str(item.get("contract") or ""),
        )
    return None


def _as_dict(payload: Any) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        dumped = payload.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if isinstance(payload, dict):
        return payload
    raise ValueError(f"author payload is not a dict: {type(payload)}")


def _first_file_item(nested: Any) -> dict[str, Any] | None:
    if isinstance(nested, str):
        text = nested.strip()
        if not text:
            return None
        nested = json.loads(text)
    if isinstance(nested, list) and nested:
        first = nested[0]
        return first if isinstance(first, dict) else None
    if isinstance(nested, dict):
        return nested
    return None


class StructuredSkillWriter:
    """Default writer: one structured call per stage / file."""

    def __init__(self, model: Any) -> None:
        self._model = model

    async def induct(self, prompt: str) -> tuple[Decision, str]:
        from agentscope.message import SystemMsg, UserMsg

        from qwenpaw_data.host.core.algo.settlement.llm import structured_call
        from qwenpaw_data.host.core.algo.skill_create.prompts import SYSTEM_SKILL_WRITER

        result = await structured_call(
            self._model,
            [
                SystemMsg(name="system", content=SYSTEM_SKILL_WRITER),
                UserMsg(name="user", content=prompt),
            ],
            InductSchema,
            attempts=1,
            timeout=INDUCT_TIMEOUT,
        )
        return Decision.from_schema(result), result.skill_card

    async def author_file(
        self,
        prompt: str,
        *,
        expected_path: str,
        timeout: float | None = None,
    ) -> dict[str, str]:
        from agentscope.message import SystemMsg, UserMsg

        from qwenpaw_data.host.core.algo.settlement.llm import structured_call
        from qwenpaw_data.host.core.algo.skill_create.prompts import SYSTEM_SKILL_WRITER

        limit = timeout
        if limit is None:
            limit = (
                AUTHOR_SKILL_MD_TIMEOUT
                if expected_path == "SKILL.md"
                else AUTHOR_BUNDLE_TIMEOUT
            )
        result = await structured_call(
            self._model,
            [
                SystemMsg(name="system", content=SYSTEM_SKILL_WRITER),
                UserMsg(name="user", content=prompt),
            ],
            AuthorFileSchema,
            attempts=1,
            timeout=limit,
        )
        return coerce_author_file(result, expected_path)
