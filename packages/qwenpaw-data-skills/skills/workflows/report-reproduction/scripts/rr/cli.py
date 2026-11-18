# -*- coding: utf-8 -*-
"""Argument parsing and subcommand dispatch.

New failures cite the rule they enforce (R1–R8). Existing messages stay
stable so callers can keep matching them.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from rr.common import (
    CHAT_ID_MAX_LENGTH,
    DEFAULT_TOLERANCE,
    EXIT_OK,
    MANIFEST_FILE,
    PRODUCT_DIR,
    ReportCliError,
    _CHAT_ID_RE,
    _SOURCE_SUFFIXES,
    emit,
    fail,
    read_text_file,
    setup_logging,
)
from rr.guard import SectionTurnGuard
from rr.models import ReportManifest, ReportRunManifest, RunCheck, SectionProgress
from rr.store import ReportStore

SUMMARY_DIFFERENCE_LIMIT = 20


class CliContext:
    """Everything a subcommand needs, validated once up front."""

    def __init__(
        self,
        *,
        artifact_dir: Path,
        chat_id: str,
        workspace_root: Path,
        reference_dir: Path | None = None,
        work_dir: Path | None = None,
    ) -> None:
        self.artifact_dir = artifact_dir
        self.chat_id = chat_id
        self.workspace_root = workspace_root
        self.cron = reference_dir is not None
        if self.cron:
            assert work_dir is not None
            self.store = ReportStore(
                artifact_dir, read_root=reference_dir, write_root=work_dir
            )
            self.guard = SectionTurnGuard(
                artifact_dir, chat_id=chat_id, turn_root=work_dir
            )
        else:
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
        reference_raw = getattr(args, "reference_dir", None)
        work_raw = getattr(args, "work_dir", None)
        if args.subcommand == "install-reference":
            if reference_raw:
                raise ReportCliError(
                    "install-reference 不接受 --reference-dir；"
                    "它按 recipe_hash 从上传目录解出参考"
                )
            return cls(
                artifact_dir=artifact_dir,
                chat_id=chat_id,
                workspace_root=workspace_root,
            )
        if bool(reference_raw) != bool(work_raw):
            raise ReportCliError(
                "【R5 产物归属】定时模式必须同时提供 --reference-dir 与 --work-dir"
            )
        reference_dir = None
        work_dir = None
        if reference_raw:
            reference_dir = _resolve_cron_path(reference_raw, artifact_dir)
            work_dir = _resolve_cron_path(work_raw, artifact_dir)
            if not reference_dir.is_dir():
                raise ReportCliError(f"--reference-dir 不是目录：{reference_dir}")
            work_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            artifact_dir=artifact_dir,
            chat_id=chat_id,
            workspace_root=workspace_root,
            reference_dir=reference_dir,
            work_dir=work_dir,
        )

    def artifact_relative(self, path: Path) -> str:
        """Express an artifact path relative to ``--artifact-dir``.

        This is the form printed on stdout: it is exactly what the console file
        endpoint and the frontend expect, so the model never has to guess how
        much of the layout to spell out.
        """
        relative = self.store._relative(path)
        if self.cron and self.store._is_write_path(relative):
            return relative
        return f"{PRODUCT_DIR}/{relative}"

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
            "继续下一个 Section；全部 approved 后用 bi-report-generation 写出 "
            "runs/<期次>/report.html，再运行 finish-run"
            if result.passed
            else "补齐数据证据或修订复现稿后重新 check-run"
        ),
        "run": _run_summary(run_manifest),
    }


def cmd_compile_report(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    raise ReportCliError(
        "compile-report 已移除。基线状态请用 status；"
        "周期 HTML 由 bi-report-generation 写出，交付请用 finish-run"
    )


def cmd_schedule_descriptor(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    raise ReportCliError(
        "schedule-descriptor 已移除。描述符由 freeze-recipe 写入 schedule_descriptor.json"
    )


def cmd_status(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    payload = context.store.status_report(period=getattr(args, "period", None))
    logger.info("已读取任务状态：%s", payload.get("mode"))
    return payload


def cmd_plan_run(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    payload = context.store.plan_run(args.run_date)
    logger.info("plan-run %s %s", payload["status"], payload["period"])
    return payload


def cmd_finish_run(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    context.guard.ensure_target_run_allowed(args.period)
    payload = context.store.finish_run(args.period)
    logger.info("已交付周期 %s", payload["period"])
    return payload


def cmd_export_reference(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    from rr.export import export_reference

    context.guard.ensure_no_section_claimed("export-reference")
    manifest = context.store.load_manifest()
    out_dir = _resolve_export_dir(context, args.out)
    period = args.baseline_period or manifest.source_period or ""
    period = context.store._run_period(period)
    payload = export_reference(
        context.store.product_root,
        manifest,
        out_dir=out_dir,
        note=args.note,
        baseline_period=period,
        artifact_dir=context.artifact_dir,
    )
    logger.info("已导出参考：%s", payload["reference_dir"])
    return payload


def cmd_diff_run(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    from rr.export import diff_run

    base_dir = _resolve_existing_dir(context, args.base_work_dir, "--base-work-dir")
    candidate_dir = _resolve_existing_dir(
        context, args.candidate_work_dir, "--candidate-work-dir"
    )
    payload = diff_run(
        period=context.store._run_period(args.period),
        base_dir=base_dir,
        candidate_dir=candidate_dir,
    )
    payload["diff_path"] = str(candidate_dir / "diff.json")
    payload["next"] = "把 diff.json 交给用户确认后，再发布新的参考"
    logger.info("已比较周期 %s 的两份工作空间", args.period)
    return payload


def cmd_install_reference(
    context: CliContext, args: argparse.Namespace, logger: logging.Logger
) -> dict[str, Any]:
    from rr.export import install_reference

    raw_work = args.install_work_dir or args.work_dir
    if not raw_work:
        raise ReportCliError("install-reference 必须提供 --work-dir")
    work_dir = _resolve_cron_path(raw_work, context.artifact_dir)
    root = context.workspace_root.resolve()
    if not work_dir.is_relative_to(root):
        raise ReportCliError(f"--work-dir 必须位于 Agent 工作区内：{raw_work}")
    work_dir.mkdir(parents=True, exist_ok=True)
    bundle = _resolve_bundle(context, args.bundle) if args.bundle else None
    payload = install_reference(
        artifact_dir=context.artifact_dir,
        workspace_root=context.workspace_root,
        work_dir=work_dir,
        recipe_hash=args.recipe_hash,
        bundle=bundle,
    )
    logger.info("已安装参考：%s", payload["reference_dir"])
    return payload


def _resolve_cron_path(raw: str, artifact_dir: Path) -> Path:
    """Resolve a cron path. Relative values start at ``--artifact-dir``."""
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = artifact_dir / candidate
    resolved = candidate.resolve()
    if resolved == artifact_dir or not resolved.is_relative_to(artifact_dir):
        raise ReportCliError(f"路径必须位于本会话产物目录内：{raw}")
    return resolved


def _resolve_export_dir(context: CliContext, raw: str | None) -> Path:
    from rr.export import EXPORT_DIR_NAME

    if raw:
        resolved = _resolve_cron_path(raw, context.artifact_dir)
    else:
        resolved = (context.artifact_dir / EXPORT_DIR_NAME).resolve()
    root = context.artifact_dir.resolve()
    if not resolved.is_relative_to(root):
        raise ReportCliError(f"--out 必须位于本会话产物目录内：{raw}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _resolve_bundle(context: CliContext, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        uploads = (
            context.workspace_root / "uploads" / context.artifact_dir.name / candidate
        )
        under_artifact = context.artifact_dir / candidate
        candidate = uploads if uploads.is_file() else under_artifact
    resolved = candidate.resolve()
    uploads = context.workspace_root / "uploads" / context.artifact_dir.name
    if not resolved.is_file() or not (resolved.is_relative_to(uploads) or resolved.is_relative_to(context.artifact_dir)):
        raise ReportCliError(f"--bundle 不是工作区内的文件：{raw}")
    return resolved


def _resolve_existing_dir(context: CliContext, raw: str, label: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = context.workspace_root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(context.workspace_root):
        raise ReportCliError(f"{label} 必须位于 Agent 工作区内：{raw}")
    if not resolved.is_dir():
        raise ReportCliError(f"{label} 不是目录：{resolved}")
    return resolved


_CRON_COMMANDS = frozenset(
    {
        "install-reference",
        "plan-run",
        "prepare-run",
        "update-section",
        "check-run",
        "finish-run",
        "status",
    }
)


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
    "status": cmd_status,
    "plan-run": cmd_plan_run,
    "finish-run": cmd_finish_run,
    "export-reference": cmd_export_reference,
    "install-reference": cmd_install_reference,
    "diff-run": cmd_diff_run,
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
  9. export-reference   导出 reference/、seed/，以及可挂到定时任务的 rr-bundle-*.zip
  10. 定时运行先 install-reference，再带 --reference-dir 与 --work-dir：
      plan-run、prepare-run、finish-run

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
    parser.add_argument(
        "--reference-dir",
        default=None,
        help="定时模式的只读参考目录；必须与 --work-dir 同时提供",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="定时模式的可写工作空间；必须与 --reference-dir 同时提供",
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
        help="已移除",
        description="该命令已移除。请使用 status 查看基线，使用 finish-run 交付周期。",
    )
    p_compile.add_argument(
        "--run-period",
        default=None,
        help="已忽略",
    )

    subparsers.add_parser(
        "schedule-descriptor",
        help="已移除；描述符由 freeze-recipe 写入",
        description="该命令已移除。freeze-recipe 会写出 schedule_descriptor.json。",
    )

    p_status = subparsers.add_parser(
        "status",
        help="只读查看基线或当前周期的状态",
        description="基线模式列出每节的状态、证据来源和放行理由；定时模式列出周期章节与检查结果。",
    )
    p_status.add_argument("--period", default=None, help="定时模式下要查看的期次")

    p_plan = subparsers.add_parser(
        "plan-run",
        help="按运行日和 STATE.json 决定本期是否执行",
        description="读取运行配方的 schedule 与工作空间 STATE.json，输出 OK 或 STOP。",
    )
    p_plan.add_argument("--run-date", required=True, help="运行日，YYYY-MM-DD")

    p_finish = subparsers.add_parser(
        "finish-run",
        help="在全部章节通过且 HTML 已生成后交付本期",
        description="复制 report.html 到会话产物目录和 archive/，并追加 STATE.json。",
    )
    p_finish.add_argument("--period", required=True, help="要交付的期次")

    p_export = subparsers.add_parser(
        "export-reference",
        help="导出下一次定时运行所需的参考目录和种子",
        description="只写出参考与种子里下一次运行会读取的文件，并生成中文定时指令。",
    )
    p_export.add_argument(
        "--out",
        default=None,
        help=(
            "输出目录，必须位于本会话产物目录内；"
            "缺省为 report-reproduction-export/。其下生成 reference/、seed/ 和参考包"
        ),
    )
    p_export.add_argument("--note", required=True, help="这一版参考的中文说明")
    p_export.add_argument(
        "--baseline-period",
        default=None,
        help="写入 STATE.json 的基线期次；缺省用 manifest 的 source_period",
    )

    p_install = subparsers.add_parser(
        "install-reference",
        help="把定时任务附件里的参考包解到工作目录",
        description=(
            "在上传目录中按 recipe_hash 找到参考包，解出只读参考；"
            "工作目录还没有 STATE.json 时才写入种子。"
        ),
    )
    p_install.add_argument(
        "--recipe-hash",
        required=True,
        help="参考包 REFERENCE.json 里的 recipe_hash",
    )
    p_install.add_argument(
        "--work-dir",
        dest="install_work_dir",
        default=None,
        help="可写工作目录；相对路径相对于 --artifact-dir",
    )
    p_install.add_argument(
        "--bundle",
        default=None,
        help="指定参考包；缺省时在 uploads/<会话>/ 里按 recipe_hash 查找",
    )

    p_diff = subparsers.add_parser(
        "diff-run",
        help="比较模板修订试跑与当前工作空间的同一期次",
        description="按章节复用比对内核，把变化写到候选工作空间的 diff.json。",
    )
    p_diff.add_argument("--period", required=True, help="要比较的期次")
    p_diff.add_argument("--base-work-dir", required=True, help="当前工作空间")
    p_diff.add_argument("--candidate-work-dir", required=True, help="试跑工作空间")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and print exactly one JSON object to stdout."""
    args = build_parser().parse_args(argv)
    logger = setup_logging(args.log_level)
    try:
        context = CliContext.from_args(args)
        if context.cron and args.subcommand not in _CRON_COMMANDS:
            raise ReportCliError(
                "【R5 产物归属】定时模式只允许 install-reference、plan-run、"
                "prepare-run、update-section、check-run、finish-run、status"
            )
        if context.cron and args.subcommand == "update-section" and not args.run_period:
            raise ReportCliError(
                "【R4 一个 chat 一个章节或一个周期】定时模式的 update-section "
                "必须带 --run-period"
            )
        if context.cron and args.subcommand != "install-reference":
            context.store.verify_reference()
        payload = _SUBCOMMANDS[args.subcommand](context, args, logger)
    except ReportCliError as exc:
        fail(logger, str(exc))
    except Exception as exc:  # noqa: BLE001 - the caller is a model, not a human
        logger.debug("未处理异常", exc_info=True)
        fail(logger, f"{type(exc).__name__}: {exc}")
    result = dict(payload)
    emit(
        {
            "ok": True,
            "subcommand": args.subcommand,
            "command": args.subcommand,
            **result,
            "result": result,
            "next": result.get("next") or "",
        }
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
