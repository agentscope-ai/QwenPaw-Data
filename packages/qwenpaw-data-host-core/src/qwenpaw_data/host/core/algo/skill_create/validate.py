# -*- coding: utf-8 -*-
"""Deterministic checks on a generated skill files map."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

from qwenpaw_data.host.core.algo.skill_create.files import (
    cited_payload_paths,
    excerpt_for_path,
    flags_in_text,
    skill_payload_files,
)
from qwenpaw_data.host.core.utils.skill import parse_skill_frontmatter_text

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
STRONG_BOUNDARY_PATTERNS: dict[str, re.Pattern[str]] = {
    "sql_block": re.compile(r"^\s*```\s*sql", re.I),
    "column_literal_filter": re.compile(
        r"\b(?:WHERE|AND|OR|ON|HAVING)\b[^\n]*?\b[a-z][a-z0-9_]{2,}\s*=\s*'[^']+'",
        re.I,
    ),
}
WEAK_BOUNDARY_PATTERNS: dict[str, re.Pattern[str]] = {
    "warehouse_table": re.compile(
        r"\b(?:dwd|dws|ads|ods|dim)_[a-z0-9]+(?:_[a-z0-9]+){2,}", re.I
    ),
    "caliber_wording": re.compile(r"定义为|计算口径|判定口径"),
    "enumeration_semantics": re.compile(r"枚举值[对照是为]|=\s*\d+\s*(?:表示|即|是)"),
}
BOUNDARY_HINT = "见 references/capture-boundary.md"
HARDCODED_DATE_RE = re.compile(r"\b20\d{2}[-/]?(0[1-9]|1[0-2])\b")
FORBIDDEN_FILES = {"README.md", "CHANGELOG.md", "readme.md", "changelog.md"}
CROSS_SKILL_PATH_RE = re.compile(
    r"skill-hub/(?:routing|planning|workflows|atomic|runtime|meta|domains)/"
    r"(?P<skill>[a-z0-9-]+)/(?P<rest>[A-Za-z0-9_./-]+\.(?:py|md|html|json))"
)
MAX_SKILL_MD_LINES = 500
REFERENCE_TOC_THRESHOLD = 300


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    severity: str = "error"


@dataclass
class ValidationReport:
    skill_name: str
    checks: list[Check] = field(default_factory=list)

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "checks": [asdict(item) for item in self.checks],
        }

    def summary(self) -> str:
        passed = sum(1 for item in self.checks if item.passed)
        return (
            f"{passed}/{len(self.checks)} 项通过"
            f"｜错误 {len(self.errors)}｜警告 {len(self.warnings)}"
        )

    def error_text(self) -> str:
        if self.ok:
            return ""
        return "\n".join(f"- {item.name}: {item.detail}" for item in self.errors)


def validate_files(
    files: dict[str, str],
    *,
    taken_names: set[str],
    expected_name: str = "",
    planned_paths: set[str] | None = None,
) -> ValidationReport:
    payload = skill_payload_files(files)
    report = ValidationReport(skill_name=expected_name)
    add = report.checks.append
    text = payload.get("SKILL.md")
    if not isinstance(text, str) or not text.strip():
        add(Check("skill_md_exists", False, "缺少 SKILL.md"))
        return report
    add(Check("skill_md_exists", True, "SKILL.md 存在"))

    try:
        meta = parse_skill_frontmatter_text(text, source="SKILL.md")
        name = str(meta.get("name") or "").strip()
        description = str(meta.get("description") or "").strip()
    except ValueError as exc:
        add(Check("frontmatter_parsed", False, str(exc)))
        return report

    _check_frontmatter(add, text, name, description, expected_name)
    _check_chinese(add, payload, text, description)
    _check_collision(add, taken_names, name, expected_name)
    _check_body(add, text)
    _check_planned(add, text, planned_paths)
    _check_bundles(add, payload, text)
    _check_script_flags(add, payload, text)
    _check_capture_boundary(add, payload)
    _check_generalization(add, payload)
    _check_self_contained(add, payload, name)
    return report


def _check_frontmatter(
    add,
    text: str,
    name: str,
    description: str,
    expected_name: str,
) -> None:
    add(Check("frontmatter_parsed", bool(name and description), f"name={name!r}"))
    add(
        Check(
            "name_format",
            bool(name) and bool(NAME_RE.match(name)) and len(name) <= 64,
            f"name={name!r}（要求小写连字符、≤64 字符）",
        )
    )
    if expected_name:
        add(
            Check(
                "name_matches_decision",
                name == expected_name,
                f"{name!r} vs 决策 {expected_name!r}",
            )
        )
    add(
        Check(
            "description_has_trigger",
            any(key in description for key in ("触发", "当", "时")),
            "description 需说明何时触发",
            severity="warn",
        )
    )
    add(
        Check(
            "description_length",
            20 <= len(description) <= 600,
            f"description 长度 {len(description)}",
            severity="warn",
        )
    )
    body = text.split("---", 2)[-1]
    add(
        Check(
            "no_trigger_in_body",
            "触发条件" not in body,
            "触发条件应只写在 frontmatter",
            severity="warn",
        )
    )


def _han_count(text: str) -> int:
    return len(_HAN_RE.findall(text))


def _check_chinese(add, files: dict[str, str], text: str, description: str) -> None:
    offenders: list[str] = []
    if _han_count(description) < 8:
        offenders.append("description")
    body = text.split("---", 2)[-1]
    if _han_count(body) < 30:
        offenders.append("SKILL.md")
    for path, content in sorted(files.items()):
        if not path.startswith("references/") or not path.endswith(".md"):
            continue
        if content.strip() and _han_count(content) < 10:
            offenders.append(path)
    add(
        Check(
            "human_text_zh",
            not offenders,
            f"面向人读的文字需用中文：{offenders}" if offenders else "中文",
        )
    )


def _check_collision(
    add,
    taken_names: set[str],
    name: str,
    expected_name: str,
) -> None:
    clash = name in taken_names and name != expected_name
    add(
        Check(
            "no_name_collision",
            not clash,
            f"重名于既有技能 {name!r}" if clash else "无重名",
        )
    )


def _check_body(add, text: str) -> None:
    lines = text.splitlines()
    add(
        Check(
            "skill_md_length",
            len(lines) <= MAX_SKILL_MD_LINES,
            f"{len(lines)} 行（上限 {MAX_SKILL_MD_LINES}）",
        )
    )
    add(
        Check(
            "code_fences_balanced",
            text.count("```") % 2 == 0,
            f"围栏计数 {text.count('```')}",
        )
    )
    add(Check("skill_md_nonempty", len(text.strip()) > 200, f"{len(text)} 字符"))


def _check_planned(add, text: str, planned_paths: set[str] | None) -> None:
    if planned_paths is None:
        return
    planned_bundle = sorted(path for path in planned_paths if path != "SKILL.md")
    missing = [
        PurePosixPath(path).name
        for path in planned_bundle
        if PurePosixPath(path).name not in text
    ]
    add(
        Check(
            "planned_referenced",
            not missing,
            f"计划文件未在 SKILL.md 引用：{missing}" if missing else "计划文件均已引用",
        )
    )
    cited = cited_payload_paths(text)
    unplanned = sorted(path for path in cited if path not in planned_paths)
    add(
        Check(
            "no_unplanned_refs",
            not unplanned,
            f"引用了计划外路径：{unplanned}" if unplanned else "无计划外引用",
        )
    )
    if any(path.startswith("scripts/") for path in planned_bundle):
        add(
            Check(
                "has_fallback",
                "fallback" in text.lower(),
                "计划含脚本但未写 fallback",
                severity="warn",
            )
        )


def _check_script_flags(add, files: dict[str, str], text: str) -> None:
    missing: list[str] = []
    for path, content in sorted(files.items()):
        if not path.startswith("scripts/") or not path.endswith(".py"):
            continue
        flags = flags_in_text(excerpt_for_path(text, path))
        absent = [flag for flag in flags if flag not in content]
        if absent:
            missing.append(f"{path}: {absent}")
    add(
        Check(
            "script_flags_match_skill_md",
            not missing,
            f"脚本未实现 SKILL.md 参数：{missing}"
            if missing
            else "脚本参数与 SKILL.md 一致",
        )
    )


def _check_bundles(add, files: dict[str, str], text: str) -> None:
    stray = [key for key in files if PurePosixPath(key).name in FORBIDDEN_FILES]
    add(Check("no_aux_docs", not stray, f"多余文档 {stray}" if stray else "无"))

    scripts = [
        key for key in files if key.startswith("scripts/") and key.endswith(".py")
    ]
    if scripts:
        unreferenced = [
            PurePosixPath(key).name
            for key in scripts
            if PurePosixPath(key).name not in text
        ]
        add(
            Check(
                "scripts_referenced",
                not unreferenced,
                f"未在 SKILL.md 引用：{unreferenced}" if unreferenced else "全部已引用",
            )
        )
        no_cli = [
            PurePosixPath(key).name for key in scripts if "argparse" not in files[key]
        ]
        add(
            Check(
                "scripts_have_cli",
                not no_cli,
                f"缺少 argparse：{no_cli}" if no_cli else "均有命令行入口",
                severity="warn",
            )
        )
        add(
            Check(
                "has_fallback",
                "fallback" in text.lower(),
                "有脚本但未写 fallback",
                severity="warn",
            )
        )

    refs = [
        key for key in files if key.startswith("references/") and key.endswith(".md")
    ]
    if refs:
        unreferenced = [
            PurePosixPath(key).name
            for key in refs
            if PurePosixPath(key).name not in text
        ]
        add(
            Check(
                "references_referenced",
                not unreferenced,
                f"未在 SKILL.md 引用：{unreferenced}" if unreferenced else "全部已引用",
            )
        )
        missing_toc = [
            PurePosixPath(key).name
            for key in refs
            if len(files[key].splitlines()) > REFERENCE_TOC_THRESHOLD
            and "## 目录" not in files[key]
        ]
        add(
            Check(
                "long_references_have_toc",
                not missing_toc,
                f"超长且缺目录：{missing_toc}" if missing_toc else "符合",
                severity="warn",
            )
        )


def _scan_lines(files: dict[str, str], pattern: re.Pattern[str]) -> list[str]:
    hits: list[str] = []
    for rel, content in sorted(files.items()):
        if not rel.endswith((".md", ".py")):
            continue
        for num, line in enumerate(content.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{rel}:{num}")
    return hits


def _check_capture_boundary(add, files: dict[str, str]) -> None:
    for name, rx in STRONG_BOUNDARY_PATTERNS.items():
        found = _scan_lines(files, rx)
        add(
            Check(
                f"no_{name}",
                not found,
                f"{len(found)} 处：{found[:5]}（{BOUNDARY_HINT}）"
                if found
                else "未发现",
            )
        )
    for name, rx in WEAK_BOUNDARY_PATTERNS.items():
        found = _scan_lines(files, rx)
        add(
            Check(
                f"no_{name}",
                not found,
                f"{len(found)} 处待人工判断：{found[:5]}（{BOUNDARY_HINT}）"
                if found
                else "未发现",
                severity="warn",
            )
        )


def _check_generalization(add, files: dict[str, str]) -> None:
    offenders = _scan_lines(files, HARDCODED_DATE_RE)
    add(
        Check(
            "no_hardcoded_dates",
            not offenders,
            f"疑似写死日期：{offenders}" if offenders else "未发现写死日期",
            severity="warn",
        )
    )


def _check_self_contained(add, files: dict[str, str], own: str) -> None:
    offenders: list[str] = []
    for rel, content in sorted(files.items()):
        if not rel.endswith((".md", ".py")):
            continue
        for num, line in enumerate(content.splitlines(), 1):
            for match in CROSS_SKILL_PATH_RE.finditer(line):
                if match.group("skill") != own:
                    offenders.append(f"{rel}:{num} -> {match.group(0)}")
    add(
        Check(
            "no_cross_skill_paths",
            not offenders,
            f"引用了其它技能目录内的文件：{offenders[:5]}"
            if offenders
            else "未发现跳出本目录的文件引用",
            severity="warn",
        )
    )
