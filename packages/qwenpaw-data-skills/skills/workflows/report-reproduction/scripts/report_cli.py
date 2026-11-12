#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Report reproduction workflow CLI — the single entry point of this skill.

Every artifact and every state transition of the report reproduction workflow
lives behind this script. The host keeps no report semantics at all: it only
contributes the generic comparison kernel
(``qwenpaw_data.host.core.agent.tools.compare``) and the workflow marker that makes
the agent load this skill in the first place.

Usage::

    python scripts/report_cli.py --artifact-dir <dir> --chat-id <id> <sub> [...]

Conventions shared with the other workflow skills:

* structured results go to stdout as one JSON object, logs go to stderr;
* failures are reported as ``{"ok": false, "error": ...}`` with exit code 1 and
  never as a Python traceback, because the caller is a model reading stdout;
* every path this script writes is confined to ``--artifact-dir``;
* ``--chat-id`` is mandatory and whitelisted, because it names the turn-scope
  file that keeps one chat to one Section and one target period.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import logging
import re
import shutil
import socket
import ssl
import sys
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, NoReturn
from urllib.parse import urlparse, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Layout and limits
# --------------------------------------------------------------------------- #

PRODUCT_DIR = "report-reproduction"
MANIFEST_FILE = "report_manifest.json"

#: Bumped for the ``validation.json`` -> ``comparison.json`` rename. Older
#: manifests are migrated in place on first load (see ``ReportStore``).
MANIFEST_SCHEMA_VERSION = 3
RUN_MANIFEST_SCHEMA_VERSION = 1

COMPARISON_FILE = "comparison.json"
LEGACY_VALIDATION_FILE = "validation.json"

EXIT_OK = 0
EXIT_ERROR = 1

DEFAULT_TOLERANCE = 0.001

# ``--chat-id`` ends up as a file name under ``turns/``, so it is validated
# instead of sanitised: a value that would have to be rewritten is rejected,
# because a rewritten name silently splits one chat into two turn scopes.
CHAT_ID_MAX_LENGTH = 128
_CHAT_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")

_RUN_PERIOD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_UNRESOLVED_PARAMETER_RE = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}"
)
_WEEK_SLOT_PARAMETER_RE = re.compile(r"^w(\d+)_(start|end)$", re.IGNORECASE)
_MONTH_SLOT_PARAMETER_RE = re.compile(r"^(.+)_(\d+)$", re.IGNORECASE)

_SOURCE_SUFFIXES = frozenset({".docx", ".html", ".htm", ".md", ".txt"})
_MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
_CONTENT_SUFFIXES = {
    "text/html": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

_SYNTHETIC_SECTION_TITLES = {"正文"}

#: Where a baseline Section's numbers came from. Only artifacts landed on disk can
#: prove ``重算``; without them the numbers can only have been lifted from the
#: original report, which a reproduction is never allowed to do.
PROVENANCE_RECOMPUTED = "重算"
PROVENANCE_REFERENCE = "原报告"

#: A draft that discloses an unclosable gap says outright its numbers were not
#: independently recomputed, so the gate treats the tag as a provenance signal.
IRREPRODUCIBLE_TAG = "【无法复现】"

#: A ``queries/*.sql`` file only counts as evidence when it actually queries
#: something; an empty or comment-only stub proves no recomputation happened.
_SQL_STATEMENT_RE = re.compile(r"\bselect\b", re.IGNORECASE)

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOGGER_NAME = "report-reproduction"


class ReportCliError(Exception):
    """An expected failure: reported as JSON on stdout, never as a traceback."""


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #


def setup_logging(level_name: str = "info") -> logging.Logger:
    """Send every log record to stderr so stdout stays machine-readable.

    The handler is attached to this skill's own logger rather than the root
    logger: a caller that imports this module keeps its own logging
    configuration, and a second invocation rebinds to the current
    ``sys.stderr`` instead of a stream captured by the first one.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)
    return logger


def emit(payload: dict[str, Any]) -> None:
    """Write the one structured result of this invocation to stdout."""
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def fail(logger: logging.Logger, message: str) -> NoReturn:
    """Report a failure to both channels and stop with ``EXIT_ERROR``."""
    logger.error("%s", message)
    emit({"ok": False, "error": message})
    raise SystemExit(EXIT_ERROR)


def write_json_atomic(path: Path, model: BaseModel) -> None:
    """Write a pydantic model atomically so no reader sees a half-written file."""
    write_text_atomic(path, model.model_dump_json(indent=2) + "\n")


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as stream:
        stream.write(text)
        temporary = Path(stream.name)
    temporary.replace(path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_text_file(path: Path, *, label: str) -> str:
    if not path.exists():
        raise ReportCliError(f"{label}不存在：{path}")
    if path.is_dir():
        raise ReportCliError(f"{label}是目录而不是文件：{path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ReportCliError(f"{label}不是 utf-8 文本：{path}") from exc


def _file_has_content(path: Path) -> bool:
    """True when a landed file carries more than whitespace.

    Binary payloads count by size, text payloads must hold a visible character,
    so an empty placeholder cannot stand in for real query evidence.
    """
    try:
        if path.stat().st_size == 0:
            return False
    except OSError:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Binary data (xlsx, parquet, png …) is a legitimate query result.
        return True
    except OSError:
        return False
    return bool(text.strip())


def _normalized_digest(text: str) -> str:
    """Whitespace-insensitive digest, so a reformatted copy still matches."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def validated_section_id(section_id: str) -> str:
    """Reject an identifier that could not be a directory name.

    Every Section-scoped subcommand claims its turn scope first, so this check
    has to run before anything is persisted: binding a chat to an identifier
    the store is about to refuse would lock that chat out of the whole
    workflow with no way to unbind it.
    """
    normalized = (section_id or "").strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or Path(normalized).name != normalized
    ):
        raise ReportCliError(f"非法的 section_id：{section_id!r}")
    return normalized


# --------------------------------------------------------------------------- #
# Artifact models
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Generic comparison kernel (host side, imported lazily)
# --------------------------------------------------------------------------- #

_KERNEL: tuple[type[BaseModel], Any] | None = None


def _comparison_kernel() -> tuple[type[BaseModel], Any]:
    """Return ``(SectionComparison, compare_documents)`` from the host kernel.

    The import stays lazy on purpose: ``parse``, ``prepare-run``, ``check-run``
    and friends must keep working where the host package is not installed, and
    only the two comparison subcommands need the kernel. The wrapper model adds
    the review trail without forking the comparison algorithm, so this skill and
    every other consumer of the kernel share one acceptance criteria.
    """
    global _KERNEL
    if _KERNEL is not None:
        return _KERNEL
    try:
        from qwenpaw_data.host.core.agent.tools.compare import (
            ComparisonResult,
            compare_documents,
        )
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ReportCliError(
            "该子命令需要 host 提供的通用比对内核 "
            "`qwenpaw_data.host.core.agent.tools.compare`，当前 Python 环境导入不到它。"
            "注意：`qwenpaw-data` 是本仓库自带的命名空间包（由 qwenpaw-data-host-core 提供），"
            "不是 PyPI 第三方包，请勿执行 `pip install qwenpaw-data`。正确做法：用项目虚拟"
            "环境运行本脚本（`.venv/bin/python .../report_cli.py ...`），或在镜像/环境"
            "中先执行 `uv sync --all-packages` 装上全部成员包后重试。"
            f"原始导入错误：{exc}"
        ) from exc

    class SectionComparison(ComparisonResult):  # type: ignore[misc,valid-type]
        """``comparison.json`` as this skill owns it.

        A superset of the generic result: the machine verdict comes from the
        kernel untouched, and ``review`` records who accepted a remaining gap.
        """

        review: ReviewRecord | None = None
        evidence_gate: EvidenceGate | None = None

    _KERNEL = (SectionComparison, compare_documents)
    return _KERNEL


# --------------------------------------------------------------------------- #
# Turn scope
# --------------------------------------------------------------------------- #


class SectionTurnGuard:
    """Persist the report-reproduction phase assigned to one chat.

    One chat may advance exactly one baseline Section, or exactly one target
    period. That constraint is what makes per-Section human review possible, so
    it survives the move out of the host unchanged.
    """

    def __init__(self, artifact_dir: Path, *, chat_id: str) -> None:
        self._product_root = Path(artifact_dir) / PRODUCT_DIR
        self._chat_id = chat_id.strip()

    @property
    def enabled(self) -> bool:
        return bool(self._chat_id)

    def mark_planning(self) -> None:
        """Reserve this chat for report parsing, clarification, and planning."""
        if not self.enabled:
            return
        payload = self._load()
        mode = self._mode(payload)
        if mode and mode != "planning":
            raise ReportCliError("报告解析与规划必须在单独的 chat 中进行")
        self._write({"mode": "planning"})

    def start_target_run(self, run_period: str) -> None:
        """Allow one target-period chat to advance every run Section."""
        normalized_period = self.ensure_target_run_allowed(run_period)
        if not self.enabled:
            return
        self._write({"mode": "target_run", "run_period": normalized_period})

    def ensure_target_run_allowed(self, run_period: str) -> str:
        """Validate target-period chat scope without changing persisted state."""
        normalized_period = (run_period or "").strip()
        if not self.enabled:
            return normalized_period
        if not normalized_period:
            raise ReportCliError("缺少 --run-period：目标周期执行必须指定周期")
        payload = self._load()
        mode = self._mode(payload)
        claimed_period = str(payload.get("run_period") or "").strip()
        if mode == "planning":
            raise ReportCliError(
                "目标周期执行必须在报告规划完成之后、于单独的 chat 中进行"
            )
        if mode == "baseline_section":
            raise ReportCliError(
                "目标周期执行必须在基线 Section 完成之后、于单独的 chat 中进行"
            )
        if mode == "target_run" and claimed_period != normalized_period:
            raise ReportCliError(
                "一个 chat 只能执行一个目标周期；"
                f"当前 chat 已绑定周期 {claimed_period!r}"
            )
        return normalized_period

    def claim(self, section_id: str, *, run_period: str | None = None) -> None:
        if not self.enabled:
            return
        normalized = validated_section_id(section_id)
        if run_period is not None:
            self.start_target_run(run_period)
            return

        payload = self._load()
        mode = self._mode(payload)
        if mode == "planning":
            raise ReportCliError(
                "基线 Section 执行必须在报告解析与规划完成之后、于单独的 chat 中进行"
            )
        if mode == "target_run":
            raise ReportCliError("基线 Section 执行不能在目标周期 chat 中进行")
        claimed = str(payload.get("section_id") or "").strip()
        if mode == "baseline_section" and claimed and claimed != normalized:
            raise ReportCliError(
                f"一个 chat 只能推进一个 Section；当前 chat 已绑定 Section {claimed!r}"
            )
        self._write({"mode": "baseline_section", "section_id": normalized})

    def claimed_section(self) -> str | None:
        if not self.enabled:
            return None
        claimed = str(self._load().get("section_id") or "").strip()
        return claimed or None

    def ensure_no_section_claimed(self, action: str) -> None:
        if not self.enabled:
            return
        payload = self._load()
        mode = self._mode(payload)
        if mode == "planning":
            raise ReportCliError(
                f"{action} 必须在报告规划完成之后、于单独的 chat 中进行"
            )
        if mode == "target_run":
            raise ReportCliError(f"{action} 必须在目标周期执行开始之前完成")
        claimed = str(payload.get("section_id") or "").strip()
        if mode == "baseline_section" and claimed:
            raise ReportCliError(
                f"{action} 必须在 Section {claimed!r} 完成之后、于单独的 chat 中进行"
            )

    def ensure_compile_allowed(
        self,
        action: str,
        *,
        run_period: str | None = None,
    ) -> None:
        if run_period is None:
            self.ensure_no_section_claimed(action)
            return
        if not self.enabled:
            return
        normalized_period = run_period.strip()
        payload = self._load()
        mode = self._mode(payload)
        claimed_period = str(payload.get("run_period") or "").strip()
        if mode != "target_run" or claimed_period != normalized_period:
            raise ReportCliError(
                f"{action} 需要先在当前 chat 中执行 prepare-run --period "
                f"{normalized_period!r}"
            )

    def _load(self) -> dict[str, Any]:
        path = self._path()
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _mode(payload: dict[str, Any]) -> str:
        mode = str(payload.get("mode") or "").strip()
        if not mode and payload.get("section_id"):
            return "baseline_section"
        return mode

    def _write(self, values: dict[str, Any]) -> None:
        path = self._path()
        write_text_atomic(
            path,
            json.dumps(
                {
                    "chat_id": self._chat_id,
                    **values,
                    "updated_at": utc_now(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def _path(self) -> Path:
        safe_chat_id = _SAFE_ID_RE.sub("_", self._chat_id).strip("._-")
        if not safe_chat_id:
            raise ReportCliError("--chat-id 非法，无法作为 turn 记录文件名")
        return self._product_root / "turns" / f"{safe_chat_id}.json"


# --------------------------------------------------------------------------- #
# Artifact store
# --------------------------------------------------------------------------- #


class ReportStore:
    """Deterministic owner of every report artifact and state transition."""

    def __init__(self, artifact_dir: Path) -> None:
        self.artifact_dir = Path(artifact_dir)
        self.product_root = self.artifact_dir / PRODUCT_DIR
        self.manifest_path = self.product_root / MANIFEST_FILE
        # One CLI invocation is one process, so this only serialises the nested
        # calls inside a single subcommand; artifact writes stay atomic anyway.
        self._lock = threading.RLock()

    # -- path confinement -------------------------------------------------- #

    def _safe_path(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute():
            raise ReportCliError(f"产物路径必须是相对路径：{relative_path}")
        root = self.product_root.resolve()
        resolved = (root / relative).resolve()
        if not resolved.is_relative_to(root):
            raise ReportCliError(f"产物路径越出任务目录（{root}）：{relative_path}")
        return resolved

    def _relative(self, path: Path) -> str:
        """Express an artifact path relative to the product root."""
        return str(path.resolve().relative_to(self.product_root.resolve()))

    def _section_path(self, section_id: str, filename: str) -> Path:
        validated_section_id(section_id)
        return self._safe_path(f"sections/{section_id}/{filename}")

    def _run_period(self, period: str) -> str:
        normalized = (period or "").strip()
        if not _RUN_PERIOD_RE.fullmatch(normalized):
            raise ReportCliError(
                "--period 只能使用 1-64 个字母、数字、点、短横线或下划线，"
                f"收到：{period!r}"
            )
        return normalized

    def _run_manifest_path(self, period: str) -> Path:
        return self._safe_path(f"runs/{self._run_period(period)}/run_manifest.json")

    def _run_section_path(
        self,
        section_id: str,
        period: str,
        filename: str,
    ) -> Path:
        return self._section_path(
            section_id,
            f"runs/{self._run_period(period)}/{filename}",
        )

    def _comparison_path(self, section_id: str) -> Path:
        return self._section_path(section_id, f"baseline/{COMPARISON_FILE}")

    # -- lifecycle --------------------------------------------------------- #

    def initialize(
        self,
        *,
        source_path: Path | None = None,
        url: str | None = None,
        report_id: str | None = None,
        source_period: str | None = None,
        target_period: str | None = None,
        force_reinitialize: bool = False,
    ) -> ReportManifest:
        with self._lock:
            if self.manifest_path.is_file() and not force_reinitialize:
                return self.load_manifest()
            _validate_source(source_path, url)
            if force_reinitialize and self.product_root.exists():
                shutil.rmtree(self.product_root)
            self.product_root.mkdir(parents=True, exist_ok=True)
            evidence = self.product_root / "evidence"
            evidence.mkdir(exist_ok=True)
            global_evidence = evidence / "global.json"
            if not global_evidence.exists():
                global_evidence.write_text("{}\n", encoding="utf-8")
            manifest = parse_document(
                product_root=self.product_root,
                source_path=source_path,
                url=url,
                report_id=report_id,
                source_period=source_period,
                target_period=target_period,
            )
            manifest.save(self.manifest_path)
            return manifest

    def load_manifest(self) -> ReportManifest:
        """Load the manifest, chain-migrating artifacts to schema version 3."""
        with self._lock:
            if not self.manifest_path.is_file():
                raise ReportCliError(
                    f"报告复现任务尚未初始化：缺少 {PRODUCT_DIR}/{MANIFEST_FILE}，"
                    "请先运行 parse 子命令"
                )
            payload = self._read_manifest_payload()
            migrated = self._migrate_artifacts(payload)
            manifest = ReportManifest.model_validate(
                _normalize_manifest_payload(payload)
            )
            # Not short-circuited on purpose: a migration renames the verdict
            # file but the manifest still records the old name, so the recorded
            # paths have to be canonicalised in the very same pass.
            backfilled = self._backfill_section_layout(manifest)
            if migrated or backfilled:
                manifest.schema_version = MANIFEST_SCHEMA_VERSION
                manifest.save(self.manifest_path)
            if migrated:
                self._rebase_runs_on(manifest.revision)
            return manifest

    def _read_manifest_payload(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReportCliError(f"{MANIFEST_FILE} 无法解析：{exc}") from exc
        if not isinstance(payload, dict):
            raise ReportCliError(f"{MANIFEST_FILE} 顶层必须是 JSON 对象")
        return payload

    def _migrate_artifacts(self, payload: dict[str, Any]) -> bool:
        """Move pre-v3 artifacts into the current layout.

        Returns True when anything moved, so the caller persists the new
        ``schema_version`` exactly once. Every renamed verdict keeps a ``.bak``
        copy, and a conversion failure aborts before touching the manifest.
        """
        try:
            version = int(payload.get("schema_version") or 1)
        except (TypeError, ValueError):
            version = 1
        if version >= MANIFEST_SCHEMA_VERSION:
            return False

        moved = False
        for entry in payload.get("sections") or []:
            section = entry.get("section") if isinstance(entry, dict) else None
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("section_id") or "").strip()
            if not section_id or Path(section_id).name != section_id:
                continue

            baseline = self._section_path(section_id, "baseline")
            baseline.mkdir(parents=True, exist_ok=True)
            (baseline / "data").mkdir(exist_ok=True)

            # v1 -> v2: draft and verdict used to live directly under the Section.
            for legacy_name, target_name in (
                ("draft.md", "baseline/draft.md"),
                (LEGACY_VALIDATION_FILE, f"baseline/{LEGACY_VALIDATION_FILE}"),
            ):
                legacy = self._section_path(section_id, legacy_name)
                target = self._section_path(section_id, target_name)
                if legacy.is_file() and not target.exists():
                    legacy.replace(target)
                    moved = True

            # v2 -> v3: the verdict file became a generic comparison.
            legacy_verdict = self._section_path(
                section_id, f"baseline/{LEGACY_VALIDATION_FILE}"
            )
            comparison = self._comparison_path(section_id)
            if legacy_verdict.is_file() and not comparison.is_file():
                self._convert_legacy_validation(legacy_verdict, comparison, section_id)
                legacy_verdict.replace(
                    legacy_verdict.parent / f"{legacy_verdict.name}.bak"
                )
                moved = True
        return moved

    def _convert_legacy_validation(
        self,
        legacy: Path,
        comparison: Path,
        section_id: str,
    ) -> None:
        """Rewrite a v2 ``validation.json`` as a v3 ``comparison.json``.

        Field renames only (``source``/``draft`` -> ``reference``/``target``);
        numbers and verdict are copied verbatim so an already reviewed Section
        keeps its outcome across the migration.
        """
        model, _ = _comparison_kernel()
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReportCliError(
                f"存量比对结果无法解析，迁移中止：{legacy}（{exc}）"
            ) from exc
        if not isinstance(raw, dict):
            # Same wording as the other migration failures: an aborted migration
            # has to be recognisable as such, because it leaves the manifest at
            # the version it was found at.
            raise ReportCliError(
                f"存量比对结果顶层必须是 JSON 对象，迁移中止：{legacy}"
            )

        differences = [
            {
                "reference": item.get("source"),
                "target": item.get("draft"),
                "relative_error": item.get("relative_error"),
                "matched": bool(item.get("matched", False)),
                "note": item.get("note"),
                "target_start": item.get("draft_start"),
                "target_end": item.get("draft_end"),
            }
            for item in (raw.get("numeric_differences") or [])
            if isinstance(item, dict)
        ]
        reviewed = bool(raw.get("passed_with_review"))
        payload = {
            "schema_version": 1,
            "target_path": f"sections/{section_id}/baseline/draft.md",
            "reference_path": f"sections/{section_id}/source.md",
            "passed": bool(raw.get("passed", False)),
            "passed_with_review": reviewed,
            "numeric_coverage": float(raw.get("numeric_coverage") or 0.0),
            "numeric_differences": differences,
            "structure_differences": raw.get("structure_differences") or [],
            "criteria_analysis": raw.get("criteria_analysis") or [],
            "notes": raw.get("notes") or "",
            "tolerance": float(raw.get("tolerance") or DEFAULT_TOLERANCE),
            "created_at": raw.get("created_at") or utc_now(),
            "review": (
                {
                    "approved_by_user": True,
                    "reason": "由存量 validation.json 迁移（原放行理由未记录）",
                    "reviewed_at": utc_now(),
                }
                if reviewed
                else None
            ),
        }
        try:
            converted = model.model_validate(payload)
        except ValueError as exc:
            raise ReportCliError(
                f"存量比对结果字段不兼容，迁移中止：{legacy}（{exc}）"
            ) from exc
        write_json_atomic(comparison, converted)

    def _backfill_section_layout(self, manifest: ReportManifest) -> bool:
        """Create the baseline directories and canonicalise recorded paths."""
        changed = False
        for progress in manifest.sections:
            section_id = progress.section.section_id
            baseline = self._section_path(section_id, "baseline")
            baseline.mkdir(parents=True, exist_ok=True)
            (baseline / "data").mkdir(exist_ok=True)
            expected = {
                "draft_path": self._relative(
                    self._section_path(section_id, "baseline/draft.md")
                ),
                "comparison_path": self._relative(self._comparison_path(section_id)),
            }
            for field_name, value in expected.items():
                if getattr(progress.section, field_name) != value:
                    setattr(progress.section, field_name, value)
                    changed = True
        return changed

    def _rebase_runs_on(self, baseline_revision: int) -> None:
        """Keep already-prepared target runs usable across a migration.

        Migrating bumps ``manifest.revision``, which is exactly what
        ``_ensure_run_recipe_current`` compares against. Without this rebase a
        run prepared before the migration would be rejected as stale even though
        neither the baseline drafts nor ``recipe.json`` changed.
        """
        runs_root = self._safe_path("runs")
        if not runs_root.is_dir():
            return
        resolved_root = runs_root.resolve()
        for path in sorted(runs_root.glob("*/run_manifest.json")):
            if not path.resolve().is_relative_to(resolved_root):
                continue
            try:
                run_manifest = ReportRunManifest.load(path)
            except (OSError, ValueError):
                continue
            if run_manifest.baseline_revision == baseline_revision:
                continue
            run_manifest.baseline_revision = baseline_revision
            run_manifest.save(path)

    # -- target period runs ------------------------------------------------ #

    def prepare_run(
        self,
        period: str,
        *,
        force_reinitialize: bool = False,
    ) -> ReportRunManifest:
        with self._lock:
            manifest = self.load_manifest()
            unfinished = [
                progress.section.section_id
                for progress in manifest.sections
                if progress.status != "approved"
            ]
            if unfinished:
                raise ReportCliError(
                    "基线尚未全部 approved，无法准备目标周期运行："
                    + ", ".join(unfinished)
                )
            recipe_path = self._safe_path("recipe.json")
            if not recipe_path.is_file():
                raise ReportCliError(
                    "准备目标周期运行前必须先 freeze-recipe 生成 recipe.json"
                )
            recipe_bytes = recipe_path.read_bytes()
            if not json.loads(recipe_bytes).get("queries"):
                raise ReportCliError("recipe.json 中没有可用于周期运行的查询模板")

            normalized_period = self._run_period(period)
            run_manifest_path = self._run_manifest_path(normalized_period)
            run_root = run_manifest_path.parent
            if run_manifest_path.is_file() and not force_reinitialize:
                existing_run = ReportRunManifest.load(run_manifest_path)
                self._ensure_run_recipe_current(existing_run)
                return existing_run
            if force_reinitialize and run_root.exists():
                shutil.rmtree(run_root)

            run_manifest = ReportRunManifest(
                report_id=manifest.report_id,
                period=normalized_period,
                baseline_revision=manifest.revision,
                recipe_hash=hashlib.sha256(recipe_bytes).hexdigest(),
                sections=[
                    RunSectionProgress(section_id=progress.section.section_id)
                    for progress in manifest.sections
                ],
            )
            for progress in manifest.sections:
                self._run_section_path(
                    progress.section.section_id,
                    normalized_period,
                    "data",
                ).mkdir(parents=True, exist_ok=True)
            run_manifest.save(run_manifest_path)
            return run_manifest

    def load_run_manifest(self, period: str) -> ReportRunManifest:
        with self._lock:
            path = self._run_manifest_path(period)
            if not path.is_file():
                raise ReportCliError(
                    f"周期 {period!r} 的运行尚未初始化，请先运行 prepare-run"
                )
            return ReportRunManifest.load(path)

    def _ensure_run_recipe_current(
        self,
        run_manifest: ReportRunManifest,
    ) -> None:
        manifest = self.load_manifest()
        if manifest.revision != run_manifest.baseline_revision:
            raise ReportCliError(
                "该运行准备完成后基线发生了变化；请显式使用 "
                "prepare-run --force-reinitialize 重建运行"
            )
        recipe_path = self._safe_path("recipe.json")
        if not recipe_path.is_file():
            raise ReportCliError("周期运行所需的 recipe.json 缺失")
        current_hash = hashlib.sha256(recipe_path.read_bytes()).hexdigest()
        if current_hash != run_manifest.recipe_hash:
            raise ReportCliError(
                "该运行准备完成后 recipe.json 发生了变化；请显式使用 "
                "prepare-run --force-reinitialize 重建运行"
            )

    def update_section(
        self,
        *,
        section_id: str,
        markdown: str,
        status: str = "validating",
        run_period: str | None = None,
        reopen_baseline: bool = False,
    ) -> ReportManifest | ReportRunManifest:
        """Write one Section draft and advance the state machine.

        ``markdown`` is stored verbatim (atomically), so it may carry the
        Agent-written 「复现差异与数据缺口说明」 block at the very top of the
        draft — the CLI neither parses nor validates that block; it is part of
        the draft contract (see SKILL.md 「草稿排版规范」 rule 6). Offsets in
        ``comparison.json`` stay relative to the whole draft, so the frontend
        re-aligns them after lifting the top block out.
        """
        if not markdown.strip():
            raise ReportCliError("Section 内容不能为空")
        if status not in {"validating", "needs_input"}:
            raise ReportCliError("--status 只能是 validating 或 needs_input")

        if run_period is not None:
            if reopen_baseline:
                raise ReportCliError("--reopen-baseline 只对基线更新有效")
            with self._lock:
                baseline_progress = self.load_manifest().get(section_id)
                run_manifest = self.load_run_manifest(run_period)
                self._ensure_run_recipe_current(run_manifest)
                progress = run_manifest.get(section_id)
                approved = {
                    item.section_id
                    for item in run_manifest.sections
                    if item.status == "approved"
                }
                blocked_by = set(baseline_progress.section.blocked_by) - approved
                if blocked_by:
                    raise ReportCliError(
                        "运行态 Section 的前置依赖尚未 approved："
                        + ", ".join(sorted(blocked_by))
                    )
                if progress.status != "running":
                    run_manifest.transition(section_id, "running")
                draft_path = self._run_section_path(section_id, run_period, "draft.md")
                _write_draft(draft_path, markdown)
                run_manifest.transition(
                    section_id,
                    "checking" if status == "validating" else "needs_input",
                )
                run_manifest.save(self._run_manifest_path(run_period))
                return run_manifest

        with self._lock:
            manifest = self.load_manifest()
            if manifest.baseline_frozen_at and not reopen_baseline:
                raise ReportCliError("基线已冻结；如需修订请显式加上 --reopen-baseline")
            if reopen_baseline:
                manifest.baseline_frozen_at = None
            progress = manifest.get(section_id)
            if progress.status != "running":
                manifest.transition(section_id, "running")
            _write_draft(self._section_path(section_id, "baseline/draft.md"), markdown)
            manifest.transition(section_id, status)  # type: ignore[arg-type]
            manifest.save(self.manifest_path)
            return manifest

    # -- comparison -------------------------------------------------------- #

    def _baseline_evidence(self, section_id: str, *, draft: str) -> EvidenceGate:
        """Judge provenance from landed artifacts, never from what a draft says.

        Mirrors the two evidence checks ``check-run`` already applies to a target
        period — a query template and the data it produced — because the baseline
        is the run that has to prove the numbers can be recomputed at all. Two
        forgery cases are refused as well: an evidence file that is a copy of
        ``source.md`` or of the draft proves nothing, and a draft that declares
        ``【无法复现】`` says outright its numbers were not recomputed. Any hit
        forces ``provenance=原报告`` and a failing gate, which the caller turns
        into actionable feedback so the agent reflects and regenerates.
        """
        violations: list[str] = []
        query_dir = self._section_path(section_id, "queries")
        data_dir = self._section_path(section_id, "baseline/data")
        source_path = self._section_path(section_id, "source.md")

        sql_files = (
            [
                path
                for path in sorted(query_dir.glob("*.sql"))
                if _file_has_content(path)
                and _SQL_STATEMENT_RE.search(
                    path.read_text(encoding="utf-8", errors="ignore")
                )
            ]
            if query_dir.is_dir()
            else []
        )
        if not sql_files:
            violations.append(
                "queries/ 下没有含 SELECT 的非空 .sql：本节数值无法追溯到真实取数"
            )

        data_files = (
            [
                path
                for path in sorted(data_dir.rglob("*"))
                if path.is_file()
                and not path.name.startswith(".")
                and _file_has_content(path)
            ]
            if data_dir.is_dir()
            else []
        )
        if not data_files:
            violations.append(
                "baseline/data/ 下没有非空数据文件：缺少本次取数的真实结果"
            )

        source_digest = (
            _normalized_digest(source_path.read_text(encoding="utf-8"))
            if source_path.is_file()
            else None
        )
        draft_digest = _normalized_digest(draft)
        for path in data_files:
            try:
                digest = _normalized_digest(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, OSError):
                continue  # binary evidence is not a text copy of the report
            if source_digest is not None and digest == source_digest:
                violations.append(
                    f"证据文件 {path.name} 与 source.md 内容一致，是原文拷贝而非取数结果"
                )
            elif digest == draft_digest:
                violations.append(
                    f"证据文件 {path.name} 与 draft.md 内容一致，是复现稿拷贝而非取数结果"
                )

        if IRREPRODUCIBLE_TAG in draft:
            violations.append(
                "draft.md 声明【无法复现】：本节数值未独立重算，直接取自原报告"
            )

        return EvidenceGate(
            passed=not violations,
            provenance=(
                PROVENANCE_RECOMPUTED if not violations else PROVENANCE_REFERENCE
            ),
            violations=violations,
            checked_at=utc_now(),
        )

    def compare_section(
        self,
        *,
        section_id: str,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> tuple[Path, BaseModel]:
        """Recompute the machine verdict for one baseline Section.

        Writes ``baseline/comparison.json`` and nothing else. The manifest state
        only moves in :meth:`apply_comparison`, so re-running a comparison can
        never undo a human approval — the deadlock this split exists to prevent.
        The previous result is not read back: the kernel is the only authority
        on the machine verdict.
        """
        if not 0 <= tolerance <= 1:
            raise ReportCliError(f"--tolerance 必须在 0 到 1 之间，收到 {tolerance}")
        with self._lock:
            manifest = self.load_manifest()
            manifest.get(section_id)
            reference_path = self._section_path(section_id, "source.md")
            target_path = self._section_path(section_id, "baseline/draft.md")
            output = self._comparison_path(section_id)
            self._refuse_to_discard_a_review(
                output, draft_path=target_path, source_path=reference_path
            )
            # Loaded only once a recomputation is actually going to happen, so a
            # refusal above stays available where the kernel is not installed.
            model, compare_documents = _comparison_kernel()
            reference = read_text_file(reference_path, label="Section 原文 source.md")
            target = read_text_file(target_path, label="复现稿 baseline/draft.md")
            gate = self._baseline_evidence(section_id, draft=target)
            result = compare_documents(reference, target, tolerance=tolerance)
            comparison = model.model_validate(result.model_dump()).model_copy(
                update={
                    "target_path": self._relative(target_path),
                    "reference_path": self._relative(reference_path),
                    "evidence_gate": gate,
                    # A verbatim copy of the report has zero numeric differences
                    # yet is not a reproduction: the gate, not the kernel, decides
                    # that, so a failing gate forces the machine verdict false.
                    "passed": bool(result.passed and gate.passed),
                    "passed_with_review": False,
                }
            )
            write_json_atomic(output, comparison)
            return output, comparison

    def apply_comparison(
        self,
        *,
        section_id: str,
        approved_by_user: bool = False,
        reason: str = "",
        notes: str = "",
        criteria: list[dict[str, Any]] | None = None,
    ) -> tuple[ReportManifest, BaseModel]:
        """Turn a comparison result into a Section verdict.

        This is the only writer of the reviewer fields, and it refuses a result
        that does not belong to this Section or that predates the current draft.
        """
        model, _ = _comparison_kernel()
        with self._lock:
            manifest = self.load_manifest()
            manifest.get(section_id)
            comparison_path = self._comparison_path(section_id)
            draft_path = self._section_path(section_id, "baseline/draft.md")
            source_path = self._section_path(section_id, "source.md")
            if not comparison_path.is_file():
                raise ReportCliError(
                    f"缺少 {self._relative(comparison_path)}；"
                    "请先运行 compare-section 生成机器比对结果"
                )
            try:
                comparison = model.model_validate_json(
                    comparison_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ReportCliError(
                    f"{self._relative(comparison_path)} 无法解析：{exc}"
                ) from exc

            self._reject_foreign_comparison(
                comparison, section_id=section_id, path=comparison_path
            )
            self._reject_stale_comparison(
                comparison_path, draft_path=draft_path, source_path=source_path
            )

            draft_text = read_text_file(draft_path, label="复现稿 baseline/draft.md")
            gate = self._baseline_evidence(section_id, draft=draft_text)
            comparison.evidence_gate = gate
            if not gate.passed:
                # Nothing was recomputed, so there is no residual a user could
                # accept: a human override is refused outright and the only way
                # forward is to land real evidence and regenerate the draft.
                if approved_by_user:
                    raise ReportCliError(
                        "证据门禁未通过，本节数值不是真实重算，不能人工放行：\n- "
                        + "\n- ".join(gate.violations)
                        + "\n请补齐 queries/*.sql（含 SELECT）与 baseline/data/*"
                        "（本次取数的真实结果，不得用 source.md/draft.md 拷贝或空文件"
                        "充数），重写 draft.md（禁止直接搬运 source.md 数值），反思后"
                        "重新生成，再运行 compare-section。"
                    )
                comparison.passed = False
                comparison.passed_with_review = False
                comparison.review = None
                write_json_atomic(comparison_path, comparison)
                self._move_to_verdict(manifest, section_id, "failed")
                manifest.save(self.manifest_path)
                return manifest, comparison

            reason_text = reason.strip()
            if approved_by_user:
                comparison.passed_with_review = True
                comparison.passed = True
                for difference in comparison.numeric_differences:
                    difference.note = f"经用户确认：{reason_text}"
                comparison.review = ReviewRecord(
                    approved_by_user=True,
                    reason=reason_text,
                    reviewed_at=utc_now(),
                )

            note_parts: list[str] = []
            if notes.strip():
                note_parts.append(notes.strip())
            if approved_by_user:
                note_parts.append(f"[人工放行 {utc_now()}] {reason_text}")
            if note_parts:
                comparison.notes = "\n".join(note_parts)
            if criteria is not None:
                comparison.criteria_analysis = criteria

            write_json_atomic(comparison_path, comparison)
            self._move_to_verdict(
                manifest,
                section_id,
                "approved" if comparison.passed else "failed",
            )
            manifest.save(self.manifest_path)
            return manifest, comparison

    def _refuse_to_discard_a_review(
        self,
        comparison_path: Path,
        *,
        draft_path: Path,
        source_path: Path,
    ) -> None:
        """Keep a recomputation from silently erasing a human override.

        ``compare-section`` overwrites ``comparison.json`` wholesale and the
        reviewer fields live in that same file, so recomputing over an override
        that still describes the current draft would leave the Section approved
        with no record of who accepted which gap — exactly the disclosure the
        override exists to carry. Nothing is lost by refusing: the kernel is
        deterministic, so for unchanged inputs the verdict on disk already is
        the verdict a recompute would produce. Revising the draft first makes
        that verdict stale and lifts the refusal, which is the point — a new
        draft needs a new decision.
        """
        if not comparison_path.is_file():
            return
        try:
            self._reject_stale_comparison(
                comparison_path, draft_path=draft_path, source_path=source_path
            )
        except ReportCliError:
            return
        try:
            payload = json.loads(comparison_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict) or payload.get("review") is None:
            return
        raise ReportCliError(
            f"{self._relative(comparison_path)} 已带有针对当前 draft.md 的人工放行"
            "记录，重新比对会抹掉放行理由与口径披露；且输入未变，重算结果与磁盘上"
            "的一致。若确需重算，请先用 update-section 修订草稿（旧记录随草稿过期），"
            "或用 apply-comparison 重新落定该 Section"
        )

    def _reject_foreign_comparison(
        self,
        comparison: BaseModel,
        *,
        section_id: str,
        path: Path,
    ) -> None:
        """Refuse a result that was not computed from this Section's documents.

        ``comparison.json`` is a plain file, so without this check a copy from
        another Section — or a hand-written one — would approve a Section whose
        numbers were never compared.
        """
        expected_target = self._relative(
            self._section_path(section_id, "baseline/draft.md")
        )
        expected_reference = self._relative(self._section_path(section_id, "source.md"))
        target_path = getattr(comparison, "target_path", None)
        reference_path = getattr(comparison, "reference_path", None)
        if target_path != expected_target or reference_path != expected_reference:
            raise ReportCliError(
                f"{self._relative(path)} 不是本 Section 的比对结果"
                f"（target_path 应为 {expected_target}、reference_path 应为 "
                f"{expected_reference}，实际为 {target_path!r} / "
                f"{reference_path!r}）；请重新运行 compare-section"
            )

    def _reject_stale_comparison(
        self,
        comparison_path: Path,
        *,
        draft_path: Path,
        source_path: Path,
    ) -> None:
        """Refuse a verdict computed before the documents it describes."""
        try:
            verdict_mtime = comparison_path.stat().st_mtime
        except OSError as exc:
            raise ReportCliError(f"无法读取比对结果时间戳：{exc}") from exc
        for label, path in (("复现稿", draft_path), ("Section 原文", source_path)):
            if not path.is_file():
                raise ReportCliError(f"{label}缺失：{self._relative(path)}")
            if path.stat().st_mtime > verdict_mtime:
                raise ReportCliError(
                    f"{label} {self._relative(path)} 在比对之后被修改过，"
                    "比对结果已失效；请重新运行 compare-section"
                )

    @staticmethod
    def _move_to_verdict(
        manifest: ReportManifest,
        section_id: str,
        status: SectionStatus,
    ) -> None:
        """Drive the state machine to a verdict from any starting state."""
        progress = manifest.get(section_id)
        if progress.status == status:
            return
        if progress.status in {"approved", "failed"}:
            manifest.transition(section_id, "running")
        if progress.status == "running" and status in {"approved", "failed"}:
            manifest.transition(section_id, "validating")
        manifest.transition(section_id, status)

    # -- run checks -------------------------------------------------------- #

    def check_run_section(
        self,
        *,
        section_id: str,
        run_period: str,
        business_checks: list[RunCheck] | None = None,
        require_data_evidence: bool = True,
        require_query_evidence: bool = True,
    ) -> SectionRunChecks:
        with self._lock:
            manifest = self.load_manifest()
            manifest.get(section_id)
            run_manifest = self.load_run_manifest(run_period)
            self._ensure_run_recipe_current(run_manifest)
            progress = run_manifest.get(section_id)
            draft_path = self._run_section_path(section_id, run_period, "draft.md")
            draft = (
                draft_path.read_text(encoding="utf-8") if draft_path.is_file() else ""
            )
            data_dir = self._run_section_path(section_id, run_period, "data")
            has_data = data_dir.is_dir() and any(
                path.is_file() and not path.name.startswith(".")
                for path in data_dir.rglob("*")
            )
            query_dir = self._section_path(section_id, "queries")
            has_query_recipe = query_dir.is_dir() and any(query_dir.glob("*.sql"))
            unresolved = sorted(set(_UNRESOLVED_PARAMETER_RE.findall(draft)))
            checks = [
                RunCheck(
                    name="draft_present",
                    passed=bool(draft.strip()),
                    details="目标周期复现稿存在且非空",
                ),
                RunCheck(
                    name="data_evidence_present",
                    passed=has_data or not require_data_evidence,
                    details="运行数据证据可用" if has_data else "运行数据证据缺失",
                ),
                RunCheck(
                    name="query_recipe_present",
                    passed=has_query_recipe or not require_query_evidence,
                    details=(
                        "基线 SQL 模板可用" if has_query_recipe else "基线 SQL 模板缺失"
                    ),
                ),
                RunCheck(
                    name="no_unresolved_parameters",
                    passed=not unresolved,
                    details=(
                        "复现稿中没有未解析的参数占位符"
                        if not unresolved
                        else "未解析的参数占位符：" + ", ".join(unresolved)
                    ),
                ),
                *(business_checks or []),
            ]
            passed = all(check.passed for check in checks)
            result = SectionRunChecks(
                report_id=manifest.report_id,
                period=self._run_period(run_period),
                section_id=section_id,
                passed=passed,
                checks=checks,
                notes=(
                    "跨周期运行检查通过；未执行原报告 GT 数值比较。"
                    if passed
                    else "跨周期运行仍有数据、口径或完整性检查未通过。"
                ),
            )
            write_json_atomic(
                self._run_section_path(section_id, run_period, "run_checks.json"),
                result,
            )
            if progress.status != "checking":
                if progress.status != "running":
                    run_manifest.transition(section_id, "running")
                run_manifest.transition(section_id, "checking")
            run_manifest.transition(section_id, "approved" if passed else "running")
            run_manifest.save(self._run_manifest_path(run_period))
            return result

    # -- recipe, schedule and compilation ---------------------------------- #

    def freeze_recipe(self, *, require_queries: bool = True) -> Path:
        with self._lock:
            manifest = self.load_manifest()
            unfinished = [
                progress.section.section_id
                for progress in manifest.sections
                if progress.status != "approved"
            ]
            if unfinished:
                raise ReportCliError(
                    "基线尚未全部 approved，无法冻结 recipe：" + ", ".join(unfinished)
                )
            recipe = build_recipe(self.product_root, manifest)
            if require_queries and not recipe.queries:
                raise ReportCliError(
                    "没有可冻结的 SQL 模板：请先在各 Section 的 queries/ 目录下"
                    "落地 .sql 文件（如确需空 recipe，请加 --allow-empty-queries）"
                )
            output = self._safe_path("recipe.json")
            write_json_atomic(output, recipe)
            manifest.baseline_frozen_at = utc_now()
            manifest.save(self.manifest_path)
            # recipe.json 与 schedule_descriptor.json 是同一次冻结的两个产物：
            # descriptor 的每个字段都是 recipe + manifest 的纯派生，冻结成功后立即
            # 写出，前端“提炼为定时任务”就能直接读到预填参数，正常无需再单独跑
            # schedule-descriptor（它是只重算 descriptor 的可选刷新入口）。
            self._write_schedule_descriptor(output, manifest)
            return output

    def schedule_descriptor(self) -> dict[str, Any]:
        """(Re)derive schedule_descriptor.json from the existing recipe.json."""
        with self._lock:
            recipe_path = self._safe_path("recipe.json")
            if not recipe_path.is_file():
                recipe_path = self.freeze_recipe()
            manifest = self.load_manifest()
            descriptor = self._write_schedule_descriptor(recipe_path, manifest)
            if descriptor is None:
                raise ReportCliError("recipe.json 中没有可用于周期运行的查询模板")
            return descriptor

    def _write_schedule_descriptor(
        self, recipe_path: Path, manifest: ReportManifest
    ) -> dict[str, Any] | None:
        """Derive schedule_descriptor.json from an already-frozen recipe.

        Every field is a pure function of ``recipe.json`` + ``report_manifest.json``
        (report_id/title from the manifest, recipe_hash = sha256 of the recipe
        bytes, period/parameters inferred from the recipe's parameter names), so
        the descriptor carries nothing that is not already frozen. The cron
        executor validates recipe.json + manifest directly and never reads this
        file; it exists only so the frontend can prefill the "create scheduled
        job" form without re-deriving the cadence itself. Callers must hold
        ``self._lock`` (both freeze_recipe and schedule_descriptor already do).
        An empty recipe frozen via ``--allow-empty-queries`` cannot run on a
        schedule, so no descriptor is written and ``None`` is returned.
        """
        recipe_bytes = recipe_path.read_bytes()
        recipe = json.loads(recipe_bytes)
        if not recipe.get("queries"):
            return None
        descriptor = {
            "report_id": manifest.report_id,
            "title": manifest.title,
            "recipe_hash": hashlib.sha256(recipe_bytes).hexdigest(),
            "period": _infer_period_spec(recipe.get("parameters") or []),
            "parameters": _infer_parameter_templates(recipe.get("parameters") or []),
        }
        write_text_atomic(
            self._safe_path("schedule_descriptor.json"),
            json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n",
        )
        return descriptor

    def compile_report(self, *, run_period: str | None = None) -> dict[str, Any]:
        """Merge the produced Sections into one Markdown report.

        Without ``run_period`` this is the baseline reproduction
        (``report.md``). With it, the target-period drafts are merged into
        ``runs/{period}/report.md``, so a scheduled run leaves its own document
        instead of overwriting the approved baseline.
        """
        with self._lock:
            manifest = self.load_manifest()
            if run_period is None:
                title = manifest.title
                ordered = [
                    (progress.section.section_id, progress.status)
                    for progress in manifest.sections
                ]
                output = self._safe_path("report.md")
            else:
                run_manifest = self.load_run_manifest(run_period)
                self._ensure_run_recipe_current(run_manifest)
                title = f"{manifest.title}（{run_manifest.period}）"
                ordered = [
                    (progress.section_id, progress.status)
                    for progress in run_manifest.sections
                ]
                output = self._safe_path(
                    f"runs/{self._run_period(run_period)}/report.md"
                )

            header = f"# {title}"
            chunks: list[str] = [header]
            sections_index: list[dict[str, Any]] = []
            cursor = len(header)
            for section_id, status in ordered:
                draft_path = (
                    self._section_path(section_id, "baseline/draft.md")
                    if run_period is None
                    else self._run_section_path(section_id, run_period, "draft.md")
                )
                if not draft_path.is_file():
                    continue
                draft = draft_path.read_text(encoding="utf-8").strip()
                if not draft:
                    continue
                # +2 accounts for the "\n\n" that joins this chunk to the header.
                offset = cursor + 2
                sections_index.append(
                    {
                        "section_id": section_id,
                        "title": manifest.get(section_id).section.title,
                        "status": status,
                        "offset": offset,
                        "length": len(draft),
                    }
                )
                chunks.append(draft)
                cursor = offset + len(draft)
            content = "\n\n".join(chunks).rstrip() + "\n"
            if not output.is_file() or output.read_text(encoding="utf-8") != content:
                write_text_atomic(output, content)
            return {
                "path": self._relative(output),
                "character_count": len(content),
                "sections": sections_index,
            }


def _write_draft(path: Path, markdown: str) -> None:
    write_text_atomic(path, markdown.rstrip() + "\n")


def _normalize_manifest_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Rename pre-v3 manifest keys so the strict models can validate them."""
    normalized = dict(payload)
    normalized["schema_version"] = MANIFEST_SCHEMA_VERSION
    sections: list[Any] = []
    for entry in normalized.get("sections") or []:
        if not isinstance(entry, dict):
            sections.append(entry)
            continue
        entry = dict(entry)
        section = dict(entry.get("section") or {})
        if "comparison_path" not in section and "validation_path" in section:
            section["comparison_path"] = section.pop("validation_path")
        entry["section"] = section
        sections.append(entry)
    normalized["sections"] = sections
    return normalized


def _parameter_names(recipe_parameters: list[Any]) -> list[str]:
    return [
        str(item.get("name", "")).strip()
        for item in recipe_parameters
        if isinstance(item, dict)
    ]


def _week_slot_numbers(names: list[str]) -> list[int]:
    return sorted(
        {
            int(match.group(1))
            for name in names
            if (match := _WEEK_SLOT_PARAMETER_RE.fullmatch(name))
        }
    )


def _month_slot_groups(names: list[str]) -> dict[str, list[int]]:
    """Group ``{stem}_N`` month parameters by stem.

    A stem only counts as a window slot sequence when at least two distinct
    indices share it (``ds_month_1``..``ds_month_4``); a lone ``{stem}_1`` is
    ambiguous and falls back to the plain period binding.
    """
    groups: dict[str, set[int]] = {}
    for name in names:
        match = _MONTH_SLOT_PARAMETER_RE.fullmatch(name)
        if not match:
            continue
        stem = match.group(1)
        if "month" not in stem.lower():
            continue
        groups.setdefault(stem, set()).add(int(match.group(2)))
    return {
        stem: sorted(indices) for stem, indices in groups.items() if len(indices) >= 2
    }


def _infer_period_spec(recipe_parameters: list[Any]) -> dict[str, Any]:
    """Infer the cron period shape (unit/offset/window/label) from parameters."""
    names = _parameter_names(recipe_parameters)
    week_slots = _week_slot_numbers(names)
    if week_slots:
        return {
            "unit": "week",
            "offset": -1,
            "window_size": len(week_slots),
            "label_format": "%G-W%V",
        }
    month_groups = _month_slot_groups(names)
    if month_groups:
        return {
            "unit": "month",
            "offset": -1,
            "window_size": max(len(indices) for indices in month_groups.values()),
            "label_format": "%Y%m",
        }
    return {"unit": "month", "offset": -1, "window_size": 1, "label_format": "%Y%m"}


def _infer_parameter_templates(
    recipe_parameters: list[Any],
) -> dict[str, str]:
    """Map recipe parameter names onto the cron period templates they bind to."""
    parameters: dict[str, str] = {}
    names = _parameter_names(recipe_parameters)
    week_slots = _week_slot_numbers(names)
    week_slot_indices = {week: index for index, week in enumerate(week_slots)}
    month_groups = _month_slot_groups(names)
    for item in recipe_parameters:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        lowered = name.lower()
        week_slot_match = _WEEK_SLOT_PARAMETER_RE.fullmatch(name)
        month_slot_match = _MONTH_SLOT_PARAMETER_RE.fullmatch(name)
        if week_slot_match:
            week = int(week_slot_match.group(1))
            slot = week_slot_indices[week]
            suffix = week_slot_match.group(2).lower()
            template = f"${{window_week_{suffix}:{slot}:%Y%m%d}}"
        elif month_slot_match and month_slot_match.group(1) in month_groups:
            slot = month_groups[month_slot_match.group(1)].index(
                int(month_slot_match.group(2))
            )
            template = f"${{window_month:{slot}:%Y%m}}"
        elif "start" in lowered:
            template = "${window_start:%Y%m%d}"
        elif "end" in lowered:
            template = "${period_end:%Y%m%d}"
        elif "month" in lowered or "period" in lowered:
            template = "${period:%Y%m}"
        else:
            template = "${period:%Y%m%d}"
        parameters[name] = template
    return parameters


# --------------------------------------------------------------------------- #
# Source document parsing
# --------------------------------------------------------------------------- #


def _gold_splitter(media_dir: Path | None) -> Any:
    """Build the deterministic heading-aware splitter shipped with this skill.

    Lazy for the same reason as the comparison kernel: only ``parse`` needs it,
    and a missing dependency must be a clear message rather than an ImportError
    traceback that hides which subcommand actually failed. The package sits next
    to this script, so it is resolved through the script directory and never
    depends on how the interpreter was pointed at this file.
    """
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from gold_parser import ReportSectionSplitter
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ReportCliError(
            "parse 子命令需要本 Skill 自带的 scripts/gold_parser 包，当前 Python "
            "环境导入不到它。请确认 Skill 目录完整（该包随 Skill 一起分发，不是 "
            f"PyPI 第三方包），原始导入错误：{exc}"
        ) from exc
    return ReportSectionSplitter(media_dir=media_dir)


def split_report(
    source_path: Path,
    *,
    report_id: str | None = None,
    media_dir: Path | None = None,
) -> Any:
    splitter = _gold_splitter(media_dir)
    report = splitter.split(source_path, report_id=report_id)
    if not report.sections:
        raise ReportCliError(
            "报告中没有可解析的章节；请使用 DOCX、HTML、Markdown 或 TXT，"
            "并确认文档含有标题结构"
        )
    return report


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection bound to an already vetted address.

    Resolving the hostname a second time would open a DNS rebinding window
    between the public-address check and the actual connect, so the checked
    address is pinned and the hostname is only used for SNI and certificate
    validation.
    """

    def __init__(self, hostname: str, address: str, port: int) -> None:
        super().__init__(hostname, port=port, timeout=20)
        self._address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._address, self.port),
            timeout=self.timeout,
        )
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(raw_socket, server_hostname=self.host)


def _resolve_public_host(hostname: str, port: int) -> str:
    addresses: list[str] = []
    for record in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(record[4][0])
        if not address.is_global:
            raise ReportCliError(
                f"报告 URL 必须解析到公网地址，禁止内网或回环地址：{hostname}"
            )
        addresses.append(str(address))
    if not addresses:
        raise ReportCliError(f"报告 URL 域名无法解析：{hostname}")
    return addresses[0]


def _resolve_report_url(url: str) -> tuple[Any, int, str]:
    """Vet a report URL and pin the address it resolves to.

    Returns ``(parsed, port, address)``. Split out of the download for two
    reasons: ``initialize`` can refuse a URL before it creates anything on
    disk, and the address that passed the public-range check is the very
    address that gets dialled — resolving the name a second time would reopen
    the DNS rebinding window this guard exists to close.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ReportCliError("报告 URL 必须使用 http 或 https")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed, port, _resolve_public_host(parsed.hostname, port)


def load_report_url(url: str, destination_dir: Path) -> Path:
    """Download a report over HTTP(S) without following redirects."""
    parsed, port, address = _resolve_report_url(url)
    connection: http.client.HTTPConnection
    if parsed.scheme == "https":
        connection = _PinnedHTTPSConnection(parsed.hostname, address, port)
    else:
        connection = http.client.HTTPConnection(address, port=port, timeout=20)
    request_path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    host_header = parsed.hostname if parsed.port is None else parsed.netloc
    try:
        connection.request(
            "GET",
            request_path,
            headers={
                "Host": host_header,
                "User-Agent": "QwenPawData-Report-Reproduction/0.2",
                "Accept": (
                    "text/html,text/markdown,text/plain,"
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
            },
        )
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ReportCliError("报告 URL 不允许重定向；请直接提供最终地址")
        if response.status < 200 or response.status >= 300:
            raise ReportCliError(f"报告 URL 返回 HTTP {response.status}")
        content_type = response.headers.get_content_type()
        payload = response.read(_MAX_DOWNLOAD_BYTES + 1)
    except (http.client.HTTPException, OSError) as exc:
        raise ReportCliError(f"下载报告失败：{exc}") from exc
    finally:
        connection.close()
    if len(payload) > _MAX_DOWNLOAD_BYTES:
        raise ReportCliError("报告 URL 响应超过 10 MB 上限")

    suffix = Path(parsed.path).suffix.lower() or _CONTENT_SUFFIXES.get(
        content_type, ".html"
    )
    if suffix not in _SOURCE_SUFFIXES:
        raise ReportCliError(f"不支持的报告内容类型：{content_type}")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"original{suffix}"
    destination.write_bytes(payload)
    return destination


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "_", normalized).strip("_")
    return normalized[:80] or "section"


def _render_subsection(subsection: Any) -> str:
    hashes = "#" * max(2, min(subsection.level, 6))
    parts = [f"{hashes} {subsection.title}"]
    if subsection.content.strip():
        parts.append(subsection.content.strip())
    for child in subsection.children:
        rendered = _render_subsection(child)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts).strip()


def _subsection_body(subsection: Any) -> str:
    parts: list[str] = []
    if subsection.content.strip():
        parts.append(subsection.content.strip())
    for child in subsection.children:
        rendered = _render_subsection(child)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts).strip()


def _promote_subsections(parent: Any) -> list[tuple[str, str, int, str]]:
    items: list[tuple[str, str, int, str]] = []
    for subsection in parent.subsections:
        raw_text = _subsection_body(subsection)
        if not raw_text.strip():
            continue
        section_id = (
            f"{parent.section_id}__{subsection.order:02d}_{_slug(subsection.title)}"
        )
        items.append((section_id, subsection.title, subsection.level, raw_text))
    return items


def _manifest_sections(
    parsed_sections: list[Any],
) -> list[tuple[str, str, int, str]]:
    # Header chrome before the first heading (kicker / meta) used to become a
    # synthetic "正文" root and blocked promoting 一、/二、 H2 chapters.
    working = [
        section
        for section in parsed_sections
        if section.title.strip() not in _SYNTHETIC_SECTION_TITLES
    ]
    if not working:
        working = parsed_sections
    if len(working) == 1 and working[0].subsections:
        promoted = _promote_subsections(working[0])
        if promoted:
            return promoted
    return [
        (
            parsed_section.section_id,
            parsed_section.title,
            parsed_section.level,
            parsed_section.raw_text,
        )
        for parsed_section in working
        if parsed_section.raw_text.strip()
    ]


def _validate_source(source_path: Path | None, url: str | None) -> None:
    """Reject an unusable report source before anything is written.

    A refused URL or suffix must leave no trace: an empty product root would
    not trigger the workflow marker, but it is still litter that the next
    ``parse`` would have to reason about.
    """
    if bool(source_path) == bool(url):
        raise ReportCliError("parse 需要且只需要 --file 与 --url 中的一个")
    if url is not None:
        _resolve_report_url(url)
        return
    assert source_path is not None
    if source_path.suffix.lower() not in _SOURCE_SUFFIXES:
        raise ReportCliError(
            "支持的报告格式：DOCX、HTML、Markdown、TXT（.docx/.html/.htm/.md/.txt）"
        )


def parse_document(
    *,
    product_root: Path,
    source_path: Path | None = None,
    url: str | None = None,
    report_id: str | None = None,
    source_period: str | None = None,
    target_period: str | None = None,
) -> ReportManifest:
    """Materialise the source report and split it into reproducible Sections."""
    _validate_source(source_path, url)

    source_dir = product_root / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    if url:
        materialized = load_report_url(url, source_dir)
    else:
        assert source_path is not None
        materialized = source_dir / f"original{source_path.suffix.lower()}"
        if source_path != materialized:
            shutil.copy2(source_path, materialized)

    parsed = split_report(
        materialized,
        report_id=report_id,
        media_dir=source_dir / "media",
    )
    section_progress: list[SectionProgress] = []
    previous_id: str | None = None
    for section_id, title, level, raw_text in _manifest_sections(parsed.sections):
        section_dir = product_root / "sections" / section_id
        section_dir.mkdir(parents=True, exist_ok=True)
        (section_dir / "baseline" / "data").mkdir(parents=True, exist_ok=True)
        source_md = section_dir / "source.md"
        source_md.write_text(
            f"# {title}\n\n{raw_text.strip()}\n",
            encoding="utf-8",
        )
        section_progress.append(
            SectionProgress(
                section=ReportSection(
                    section_id=section_id,
                    title=title,
                    level=level,
                    source_path=str(source_md.relative_to(product_root)),
                    draft_path=str(
                        (section_dir / "baseline" / "draft.md").relative_to(
                            product_root
                        )
                    ),
                    spec_path=str(
                        (section_dir / "spec.json").relative_to(product_root)
                    ),
                    comparison_path=str(
                        (section_dir / "baseline" / COMPARISON_FILE).relative_to(
                            product_root
                        )
                    ),
                    blocked_by=[previous_id] if previous_id else [],
                )
            )
        )
        previous_id = section_id

    return ReportManifest(
        report_id=parsed.report_id,
        title=parsed.title,
        source_path=str(materialized.relative_to(product_root)),
        source_period=source_period,
        target_period=target_period,
        sections=section_progress,
    )


# --------------------------------------------------------------------------- #
# Recipe
# --------------------------------------------------------------------------- #

_PARAMETER_PATTERNS = (
    re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"),
    re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}"),
    re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)\b"),
)

_PERIOD_PARAMETER_NAMES = {"date", "start_date", "end_date", "month", "period"}


def extract_query_parameters(sql: str) -> list[str]:
    """Collect every placeholder name a frozen query still expects."""
    names: set[str] = set()
    for pattern in _PARAMETER_PATTERNS:
        names.update(pattern.findall(sql))
    return sorted(names)


def build_recipe(product_root: Path, manifest: ReportManifest) -> ReportRecipe:
    queries: list[QueryRecipe] = []
    parameter_names: set[str] = set()
    resolved_root = product_root.resolve()
    for progress in manifest.sections:
        section_id = progress.section.section_id
        query_dir = (resolved_root / "sections" / section_id / "queries").resolve()
        if not query_dir.is_relative_to(resolved_root):
            raise ReportCliError(f"查询目录越出报告任务范围：{query_dir}")
        if not query_dir.is_dir():
            continue
        for query_path in sorted(query_dir.glob("*.sql")):
            if not query_path.resolve().is_relative_to(resolved_root):
                raise ReportCliError(f"SQL 产物越出报告任务范围：{query_path}")
            names = extract_query_parameters(
                read_text_file(query_path, label="SQL 模板")
            )
            parameter_names.update(names)
            queries.append(
                QueryRecipe(
                    section_id=section_id,
                    query_path=str(query_path.resolve().relative_to(resolved_root)),
                    parameters=names,
                )
            )
    return ReportRecipe(
        report_id=manifest.report_id,
        parameters=[
            RecipeParameter(
                name=name,
                description="报告周期参数" if name in _PERIOD_PARAMETER_NAMES else "",
            )
            for name in sorted(parameter_names)
        ],
        queries=queries,
        section_order=[progress.section.section_id for progress in manifest.sections],
    )


# --------------------------------------------------------------------------- #
# Invocation context
# --------------------------------------------------------------------------- #

#: How many difference rows a comparison subcommand echoes to stdout. The file
#: keeps the full list; stdout keeps enough for the model to revise the draft.
SUMMARY_DIFFERENCE_LIMIT = 20


class CliContext:
    """Everything a subcommand needs, validated once up front."""

    def __init__(
        self,
        *,
        artifact_dir: Path,
        chat_id: str,
        workspace_root: Path,
    ) -> None:
        self.artifact_dir = artifact_dir
        self.chat_id = chat_id
        self.workspace_root = workspace_root
        self.store = ReportStore(artifact_dir)
        self.guard = SectionTurnGuard(artifact_dir, chat_id=chat_id)

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> CliContext:
        chat_id = (args.chat_id or "").strip()
        if not chat_id:
            raise ReportCliError(
                "缺少 --chat-id：它是 turn 记录的文件名，缺失会导致一个 chat "
                "同时推进多个 Section，逐节人工复核随即失效"
            )
        if len(chat_id) > CHAT_ID_MAX_LENGTH:
            raise ReportCliError(f"--chat-id 长度不得超过 {CHAT_ID_MAX_LENGTH} 个字符")
        if not _CHAT_ID_RE.fullmatch(chat_id):
            raise ReportCliError(
                "--chat-id 只能包含字母、数字、下划线和短横线"
                f"（该值会作为 turns/ 下的文件名），收到：{args.chat_id!r}"
            )

        artifact_dir = Path(args.artifact_dir or "").expanduser()
        if not str(artifact_dir).strip():
            raise ReportCliError("缺少 --artifact-dir")
        artifact_dir = artifact_dir.resolve()
        if artifact_dir.exists() and not artifact_dir.is_dir():
            raise ReportCliError(f"--artifact-dir 不是目录：{artifact_dir}")

        if args.workspace_root:
            workspace_root = Path(args.workspace_root).expanduser().resolve()
        else:
            # artifact_dir is <workspace>/artifacts/<session_id>.
            parents = artifact_dir.parents
            if len(parents) < 2:
                raise ReportCliError(
                    "--artifact-dir 层级不足，无法推断工作区根目录；"
                    "请显式提供 --workspace-root"
                )
            workspace_root = parents[1]
        if not workspace_root.is_dir():
            raise ReportCliError(
                f"工作区根目录不存在：{workspace_root}；"
                "请检查 --artifact-dir 或显式提供 --workspace-root"
            )
        return cls(
            artifact_dir=artifact_dir,
            chat_id=chat_id,
            workspace_root=workspace_root,
        )

    def artifact_relative(self, path: Path) -> str:
        """Express an artifact path relative to ``--artifact-dir``.

        This is the form printed on stdout: it is exactly what the console file
        endpoint and the frontend expect, so the model never has to guess how
        much of the layout to spell out.
        """
        return f"{PRODUCT_DIR}/{self.store._relative(path)}"

    def resolve_input_file(
        self,
        raw: str,
        *,
        label: str,
        suffixes: frozenset[str] | None = None,
    ) -> Path:
        """Resolve an input path, keeping it inside the agent workspace."""
        candidate = Path(raw).expanduser()
        if candidate.is_absolute() and candidate.is_relative_to(Path("/workspace")):
            # The docker backend publishes /workspace paths in the prompt while
            # the real workspace root differs, so remap instead of rejecting.
            candidate = self.workspace_root / candidate.relative_to("/workspace")
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.workspace_root):
            raise ReportCliError(
                f"{label}必须位于 Agent 工作区内（{self.workspace_root}）：{raw}"
            )
        if not resolved.is_file():
            raise ReportCliError(f"{label}不存在：{resolved}")
        if suffixes is not None and resolved.suffix.lower() not in suffixes:
            raise ReportCliError(
                f"{label}格式不支持：{resolved.suffix or '（无扩展名）'}；"
                "允许的扩展名：" + ", ".join(sorted(suffixes))
            )
        return resolved


# --------------------------------------------------------------------------- #
# Stdout payload helpers
# --------------------------------------------------------------------------- #


def _section_summary(progress: SectionProgress) -> dict[str, Any]:
    return {
        "section_id": progress.section.section_id,
        "title": progress.section.title,
        "level": progress.section.level,
        "status": progress.status,
        "attempt": progress.attempt,
        "draft_path": progress.section.draft_path,
        "comparison_path": progress.section.comparison_path,
        "blocked_by": progress.section.blocked_by,
        "error": progress.error,
    }


def _manifest_summary(context: CliContext, manifest: ReportManifest) -> dict[str, Any]:
    next_progress = manifest.next_runnable()
    return {
        "manifest": f"{PRODUCT_DIR}/{MANIFEST_FILE}",
        "report_id": manifest.report_id,
        "title": manifest.title,
        "schema_version": manifest.schema_version,
        "revision": manifest.revision,
        "source_path": f"{PRODUCT_DIR}/{manifest.source_path}",
        "source_period": manifest.source_period,
        "target_period": manifest.target_period,
        "baseline_frozen_at": manifest.baseline_frozen_at,
        "next_runnable_section": (
            next_progress.section.section_id if next_progress else None
        ),
        "sections": [_section_summary(progress) for progress in manifest.sections],
    }


def _run_summary(run_manifest: ReportRunManifest) -> dict[str, Any]:
    return {
        "period": run_manifest.period,
        "run_manifest": f"{PRODUCT_DIR}/runs/{run_manifest.period}/run_manifest.json",
        "revision": run_manifest.revision,
        "baseline_revision": run_manifest.baseline_revision,
        "recipe_hash": run_manifest.recipe_hash,
        "sections": [
            {
                "section_id": progress.section_id,
                "status": progress.status,
                "attempt": progress.attempt,
                "error": progress.error,
            }
            for progress in run_manifest.sections
        ],
    }


def _comparison_summary(comparison: BaseModel) -> dict[str, Any]:
    differences = list(comparison.numeric_differences)
    return {
        "passed": comparison.passed,
        "passed_with_review": comparison.passed_with_review,
        "numeric_coverage": comparison.numeric_coverage,
        "tolerance": comparison.tolerance,
        "numeric_difference_count": len(differences),
        "numeric_differences": [
            difference.model_dump(mode="json")
            for difference in differences[:SUMMARY_DIFFERENCE_LIMIT]
        ],
        "numeric_differences_truncated": len(differences) > SUMMARY_DIFFERENCE_LIMIT,
        "structure_differences": [
            difference.model_dump(mode="json")
            for difference in comparison.structure_differences
        ],
        "criteria_analysis": comparison.criteria_analysis,
        "notes": comparison.notes,
        "review": (
            comparison.review.model_dump(mode="json")
            if getattr(comparison, "review", None) is not None
            else None
        ),
        "evidence_gate": (
            comparison.evidence_gate.model_dump(mode="json")
            if getattr(comparison, "evidence_gate", None) is not None
            else None
        ),
    }


def _parse_json_text(text: str, label: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReportCliError(f"{label} 不是合法 JSON：{exc}") from exc


def _load_json_argument(raw: str, label: str) -> Any:
    """Accept an argument as inline JSON or as a path to a JSON document."""
    text = raw.strip()
    if not text:
        raise ReportCliError(f"{label} 不能为空")
    if text[0] in "[{":
        return _parse_json_text(text, label)
    path = Path(text).expanduser().resolve()
    return _parse_json_text(read_text_file(path, label=label), label)


def _load_criteria(raw: str | None) -> list[dict[str, Any]] | None:
    """Parse ``--criteria`` into the ``criteria_analysis`` payload."""
    if raw is None:
        return None
    payload = _load_json_argument(raw, "--criteria")
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        raise ReportCliError(
            "--criteria 必须是 JSON 对象数组，例如 "
            '[{"criterion":"GMV 口径","met":true,"evidence":"取自 dws_gmv 日汇总"}]'
        )
    return payload


def _load_business_checks(values: list[str]) -> list[RunCheck]:
    checks: list[RunCheck] = []
    for raw in values:
        payload = _load_json_argument(raw, "--business-check")
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            try:
                checks.append(RunCheck.model_validate(item))
            except ValueError as exc:
                raise ReportCliError(
                    "--business-check 每一项必须是 "
                    f'{{"name": str, "passed": bool, "details": str}}：{exc}'
                ) from exc
    return checks


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #


def cmd_parse(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    if bool(args.file) == bool(args.url):
        raise ReportCliError("parse 需要且只需要 --file 与 --url 中的一个")
    source_path = (
        context.resolve_input_file(
            args.file,
            label="--file 指定的报告源文件",
            suffixes=_SOURCE_SUFFIXES,
        )
        if args.file
        else None
    )
    was_initialized = context.store.manifest_path.is_file()
    manifest = context.store.initialize(
        source_path=source_path,
        url=args.url,
        report_id=args.report_id,
        source_period=args.source_period,
        target_period=args.target_period,
        force_reinitialize=args.force_reinitialize,
    )
    if args.force_reinitialize or not was_initialized:
        # Parsing, clarification and planning own this chat: no Section may be
        # advanced here, which is what keeps per-Section review meaningful.
        context.guard.mark_planning()
    logger.info("解析完成，共 %d 个 Section", len(manifest.sections))
    payload = _manifest_summary(context, manifest)
    payload["next"] = "逐节运行 update-section 写入复现稿"
    return payload


def _read_section_content(context: CliContext, args: argparse.Namespace) -> str:
    if bool(args.content) == bool(args.content_file):
        raise ReportCliError("需要且只需要 --content 与 --content-file 中的一个")
    if args.content is not None:
        return args.content
    path = context.resolve_input_file(
        args.content_file, label="--content-file 指定的复现稿"
    )
    return read_text_file(path, label="--content-file 指定的复现稿")


def cmd_update_section(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    content = _read_section_content(context, args)
    context.guard.claim(args.section_id, run_period=args.run_period)
    result = context.store.update_section(
        section_id=args.section_id,
        markdown=content,
        status=args.status,
        run_period=args.run_period,
        reopen_baseline=args.reopen_baseline,
    )

    if args.run_period is None:
        progress = result.get(args.section_id)
        draft_path = context.store._section_path(args.section_id, "baseline/draft.md")
        payload = _manifest_summary(context, result)
        payload.update(
            {
                "scope": "baseline",
                "section_id": args.section_id,
                "status": progress.status,
                "draft_path": context.artifact_relative(draft_path),
                "next": "运行 compare-section 生成机器比对结果",
            }
        )
        return payload

    progress = result.get(args.section_id)
    draft_path = context.store._run_section_path(
        args.section_id, args.run_period, "draft.md"
    )
    return {
        "scope": "run",
        "section_id": args.section_id,
        "status": progress.status,
        "draft_path": context.artifact_relative(draft_path),
        "next": "运行 check-run 做目标周期质量检查",
        "run": _run_summary(result),
    }


def cmd_compare_section(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    context.guard.claim(args.section_id)
    output, comparison = context.store.compare_section(
        section_id=args.section_id, tolerance=args.tolerance
    )
    manifest = context.store.load_manifest()
    gate = getattr(comparison, "evidence_gate", None)
    gate_passed = gate is None or gate.passed
    payload = {
        "section_id": args.section_id,
        "comparison_path": context.artifact_relative(output),
        "status": manifest.get(args.section_id).status,
        # A comparison never moves the state machine: only apply-comparison does,
        # so re-running it can never undo a human approval.
        "status_changed": False,
        "next": (
            "运行 apply-comparison 落定状态（人工放行需 --approved-by-user --reason）"
            if gate_passed
            else "证据门禁未通过：本节数值不是真实重算，不会进入数值验收。请按 "
            "evidence_gate.violations 补齐 queries/*.sql（含 SELECT）与 "
            "baseline/data/*（真实取数结果，不得用 source.md/draft.md 拷贝或空文件"
            "充数），重写 draft.md（禁止直接搬运 source.md 数值），反思后重新生成，"
            "再运行 compare-section；此情形不能人工放行。"
        ),
    }
    payload.update(_comparison_summary(comparison))
    return payload


def cmd_apply_comparison(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    reason = (args.reason or "").strip()
    if args.approved_by_user and not reason:
        raise ReportCliError(
            "--approved-by-user 必须同时提供 --reason，"
            "写清用户接受残差的口径理由（该理由会落盘并可审计）"
        )
    if reason and not args.approved_by_user:
        raise ReportCliError("--reason 只能与 --approved-by-user 一起使用")
    criteria = _load_criteria(args.criteria)

    context.guard.claim(args.section_id)
    manifest, comparison = context.store.apply_comparison(
        section_id=args.section_id,
        approved_by_user=args.approved_by_user,
        reason=reason,
        notes=args.notes or "",
        criteria=criteria,
    )
    progress = manifest.get(args.section_id)
    gate = getattr(comparison, "evidence_gate", None)
    if progress.status == "approved":
        next_step = "继续处理下一个 Section；全部 approved 后运行 freeze-recipe"
    elif gate is not None and not gate.passed:
        next_step = (
            "证据门禁未通过：补齐 queries/*.sql 与 baseline/data/* 真实取数证据、"
            "重写 draft.md（禁止搬运 source.md），重新 compare-section；此情形不能人工放行"
        )
    else:
        next_step = "按比对差异修订复现稿，重新 update-section 后再 compare-section"
    payload = _manifest_summary(context, manifest)
    payload.update(
        {
            "section_id": args.section_id,
            "status": progress.status,
            "comparison_path": context.artifact_relative(
                context.store._comparison_path(args.section_id)
            ),
            "approved_by_user": args.approved_by_user,
            "next": next_step,
        }
    )
    payload.update(_comparison_summary(comparison))
    return payload


def cmd_freeze_recipe(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    context.guard.ensure_no_section_claimed("freeze-recipe")
    output = context.store.freeze_recipe(require_queries=not args.allow_empty_queries)
    recipe = json.loads(output.read_text(encoding="utf-8"))
    manifest = context.store.load_manifest()
    logger.info("已冻结 recipe：%d 条查询模板", len(recipe.get("queries") or []))
    payload: dict[str, Any] = {
        "recipe_path": context.artifact_relative(output),
        "query_count": len(recipe.get("queries") or []),
        "parameters": [item.get("name") for item in recipe.get("parameters") or []],
        "section_order": recipe.get("section_order") or [],
        "baseline_frozen_at": manifest.baseline_frozen_at,
        "revision": manifest.revision,
    }
    descriptor_path = output.parent / "schedule_descriptor.json"
    if descriptor_path.is_file():
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        payload["descriptor_path"] = f"{PRODUCT_DIR}/schedule_descriptor.json"
        payload["recipe_hash"] = descriptor["recipe_hash"]
        payload["period"] = descriptor["period"]
        payload["schedule_parameters"] = descriptor["parameters"]
        payload["next"] = (
            "报告基线已冻结，并已顺带生成 schedule_descriptor.json；"
            "引导用户到会话工作区的定时任务入口据此创建周期任务，"
            "或 prepare-run 建立目标周期运行（无需再单独跑 schedule-descriptor）"
        )
    else:
        payload["next"] = (
            "空 recipe（--allow-empty-queries）不生成定时任务描述符；"
            "如需周期运行，请先在各 Section 落地 SQL 模板后重新 freeze-recipe"
        )
    return payload


def cmd_prepare_run(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    context.guard.ensure_target_run_allowed(args.period)
    run_manifest = context.store.prepare_run(
        args.period, force_reinitialize=args.force_reinitialize
    )
    context.guard.start_target_run(args.period)
    logger.info("已准备目标周期运行：%s", run_manifest.period)
    payload = _run_summary(run_manifest)
    payload["next"] = "逐节运行 update-section --run-period 与 check-run"
    return payload


def cmd_check_run(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    context.guard.claim(args.section_id, run_period=args.period)
    result = context.store.check_run_section(
        section_id=args.section_id,
        run_period=args.period,
        business_checks=_load_business_checks(args.business_check),
        require_data_evidence=not args.skip_data_evidence,
        require_query_evidence=not args.skip_query_evidence,
    )
    run_manifest = context.store.load_run_manifest(args.period)
    return {
        "section_id": args.section_id,
        "period": result.period,
        "run_checks_path": context.artifact_relative(
            context.store._run_section_path(
                args.section_id, args.period, "run_checks.json"
            )
        ),
        "passed": result.passed,
        "checks": [check.model_dump(mode="json") for check in result.checks],
        "notes": result.notes,
        "status": run_manifest.get(args.section_id).status,
        "next": (
            "继续下一个 Section；全部 approved 后运行 compile-report --run-period"
            if result.passed
            else "补齐数据证据或修订复现稿后重新 check-run"
        ),
        "run": _run_summary(run_manifest),
    }


def cmd_compile_report(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    context.guard.ensure_compile_allowed("compile-report", run_period=args.run_period)
    payload = context.store.compile_report(run_period=args.run_period)
    payload["report_path"] = f"{PRODUCT_DIR}/{payload.pop('path')}"
    payload["scope"] = "run" if args.run_period else "baseline"
    logger.info(
        "已合并报告：%s（%d 字符）",
        payload["report_path"],
        payload["character_count"],
    )
    return payload


def cmd_schedule_descriptor(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    context.guard.ensure_no_section_claimed("schedule-descriptor")
    descriptor = context.store.schedule_descriptor()
    logger.info("已重算定时任务描述符：%s", descriptor["recipe_hash"][:12])
    return {
        "descriptor_path": f"{PRODUCT_DIR}/schedule_descriptor.json",
        **descriptor,
        "next": "引导用户在会话工作区中据此创建定时任务",
    }


_SUBCOMMANDS: dict[str, Any] = {
    "parse": cmd_parse,
    "update-section": cmd_update_section,
    "compare-section": cmd_compare_section,
    "apply-comparison": cmd_apply_comparison,
    "freeze-recipe": cmd_freeze_recipe,
    "prepare-run": cmd_prepare_run,
    "check-run": cmd_check_run,
    "compile-report": cmd_compile_report,
    "schedule-descriptor": cmd_schedule_descriptor,
}


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

_EPILOG = """\
典型流程（基线复现 -> 周期运行）：
  1. parse              解析原报告：source/original.*、sections/{id}/source.md、report_manifest.json
  2. update-section     写入某 Section 的复现稿 baseline/draft.md
  3. compare-section    与原 Section 比对数值与结构，写 baseline/comparison.json（不改状态）
  4. apply-comparison   落定该 Section：passed -> approved，否则 failed
  5. 重复 2-4 直到全部 Section approved
  6. freeze-recipe      冻结 SQL 模板为 recipe.json，并记录 baseline_frozen_at
  7. prepare-run        为目标周期建立 runs/{period}/run_manifest.json
  8. update-section --run-period + check-run   逐节生成并检查目标周期报告
  9. compile-report     合并各 Section 为 report.md，stdout 给出偏移索引

铁律：
  * comparison.json 只能由 compare-section 与 apply-comparison 写入，禁止手工编辑；
  * 人工放行只能用 apply-comparison --approved-by-user --reason "<用户口径>"，
    且必须在 ask_user_question 得到用户明确接受之后；能通过修订消除的差异一律不得放行；
  * 一个 chat 只推进一个 Section（或一个目标周期）；--chat-id 取自环境说明中的当前 chat_id。
"""


def _add_section_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--section-id",
        required=True,
        help="report_manifest.json 中的 section_id",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/report_cli.py",
        description=(
            "报告复现工作流的唯一入口：产物落盘与 Section 状态机全部由本脚本负责，"
            "结构化结果输出到 stdout，日志输出到 stderr。"
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--artifact-dir",
        required=True,
        help="本会话的产物目录（环境说明中的 artifact_dir），所有写入都被限制在其中",
    )
    parser.add_argument(
        "--chat-id",
        required=True,
        help=(
            "当前 chat_id（环境说明中给出）；用于 turn 记录，"
            f"只允许字母、数字、下划线、短横线，最长 {CHAT_ID_MAX_LENGTH} 字符"
        ),
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="Agent 工作区根目录，缺省由 --artifact-dir 上溯两级推断",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="info",
        help="日志级别，日志一律走 stderr（默认 %(default)s）",
    )

    subparsers = parser.add_subparsers(
        dest="subcommand", required=True, metavar="<subcommand>"
    )

    p_parse = subparsers.add_parser(
        "parse",
        help="解析原报告并初始化 report_manifest.json",
        description="解析 DOCX/HTML/Markdown/TXT 报告，切分为稳定 Section 并建立任务状态。",
    )
    p_parse.add_argument("--file", default=None, help="本地报告文件路径（工作区内）")
    p_parse.add_argument(
        "--url", default=None, help="报告 URL（http/https，禁止重定向与内网地址）"
    )
    p_parse.add_argument("--report-id", default=None, help="自定义 report_id")
    p_parse.add_argument("--source-period", default=None, help="原报告所属周期")
    p_parse.add_argument("--target-period", default=None, help="本次复现的目标周期")
    p_parse.add_argument(
        "--force-reinitialize",
        action="store_true",
        help="清空并重建 report-reproduction/ 目录（会丢弃已有产物）",
    )

    p_update = subparsers.add_parser(
        "update-section",
        help="原子写入一个 Section 的复现稿",
        description="写入 baseline/draft.md（或目标周期的 draft.md）并推进状态机。",
    )
    _add_section_argument(p_update)
    p_update.add_argument("--content", default=None, help="复现稿 Markdown 正文")
    p_update.add_argument(
        "--content-file", default=None, help="复现稿文件路径（与 --content 二选一）"
    )
    p_update.add_argument(
        "--status",
        choices=["validating", "needs_input"],
        default="validating",
        help="写入后的状态：validating 待比对，needs_input 仍需补充信息（默认 %(default)s）",
    )
    p_update.add_argument(
        "--run-period", default=None, help="目标周期；给出后写入运行态而非基线"
    )
    p_update.add_argument(
        "--reopen-baseline",
        action="store_true",
        help="基线已冻结时显式解冻并修订（仅基线有效）",
    )

    p_compare = subparsers.add_parser(
        "compare-section",
        help="比对复现稿与原 Section，只写结果不改状态",
        description=(
            "调用 host 通用比对内核计算数值与结构差异，写入 baseline/comparison.json；"
            "不修改 manifest 状态，也不读取上一次结果。"
        ),
    )
    _add_section_argument(p_compare)
    p_compare.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help="数值相对误差容差，取值 0 到 1（默认 %(default)s）",
    )

    p_apply = subparsers.add_parser(
        "apply-comparison",
        help="把比对结果落定为 Section 状态（人工字段的唯一写入方）",
        description=(
            "读取 baseline/comparison.json，写入 notes / 逐条 note / criteria_analysis "
            "与人工放行记录，并推进状态：passed -> approved，否则 failed。"
        ),
    )
    _add_section_argument(p_apply)
    p_apply.add_argument(
        "--approved-by-user",
        action="store_true",
        help=(
            "用户已明确接受当前残差；必须同时提供 --reason，"
            "且只能在 ask_user_question 得到用户接受之后使用。"
            "证据门禁未通过（数值非真实重算）时本参数会被拒绝——那种情况只能补证据重算"
        ),
    )
    p_apply.add_argument(
        "--reason", default=None, help="用户放行的口径理由，会写入每条差异的 note"
    )
    p_apply.add_argument(
        "--notes",
        default=None,
        help="写入 comparison.json 顶层 notes（前端原样换行显示）",
    )
    p_apply.add_argument(
        "--criteria",
        default=None,
        help="逐指标口径判定，JSON 对象数组或其文件路径，写入 criteria_analysis",
    )

    p_freeze = subparsers.add_parser(
        "freeze-recipe",
        help="冻结已验收 Section 的 SQL 模板为 recipe.json",
        description="扫描 sections/*/queries/*.sql，抽取参数占位符并记录 baseline_frozen_at。",
    )
    p_freeze.add_argument(
        "--allow-empty-queries",
        action="store_true",
        help="允许冻结不含任何查询模板的 recipe（默认拒绝）",
    )

    p_prepare = subparsers.add_parser(
        "prepare-run",
        help="从已冻结基线建立目标周期运行状态",
        description="要求全部 Section approved 且 recipe.json 含查询模板；绝不覆盖基线产物。",
    )
    p_prepare.add_argument("--period", required=True, help="目标周期标识，如 202609")
    p_prepare.add_argument(
        "--force-reinitialize",
        action="store_true",
        help="清空并重建该周期的运行目录",
    )

    p_check = subparsers.add_parser(
        "check-run",
        help="对目标周期 Section 做非 GT 质量检查",
        description="检查复现稿、数据证据、SQL 模板、未解析参数与周标签时效性，写 run_checks.json。",
    )
    _add_section_argument(p_check)
    p_check.add_argument("--period", required=True, help="目标周期标识")
    p_check.add_argument(
        "--business-check",
        action="append",
        default=[],
        help=(
            "追加业务检查，可重复；JSON 对象或其文件路径，形如 "
            '{"name":"口径一致","passed":true,"details":"..."}'
        ),
    )
    p_check.add_argument(
        "--skip-data-evidence",
        action="store_true",
        help="不把缺少运行数据证据判为失败（默认要求）",
    )
    p_check.add_argument(
        "--skip-query-evidence",
        action="store_true",
        help="不把缺少基线 SQL 模板判为失败（默认要求）",
    )

    p_compile = subparsers.add_parser(
        "compile-report",
        help="合并各 Section 复现稿为 report.md",
        description="stdout 返回每个 Section 在合并文档中的偏移索引，便于定位与展示。",
    )
    p_compile.add_argument(
        "--run-period",
        default=None,
        help="目标周期；给出后合并运行态复现稿并写入 runs/{period}/report.md",
    )

    subparsers.add_parser(
        "schedule-descriptor",
        help="（可选）按现有 recipe 重算定时任务描述符 schedule_descriptor.json；freeze-recipe 已顺带生成",
        description="只按现有 recipe.json 重新派生周期参数模板供前端预填，不重新冻结、不改 baseline_frozen_at（recipe 尚未冻结时才先冻结）；freeze-recipe 冻结时已顺带写出，通常无需单独运行。",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and print exactly one JSON object to stdout."""
    args = build_parser().parse_args(argv)
    logger = setup_logging(args.log_level)
    try:
        context = CliContext.from_args(args)
        payload = _SUBCOMMANDS[args.subcommand](context, args, logger)
    except ReportCliError as exc:
        fail(logger, str(exc))
    except Exception as exc:  # noqa: BLE001 - the caller is a model, not a human
        logger.debug("未处理异常", exc_info=True)
        fail(logger, f"{type(exc).__name__}: {exc}")
    emit({"ok": True, "subcommand": args.subcommand, **payload})
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
