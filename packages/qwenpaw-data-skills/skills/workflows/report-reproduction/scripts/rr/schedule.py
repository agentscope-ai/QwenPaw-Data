# -*- coding: utf-8 -*-
"""Recipe building and period-parameter inference.

Plan-run and prepare-run share the binding renderer below. Week slot 0 is the
earliest week in the window; the last slot is the target week.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from rr.common import (
    ReportCliError,
    _MONTH_SLOT_PARAMETER_RE,
    _WEEK_SLOT_PARAMETER_RE,
    read_text_file,
)
from rr.models import (
    ComputeRecipe,
    QueryRecipe,
    RecipeParameter,
    ReportManifest,
    ReportRecipe,
)


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
                    query_path=query_path.resolve()
                    .relative_to(resolved_root)
                    .as_posix(),
                    parameters=names,
                )
            )
    computes: list[ComputeRecipe] = []
    for progress in manifest.sections:
        section_id = progress.section.section_id
        compute_dir = (resolved_root / "sections" / section_id / "compute").resolve()
        if not compute_dir.is_dir() or not compute_dir.is_relative_to(resolved_root):
            continue
        for script in sorted(compute_dir.glob("*.py")):
            if not script.resolve().is_relative_to(resolved_root):
                raise ReportCliError(f"计算脚本越出报告任务范围：{script}")
            computes.append(
                ComputeRecipe(
                    section_id=section_id,
                    path=script.resolve().relative_to(resolved_root).as_posix(),
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
        computes=computes,
        section_order=[progress.section.section_id for progress in manifest.sections],
    )


# --------------------------------------------------------------------------- #
# Period windows and SQL rendering
# --------------------------------------------------------------------------- #

_BINDING_RE = re.compile(
    r"\$\{(window_week_start|window_week_end|window_month|window_start|period_end|period)"
    r"(?::(\d+))?:([^}]+)\}"
)
_DOLLAR_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MUSTACHE_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_COLON_PLACEHOLDER_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)\b")
_WEEK_LABEL_RE = re.compile(r"(\d{4})-W(\d{2})")
_MONTH_LABEL_RE = re.compile(r"(\d{4})(\d{2})")


def parse_run_date(value: str) -> date:
    text = (value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ReportCliError(f"--run-date 必须是 YYYY-MM-DD，收到：{value!r}") from exc


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    shifted_year, shifted_month = divmod(index, 12)
    return shifted_year, shifted_month + 1


def period_for_run_date(run_date: date, period: dict[str, Any]) -> str:
    """Label of the period a run on ``run_date`` should produce."""
    unit = str(period.get("unit") or "month")
    offset = int(period.get("offset", -1))
    label_format = str(period.get("label_format") or "")
    if unit == "week":
        monday = run_date - timedelta(days=run_date.weekday())
        target = monday + timedelta(weeks=offset)
        return target.strftime(label_format or "%G-W%V")
    year, month = _shift_month(run_date.year, run_date.month, offset)
    return date(year, month, 1).strftime(label_format or "%Y%m")


def window_for_period(
    period_label: str, period: dict[str, Any]
) -> tuple[date, date, list[tuple[date, date]]]:
    """Return ``(window_start, period_end, slots)`` for an already chosen label."""
    unit = str(period.get("unit") or "month")
    window = int(period.get("window_size") or 1)
    if window < 1:
        raise ReportCliError(f"schedule.window_size 非法：{window}")
    if unit == "week":
        match = _WEEK_LABEL_RE.fullmatch(period_label)
        if not match:
            raise ReportCliError(f"周周期标签无法解析：{period_label}")
        target = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
        first = target - timedelta(weeks=window - 1)
        slots = [
            (start, start + timedelta(days=6))
            for index in range(window)
            if (start := first + timedelta(weeks=index))
        ]
        return slots[0][0], slots[-1][1], slots
    match = _MONTH_LABEL_RE.fullmatch(period_label)
    if not match:
        raise ReportCliError(f"月周期标签无法解析：{period_label}")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise ReportCliError(f"月周期标签无法解析：{period_label}")
    slots: list[tuple[date, date]] = []
    for index in range(window):
        slot_year, slot_month = _shift_month(year, month, index - (window - 1))
        last_day = calendar.monthrange(slot_year, slot_month)[1]
        slots.append(
            (date(slot_year, slot_month, 1), date(slot_year, slot_month, last_day))
        )
    return slots[0][0], slots[-1][1], slots


def _apply_binding_template(
    template: str,
    *,
    window_start: date,
    period_end: date,
    slots: list[tuple[date, date]],
) -> str:
    def replace(match: re.Match[str]) -> str:
        kind = match.group(1)
        slot = int(match.group(2) or 0)
        fmt = match.group(3)
        if kind == "window_start":
            moment = window_start
        elif kind == "period_end":
            moment = period_end
        elif kind == "period":
            moment = slots[-1][0]
        elif kind in {"window_week_start", "window_month"}:
            if slot >= len(slots):
                raise ReportCliError(f"参数模板槽位越界：{match.group(0)}")
            moment = slots[slot][0]
        elif kind == "window_week_end":
            if slot >= len(slots):
                raise ReportCliError(f"参数模板槽位越界：{match.group(0)}")
            moment = slots[slot][1]
        else:
            return match.group(0)
        return moment.strftime(fmt)

    return _BINDING_RE.sub(replace, template)


def render_bindings(
    period_label: str,
    period: dict[str, Any],
    templates: dict[str, str],
) -> dict[str, str]:
    window_start, period_end, slots = window_for_period(period_label, period)
    return {
        name: _apply_binding_template(
            template,
            window_start=window_start,
            period_end=period_end,
            slots=slots,
        )
        for name, template in templates.items()
    }


def render_sql(sql: str, bindings: dict[str, str]) -> str:
    """Substitute frozen placeholders. Unknown names are left in place."""

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        return bindings.get(name, match.group(0))

    rendered = _DOLLAR_PLACEHOLDER_RE.sub(substitute, sql)
    rendered = _MUSTACHE_PLACEHOLDER_RE.sub(substitute, rendered)
    return _COLON_PLACEHOLDER_RE.sub(substitute, rendered)


def period_steps(previous: str, current: str, unit: str) -> int:
    """How many period steps ``current`` is after ``previous``."""
    if unit == "week":
        previous_match = _WEEK_LABEL_RE.fullmatch(previous)
        current_match = _WEEK_LABEL_RE.fullmatch(current)
        if not previous_match or not current_match:
            raise ReportCliError(f"无法比较周期：{previous} 与 {current}")
        previous_day = date.fromisocalendar(
            int(previous_match.group(1)), int(previous_match.group(2)), 1
        )
        current_day = date.fromisocalendar(
            int(current_match.group(1)), int(current_match.group(2)), 1
        )
        return (current_day - previous_day).days // 7
    previous_match = _MONTH_LABEL_RE.fullmatch(previous)
    current_match = _MONTH_LABEL_RE.fullmatch(current)
    if not previous_match or not current_match:
        raise ReportCliError(f"无法比较周期：{previous} 与 {current}")
    previous_index = int(previous_match.group(1)) * 12 + int(previous_match.group(2))
    current_index = int(current_match.group(1)) * 12 + int(current_match.group(2))
    return current_index - previous_index
