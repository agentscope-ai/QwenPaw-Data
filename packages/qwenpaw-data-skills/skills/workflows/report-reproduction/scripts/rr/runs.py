# -*- coding: utf-8 -*-
"""Target-period runs: prepare, check, plan and finish.

Enforces R4 for a period chat and, in cron mode, R1's scheduled form
(rendered SQL, acceptance items, no leftover masks).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from rr.common import (
    ReportCliError,
    _UNRESOLVED_PARAMETER_RE,
    write_json_atomic,
    write_text_atomic,
)
from rr.models import ReportRunManifest, RunCheck, RunSectionProgress, SectionRunChecks
from rr.schedule import (
    period_for_run_date,
    period_steps,
    render_bindings,
    render_sql,
)


class RunsMixin:
    def prepare_run(
        self,
        period: str,
        *,
        force_reinitialize: bool = False,
    ) -> ReportRunManifest:
        if self.cron:
            return self._prepare_cron_run(period, force_reinitialize=force_reinitialize)
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
        if self.cron:
            self.verify_reference()
            recipe_path = self.read_root / "recipe.json"
            current_hash = hashlib.sha256(recipe_path.read_bytes()).hexdigest()
            if current_hash != run_manifest.recipe_hash:
                raise ReportCliError(
                    "【R5 产物归属】该运行准备完成后参考 recipe 发生了变化；"
                    "请显式使用 prepare-run --force-reinitialize 重建运行"
                )
            return
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
            if self.cron:
                self.verify_reference()
                recipe = self.load_run_recipe()
                recipe.get(section_id)
                report_id = recipe.report_id
            else:
                manifest = self.load_manifest()
                manifest.get(section_id)
                report_id = manifest.report_id
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
            if self.cron:
                checks.extend(
                    self._cron_run_checks(
                        section_id=section_id,
                        run_period=run_period,
                        draft=draft,
                        business_checks=business_checks or [],
                    )
                )
            passed = all(check.passed for check in checks)
            result = SectionRunChecks(
                report_id=report_id,
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

    def _cron_run_checks(
        self,
        *,
        section_id: str,
        run_period: str,
        draft: str,
        business_checks: list[RunCheck],
    ) -> list[RunCheck]:
        spec_path = self._section_path(section_id, "spec.json")
        spec: dict[str, Any] = {}
        if spec_path.is_file():
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        criteria = spec.get("success_criteria") or []
        criterion_ids = [
            str(item.get("id"))
            for item in criteria
            if isinstance(item, dict) and item.get("id")
        ]
        provided = {check.name for check in business_checks}
        missing = [item_id for item_id in criterion_ids if item_id not in provided]
        rendered_dir = self._run_section_path(section_id, run_period, "queries")
        recipe = self.load_run_recipe()
        expected = [query for query in recipe.queries if query.section_id == section_id]
        rendered_ok = True
        rendered_details = "已渲染 SQL 存在且占位符已替换"
        if not expected:
            rendered_details = "本节没有 SQL 模板"
        else:
            problems: list[str] = []
            for query in expected:
                name = Path(query.query_path).name
                path = rendered_dir / name
                if not path.is_file() or not path.read_text(encoding="utf-8").strip():
                    problems.append(f"{name} 缺失")
                    continue
                leftover = sorted(
                    set(
                        _UNRESOLVED_PARAMETER_RE.findall(
                            path.read_text(encoding="utf-8")
                        )
                    )
                )
                if leftover:
                    problems.append(f"{name} 仍有占位符：" + ", ".join(leftover))
            rendered_ok = not problems
            if problems:
                rendered_details = "；".join(problems)
        provenances = [
            str(item.get("provenance"))
            for item in spec.get("metric_definitions") or []
            if isinstance(item, dict) and item.get("provenance")
        ]
        extra = [
            RunCheck(
                name="criteria_covered",
                passed=not missing,
                details=(
                    "验收项均已检查"
                    if not missing
                    else "未覆盖的验收项：" + ", ".join(missing)
                ),
            ),
            RunCheck(
                name="no_mask_left",
                passed="〔数〕" not in draft,
                details=(
                    "草稿中没有残留的〔数〕"
                    if "〔数〕" not in draft
                    else "【R6 草稿正文镜像原报告】草稿仍含〔数〕，须替换为本期数值"
                ),
            ),
            RunCheck(
                name="rendered_sql_present",
                passed=rendered_ok,
                details=rendered_details,
            ),
        ]
        if not any(check.name == "口径来源披露" for check in business_checks):
            absent = [item for item in provenances if item not in draft]
            extra.append(
                RunCheck(
                    name="口径来源披露",
                    passed=not absent,
                    details=(
                        "已按冻结口径披露：" + "、".join(provenances)
                        if provenances and not absent
                        else (
                            "本节没有需要披露的冻结口径"
                            if not provenances
                            else "【R3 口径必须标来源】草稿未披露：" + "、".join(absent)
                        )
                    ),
                )
            )
        return extra

    def _prepare_cron_run(
        self,
        period: str,
        *,
        force_reinitialize: bool = False,
    ) -> ReportRunManifest:
        with self._lock:
            reference = self.verify_reference()
            recipe = self.load_run_recipe()
            normalized_period = self._run_period(period)
            bindings = render_bindings(
                normalized_period,
                recipe.schedule.period.model_dump(),
                recipe.schedule.parameters,
            )
            run_manifest_path = self._run_manifest_path(normalized_period)
            if run_manifest_path.is_file() and not force_reinitialize:
                existing_run = ReportRunManifest.load(run_manifest_path)
                self._ensure_run_recipe_current(existing_run)
                return existing_run
            if force_reinitialize and run_manifest_path.parent.exists():
                shutil.rmtree(run_manifest_path.parent)
            self._render_queries(recipe, normalized_period, bindings)
            state = self._read_state()
            params = {
                "period": normalized_period,
                "prev_period": state.get("last_period"),
                "bindings": bindings,
            }
            write_text_atomic(
                self._safe_path(f"runs/{normalized_period}/params.json"),
                json.dumps(params, ensure_ascii=False, indent=2) + "\n",
            )
            run_manifest = ReportRunManifest(
                report_id=recipe.report_id,
                period=normalized_period,
                baseline_revision=0,
                recipe_hash=str(reference["recipe_hash"]),
                sections=[
                    RunSectionProgress(section_id=section.section_id)
                    for section in recipe.sections
                ],
            )
            for section in recipe.sections:
                self._run_section_path(
                    section.section_id, normalized_period, "data"
                ).mkdir(parents=True, exist_ok=True)
            run_manifest.save(run_manifest_path)
            return run_manifest

    def _render_queries(
        self, recipe: Any, period: str, bindings: dict[str, str]
    ) -> None:
        for query in recipe.queries:
            source = (self.read_root / query.query_path).resolve()
            if not source.is_file() or not source.is_relative_to(
                self.read_root.resolve()
            ):
                raise ReportCliError(
                    f"【R5 产物归属】参考 SQL 缺失：{query.query_path}"
                )
            rendered = render_sql(source.read_text(encoding="utf-8"), bindings)
            leftover = sorted(set(_UNRESOLVED_PARAMETER_RE.findall(rendered)))
            if leftover:
                raise ReportCliError(
                    "【R1 证据门禁】SQL 模板仍有未绑定的参数：" + ", ".join(leftover)
                )
            destination = self._run_section_path(
                query.section_id, period, f"queries/{Path(query.query_path).name}"
            )
            write_text_atomic(destination, rendered)

    def plan_run(self, run_date: str) -> dict[str, Any]:
        from rr.schedule import parse_run_date

        with self._lock:
            self.verify_reference()
            recipe = self.load_run_recipe()
            moment = parse_run_date(run_date)
            period_spec = recipe.schedule.period.model_dump()
            period = period_for_run_date(moment, period_spec)
            bindings = render_bindings(period, period_spec, recipe.schedule.parameters)
            state = self._read_state()
            last = str(state.get("last_period") or "")
            history = state.get("history") or []
            delivered = {
                str(item.get("period"))
                for item in history
                if isinstance(item, dict) and item.get("status") == "delivered"
            }
            status = "OK"
            reason = ""
            message = f"本期可执行：{period}"
            prev = last or None
            if period in delivered:
                status = "STOP"
                reason = "already_delivered"
                message = f"本期 {period} 已交付，不再重复执行"
            elif last:
                steps = period_steps(last, period, recipe.schedule.period.unit)
                if steps < 0:
                    status = "STOP"
                    reason = "state_ahead"
                    message = f"状态中的最近期次 {last} 晚于本期 {period}，请核对运行日"
                elif steps > 1:
                    status = "STOP"
                    reason = "gap"
                    message = (
                        f"状态停在 {last}，与本期 {period} 之间有缺口，请先补齐缺失期次"
                    )
                elif steps == 0 and last in delivered:
                    status = "STOP"
                    reason = "already_delivered"
                    message = f"本期 {period} 已交付，不再重复执行"
            return {
                "status": status,
                "period": period,
                "bindings": bindings,
                "prev_period": prev,
                "reason": reason,
                "message": message,
            }

    def finish_run(self, period: str) -> dict[str, Any]:
        with self._lock:
            self.verify_reference()
            recipe = self.load_run_recipe()
            normalized = self._run_period(period)
            run_manifest = self.load_run_manifest(normalized)
            self._ensure_run_recipe_current(run_manifest)
            pending = [
                progress.section_id
                for progress in run_manifest.sections
                if progress.status != "approved"
            ]
            if pending:
                raise ReportCliError("仍有章节未通过 check-run：" + ", ".join(pending))
            html = self._safe_path(f"runs/{normalized}/report.html")
            if not html.is_file():
                raise ReportCliError(
                    "【R8 HTML 只经 bi-report-generation】"
                    f"缺少 runs/{normalized}/report.html，本期不能交付"
                )
            archive = self._safe_path(f"archive/{normalized}")
            archive.mkdir(parents=True, exist_ok=True)
            shutil.copy2(html, archive / "report.html")
            delivered = self.artifact_dir / "report.html"
            shutil.copy2(html, delivered)
            state = self._read_state()
            history = list(state.get("history") or [])
            if any(
                isinstance(item, dict)
                and item.get("period") == normalized
                and item.get("status") == "delivered"
                for item in history
            ):
                raise ReportCliError(
                    f"本期 {normalized} 已在 STATE.json 中标记为已交付"
                )
            history.append({"period": normalized, "status": "delivered"})
            state = {
                "report_id": recipe.report_id,
                "last_period": normalized,
                "history": history,
            }
            write_text_atomic(
                self._safe_path("STATE.json"),
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            )
            return {
                "period": normalized,
                "report_path": "report.html",
                "archive_path": f"archive/{normalized}/report.html",
                "last_period": normalized,
                "next": "本期已交付。下一期由 plan-run 根据 STATE.json 继续",
            }

    def _read_state(self) -> dict[str, Any]:
        path = self.write_root / "STATE.json"
        if not path.is_file():
            raise ReportCliError(
                "工作目录缺少 STATE.json。请先运行 install-reference；"
                "STATE.json 只能由 install-reference 与 finish-run 写入"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ReportCliError("STATE.json 顶层必须是 JSON 对象")
        return payload
