# -*- coding: utf-8 -*-
"""Artifact store: read root for templates, write root for period state.

Enforces R5. In normal mode both roots are the session product directory.
In cron mode the reference is read-only and the workspace is writable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from pathlib import Path
from typing import Any

from rr.baseline import BaselineMixin
from rr.common import (
    COMPARISON_FILE,
    CONTRACT_VERSION,
    MANIFEST_FILE,
    MANIFEST_SCHEMA_VERSION,
    PRODUCT_DIR,
    ReportCliError,
    _RUN_PERIOD_RE,
    utc_now,
    validated_section_id,
    write_json_atomic,
    write_text_atomic,
)
from rr.legacy import LegacyMixin, _normalize_manifest_payload
from rr.models import ReportManifest, RunRecipe
from rr.parse import _validate_source, parse_document
from rr.runs import RunsMixin
from rr.schedule import _infer_parameter_templates, _infer_period_spec, build_recipe


class ReportStore(LegacyMixin, BaselineMixin, RunsMixin):
    """Deterministic owner of every report artifact and state transition."""

    def __init__(
        self,
        artifact_dir: Path,
        *,
        read_root: Path | None = None,
        write_root: Path | None = None,
    ) -> None:
        self.artifact_dir = Path(artifact_dir)
        self.cron = read_root is not None
        if self.cron:
            if write_root is None:
                raise ReportCliError("定时模式必须同时提供读写目录")
            self.read_root = Path(read_root)
            self.write_root = Path(write_root)
            self.product_root = self.read_root
            self.manifest_path = self.read_root / MANIFEST_FILE
        else:
            self.product_root = self.artifact_dir / PRODUCT_DIR
            self.read_root = self.product_root
            self.write_root = self.product_root
            self.manifest_path = self.product_root / MANIFEST_FILE
        self._lock = threading.RLock()

    def _is_write_path(self, relative_path: str) -> bool:
        rel = relative_path.replace("\\", "/").lstrip("/")
        return (
            rel == "STATE.json"
            or rel.startswith("runs/")
            or rel.startswith("archive/")
            or rel.startswith("turns/")
            or "/runs/" in rel
        )

    def _safe_path(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute():
            raise ReportCliError(f"产物路径必须是相对路径：{relative_path}")
        root = (
            self.write_root if self._is_write_path(relative_path) else self.read_root
        ).resolve()
        resolved = (root / relative).resolve()
        if not resolved.is_relative_to(root):
            raise ReportCliError(f"产物路径越出任务目录（{root}）：{relative_path}")
        return resolved

    def _relative(self, path: Path) -> str:
        resolved = path.resolve()
        for root in (self.write_root, self.read_root):
            try:
                return resolved.relative_to(root.resolve()).as_posix()
            except ValueError:
                continue
        raise ReportCliError(f"产物路径不在读写目录内：{path}")

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
        if self.cron:
            raise ReportCliError(
                "【R5 产物归属】定时模式不读取 report_manifest.json，"
                "只读取参考目录中的运行配方"
            )
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

    def verify_reference(self) -> dict[str, Any]:
        """Check contract_version, every md5 and recipe_hash. Reads only."""
        document_path = self.read_root / "REFERENCE.json"
        if not document_path.is_file():
            raise ReportCliError("参考目录缺少 REFERENCE.json")
        try:
            document = json.loads(document_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReportCliError(f"REFERENCE.json 无法解析：{exc}") from exc
        if not isinstance(document, dict):
            raise ReportCliError("REFERENCE.json 顶层必须是 JSON 对象")
        version = document.get("contract_version")
        if version != CONTRACT_VERSION:
            raise ReportCliError(
                f"参考目录的 contract_version={version} 不受支持，"
                f"当前只接受 {CONTRACT_VERSION}"
            )
        files = document.get("files") or {}
        if not isinstance(files, dict):
            raise ReportCliError("REFERENCE.json 的 files 必须是对象")
        root = self.read_root.resolve()
        for relative, expected in files.items():
            path = (self.read_root / str(relative)).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ReportCliError(f"【R5 产物归属】参考文件缺失或被改动：{relative}")
            actual = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
            if actual != expected:
                raise ReportCliError(f"【R5 产物归属】参考文件校验失败：{relative}")
        recipe_path = self.read_root / "recipe.json"
        if not recipe_path.is_file():
            raise ReportCliError("【R5 产物归属】参考目录缺少 recipe.json")
        digest = hashlib.sha256(recipe_path.read_bytes()).hexdigest()
        if digest != document.get("recipe_hash"):
            raise ReportCliError("【R5 产物归属】recipe_hash 与 recipe.json 不一致")
        return document

    def load_run_recipe(self) -> RunRecipe:
        path = self.read_root / "recipe.json"
        if not path.is_file():
            raise ReportCliError("参考目录缺少 recipe.json")
        try:
            return RunRecipe.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ReportCliError(f"recipe.json 不是可执行的运行配方：{exc}") from exc

    def status_report(self, *, period: str | None = None) -> dict[str, Any]:
        if self.cron:
            return self._cron_status(period)
        return self._baseline_status()

    def _baseline_status(self) -> dict[str, Any]:
        manifest = self.load_manifest()
        turns = self._turn_index()
        sections = []
        for progress in manifest.sections:
            section_id = progress.section.section_id
            provenance = None
            released = False
            reason = ""
            comparison = self._comparison_path(section_id)
            if comparison.is_file():
                payload = json.loads(comparison.read_text(encoding="utf-8"))
                gate = payload.get("evidence_gate") or {}
                review = payload.get("review") or {}
                if isinstance(gate, dict):
                    provenance = gate.get("provenance")
                if isinstance(review, dict) and review.get("approved_by_user"):
                    released = True
                    reason = str(review.get("reason") or "")
            sections.append(
                {
                    "section_id": section_id,
                    "title": progress.section.title,
                    "status": progress.status,
                    "evidence_provenance": provenance,
                    "released_by_user": released,
                    "release_reason": reason,
                    "last_chat": turns.get(section_id),
                }
            )
        return {
            "mode": "baseline",
            "report_id": manifest.report_id,
            "title": manifest.title,
            "baseline_frozen_at": manifest.baseline_frozen_at,
            "sections": sections,
            "next": "逐节推进未 approved 的章节；全部通过后 freeze-recipe",
        }

    def _cron_status(self, period: str | None) -> dict[str, Any]:
        self.verify_reference()
        state = self._read_state()
        chosen = (period or "").strip() or str(state.get("last_period") or "")
        delivered = {
            str(item.get("period"))
            for item in state.get("history") or []
            if isinstance(item, dict) and item.get("status") == "delivered"
        }
        sections: list[dict[str, Any]] = []
        run_path = self.write_root / "runs" / chosen / "run_manifest.json"
        if chosen and run_path.is_file():
            run_manifest = self.load_run_manifest(chosen)
            for progress in run_manifest.sections:
                checks_path = self._run_section_path(
                    progress.section_id, chosen, "run_checks.json"
                )
                checks: list[dict[str, Any]] = []
                if checks_path.is_file():
                    payload = json.loads(checks_path.read_text(encoding="utf-8"))
                    checks = [
                        {
                            "name": item.get("name"),
                            "passed": item.get("passed"),
                        }
                        for item in payload.get("checks") or []
                        if isinstance(item, dict)
                    ]
                sections.append(
                    {
                        "section_id": progress.section_id,
                        "status": progress.status,
                        "checks": checks,
                    }
                )
        return {
            "mode": "cron",
            "period": chosen or None,
            "delivered": chosen in delivered,
            "sections": sections,
            "next": "按定时指令执行 plan-run；STOP 时回复 message 并结束",
        }

    def _turn_index(self) -> dict[str, str]:
        directory = self.write_root / "turns"
        latest: dict[str, tuple[str, str]] = {}
        if not directory.is_dir():
            return {}
        for path in directory.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            section_id = str(payload.get("section_id") or "")
            if not section_id:
                continue
            stamp = str(payload.get("updated_at") or "")
            chat_id = str(payload.get("chat_id") or path.stem)
            previous = latest.get(section_id)
            if previous is None or stamp >= previous[0]:
                latest[section_id] = (stamp, chat_id)
        return {section_id: chat_id for section_id, (_stamp, chat_id) in latest.items()}

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
