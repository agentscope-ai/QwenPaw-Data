# -*- coding: utf-8 -*-
"""Export the next-run reference and compare two dry-run workspaces.

Enforces R6 (exemplar keeps structure, not baseline numbers) and R7 (gap
blocks travel into the prompt, business numbers do not). ``diff-run`` reuses
the comparison kernel without importing it until the command actually runs.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Any

from rr.common import (
    CONTRACT_VERSION,
    DEFAULT_TOLERANCE,
    ReportCliError,
    validated_section_id,
    write_text_atomic,
)
from rr.models import (
    ComputeRecipe,
    QueryRecipe,
    RecipeParameter,
    ReportManifest,
    RunRecipe,
    RunSchedule,
    RunSectionRecipe,
    SchedulePeriod,
)
from rr.schedule import render_bindings

MASK = "〔数〕"
GAP_HEADING = "## 复现差异与数据缺口说明"
CRON_WORK_DIR = "report-reproduction-cron"
EXPORT_DIR_NAME = "report-reproduction-export"
BUNDLE_LIMIT_BYTES = 20 * 1024 * 1024
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_STRUCTURAL_HEADING_RE = re.compile(r"\s*#+\s*")
_GAP_NEXT_HEADING_RE = re.compile(r"^#{1,6} ")


def _dump(path: Path, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportCliError(f"{label} 无法解析：{exc}") from exc


def _structural_number(line: str, column: int, raw: str) -> bool:
    """Heading ordinals and ordered-list markers are not business numbers."""
    prefix = line[:column]
    suffix = line[column + len(raw) :]
    if _STRUCTURAL_HEADING_RE.fullmatch(prefix):
        return True
    if re.fullmatch(r"\s*", prefix) and re.fullmatch(r"\d+", raw):
        if suffix[:1] in {".", ")", "）", "、"}:
            return True
    if suffix.startswith(("年", "月", "日", "周", "季")):
        return True
    if prefix.endswith("第"):
        return True
    if _YEAR_RE.fullmatch(raw) and (
        suffix.startswith(("年", "-", "—", "/")) or prefix.endswith((" ", "年"))
    ):
        return True
    return False


def mask_business_numbers(text: str) -> str:
    """Replace business numbers with ``〔数〕``, keeping ordinals and dates."""
    from qwenpaw_data.host.core.agent.tools.compare import extract_numbers

    spans: list[tuple[int, int]] = []
    for token in extract_numbers(text):
        line_start = text.rfind("\n", 0, token.start) + 1
        line_end = text.find("\n", token.end)
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        if _structural_number(line, token.start - line_start, token.raw):
            continue
        spans.append((token.start, token.end))
    rendered = text
    for start, end in reversed(spans):
        rendered = rendered[:start] + MASK + rendered[end:]
    return rendered


def _slim_metric(item: Any) -> dict[str, str]:
    if isinstance(item, str):
        return {"text": item, "provenance": ""}
    if isinstance(item, dict):
        return {
            "text": str(item.get("text") or item.get("definition") or ""),
            "provenance": str(item.get("provenance") or ""),
        }
    raise ReportCliError("metric_definitions 的每一项必须是字符串或对象")


def _slim_criteria(items: Any, section_index: int) -> list[dict[str, str]]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise ReportCliError("success_criteria 必须是数组")
    criteria: list[dict[str, str]] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, str):
            criteria.append({"id": f"S{section_index}.{index}", "text": item})
            continue
        if not isinstance(item, dict):
            raise ReportCliError("success_criteria 的每一项必须是字符串或 {id, text}")
        text = str(item.get("text") or item.get("criterion") or "").strip()
        if not text:
            raise ReportCliError("success_criteria 缺少 text")
        criteria.append(
            {
                "id": str(item.get("id") or f"S{section_index}.{index}"),
                "text": text,
            }
        )
    return criteria


def _slim_guards(items: Any, section_index: int) -> list[dict[str, str]]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise ReportCliError("run_guards 必须是数组")
    if len(items) > 5:
        raise ReportCliError(f"每个章节最多 5 条 run_guards，本节有 {len(items)} 条")
    guards: list[dict[str, str]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ReportCliError(
                "run_guards 的每一项必须是 {id, rule, why, provenance}"
            )
        rule = str(item.get("rule") or "").strip()
        why = str(item.get("why") or "").strip()
        if not rule or not why:
            raise ReportCliError("run_guards 必须同时写 rule 与 why")
        guards.append(
            {
                "id": str(item.get("id") or f"S{section_index}.G{index}"),
                "rule": rule,
                "why": why,
                "provenance": str(item.get("provenance") or ""),
            }
        )
    return guards


def slim_spec(raw: dict[str, Any], section_index: int) -> dict[str, Any]:
    """Keep only the fields the next run reads."""
    return {
        "metric_definitions": [
            _slim_metric(item) for item in raw.get("metric_definitions") or []
        ],
        "required_data": [str(item) for item in raw.get("required_data") or []],
        "success_criteria": _slim_criteria(raw.get("success_criteria"), section_index),
        "markdown_outline": [str(item) for item in raw.get("markdown_outline") or []],
        "run_guards": _slim_guards(raw.get("run_guards"), section_index),
    }


def sanitize_format_spec(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop the original document. ``source_css`` is what report_builder injects."""
    if not isinstance(raw, dict):
        raise ReportCliError("format_spec.json 顶层必须是 JSON 对象")
    spec = json.loads(json.dumps(raw, ensure_ascii=False))
    source = spec.get("source")
    if isinstance(source, dict):
        source.pop("evidence", None)
        source.pop("path", None)
        if not source:
            spec.pop("source", None)
    css = spec.get("source_css")
    if not isinstance(css, str) or not css.strip():
        raise ReportCliError(
            "【R8 HTML 只经 bi-report-generation】format_spec.source_css 不能为空，"
            "无法导出参考。report_builder 在 source_css 非空时不会读取 source.path"
        )
    return spec


def gap_items(exemplar: str) -> list[str]:
    if GAP_HEADING not in exemplar:
        return []
    body = exemplar.split(GAP_HEADING, 1)[1]
    items: list[str] = []
    for line in body.splitlines():
        if _GAP_NEXT_HEADING_RE.match(line) and GAP_HEADING not in line:
            break
        stripped = line.strip()
        if stripped.startswith(("-", "【")):
            items.append(stripped.lstrip("- ").strip())
    return items


def _collect_datasource(product_root: Path, raw_specs: list[dict[str, Any]]) -> str:
    for spec in raw_specs:
        datasource = spec.get("datasource")
        if isinstance(datasource, str) and datasource.strip():
            return datasource.strip()
    evidence = product_root / "evidence" / "global.json"
    if evidence.is_file():
        payload = _read_json(evidence, "evidence/global.json")
        if isinstance(payload, dict):
            for key in ("datasource", "data_source"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _probe_tables(raw_specs: list[dict[str, Any]]) -> list[str]:
    tables: list[str] = []
    seen: set[str] = set()
    for spec in raw_specs:
        for item in spec.get("required_data") or []:
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                tables.append(text)
    return tables


def render_prompt(
    recipe: RunRecipe,
    specs: dict[str, dict[str, Any]],
    exemplars: dict[str, str],
    *,
    recipe_hash: str = "",
) -> str:
    """Chinese cron prompt. Empty rule and gap sections are omitted."""
    period = recipe.schedule.period
    unit = "周" if period.unit == "week" else "月"
    if period.unit == "week":
        description = "按周运行，期次为运行日所在周的上一个完整周"
    else:
        description = "按月运行，期次为运行日的上一个完整月"
    if period.window_size > 1:
        description += f"，窗口含 {period.window_size} 个{unit}"
    datasource = recipe.datasource or "沿用当前会话数据源"
    if recipe.probe_tables:
        tables = "、".join(recipe.probe_tables)
    else:
        tables = "未记录，沿用当前会话数据源，取数前仍需确认分区已就绪"
    digest = recipe_hash or "<recipe_hash>"
    reference_dir = f"{CRON_WORK_DIR}/reference/{digest}"
    work_dir = CRON_WORK_DIR
    flags = f"--reference-dir {reference_dir} --work-dir {work_dir}"
    lines = [
        f"# {recipe.title or recipe.report_id} 定时执行指令",
        "",
        "严格按既有管线执行，不要重新设计口径，不要询问，一路执行到报告产出。直接使用脚本，非必要无需安装依赖。",
        "",
        "## 1. 本期任务",
        f"- 周期：{description}。期次只认 `rr plan-run` 的输出。",
        f"- 数据源：{datasource}。取数前探测分区：{tables}。",
        f"- 交付：{len(recipe.sections)} 个章节的 HTML 报告。",
        "",
        "## 2. 执行步骤",
        "`rr` 一律带 `--artifact-dir` 与 `--chat-id`，取值见系统提示。",
        f"参考目录固定为 `{reference_dir}`，工作目录固定为 `{work_dir}`。",
        (f"0. `rr install-reference --recipe-hash {digest} --work-dir {work_dir}`。"),
        f"1. `rr plan-run --run-date <今天> {flags}`。返回 `STOP` 时回复 `message` 并结束。",
        "2. 探测第 1 节的分区。未就绪时回复预计可用时间并结束，不得用旧数据。",
        f"3. `rr prepare-run --period <期次> {flags}`。",
        "4. 按第 3 节顺序逐章节执行：",
        (
            f"   - 执行 `{work_dir}/sections/<章节>/runs/<期次>/queries/` "
            "下已渲染的 SQL，结果存入同级 `data/`；"
        ),
        "   - 章节有 `compute/` 脚本时运行它；",
        (
            f"   - 仿照 `{reference_dir}/sections/<章节>/exemplar.md` 写本期草稿，"
            "`〔数〕` 全部替换为本期数值；"
        ),
        (
            "   - `rr update-section --section-id <章节> --run-period <期次> "
            f"--content-file <草稿> {flags}`；"
        ),
        (
            f"   - `rr check-run {flags}`，每个验收项传一条 `--business-check`，"
            "`name` 用验收项编号。"
        ),
        (
            "5. 加载 `bi-report-generation`：`--format-spec` 用 "
            f"`{reference_dir}/format_spec.json`，草稿取 `{work_dir}`，"
            f"输出 `{work_dir}/runs/<期次>/report.html`。"
        ),
        f"6. `rr finish-run --period <期次> {flags}`。未成功即本期未交付。",
        "",
        "## 3. 章节",
        "| 顺序 | 章节 | 依赖 | 验收项 |",
        "| --- | --- | --- | --- |",
    ]
    rules: list[str] = []
    gaps: list[str] = []
    for index, section in enumerate(recipe.sections, start=1):
        spec = specs.get(section.section_id) or {}
        criteria = spec.get("success_criteria") or []
        criterion_text = (
            "；".join(f"{item['id']} {item['text']}" for item in criteria) or "—"
        )
        depends = "、".join(section.blocked_by) or "—"
        lines.append(
            f"| {index} | {section.section_id} {section.title} | {depends} | {criterion_text} |"
        )
        for guard in spec.get("run_guards") or []:
            rules.append(f"- [{guard['id']}] {guard['rule']}。原因：{guard['why']}。")
        for metric_index, metric in enumerate(
            spec.get("metric_definitions") or [], start=1
        ):
            if metric.get("provenance") == "用户确认" and metric.get("text"):
                rules.append(
                    f"- [S{index}.C{metric_index}] 口径不得回退：{metric['text']}。"
                )
        for item in gap_items(exemplars.get(section.section_id) or ""):
            gaps.append(f"- [{section.section_id}] {item}")
    if rules:
        lines.extend(["", "## 4. 必守规则", *rules])
    if gaps:
        lines.extend(["", "## 5. 已知缺口", *gaps])
    lines.extend(
        [
            "",
            "## 6. 最终回复",
            "三行摘要：核心指标、环比、异常点；附报告链接。未执行的检查写「未执行」。",
            "",
        ]
    )
    return "\n".join(lines)


def _copy_tree_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def export_reference(
    product_root: Path,
    manifest: ReportManifest,
    *,
    out_dir: Path,
    note: str,
    baseline_period: str,
    artifact_dir: Path,
) -> dict[str, Any]:
    """Write ``<out>/reference``, ``<out>/seed`` and one attachable zip bundle."""
    if not manifest.baseline_frozen_at:
        raise ReportCliError("基线尚未冻结，请先 freeze-recipe 再 export-reference")
    note = (note or "").strip()
    if not note:
        raise ReportCliError("--note 不能为空，用于说明这一版参考和上一版的差别")
    if product_root.resolve() != product_root or any(
        p.is_symlink() for p in product_root.rglob("*")
    ):
        raise ReportCliError("参考源不能包含符号链接")
    recipe_path = product_root / "recipe.json"
    descriptor_path = product_root / "schedule_descriptor.json"
    if not recipe_path.is_file() or not descriptor_path.is_file():
        raise ReportCliError(
            "export-reference 需要已冻结的 recipe.json 与 schedule_descriptor.json"
        )
    session_recipe = _read_json(recipe_path, "recipe.json")
    descriptor = _read_json(descriptor_path, "schedule_descriptor.json")
    if not isinstance(session_recipe, dict) or not isinstance(descriptor, dict):
        raise ReportCliError(
            "recipe.json 与 schedule_descriptor.json 顶层必须是 JSON 对象"
        )
    period = descriptor.get("period") or {}
    if period.get("unit") not in {"week", "month"}:
        raise ReportCliError("schedule_descriptor.json 缺少可执行的 period")

    raw_specs: list[dict[str, Any]] = []
    slim_by_id: dict[str, dict[str, Any]] = {}
    sections: list[RunSectionRecipe] = []
    for index, progress in enumerate(manifest.sections, start=1):
        section_id = validated_section_id(progress.section.section_id)
        spec_path = product_root / "sections" / section_id / "spec.json"
        raw = (
            _read_json(spec_path, f"sections/{section_id}/spec.json")
            if spec_path.is_file()
            else {}
        )
        if not isinstance(raw, dict):
            raise ReportCliError(
                f"sections/{section_id}/spec.json 顶层必须是 JSON 对象"
            )
        raw_specs.append(raw)
        slim_by_id[section_id] = slim_spec(raw, index)
        sections.append(
            RunSectionRecipe(
                section_id=section_id,
                title=progress.section.title,
                blocked_by=list(progress.section.blocked_by),
            )
        )

    format_path = product_root / "format_spec.json"
    if not format_path.is_file():
        raise ReportCliError("缺少 format_spec.json，无法导出参考")
    format_spec = sanitize_format_spec(_read_json(format_path, "format_spec.json"))

    # Never replace an earlier export or an unrelated user directory.
    if any(out_dir.iterdir()):
        out_dir = Path(tempfile.mkdtemp(prefix="revision-", dir=out_dir))
    reference = out_dir / "reference"
    seed = out_dir / "seed"
    reference.mkdir(parents=True)
    seed.mkdir(parents=True)

    exemplars: dict[str, str] = {}
    for progress in manifest.sections:
        section_id = validated_section_id(progress.section.section_id)
        draft_path = product_root / "sections" / section_id / "baseline" / "draft.md"
        if not draft_path.is_file():
            raise ReportCliError(f"缺少已验收草稿，无法生成 exemplar：{section_id}")
        exemplar = mask_business_numbers(draft_path.read_text(encoding="utf-8"))
        exemplars[section_id] = exemplar
        write_text_atomic(reference / "sections" / section_id / "exemplar.md", exemplar)
        _dump(reference / "sections" / section_id / "spec.json", slim_by_id[section_id])
        query_dir = product_root / "sections" / section_id / "queries"
        if query_dir.is_dir():
            for sql in sorted(query_dir.glob("*.sql")):
                _copy_tree_file(
                    sql,
                    reference / "sections" / section_id / "queries" / sql.name,
                )
        compute_dir = product_root / "sections" / section_id / "compute"
        if compute_dir.is_dir():
            for script in sorted(compute_dir.glob("*.py")):
                _copy_tree_file(
                    script,
                    reference / "sections" / section_id / "compute" / script.name,
                )
        data_dir = product_root / "sections" / section_id / "baseline" / "data"
        if data_dir.is_dir():
            for data_file in sorted(
                path for path in data_dir.rglob("*") if path.is_file()
            ):
                relative = data_file.relative_to(data_dir)
                _copy_tree_file(
                    data_file,
                    seed
                    / "sections"
                    / section_id
                    / "runs"
                    / baseline_period
                    / "data"
                    / relative,
                )

    parameters = [
        RecipeParameter.model_validate(item)
        for item in session_recipe.get("parameters") or []
        if isinstance(item, dict)
    ]
    queries = [
        QueryRecipe.model_validate(item)
        for item in session_recipe.get("queries") or []
        if isinstance(item, dict)
    ]
    computes = [
        ComputeRecipe.model_validate(item)
        for item in session_recipe.get("computes") or []
        if isinstance(item, dict)
    ]
    run_recipe = RunRecipe(
        report_id=manifest.report_id,
        title=manifest.title,
        sections=sections,
        parameters=parameters,
        queries=queries,
        computes=computes,
        schedule=RunSchedule(
            period=SchedulePeriod.model_validate(period),
            parameters={
                str(key): str(value)
                for key, value in (descriptor.get("parameters") or {}).items()
            },
        ),
        datasource=_collect_datasource(product_root, raw_specs),
        probe_tables=_probe_tables(raw_specs),
    )
    recipe_file = reference / "recipe.json"
    write_text_atomic(recipe_file, run_recipe.model_dump_json(indent=2) + "\n")
    _dump(reference / "format_spec.json", format_spec)
    recipe_hash = _sha256(recipe_file)
    prompt = render_prompt(run_recipe, slim_by_id, exemplars, recipe_hash=recipe_hash)

    files: dict[str, str] = {}
    for path in sorted(reference.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(reference).as_posix()
        files[relative] = _md5(path)
    reference_doc = {
        "contract_version": CONTRACT_VERSION,
        "report_id": manifest.report_id,
        "title": manifest.title,
        "note": note,
        "recipe_hash": recipe_hash,
        "files": files,
        "prompt": prompt,
    }
    _dump(reference / "REFERENCE.json", reference_doc)
    _dump(
        seed / "STATE.json",
        {
            "report_id": manifest.report_id,
            "last_period": baseline_period,
            "history": [{"period": baseline_period, "status": "delivered"}],
        },
    )
    bundle = _write_bundle(out_dir, recipe_hash)
    message_path = out_dir / "cron_message.md"
    write_text_atomic(message_path, prompt)
    bundle_path = _workspace_relative(artifact_dir, bundle)
    return {
        "reference_dir": str(reference),
        "seed_dir": str(seed),
        "contract_version": CONTRACT_VERSION,
        "recipe_hash": recipe_hash,
        "file_count": len(files),
        "baseline_period": baseline_period,
        "bundle_path": bundle_path,
        "cron_message_path": _relative_to(artifact_dir, message_path),
        "cron_job": {
            "session_policy": "reuse",
            "attachment_filenames": [bundle_path],
            "channel": "console",
        },
        "next": (
            "读取 cron_message.md 作为 message，调用 CronJob create："
            "session_policy=reuse，attachment_filenames 使用 bundle_path，channel=console，"
            "调度与配方周期一致。不要使用 new_each_run。"
        ),
    }


def _relative_to(root: Path, path: Path) -> str:
    resolved = path.resolve()
    base = root.resolve()
    if not resolved.is_relative_to(base):
        raise ReportCliError(f"路径不在会话产物目录内：{path}")
    return resolved.relative_to(base).as_posix()


def _workspace_relative(artifact_dir: Path, path: Path) -> str:
    """Path of an artifact file relative to the workspace root."""
    relative = _relative_to(artifact_dir, path)
    if artifact_dir.parent.name != "artifacts":
        raise ReportCliError(f"产物目录不在 artifacts 下：{artifact_dir}")
    return f"artifacts/{artifact_dir.name}/{relative}"


def _write_bundle(out_dir: Path, recipe_hash: str) -> Path:
    """Zip ``reference/`` and ``seed/`` into one flat attachment."""
    destination = out_dir / f"rr-bundle-{recipe_hash[:12]}.zip"
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for folder in ("reference", "seed"):
            root = out_dir / folder
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(out_dir).as_posix())
    size = destination.stat().st_size
    if size > BUNDLE_LIMIT_BYTES:
        destination.unlink()
        raise ReportCliError(f"参考包超过 20MB（{size} 字节），无法作为定时任务附件")
    return destination


def _safe_zip_relative(name: str) -> Path:
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        raise ReportCliError(f"压缩包路径越界：{name}")
    relative = Path(normalized)
    if (
        not relative.parts
        or PureWindowsPath(normalized).drive
        or any(
            p in {"", ".", ".."}
            or ":" in p
            or p.endswith((" ", "."))
            or PureWindowsPath(p).is_reserved()
            for p in normalized.split("/")
        )
    ):
        raise ReportCliError(f"压缩包路径越界：{name}")
    return relative


def _extract_prefixed(
    archive: zipfile.ZipFile,
    prefix: str,
    dest: Path,
    *,
    forbid_roots: tuple[str, ...] = (),
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    infos = archive.infolist()
    if len(infos) > 4096 or sum(i.file_size for i in infos) > 128 * 1024 * 1024:
        raise ReportCliError("参考包解压大小或文件数量超限")
    found = False
    for info in infos:
        name = info.filename.replace("\\", "/")
        if not name.startswith(prefix) or name.endswith("/"):
            continue
        relative = _safe_zip_relative(name[len(prefix) :])
        if relative.parts and relative.parts[0] in forbid_roots:
            raise ReportCliError(f"压缩包路径越界：{info.filename}")
        target = (dest / relative).resolve()
        if not target.is_relative_to(root):
            raise ReportCliError(f"压缩包路径越界：{info.filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target != dest / relative:
            raise ReportCliError(f"拒绝覆盖已有文件或链接：{info.filename}")
        with archive.open(info) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
        found = True
    if not found:
        raise ReportCliError(f"压缩包缺少 {prefix}")


def _peek_recipe_hash(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("reference/REFERENCE.json")
            if info.file_size > 2 * 1024 * 1024:
                return None
            raw = archive.read(info)
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError):
        return None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    digest = document.get("recipe_hash")
    return digest if isinstance(digest, str) and digest else None


def _reference_verifies(
    artifact_dir: Path, work_dir: Path, reference_dir: Path
) -> bool:
    if not (reference_dir / "REFERENCE.json").is_file():
        return False
    from rr.store import ReportStore

    try:
        ReportStore(
            artifact_dir, read_root=reference_dir, write_root=work_dir
        ).verify_reference()
    except ReportCliError:
        return False
    return True


def install_reference(
    *,
    artifact_dir: Path,
    workspace_root: Path,
    work_dir: Path,
    recipe_hash: str,
    bundle: Path | None = None,
) -> dict[str, Any]:
    """Unpack the job attachment selected by ``recipe_hash`` into ``work_dir``."""
    recipe_hash = recipe_hash.strip()
    if not re.fullmatch(r"[a-f0-9]{64}", recipe_hash):
        raise ReportCliError("--recipe-hash 必须是 SHA-256 十六进制摘要")
    selected = bundle or _find_bundle(workspace_root, artifact_dir, recipe_hash)
    if selected.stat().st_size > BUNDLE_LIMIT_BYTES:
        raise ReportCliError("参考包超过 20MB")
    found = _peek_recipe_hash(selected)
    if found != recipe_hash:
        raise ReportCliError(
            f"参考包的 recipe_hash 是 {found or '空'}，与 --recipe-hash {recipe_hash} 不一致"
        )
    reference_dir = work_dir / "reference" / recipe_hash
    if reference_dir.resolve() != reference_dir:
        raise ReportCliError("参考目录不能是链接")
    if not _reference_verifies(artifact_dir, work_dir, reference_dir):
        if reference_dir.exists():
            raise ReportCliError("已有参考目录校验失败，请使用新的工作目录")
        reference_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=reference_dir.parent) as tmp:
            staged = Path(tmp) / "reference"
            with zipfile.ZipFile(selected) as archive:
                _extract_prefixed(archive, "reference/", staged)
            if not _reference_verifies(artifact_dir, work_dir, staged):
                raise ReportCliError("解出的参考目录校验失败")
            staged.rename(reference_dir)
    seeded = False
    state_path = work_dir / "STATE.json"
    if not state_path.is_file():
        with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
            stage = Path(tmp)
            with zipfile.ZipFile(selected) as archive:
                _extract_prefixed(archive, "seed/", stage, forbid_roots=("reference",))
            if not (stage / "STATE.json").is_file():
                raise ReportCliError("参考包缺少 seed/STATE.json")
            # Publish the state marker last. Never overwrite user-edited run data.
            paths = sorted(
                (p for p in stage.rglob("*") if p.is_file()),
                key=lambda p: p == stage / "STATE.json",
            )
            created = []
            try:
                for path in paths:
                    relative = path.relative_to(stage)
                    if (
                        relative != Path("STATE.json")
                        and relative.parts[0] != "sections"
                    ):
                        raise ReportCliError("参考包包含未知 seed 文件")
                    target = work_dir / relative
                    if target.resolve() != target:
                        raise ReportCliError("seed 目标不能是链接")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as dest, path.open("rb") as src:
                        created.append(target)
                        shutil.copyfileobj(src, dest)
            except BaseException:
                for path in reversed(created):
                    path.unlink(missing_ok=True)
                raise
        seeded = True
    return {
        "reference_dir": _relative_to(artifact_dir, reference_dir)
        if reference_dir.resolve().is_relative_to(artifact_dir.resolve())
        else str(reference_dir),
        "work_dir": _relative_to(artifact_dir, work_dir)
        if work_dir.resolve().is_relative_to(artifact_dir.resolve())
        else str(work_dir),
        "recipe_hash": recipe_hash,
        "bundle": selected.name,
        "seeded": seeded,
        "next": "用返回的 reference_dir 与 work_dir 调用 plan-run",
    }


def _find_bundle(workspace_root: Path, artifact_dir: Path, recipe_hash: str) -> Path:
    uploads = workspace_root / "uploads" / artifact_dir.name
    if not uploads.is_dir() or uploads.resolve() != uploads:
        raise ReportCliError(f"上传目录不存在，找不到参考包：{uploads}")
    matches = [
        path
        for path in sorted(uploads.iterdir())
        if not path.is_symlink()
        and path.is_file()
        and path.stat().st_size <= BUNDLE_LIMIT_BYTES
        and path.suffix.lower() == ".zip"
        and _peek_recipe_hash(path) == recipe_hash
    ]
    if not matches:
        raise ReportCliError(
            f"上传目录里没有 recipe_hash={recipe_hash} 的参考包：{uploads}"
        )
    return matches[0]


def _drafts(work_dir: Path, period: str) -> dict[str, str]:
    sections = work_dir / "sections"
    found: dict[str, str] = {}
    if not sections.is_dir():
        return found
    for draft in sections.glob(f"*/runs/{period}/draft.md"):
        section_id = draft.relative_to(sections).parts[0]
        found[section_id] = draft.read_text(encoding="utf-8")
    return found


def _pop_affected(draft: str, difference: Any) -> bool:
    start = getattr(difference, "target_start", None)
    if not isinstance(start, int) or start < 0 or start > len(draft):
        context = ""
    else:
        line_start = draft.rfind("\n", 0, start) + 1
        line_end = draft.find("\n", start)
        context = draft[line_start:] if line_end < 0 else draft[line_start:line_end]
    return any(token in context for token in ("环比", "同比", "较上", "对比"))


def diff_run(*, period: str, base_dir: Path, candidate_dir: Path) -> dict[str, Any]:
    """Compare one period's drafts across two workspaces. Writes candidate ``diff.json``."""
    from rr.baseline import _comparison_kernel

    _unused_model, compare_documents = _comparison_kernel()
    del _unused_model
    base = _drafts(base_dir, period)
    candidate = _drafts(candidate_dir, period)
    sections: list[dict[str, Any]] = []
    for section_id in sorted(set(base) | set(candidate)):
        reference = base.get(section_id, "")
        target = candidate.get(section_id, "")
        result = compare_documents(reference, target, tolerance=DEFAULT_TOLERANCE)
        changed = [
            {
                "reference": difference.reference,
                "candidate": difference.target,
                "magnitude": difference.relative_error,
            }
            for difference in result.numeric_differences
            if not difference.matched
        ]
        structural = [
            difference.model_dump(mode="json")
            for difference in result.structure_differences
        ]
        affected = any(
            _pop_affected(target, difference)
            for difference in result.numeric_differences
            if not difference.matched
        )
        sections.append(
            {
                "section_id": section_id,
                "changed_metrics": changed,
                "structural_changes": structural,
                "period_over_period_affected": affected,
            }
        )
    payload = {"period": period, "sections": sections}
    _dump(candidate_dir / "diff.json", payload)
    return payload
