# -*- coding: utf-8 -*-
"""Pydantic models for the session artifact tree.

Enforces R5 (only this CLI writes these shapes) by rejecting unknown fields.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rr.common import (
    COMPARISON_FILE,
    MANIFEST_SCHEMA_VERSION,
    PROVENANCE_REFERENCE,
    RUN_MANIFEST_SCHEMA_VERSION,
    ReportCliError,
    utc_now,
    write_json_atomic,
)


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str
    title: str
    level: int = 1
    source_path: str
    draft_path: str
    spec_path: str
    #: Where ``compare-section`` writes the machine verdict and
    #: ``apply-comparison`` writes the human review. Renamed from the former
    #: ``validation_path`` in schema version 3.
    comparison_path: str
    blocked_by: list[str] = Field(default_factory=list)


class SectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    section_id: str
    inferred_questions: list[str] = Field(default_factory=list)
    metric_definitions: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    required_data: list[str] = Field(default_factory=list)
    markdown_outline: list[str] = Field(default_factory=list)
    context: str = ""


SectionStatus = Literal[
    "pending",
    "running",
    "needs_input",
    "validating",
    "approved",
    "failed",
]

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "needs_input", "failed"}),
    "running": frozenset({"needs_input", "validating", "failed"}),
    "needs_input": frozenset({"running", "failed"}),
    "validating": frozenset({"running", "needs_input", "approved", "failed"}),
    "approved": frozenset({"running"}),
    "failed": frozenset({"running"}),
}


class SectionProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: ReportSection
    status: SectionStatus = "pending"
    attempt: int = 0
    updated_at: str = Field(default_factory=utc_now)
    error: str | None = None


class ReportManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = MANIFEST_SCHEMA_VERSION
    revision: int = 0
    report_id: str
    title: str
    source_path: str
    source_period: str | None = None
    target_period: str | None = None
    baseline_frozen_at: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    sections: list[SectionProgress] = Field(default_factory=list)

    def get(self, section_id: str) -> SectionProgress:
        for progress in self.sections:
            if progress.section.section_id == section_id:
                return progress
        known = (
            ", ".join(progress.section.section_id for progress in self.sections)
            or "（manifest 中没有任何 Section）"
        )
        raise ReportCliError(f"未知的 section_id：{section_id}；可选值：{known}")

    def transition(
        self,
        section_id: str,
        status: SectionStatus,
        *,
        error: str | None = None,
    ) -> SectionProgress:
        progress = self.get(section_id)
        if (
            status != progress.status
            and status not in _ALLOWED_TRANSITIONS[progress.status]
        ):
            raise ReportCliError(
                f"非法的 Section 状态迁移：{progress.status} -> {status}"
                f"（section_id={section_id}）"
            )
        if status == "running" and progress.status != "running":
            progress.attempt += 1
        now = utc_now()
        progress.status = status
        progress.updated_at = now
        progress.error = error
        self.updated_at = now
        return progress

    def next_runnable(self) -> SectionProgress | None:
        approved = {
            progress.section.section_id
            for progress in self.sections
            if progress.status == "approved"
        }
        for progress in self.sections:
            if progress.status not in {"pending", "failed"}:
                continue
            if set(progress.section.blocked_by) <= approved:
                return progress
        return None

    def save(self, path: Path) -> None:
        self.revision += 1
        write_json_atomic(path, self)

    @classmethod
    def load(cls, path: Path) -> ReportManifest:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


RunSectionStatus = Literal[
    "pending",
    "running",
    "needs_input",
    "checking",
    "approved",
    "failed",
]

_ALLOWED_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "needs_input", "failed"}),
    "running": frozenset({"needs_input", "checking", "failed"}),
    "needs_input": frozenset({"running", "failed"}),
    "checking": frozenset({"running", "needs_input", "approved", "failed"}),
    "approved": frozenset({"running"}),
    "failed": frozenset({"running"}),
}


class RunSectionProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str
    status: RunSectionStatus = "pending"
    attempt: int = 0
    updated_at: str = Field(default_factory=utc_now)
    error: str | None = None


class ReportRunManifest(BaseModel):
    """Execution state for one target period, independent from the baseline GT."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = RUN_MANIFEST_SCHEMA_VERSION
    revision: int = 0
    report_id: str
    period: str
    baseline_revision: int
    recipe_hash: str
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    sections: list[RunSectionProgress] = Field(default_factory=list)

    def get(self, section_id: str) -> RunSectionProgress:
        for progress in self.sections:
            if progress.section_id == section_id:
                return progress
        raise ReportCliError(f"目标周期运行中没有该 section_id：{section_id}")

    def transition(
        self,
        section_id: str,
        status: RunSectionStatus,
        *,
        error: str | None = None,
    ) -> RunSectionProgress:
        progress = self.get(section_id)
        if (
            status != progress.status
            and status not in _ALLOWED_RUN_TRANSITIONS[progress.status]
        ):
            raise ReportCliError(
                f"非法的运行态 Section 迁移：{progress.status} -> {status}"
                f"（section_id={section_id}）"
            )
        if status == "running" and progress.status != "running":
            progress.attempt += 1
        now = utc_now()
        progress.status = status
        progress.updated_at = now
        progress.error = error
        self.updated_at = now
        return progress

    def next_runnable(self) -> RunSectionProgress | None:
        for index, progress in enumerate(self.sections):
            if progress.status not in {"pending", "failed"}:
                continue
            if all(previous.status == "approved" for previous in self.sections[:index]):
                return progress
        return None

    def save(self, path: Path) -> None:
        self.revision += 1
        write_json_atomic(path, self)

    @classmethod
    def load(cls, path: Path) -> ReportRunManifest:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class RunCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    details: str = ""


class SectionRunChecks(BaseModel):
    """Non-GT checks for one generated target-period Section."""

    model_config = ConfigDict(extra="forbid")

    report_id: str
    period: str
    section_id: str
    passed: bool
    checks: list[RunCheck] = Field(default_factory=list)
    notes: str = ""


class RecipeParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = "string"
    required: bool = True
    default: str | None = None
    description: str = ""


class QueryRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str
    query_path: str
    parameters: list[str] = Field(default_factory=list)


class ReportRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    parameters: list[RecipeParameter] = Field(default_factory=list)
    queries: list[QueryRecipe] = Field(default_factory=list)
    computes: list[ComputeRecipe] = Field(default_factory=list)
    section_order: list[str] = Field(default_factory=list)
    output_path: str = "report.html"


class ReviewRecord(BaseModel):
    """The human-review trail of one comparison.

    Only ``apply-comparison`` writes this. The generic kernel leaves every
    reviewer field empty and never reads a result back, so this record is the
    auditable proof that a user — not the agent — accepted a remaining gap.
    """

    model_config = ConfigDict(extra="forbid")

    approved_by_user: bool = False
    reason: str = ""
    reviewed_at: str = ""


class EvidenceGate(BaseModel):
    """Provenance of one baseline Section's numbers, judged from landed artifacts.

    ``compare-section`` and ``apply-comparison`` refuse to trust what a draft
    *says*: a Section may only claim ``重算`` when a real query template and the
    data it produced are on disk, and neither is a copy of the report being
    reproduced. When the gate fails there is nothing recomputed to accept, so
    ``passed`` is forced false and the Section can only move forward by landing
    real evidence and regenerating the draft — never by a human override.
    """

    model_config = ConfigDict(extra="forbid")

    passed: bool
    provenance: str = PROVENANCE_REFERENCE
    violations: list[str] = Field(default_factory=list)
    checked_at: str = ""


class ComputeRecipe(BaseModel):
    """A frozen post-processing script for one Section."""

    model_config = ConfigDict(extra="forbid")

    section_id: str
    path: str


class SchedulePeriod(BaseModel):
    """The period shape folded into a run recipe from schedule_descriptor.json."""

    model_config = ConfigDict(extra="forbid")

    unit: Literal["week", "month"]
    offset: int = -1
    window_size: int = 1
    label_format: str


class RunSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: SchedulePeriod
    parameters: dict[str, str] = Field(default_factory=dict)


class RunSectionRecipe(BaseModel):
    """One section as the next scheduled run sees it: identity and order only."""

    model_config = ConfigDict(extra="forbid")

    section_id: str
    title: str = ""
    blocked_by: list[str] = Field(default_factory=list)


class RunRecipe(BaseModel):
    """The reference recipe. Statuses, paths and timestamps stay in the session."""

    model_config = ConfigDict(extra="forbid")

    report_id: str
    title: str = ""
    sections: list[RunSectionRecipe] = Field(default_factory=list)
    parameters: list[RecipeParameter] = Field(default_factory=list)
    queries: list[QueryRecipe] = Field(default_factory=list)
    computes: list[ComputeRecipe] = Field(default_factory=list)
    schedule: RunSchedule
    datasource: str = ""
    probe_tables: list[str] = Field(default_factory=list)

    def get(self, section_id: str) -> RunSectionRecipe:
        for section in self.sections:
            if section.section_id == section_id:
                return section
        known = ", ".join(section.section_id for section in self.sections) or "（无）"
        raise ReportCliError(
            f"运行配方中没有该 section_id：{section_id}；可选值：{known}"
        )
