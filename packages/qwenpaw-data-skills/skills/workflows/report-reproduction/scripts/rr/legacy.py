# -*- coding: utf-8 -*-
"""v1/v2 manifest migration, isolated so it can be removed later.

A migrated approval keeps its review record (R2). The comparison file is
renamed, not recomputed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from rr.common import (
    COMPARISON_FILE,
    DEFAULT_TOLERANCE,
    LEGACY_VALIDATION_FILE,
    MANIFEST_SCHEMA_VERSION,
    ReportCliError,
    utc_now,
    write_json_atomic,
    write_text_atomic,
)
from rr.baseline import _comparison_kernel
from rr.models import ReportManifest, ReportRunManifest


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


class LegacyMixin:
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
        tolerance = raw.get("tolerance")
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
            "tolerance": DEFAULT_TOLERANCE if tolerance in (None, "") else float(tolerance),
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
