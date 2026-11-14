# -*- coding: utf-8 -*-
"""Generic file comparison tool."""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ComparisonResult",
    "DiffResult",
    "DiffRow",
    "DiffSpan",
    "DiffStats",
    "NumericDifference",
    "NumericToken",
    "StructureDifference",
    "compare_documents",
    "compare_numbers",
    "compare_structure",
    "compare_texts",
    "extract_numbers",
    "main",
]

EXIT_OK = 0
EXIT_ERROR = 1

SCHEMA_VERSION = 1
DEFAULT_TOLERANCE = 0.001
# Bound on how many difference rows the command echoes to stdout; the full list
# lives in the result file, which is what a UI reads.
SUMMARY_DIFFERENCE_LIMIT = 5
# Per-side line cap for the generic line diff; longer inputs are truncated and
# flagged so a huge file cannot make difflib's quadratic matching blow up the
# response body.
MAX_DIFF_LINES = 4000

_LOGGER = logging.getLogger("qwenpaw_data.compare")


class NumericDifference(BaseModel):
    """One reference number the target document did not reproduce."""

    model_config = ConfigDict(extra="forbid")

    reference: str
    target: str | None = None
    relative_error: float | None = None
    matched: bool
    note: str | None = None
    # Character span of `target` inside the target document, so a consumer can
    # highlight exactly the number that was compared instead of guessing by
    # string lookup. None when the reference number has no counterpart.
    target_start: int | None = None
    target_end: int | None = None


class StructureDifference(BaseModel):
    """One structural gap between the two documents."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    description: str
    severity: Literal["info", "warning", "error"] = "warning"


class ComparisonResult(BaseModel):
    """Outcome of comparing a target document against a reference document."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    target_path: str | None = None
    reference_path: str | None = None
    passed: bool
    passed_with_review: bool = False
    numeric_coverage: float = 0.0
    numeric_differences: list[NumericDifference] = Field(default_factory=list)
    structure_differences: list[StructureDifference] = Field(default_factory=list)
    criteria_analysis: list[dict[str, object]] = Field(default_factory=list)
    notes: str = ""
    tolerance: float = DEFAULT_TOLERANCE
    created_at: str = ""


# `(?<!\d-)` / `(?!-\d)` keep ISO-ish dates (`2026-08-03`, `2026-08`) out of the
# token stream: without them a date fragments into three "numbers" that then
# pair against unrelated target numbers and get highlighted at random places.
# ASCII word boundaries exclude identifiers while allowing Chinese amounts
# without spaces, such as 收入100万元 or 占比12.5%。
_NUMBER_RE = re.compile(
    r"(?<![a-zA-Z0-9_.])(?<!\d-)[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?!-\d)(?![a-zA-Z0-9_.])"
)


@dataclass(frozen=True)
class NumericToken:
    """One number found in a document, with its character span."""

    raw: str
    value: float
    start: int
    end: int


def extract_numbers(text: str) -> list[NumericToken]:
    """Extract comparable numbers in reading order."""
    tokens: list[NumericToken] = []
    for match in _NUMBER_RE.finditer(text):
        raw = match.group(0)
        normalized = raw.replace(",", "")
        percentage = normalized.endswith("%")
        if percentage:
            normalized = normalized[:-1]
        value = float(normalized)
        if percentage:
            value /= 100
        start, end = match.span()
        tokens.append(NumericToken(raw=raw, value=value, start=start, end=end))
    return tokens


@dataclass(frozen=True)
class _Pair:
    reference: NumericToken
    target: NumericToken | None


# Alignment costs. Pairing two unequal numbers is cheaper than skipping both, so
# a genuinely wrong counterpart is still paired (and reported); skipping is
# cheaper than pairing across a large positional distance, so an exact-equal
# number far away can never steal a positional counterpart.
_GAP_COST = 3.0
_PAIR_DIFF_COST = 3.0
_PAIR_CLOSE_COST = 1.0
_POSITION_WEIGHT = 4.0


def _pair_tokens(
    reference_tokens: list[NumericToken],
    target_tokens: list[NumericToken],
    *,
    tolerance: float,
) -> list[_Pair]:
    """Monotonic one-to-one pairing of reference numbers to target numbers."""
    n_reference = len(reference_tokens)
    n_target = len(target_tokens)
    # Identical numeric sequences have a zero-cost diagonal. Avoid constructing
    # the quadratic matrix for common HTML exports that differ only in styling.
    if n_reference == n_target and all(
        a.raw == b.raw for a, b in zip(reference_tokens, target_tokens)
    ):
        return [
            _Pair(reference=a, target=b)
            for a, b in zip(reference_tokens, target_tokens)
        ]
    # Keep two score rows and one byte per traceback decision instead of a
    # Python-float matrix (tens/hundreds of MB for long reports).
    decisions = [bytearray(n_target) for _ in range(n_reference)]
    next_row = [(n_target - j) * _GAP_COST for j in range(n_target + 1)]
    # Use a shared scale: appending a numeric note must not shift every earlier
    # counterpart toward a later table row just because target length increased.
    position_scale = max(n_reference, n_target, 1)
    target_positions = [j / position_scale for j in range(n_target)]
    for i in range(n_reference - 1, -1, -1):
        row = [0.0] * (n_target + 1)
        row[n_target] = (n_reference - i) * _GAP_COST
        reference = reference_tokens[i]
        reference_position = i / position_scale
        scale = max(abs(reference.value), 1e-12)
        for j in range(n_target - 1, -1, -1):
            target = target_tokens[j]
            base = (
                0.0
                if reference.raw == target.raw
                else (
                    _PAIR_CLOSE_COST
                    if abs(target.value - reference.value) / scale <= tolerance
                    else _PAIR_DIFF_COST
                )
            )
            paired = next_row[j + 1] + (
                base + _POSITION_WEIGHT * abs(reference_position - target_positions[j])
            )
            dropped = next_row[j] + _GAP_COST
            inserted = row[j + 1] + _GAP_COST
            best = min(paired, dropped, inserted)
            row[j] = best
            decisions[i][j] = (
                0
                if abs(best - paired) < 1e-9
                else 1
                if abs(best - dropped) < 1e-9
                else 2
            )
        next_row = row

    pairs: list[_Pair] = []
    i = 0
    j = 0
    while i < n_reference and j < n_target:
        if decisions[i][j] == 0:
            pairs.append(_Pair(reference=reference_tokens[i], target=target_tokens[j]))
            i += 1
            j += 1
        elif decisions[i][j] == 1:
            pairs.append(_Pair(reference=reference_tokens[i], target=None))
            i += 1
        else:
            j += 1
    while i < n_reference:
        pairs.append(_Pair(reference=reference_tokens[i], target=None))
        i += 1
    return pairs


def compare_numbers(
    reference: str,
    target: str,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> tuple[float, list[NumericDifference]]:
    """Return the coverage and the numeric differences of two documents."""
    reference_tokens = extract_numbers(reference)
    target_tokens = extract_numbers(target)
    if not reference_tokens:
        return 1.0, []

    differences: list[NumericDifference] = []
    matched = 0
    for pair in _pair_tokens(reference_tokens, target_tokens, tolerance=tolerance):
        relative_error: float | None = None
        is_matched = False
        if pair.target is not None:
            scale = max(abs(pair.reference.value), 1e-12)
            relative_error = abs(pair.target.value - pair.reference.value) / scale
            is_matched = relative_error <= tolerance
        if is_matched:
            matched += 1
        else:
            differences.append(
                NumericDifference(
                    reference=pair.reference.raw,
                    target=pair.target.raw if pair.target else None,
                    relative_error=relative_error,
                    matched=False,
                    target_start=pair.target.start if pair.target else None,
                    target_end=pair.target.end if pair.target else None,
                )
            )
    return matched / len(reference_tokens), differences


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+", re.MULTILINE)


def compare_structure(reference: str, target: str) -> list[StructureDifference]:
    """Report missing headings, missing tables and a suspiciously short target."""
    differences: list[StructureDifference] = []
    reference_headings = _HEADING_RE.findall(reference)
    target_headings = _HEADING_RE.findall(target)
    if reference_headings and not target_headings:
        differences.append(
            StructureDifference(
                kind="headings",
                description="目标文档缺少 Markdown 标题结构",
                severity="error",
            )
        )

    reference_tables = len(_TABLE_SEPARATOR_RE.findall(reference))
    target_tables = len(_TABLE_SEPARATOR_RE.findall(target))
    if reference_tables > target_tables:
        differences.append(
            StructureDifference(
                kind="tables",
                description=(
                    f"参考文档包含 {reference_tables} 个表格，目标文档仅包含 "
                    f"{target_tables} 个"
                ),
            )
        )

    if len(target.strip()) < max(20, int(len(reference.strip()) * 0.15)):
        differences.append(
            StructureDifference(
                kind="content",
                description="目标文档内容明显短于参考文档，可能遗漏结论或解释",
                severity="error",
            )
        )
    return differences


def compare_documents(
    reference: str,
    target: str,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> ComparisonResult:
    """Compare two documents and return the machine verdict."""
    coverage, numeric_differences = compare_numbers(
        reference,
        target,
        tolerance=tolerance,
    )
    structure_differences = compare_structure(reference, target)
    structural_errors = any(
        difference.severity == "error" for difference in structure_differences
    )
    passed = not numeric_differences and not structural_errors
    notes = (
        "比对通过：数值与结构均无差异。"
        if passed
        else "仍存在需要修正的数值或结构差异。"
    )
    return ComparisonResult(
        passed=passed,
        numeric_coverage=coverage,
        numeric_differences=numeric_differences,
        structure_differences=structure_differences,
        notes=notes,
        tolerance=tolerance,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


class DiffSpan(BaseModel):
    """One changed character range inside a diff line (half-open interval)."""

    model_config = ConfigDict(extra="forbid")

    start: int
    end: int


class DiffRow(BaseModel):
    """One aligned line pair of a side-by-side diff."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["equal", "insert", "delete", "replace"]
    ref_no: int | None = None
    tgt_no: int | None = None
    ref_text: str = ""
    tgt_text: str = ""
    ref_spans: list[DiffSpan] = Field(default_factory=list)
    tgt_spans: list[DiffSpan] = Field(default_factory=list)


class DiffStats(BaseModel):
    """Row counts per diff type, for a summary line without walking rows."""

    model_config = ConfigDict(extra="forbid")

    equal: int = 0
    insert: int = 0
    delete: int = 0
    replace: int = 0


class DiffResult(BaseModel):
    """Outcome of a generic line-by-line comparison of two text documents."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    target_path: str | None = None
    reference_path: str | None = None
    rows: list[DiffRow] = Field(default_factory=list)
    stats: DiffStats = Field(default_factory=DiffStats)
    truncated: bool = False
    created_at: str = ""


def _inline_spans(reference: str, target: str) -> tuple[list[DiffSpan], list[DiffSpan]]:
    """Character-level changed ranges of two aligned lines (vscode-style)."""
    matcher = difflib.SequenceMatcher(a=reference, b=target, autojunk=False)
    ref_spans: list[DiffSpan] = []
    tgt_spans: list[DiffSpan] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            ref_spans.append(DiffSpan(start=i1, end=i2))
        if j2 > j1:
            tgt_spans.append(DiffSpan(start=j1, end=j2))
    return ref_spans, tgt_spans


def compare_texts(
    reference: str,
    target: str,
    *,
    max_lines: int = MAX_DIFF_LINES,
) -> DiffResult:
    """Diff two documents line by line and return aligned, highlighted rows."""
    ref_lines = reference.splitlines()
    tgt_lines = target.splitlines()
    truncated = len(ref_lines) > max_lines or len(tgt_lines) > max_lines
    if truncated:
        ref_lines = ref_lines[:max_lines]
        tgt_lines = tgt_lines[:max_lines]

    rows: list[DiffRow] = []
    stats = DiffStats()
    matcher = difflib.SequenceMatcher(a=ref_lines, b=tgt_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append(
                    DiffRow(
                        type="equal",
                        ref_no=i1 + offset + 1,
                        tgt_no=j1 + offset + 1,
                        ref_text=ref_lines[i1 + offset],
                        tgt_text=tgt_lines[j1 + offset],
                    )
                )
            stats.equal += i2 - i1
        elif tag == "delete":
            for index in range(i1, i2):
                rows.append(
                    DiffRow(type="delete", ref_no=index + 1, ref_text=ref_lines[index])
                )
            stats.delete += i2 - i1
        elif tag == "insert":
            for index in range(j1, j2):
                rows.append(
                    DiffRow(type="insert", tgt_no=index + 1, tgt_text=tgt_lines[index])
                )
            stats.insert += j2 - j1
        else:  # replace
            ref_block = ref_lines[i1:i2]
            tgt_block = tgt_lines[j1:j2]
            for offset in range(max(len(ref_block), len(tgt_block))):
                ref_line = ref_block[offset] if offset < len(ref_block) else None
                tgt_line = tgt_block[offset] if offset < len(tgt_block) else None
                if ref_line is not None and tgt_line is not None:
                    ref_spans, tgt_spans = _inline_spans(ref_line, tgt_line)
                    rows.append(
                        DiffRow(
                            type="replace",
                            ref_no=i1 + offset + 1,
                            tgt_no=j1 + offset + 1,
                            ref_text=ref_line,
                            tgt_text=tgt_line,
                            ref_spans=ref_spans,
                            tgt_spans=tgt_spans,
                        )
                    )
                    stats.replace += 1
                elif ref_line is not None:
                    rows.append(
                        DiffRow(
                            type="delete",
                            ref_no=i1 + offset + 1,
                            ref_text=ref_line,
                        )
                    )
                    stats.delete += 1
                elif tgt_line is not None:
                    rows.append(
                        DiffRow(
                            type="insert",
                            tgt_no=j1 + offset + 1,
                            tgt_text=tgt_line,
                        )
                    )
                    stats.insert += 1
    return DiffResult(
        rows=rows,
        stats=stats,
        truncated=truncated,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _resolve_path(raw: str, root: Path | None) -> Path:
    """Resolve one command line path, rejecting anything outside ``root``."""
    candidate = Path(raw).expanduser()
    if root is not None and not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if root is not None and resolved != root and root not in resolved.parents:
        raise ValueError(f"路径超出 --root 范围（{root}）：{raw}")
    return resolved


def _read_document(path: Path) -> str:
    if not path.exists():
        raise ValueError(f"文件不存在：{path}")
    if path.is_dir():
        raise ValueError(f"期望文件但拿到目录：{path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"文件不是 utf-8 文本：{path}") from exc


def _display_path(path: Path, root: Path | None) -> str:
    """Record a compared path relative to the confinement root when given."""
    if root is None:
        return str(path)
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _write_result(result: BaseModel, path: Path) -> None:
    """Write the result atomically so no reader sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as stream:
        stream.write(result.model_dump_json(indent=2) + "\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def _summary(result: ComparisonResult, output: str) -> dict[str, object]:
    """Build the stdout digest; the result file keeps the full detail."""
    severity_counts: dict[str, int] = {"error": 0, "warning": 0, "info": 0}
    for difference in result.structure_differences:
        severity_counts[difference.severity] += 1
    return {
        "ok": True,
        "passed": result.passed,
        "numeric_coverage": result.numeric_coverage,
        "tolerance": result.tolerance,
        "numeric_difference_count": len(result.numeric_differences),
        "numeric_differences": [
            difference.model_dump(mode="json")
            for difference in result.numeric_differences[:SUMMARY_DIFFERENCE_LIMIT]
        ],
        "structure_difference_counts": severity_counts,
        "structure_differences": [
            difference.model_dump(mode="json")
            for difference in result.structure_differences[:SUMMARY_DIFFERENCE_LIMIT]
        ],
        "output": output,
    }


def _summary_diff(result: DiffResult, output: str) -> dict[str, object]:
    """Build the stdout digest for a line diff; the file keeps every row."""
    return {
        "ok": True,
        "mode": "lines",
        "stats": result.stats.model_dump(mode="json"),
        "row_count": len(result.rows),
        "truncated": result.truncated,
        "output": output,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m qwenpaw_data.host.core.agent.tools.compare",
        description=(
            "比对两份文档的数值与 Markdown 结构差异，结果原子写入 JSON 文件，"
            "摘要打印到 stdout。"
        ),
        epilog=(
            "示例：python -m qwenpaw_data.host.core.agent.tools.compare "
            "--target draft.md --reference source.md --output comparison.json"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["numeric", "lines"],
        default="numeric",
        help=(
            "比对模式：numeric=数值与 Markdown 结构验收（默认）；"
            "lines=通用逐行 diff（vscode 风格，行级 + 行内高亮）"
        ),
    )
    parser.add_argument("--target", required=True, help="目标文档路径（被检查的一方）")
    parser.add_argument(
        "--reference", required=True, help="参考文档路径（作为基准的一方）"
    )
    parser.add_argument("--output", required=True, help="比对结果 JSON 的输出路径")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help="数值相对误差容差，取值 0 到 1（默认 %(default)s）",
    )
    parser.add_argument(
        "--root",
        default=None,
        help=(
            "路径安全根目录；给出后三个路径都必须落在其中，否则报错退出，"
            "相对路径也按此目录解析"
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="info",
        help="日志级别，日志一律走 stderr（默认 %(default)s）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Compare two documents from the command line and write the result file."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )

    if not 0 <= args.tolerance <= 1:
        _LOGGER.error("--tolerance 必须在 0 到 1 之间，收到 %s", args.tolerance)
        return EXIT_ERROR

    root: Path | None = None
    if args.root is not None:
        root = Path(args.root).expanduser().resolve()
        if not root.is_dir():
            _LOGGER.error("--root 不是已存在的目录：%s", root)
            return EXIT_ERROR

    try:
        target = _resolve_path(args.target, root)
        reference = _resolve_path(args.reference, root)
        output = _resolve_path(args.output, root)
        target_text = _read_document(target)
        reference_text = _read_document(reference)
    except ValueError as exc:
        _LOGGER.error("%s", exc)
        return EXIT_ERROR

    paths_update = {
        "target_path": _display_path(target, root),
        "reference_path": _display_path(reference, root),
    }
    if args.mode == "lines":
        diff = compare_texts(reference_text, target_text).model_copy(
            update=paths_update
        )
        try:
            _write_result(diff, output)
        except OSError as exc:
            _LOGGER.error("写入比对结果失败：%s（%s）", output, exc)
            return EXIT_ERROR
        summary = _summary_diff(diff, _display_path(output, root))
    else:
        result = compare_documents(
            reference_text,
            target_text,
            tolerance=args.tolerance,
        ).model_copy(update=paths_update)
        try:
            _write_result(result, output)
        except OSError as exc:
            _LOGGER.error("写入比对结果失败：%s（%s）", output, exc)
            return EXIT_ERROR
        summary = _summary(result, _display_path(output, root))

    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
