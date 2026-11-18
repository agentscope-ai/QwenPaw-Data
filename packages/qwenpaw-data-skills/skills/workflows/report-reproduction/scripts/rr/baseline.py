# -*- coding: utf-8 -*-
"""Baseline drafts, the evidence gate and human release.

Enforces R1 (numbers come from a real query), R2 (only the user releases a gap)
and R5 (comparison.json is written only here).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from rr.common import (
    COMPARISON_FILE,
    DEFAULT_TOLERANCE,
    IRREPRODUCIBLE_TAG,
    PROVENANCE_RECOMPUTED,
    PROVENANCE_REFERENCE,
    ReportCliError,
    _SQL_STATEMENT_RE,
    _file_has_content,
    _normalized_digest,
    read_text_file,
    utc_now,
    write_json_atomic,
    write_text_atomic,
)
from rr.models import (
    EvidenceGate,
    ReportManifest,
    ReportRunManifest,
    ReviewRecord,
    SectionStatus,
)

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
    # The entry script re-exports this cache so tests can reset it.
    entry = sys.modules.get("_report_cli_under_test")
    if entry is not None and getattr(entry, "_KERNEL", _KERNEL) is None:
        _KERNEL = None
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
            "注意：`qwenpaw_data` 是本仓库自带的命名空间包（由 qwenpaw-data-host-core 提供），"
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
    if entry is not None:
        entry._KERNEL = _KERNEL
    return _KERNEL


def _write_draft(path: Path, markdown: str) -> None:
    write_text_atomic(path, markdown.rstrip() + "\n")


class BaselineMixin:
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
                if self.cron:
                    self.verify_reference()
                    blocked = set(self.load_run_recipe().get(section_id).blocked_by)
                else:
                    blocked = set(
                        self.load_manifest().get(section_id).section.blocked_by
                    )
                run_manifest = self.load_run_manifest(run_period)
                self._ensure_run_recipe_current(run_manifest)
                progress = run_manifest.get(section_id)
                approved = {
                    item.section_id
                    for item in run_manifest.sections
                    if item.status == "approved"
                }
                blocked_by = blocked - approved
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
