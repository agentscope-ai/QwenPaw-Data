# -*- coding: utf-8 -*-
"""BI 报告生成器

将多个章节片段拼接成完整的 HTML 报告。

职责边界：**这个脚本只做组装，不做格式重建。**

格式还原的责任在 Agent 手上——读 `format_spec.json`、按原报告的 DOM 契约写章节
片段和 Jinja 模板。脚本这边只负责四件事：

1. 把 `sections_*.json` 渲染进骨架（或 `--template` 指定的模板）；章节既可带手写的
   `html_fragment`，也可带声明式 `blocks`（KPI / 表格 / 下钻表 / 图表 / 时间线…，
   数据读自文件，HTML 由 `report_blocks.py` 生成，见 SKILL.md）；
2. 让图表在 ECharts 加载失败时不留空白框；
3. 复现模式下把**原报告的 `<style>` 原文**清洗后注入页面；
4. 复现模式下拿生成结果对账 `dom_contract` 里记录的原报告结构数量，塌陷即失败
   退出——堵住“只复刻外壳、把上千行数据换成几行占位”的假通过。

产物默认**完全离线**：Tailwind 子集、报告主题（`resources/theme.css`）、图标字体、
ECharts、图表兜底运行时全部内联
（`resources/report.css` / `icons.css` 由 `scripts/build_report_css.py` 生成）。生成后
还会审计外链、图表容器、下钻 id、未定义的类名与过程性内容，这些审计就是 QC，不需要
再另写校验脚本。

Usage:
    # 单主题
    python report_builder.py --sections sections_001.json --output report.html

    # 多主题
    python report_builder.py --sections sections_001.json sections_002.json \
        --output report.html \
        --report-title "XXX 产品数据分析报告"

    # 报告复现（注入原报告样式）
    python report_builder.py --sections runs/202603/sections_*.json \
        --format-spec format_spec.json --output runs/202603/report.html
"""

import argparse
import html as _html
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jinja2 import Template

# 同目录模块：脚本以文件路径运行（也可能被测试按路径导入），不能假定它在 sys.path 里。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_blocks  # noqa: E402

_RESOURCES_DIR = Path(__file__).parent.parent / "resources"

_CHART_FALLBACK_MARKER = "data-qwenpaw-data-chart-fallback-support"

# --- 原报告 CSS 透传的安全边界 -------------------------------------------------
#
# 透传的是别人写的样式表，所以清洗不是为了"好看"，是为了防三类事故：
#
# 1. `<` 开头的标签（尤其是 `</style>`）提前闭合样式块，把后面的 CSS 变成页面
#    正文，甚至注入可执行 HTML；
# 2. `@import` / `url()` 拉远程资源，让离线打开的报告去联网；
# 3. `expression()` / `behavior:` / `-moz-binding` 这类老 IE 的 CSS 执行通道。
#
# 处理方式是**就地中和**而不是丢弃整条规则。实测 22 份复现产物对应的原报告
# （2 份不同基线，CSS 均为 3 个 <style> 块 / 9912 字符）里这些模式计数全为 0，
# 清洗结果与原文逐字符一致，所以对正常数据零损伤，只在遇到脏输入时才生效。

_CSS_STYLE_ESCAPE_RE = re.compile(r"<\s*/\s*style[^<>]*>?", re.I)
# 整段摘掉，连标签之间的正文一起：只摘标签会留下 `alert(1)` 这类文本。
# 闭合标签用反向引用 \1 卡住名字，否则一个未闭合的 `<script>` 会一路吞到下一个
# 任意 `</xxx>`，把大片合法 CSS 一并删掉。
_CSS_SCRIPT_BLOCK_RE = re.compile(
    r"<\s*(script|iframe|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.DOTALL | re.I,
)
_CSS_AT_IMPORT_RE = re.compile(r"@import\s[^;]*;?", re.I)
# 允许一层嵌套括号，否则 `expression(alert(2))` 只匹到一半，留下悬空的 `)`。
_CSS_EXPRESSION_RE = re.compile(r"\bexpression\s*\((?:[^()]|\([^()]*\))*\)", re.I)
_CSS_BINDING_DECL_RE = re.compile(r"(?:behavior|-moz-binding)\s*:[^;}]*;?", re.I)
_CSS_URL_RE = re.compile(r"url\(\s*(\"[^\"]*\"|'[^']*'|[^)]*)\s*\)", re.I)
_CSS_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL | re.I)

# CSS 语法里不需要裸 `<`，它只可能出现在引号字符串里（如 `content:"<span>"`）。
# 所以把两者写成一个交替：组 1 是字符串，原样留着；匹不上组 1 的就是标签，摘掉。
_CSS_TAG_OR_STRING_RE = re.compile(
    r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
    r"|<\s*/?\s*[a-zA-Z!?][^<>]*>?"
)

# url() 只放行这两种：内联图片资源，以及已经是 https 的静态资源。
_ALLOWED_URL_PREFIXES = ("data:image/", "https://")

# --- dom_contract 体量对账（复现模式防“空壳”） --------------------------------
#
# format-extractor 在 dom_contract 里给每个结构模式记了原报告的实测出现次数
# count。复现产物若把上千行的实体表 / 上万个涨跌色单元格塌成个位数，光看“我有
# 几个章节标题”根本发现不了——必须拿生成结果的实际数量去和 count 对账。
#
# 判定用下限比例而不是精确相等：复现的是新周期，数据行数本就会和原报告有合理
# 出入；但绝不能从上千掉到个位数。实测低于 ceil(count*RATIO)（且不足 MIN）即塌陷。
_DOM_RECONCILE_RATIO = 0.34
_DOM_RECONCILE_MIN = 1
_BUILDER_MARKER = "qwenpaw-data-report-builder"

# 数结构数量用：开始标签 + class 属性。只认标签名和 class token，够体量对账用。
_TAG_OPEN_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>", re.S)
_CLASS_ATTR_RE = re.compile(r"""class\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_ID_ATTR_RE = re.compile(r"""\bid\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
# 组合器（> + ~ 和后代空格）：取最右边的 compound 来数元素本身。
_SELECTOR_COMBINATOR_RE = re.compile(r"\s*[>+~]\s*|\s+")
# 图表容器识别（与 chart_fallback.html 的 __qwenpawDataRenderMissingChartFallback 扫描口径
# 对齐）：id 以 chart 开头的 div，或 class 含 chart-container 的 div。
_CHART_ID_PREFIX = "chart"
_CHART_CONTAINER_CLASS = "chart-container"


def sanitize_css(css: str) -> Tuple[str, List[str]]:
    """中和原报告 CSS 里的危险构造，返回 (可注入的 CSS, 处理说明)。

    说明列表用于打印审计：正常情况下它应该是空的，一旦非空就说明原报告里有
    值得警惕的东西，Agent 应当知道样式被改过。
    """
    notes: List[str] = []
    cleaned = css

    blocks = len(_CSS_SCRIPT_BLOCK_RE.findall(cleaned))
    if blocks:
        cleaned = _CSS_SCRIPT_BLOCK_RE.sub("", cleaned)
        notes.append(f"移除 {blocks} 个 script/iframe/object/embed 整段")

    # `</style` 不管出现在哪里都会让浏览器提前闭合样式块——HTML 解析器不看 CSS
    # 的引号，所以连 `content:"</style>"` 也得无条件摘掉。
    escapes = len(_CSS_STYLE_ESCAPE_RE.findall(cleaned))
    if escapes:
        cleaned = _CSS_STYLE_ESCAPE_RE.sub("", cleaned)

    # 其余标签只在引号外摘，保住 content:"<span>" 这类合法字符串。
    stripped: List[str] = []

    def _drop_tag(match: "re.Match[str]") -> str:
        if match.group(1):
            return match.group(1)  # 引号字符串，合法，原样保留
        stripped.append(match.group(0))
        return ""

    cleaned = _CSS_TAG_OR_STRING_RE.sub(_drop_tag, cleaned)
    if escapes or stripped:
        detail = f"，其中 {escapes} 处试图提前闭合 <style>" if escapes else ""
        notes.append(f"移除 {escapes + len(stripped)} 处 CSS 内嵌 HTML 标签{detail}")

    imports = len(_CSS_AT_IMPORT_RE.findall(cleaned))
    if imports:
        cleaned = _CSS_AT_IMPORT_RE.sub("", cleaned)
        notes.append(f"移除 {imports} 条 @import")

    expressions = len(_CSS_EXPRESSION_RE.findall(cleaned))
    if expressions:
        cleaned = _CSS_EXPRESSION_RE.sub("inherit", cleaned)
        notes.append(f"中和 {expressions} 处 expression()")

    bindings = len(_CSS_BINDING_DECL_RE.findall(cleaned))
    if bindings:
        cleaned = _CSS_BINDING_DECL_RE.sub("", cleaned)
        notes.append(f"移除 {bindings} 条 behavior/-moz-binding")

    blocked: List[str] = []

    def _replace_url(match: "re.Match[str]") -> str:
        raw = match.group(1).strip().strip("\"'").strip()
        if raw.lower().startswith(_ALLOWED_URL_PREFIXES):
            return match.group(0)
        blocked.append(raw[:60])
        return "none"

    cleaned = _CSS_URL_RE.sub(_replace_url, cleaned)
    if blocked:
        notes.append(f"屏蔽 {len(blocked)} 个非白名单 url()：{', '.join(blocked[:5])}")

    return cleaned.strip(), notes


def load_format_spec(format_spec_path: Optional[str]) -> Dict[str, Any]:
    """加载 report-format-extractor 产出的视觉规范。"""
    if not format_spec_path:
        return {}
    path = Path(format_spec_path)
    if not path.is_file():
        print(f"警告: format spec 不存在，跳过样式注入: {path}")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            spec = json.load(f)
    except (OSError, json.JSONDecodeError) as error:
        print(f"警告: format spec 无法解析，跳过样式注入: {error}")
        return {}
    return spec if isinstance(spec, dict) else {}


def _wrap_style(css: str, origin: str) -> str:
    """给清洗后的 CSS 套上 `<style>` 和一行来源注释。"""
    return f"<style>\n/* 原报告样式透传: {origin} */\n{css}\n</style>"


def _extract_source_css(
    spec: Dict[str, Any],
    task_root: Optional[Path],
) -> Tuple[str, List[str], str]:
    """按 `spec["source"]["path"]` 打开原报告，抽出全部 `<style>` 并清洗。

    返回 (CSS, 处理说明, 来源描述)；抽不到时 CSS 为空字符串，说明里写清原因。
    """
    source = spec.get("source")
    if not isinstance(source, dict):
        return "", ["format spec 缺少 source 字段，未注入原报告样式"], ""
    if str(source.get("type", "")).lower() != "html":
        return "", [f"原报告类型为 {source.get('type')!r}，无 CSS 可透传"], ""

    relative = source.get("path")
    if not relative or not isinstance(relative, str):
        return "", ["format spec 的 source.path 缺失，未注入原报告样式"], ""

    root = task_root if task_root is not None else Path(".")
    source_path = (root / relative).resolve()
    if not source_path.is_file():
        return "", [f"原报告不存在: {source_path}，未注入样式"], ""

    try:
        html = source_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return "", [f"原报告读取失败: {error}"], ""

    blocks = _CSS_STYLE_BLOCK_RE.findall(html)
    if not blocks:
        return "", [f"原报告没有 <style> 块: {source_path.name}"], ""

    # 原报告常有多个 <style>（基础布局 + 主题覆盖），顺序即层叠优先级，原样保留。
    raw = "\n".join(block.strip() for block in blocks if block.strip())
    cleaned, notes = sanitize_css(raw)
    if not cleaned:
        return "", ["原报告 CSS 清洗后为空"], ""

    origin = f"{source_path.name} · {len(blocks)} 个 <style> 块 · {len(raw)} 字符"
    return cleaned, notes, origin


def build_format_css(
    spec: Dict[str, Any],
    task_root: Optional[Path] = None,
) -> Tuple[str, List[str]]:
    """取出原报告 CSS 并清洗，返回 (可注入的 `<style>`, 处理说明)。

    两个来源，按优先级：

    1. `spec["source_css"]`——extractor 已经落盘的 `<style>` 原文，不依赖原报告
       文件是否还在；
    2. `spec["source"]["path"]`——现读现抽，用于没有 `source_css` 的存量 spec。

    两边都拿不到就返回空字符串——此时页面退回模板自带样式，不报错也不猜：一份
    没有原报告的普通 BI 分析本来就没有样式要还原。
    """
    if not spec:
        return "", []

    notes: List[str] = []

    embedded = spec.get("source_css")
    if isinstance(embedded, str) and embedded.strip():
        cleaned, embedded_notes = sanitize_css(embedded)
        notes.extend(embedded_notes)
        if cleaned:
            origin = f"format_spec.source_css · {len(cleaned)} 字符"
            return _wrap_style(cleaned, origin), notes
        notes.append("format_spec.source_css 清洗后为空，改从 source.path 抽取")

    cleaned, extract_notes, origin = _extract_source_css(spec, task_root)
    notes.extend(extract_notes)
    if not cleaned:
        return "", notes
    return _wrap_style(cleaned, origin), notes


# --- ECharts 自包含与图表兜底 --------------------------------------------------


def build_echarts_loader() -> str:
    """内联本地 ECharts；缺失时显式启用离线图表兜底。"""
    local = _RESOURCES_DIR / "echarts.min.js"
    if local.is_file():
        try:
            code = local.read_text(encoding="utf-8")
        except OSError:
            code = ""
        if code.strip():
            return "<script>\n" + code + "\n</script>"
    return (
        "<script>window.__qwenpawDataEchartsLoadError=true;</script>"
    )


def load_chart_fallback_runtime() -> str:
    """读取图表兜底运行时（resources/chart_fallback.html）。"""
    path = _RESOURCES_DIR / "chart_fallback.html"
    if not path.is_file():
        print(f"警告: 图表兜底运行时缺失: {path}")
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as error:
        print(f"警告: 图表兜底运行时读取失败: {error}")
        return ""


def _read_resource(name: str, required: bool = False) -> str:
    path = _RESOURCES_DIR / name
    if not path.is_file():
        if required:
            print(f"警告: 资源缺失: {path}")
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as error:
        print(f"警告: 资源读取失败 {path}: {error}")
        return ""


def load_report_css() -> str:
    """读取离线 Tailwind 子集；缺失时保留无外链的语义 HTML。"""
    css = _read_resource("report.css")
    if not css:
        print(
            "警告: resources/report.css 缺失，将使用无外链的语义 HTML"
        )
    return css


def load_icons_css() -> str:
    """Bootstrap Icons 类表 + 内联字体（离线）。"""
    return _read_resource("icons.css")


def load_theme_css() -> str:
    """报告主题（rp-* 类：标题区、编号章节、卡片、KPI 带、表格、标签）。"""
    return _read_resource("theme.css", required=True)


_THEME_MARKER = "data-qwenpaw-data-theme-css"
_THEME_USED_RE = re.compile(r"""class=["'][^"']*\brp-(?:card|table|kpi|lead|sec)""")


def inject_theme_support(html: str) -> str:
    """给 `--template` 自定义模板补上 blocks 用到的主题样式（内置骨架已自带）。"""
    if _THEME_MARKER in html or not _THEME_USED_RE.search(html):
        return html
    css = load_theme_css()
    if not css:
        return html
    style = f"<style {_THEME_MARKER}>{css}</style>"
    if "</head>" in html:
        return html.replace("</head>", style + "\n</head>", 1)
    return style + "\n" + html


def load_bi_helpers() -> str:
    """下钻展开 / 图表视图切换的最小脚本（resources/bi_helpers.html）。"""
    return _read_resource("bi_helpers.html")


_BI_HELPERS_MARKER = "data-qwenpaw-data-bi-helpers"
_BI_HELPERS_USED_RE = re.compile(
    r"__biToggleRow|__biChartView|__biSortTable|__biFilterTable"
)


def inject_bi_helpers_support(html: str) -> str:
    """给 `--template` 自定义模板补上 blocks 需要的辅助脚本（内置骨架已自带）。"""
    if _BI_HELPERS_MARKER in html or not _BI_HELPERS_USED_RE.search(html):
        return html
    helpers = load_bi_helpers()
    if not helpers:
        return html
    if "</head>" in html:
        return html.replace("</head>", helpers + "\n</head>", 1)
    return helpers + "\n" + html


def inject_chart_fallback_support(html: str) -> str:
    """给没带图表兜底运行时的模板补上一份。

    内置骨架已经通过 `{{ chart_fallback }}` 渲染过，带着标记，这里会直接返回；
    本函数只为 `--template` 传入的自定义模板兜底。
    """
    if _CHART_FALLBACK_MARKER in html:
        return html
    runtime = load_chart_fallback_runtime()
    if not runtime:
        return html
    if "</head>" in html:
        return html.replace("</head>", runtime + "\n</head>", 1)
    return runtime + "\n" + html


# --- 组装 ---------------------------------------------------------------------


def load_skeleton_template(template_path: Optional[str] = None) -> Template:
    """加载骨架模板

    Args:
        template_path: 模板文件路径，默认使用上级目录 resources/skeleton_template.html

    Returns:
        Jinja2 Template 对象
    """
    if template_path is None:
        template_path = _RESOURCES_DIR / "skeleton_template.html"

    with open(template_path, "r", encoding="utf-8") as f:
        template_content = f.read()

    return Template(template_content)


def load_sections(sections_sources: List[str]) -> List[Dict[str, Any]]:
    """加载章节数据

    Args:
        sections_sources: JSON 文件路径列表

    Returns:
        章节列表，每个章节包含 title, subtitle, html_fragment 和/或 blocks
    """
    all_sections = []
    for source in sections_sources:
        if source.endswith(".json") and os.path.exists(source):
            with open(source, "r", encoding="utf-8") as f:
                sections = json.load(f)
            if not isinstance(sections, list):
                sections = [sections]
            # blocks 里的数据文件路径默认相对 sections 文件所在目录。
            base_dir = str(Path(source).resolve().parent)
            for section in sections:
                if isinstance(section, dict) and section.get("blocks"):
                    section.setdefault("_base_dir", base_dir)
            all_sections.extend(sections)
        else:
            raise ValueError(f"无效的 sections 文件: {source}")
    return all_sections


def render_section_blocks(
    sections: List[Dict[str, Any]], data_root: Optional[Path] = None
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把带 ``blocks`` 的章节渲染成 ``html_fragment``，返回 (新章节列表, 警告)。

    ``blocks`` 先渲染，章节里手写的 ``html_fragment`` 追加在后面，两者可以并存。
    原章节对象不被修改。
    """
    ctx = report_blocks.BlockContext()
    rendered: List[Dict[str, Any]] = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict) or not section.get("blocks"):
            rendered.append(section)
            continue
        base = data_root or section.get("_base_dir")
        ctx.base_dir = Path(base) if base else Path(".")
        fragment = report_blocks.render_section(ctx, section, index)
        merged = dict(section)
        merged["html_fragment"] = fragment + str(section.get("html_fragment") or "")
        rendered.append(merged)
    return rendered, ctx.warnings


def build_report(
    sections: List[Dict[str, Any]],
    report_title: Optional[str] = None,
    date: Optional[str] = None,
    template_path: Optional[str] = None,
    format_spec: Optional[Dict[str, Any]] = None,
    task_root: Optional[Path] = None,
    data_root: Optional[Path] = None,
) -> str:
    """构建完整报告

    Args:
        sections: 章节列表，每个章节包含 title, subtitle(可选), type(可选), html_fragment 和/或 blocks
        report_title: 报告标题（可选，显示在顶部标题区）
        date: 生成日期（可选，默认今天）
        template_path: 骨架模板路径（可选，默认使用内置模板）
        format_spec: report-format-extractor 产出的视觉规范（可选）
        task_root: 任务根目录，用于解析 format_spec 里的 source.path 相对路径
        data_root: blocks 里数据文件路径的根目录（默认：sections 文件所在目录）

    Returns:
        完整的 HTML 字符串
    """
    template = load_skeleton_template(template_path)

    if date is None:
        date = datetime.now().strftime("%Y年%m月%d日")

    sections, block_warnings = render_section_blocks(sections, data_root)
    for warning in block_warnings:
        print(f"  [blocks] 警告: {warning}")
    # type=hero 的章节（通常是 KPI 带）进标题区；其余按顺序编号、纵向排列。
    hero_sections = [s for s in sections if s.get("type") == "hero"]
    body_sections = [s for s in sections if s.get("type") != "hero"]
    # tab_sections / timeline_sections 只为旧的 `--template` 自定义模板保留。
    tab_sections = [s for s in body_sections if s.get("type") != "timeline"]
    timeline_sections = [s for s in body_sections if s.get("type") == "timeline"]

    html = template.render(
        report_title=report_title or "",
        date=date,
        sections=sections,
        hero_sections=hero_sections,
        body_sections=body_sections,
        tab_sections=tab_sections,
        timeline_sections=timeline_sections,
        format_spec=format_spec or {},
        echarts_loader=build_echarts_loader(),
        chart_fallback=load_chart_fallback_runtime(),
        bi_helpers=load_bi_helpers(),
        report_css=load_report_css(),
        icons_css=load_icons_css(),
        theme_css=load_theme_css(),
    )

    controlled_css, notes = build_format_css(format_spec or {}, task_root)
    for note in notes:
        print(f"  [format] {note}")
    if controlled_css:
        if "</head>" not in html:
            raise ValueError("报告模板缺少 </head>，无法安全注入原报告样式")
        # 注入在 </head> 之前：晚于模板自带样式，同特异度下层叠优先级更高。
        html = html.replace("</head>", controlled_css + "\n</head>", 1)
        print(f"  [format] 已注入原报告样式 {len(controlled_css):,} 字符")

    return inject_theme_support(
        inject_bi_helpers_support(inject_chart_fallback_support(html))
    )


def _parse_simple_selector(selector: str) -> Tuple[Optional[str], List[str]]:
    """把 dom_contract 的 selector 拆成 (标签名, [类名...])。

    只处理复现里出现的简单形态：``tag.class``、``.class``、``.a.b``；带组合器时
    取最右边的 compound（``details.panel > summary`` → ``summary``）。伪类、属性
    选择器、id 一律忽略——它们不影响“这个类挂没挂对标签”的体量统计。解析不出
    任何标签或类名时返回 ``(None, [])``，调用方据此跳过该项。
    """
    if not isinstance(selector, str) or not selector.strip():
        return None, []
    compound = _SELECTOR_COMBINATOR_RE.split(selector.strip())[-1]
    compound = re.split(r"[:\[#]", compound)[0]
    if not compound:
        return None, []
    pieces = compound.split(".")
    head = pieces[0]
    tag = head.lower() if head else None
    classes = [piece for piece in pieces[1:] if piece]
    return tag, classes


def _element_classes(attrs: str) -> set:
    """从一个开始标签的属性串里取出 class token 集合。"""
    match = _CLASS_ATTR_RE.search(attrs)
    if not match:
        return set()
    return set((match.group(1) or match.group(2) or "").split())


def _count_selector(html: str, selector: str) -> Optional[int]:
    """数 html 里匹配 selector 的元素个数；selector 解析不了时返回 None。"""
    tag, classes = _parse_simple_selector(selector)
    if tag is None and not classes:
        return None
    required = set(classes)
    total = 0
    for match in _TAG_OPEN_RE.finditer(html):
        if tag is not None and match.group(1).lower() != tag:
            continue
        if required and not required.issubset(_element_classes(match.group(2))):
            continue
        total += 1
    return total


def reconcile_dom_contract(html: str, spec: Dict[str, Any]) -> Tuple[List[str], int]:
    """拿生成结果的实际结构数量对账 dom_contract 记录的原报告 count。

    返回 ``(violations, checked)``：
      - 跳过 state 类和 count<=0 的项——交互态在静态 HTML 里本就为 0；
      - 其余项要求实测数量 >= max(MIN, ceil(count * RATIO))，否则记一条 violation。
    没有 dom_contract（普通 BI 分析）时返回 ``([], 0)``，对账不参与。
    """
    contract = spec.get("dom_contract") if isinstance(spec, dict) else None
    if not isinstance(contract, list) or not contract:
        return [], 0
    violations: List[str] = []
    checked = 0
    for item in contract:
        if not isinstance(item, dict) or item.get("state") is True:
            continue
        expected = item.get("count")
        selector = item.get("selector")
        if not isinstance(expected, int) or expected <= 0:
            continue
        actual = _count_selector(html, selector) if isinstance(selector, str) else None
        if actual is None:
            continue
        checked += 1
        floor = max(_DOM_RECONCILE_MIN, math.ceil(expected * _DOM_RECONCILE_RATIO))
        if actual < floor:
            note = item.get("semantics")
            suffix = f" · {note}" if isinstance(note, str) and note else ""
            violations.append(
                f"{selector}: 原报告 {expected} 个、复现 {actual} 个（下限 {floor}）{suffix}"
            )
    return violations, checked


def _find_chart_containers(html: str) -> List[Tuple[str, bool]]:
    """找出图表容器，返回 ``[(id, 是否空转)]``。

    容器口径与 chart_fallback.html 的兜底扫描一致：``<div>`` 且 id 以 ``chart`` 开头，
    或 class 含 ``chart-container``。“空转”指开标签后紧跟 ``</div>``——里面既没有
    ``<svg>``/``<canvas>`` 这类预渲染图元，也没有任何子节点。
    """
    containers: List[Tuple[str, bool]] = []
    for match in _TAG_OPEN_RE.finditer(html):
        if match.group(1).lower() != "div":
            continue
        attrs = match.group(2)
        id_match = _ID_ATTR_RE.search(attrs)
        cid = (id_match.group(1) or id_match.group(2) or "") if id_match else ""
        classes = _element_classes(attrs)
        if not (
            cid.lower().startswith(_CHART_ID_PREFIX)
            or _CHART_CONTAINER_CLASS in classes
        ):
            continue
        tail = html[match.end() : match.end() + 40].lstrip()
        containers.append((cid, tail.startswith("</div>")))
    return containers


def audit_chart_rendering(html: str) -> Tuple[List[str], int]:
    """审计图表容器有没有渲染出路，返回 ``(violations, container_count)``。

    一个空容器要有出路，必须被脚本按 id 初始化（``getElementById('chartX')`` 等，表现为
    id 在文档里出现不止一次）。既空转、又没人按 id 初始化的容器，浏览器打开就是一片
    空白——这正是手搓 HTML 只写空 ``<div id="chart_*">``、却没有任何 ``<script>`` 时的
    症状。没有图表容器时返回 ``([], 0)``，审计不参与：普通报告可以没有图。

    判定留了两道容忍：全文有脚本时，只有空转占多数才判塌陷（容忍图表/表格双视图、
    批量 ``querySelectorAll`` 初始化带来的个别误判）；全文无脚本时只要有空转就直接判
    塌陷，因为没有任何 JS 能把图画出来。
    """
    containers = _find_chart_containers(html)
    if not containers:
        return [], 0
    total = len(containers)
    script_count = len(re.findall(r"<script\b", html, re.I))
    orphan_ids = [
        cid for cid, empty in containers if empty and not (cid and html.count(cid) >= 2)
    ]
    orphan = len(orphan_ids)
    if orphan == 0:
        return [], total
    if script_count > 0 and orphan * 2 < total:
        return [], total
    sample = "、".join(cid or "(无 id)" for cid in orphan_ids[:5])
    suffix = "…" if orphan > 5 else ""
    reason = (
        "全文没有任何 <script>，既无 ECharts 初始化也无兜底运行时"
        if script_count == 0
        else "既无内联 <svg>/<canvas> 内容，也没有脚本按 id 调用 echarts.init 初始化"
    )
    return [f"{orphan}/{total} 个图表容器空转：{sample}{suffix}（{reason}）"], total


# --- 离线 / 下钻 / 类名 / 内容 审计 ---------------------------------------------
#
# 这几道审计取代了 Agent 过去手写的 QC 脚本：外链、空图表、悬空下钻 id、没有样式的
# 类名、过程性内容，构建时一次性查完。外链和下钻 id 是硬错误（退出码 2），类名与
# 内容只告警——它们是风格问题，不该挡住交付。

_SCRIPT_INLINE_RE = re.compile(
    r"<script\b(?![^>]*\bsrc\s*=)[^>]*>.*?</script>", re.S | re.I
)
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_REMOTE = r"(?:https?:)?//"
_EXTERNAL_RES: List[Tuple[str, "re.Pattern[str]"]] = [
    (
        "<link href>",
        re.compile(r"<link\b[^>]*\bhref\s*=\s*[\"']\s*(" + _REMOTE + r"[^\"']+)", re.I),
    ),
    (
        "<script src>",
        re.compile(r"<script\b[^>]*\bsrc\s*=\s*[\"']\s*(?!data:)([^\"']+)", re.I),
    ),
    (
        "<img/iframe/media src>",
        re.compile(
            r"<(?:img|iframe|embed|source|video|audio)\b[^>]*\b(?:src|srcset)\s*=\s*[\"']\s*("
            + _REMOTE
            + r"[^\"']+)",
            re.I,
        ),
    ),
    (
        "@import",
        re.compile(
            r"@import\s+(?:url\()?\s*[\"']?\s*(" + _REMOTE + r"[^\"')\s;]+)", re.I
        ),
    ),
    ("css url()", re.compile(r"url\(\s*[\"']?\s*(" + _REMOTE + r"[^\"')\s]+)", re.I)),
]


def audit_external_resources(html: str) -> List[str]:
    """页面会向外部发请求的资源引用；空列表 = 完全离线。

    内联 `<script>` 先摘掉（内联的 ECharts 库体里有一堆 `http://` 字符串，不是请求）；
    `<a href>` 是用户点了才跳转的链接，不算资源。
    """
    scanned = _COMMENT_RE.sub("", _SCRIPT_INLINE_RE.sub("<script></script>", html))
    found: List[str] = []
    for label, pattern in _EXTERNAL_RES:
        urls = sorted({match.group(1) for match in pattern.finditer(scanned)})
        for url in urls[:5]:
            found.append(f"{label}: {url[:120]}")
        if len(urls) > 5:
            found.append(f"{label}: …另有 {len(urls) - 5} 处")
    return found


_TOGGLE_CALL_RE = re.compile(r"__biToggleRow\(\s*'([^']+)'\s*\)")


def audit_drill_refs(html: str) -> Tuple[List[str], int]:
    """每个 `__biToggleRow('id')` 都必须有对应 `id="..."` 的行，否则点了没反应。"""
    scanned = _SCRIPT_INLINE_RE.sub("<script></script>", html)
    wanted = sorted(set(_TOGGLE_CALL_RE.findall(scanned)))
    if not wanted:
        return [], 0
    present = {m.group(1) or m.group(2) for m in _ID_ATTR_RE.finditer(scanned)}
    missing = [rid for rid in wanted if rid not in present]
    if not missing:
        return [], len(wanted)
    sample = "、".join(missing[:5]) + ("…" if len(missing) > 5 else "")
    return [f"{len(missing)}/{len(wanted)} 个下钻开关找不到目标行：{sample}"], len(
        wanted
    )


_CLASS_TOKEN_ATTR_RE = re.compile(r"""\bclass\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_CSS_CLASS_RE = re.compile(r"\.((?:\\.|[A-Za-z0-9_-])+)")
_CSS_ESCAPE_RE = re.compile(r"\\(?:([0-9a-fA-F]{1,6}) ?|(.))", re.S)


def _unescape_css_ident(token: str) -> str:
    """`lg\\:flex` -> `lg:flex`，`\\32xl\\:p-2` -> `2xl:p-2`（CSS 标识符转义）。"""

    def _sub(match: "re.Match[str]") -> str:
        if match.group(1):
            return chr(int(match.group(1), 16))
        return match.group(2)

    return _CSS_ESCAPE_RE.sub(_sub, token)


def audit_class_coverage(html: str) -> Tuple[List[str], int]:
    """class 属性里用到、但页面任何 `<style>` 里都没有规则的类名（多半是笔误或 Tailwind 里没有）。

    仅告警：没有规则的类不会报错，只是悄悄没样式——这正是最难靠肉眼发现的那种问题。
    """
    defined = set()
    for css in _STYLE_BLOCK_RE.findall(html):
        for match in _CSS_CLASS_RE.finditer(css):
            defined.add(_unescape_css_ident(match.group(1)))
    body = _SCRIPT_INLINE_RE.sub("<script></script>", _STYLE_BLOCK_RE.sub("", html))
    used = set()
    for match in _CLASS_TOKEN_ATTR_RE.finditer(body):
        for token in (match.group(1) or match.group(2) or "").split():
            if "{" not in token and "}" not in token:
                used.add(token)
    # `bi` 是图标字体基类；`hidden` 等由脚本切换的类只要 CSS 里有就已在 defined 里。
    unknown = sorted(token for token in used if token not in defined)
    if not unknown:
        return [], len(used)
    sample = "、".join(unknown[:12]) + ("…" if len(unknown) > 12 else "")
    return [f"{len(unknown)} 个类名没有任何样式规则：{sample}"], len(used)


_LINT_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("取数过程（SQL）", re.compile(r"\bSQL\b|\bSELECT\b[^<]{0,80}\bFROM\b", re.I)),
    (
        "校验 / 自检信息",
        re.compile(
            r"\bQC\b|\bPASS(?:ED)?\b|\bFAIL(?:ED)?\b|自检|校验通过|已通过校验|对账"
        ),
    ),
    ("md5 / 文件哈希", re.compile(r"\bmd5\b", re.I)),
    (
        "版本 / 修订信息",
        re.compile(
            r"\bv\d+\s*(?:→|->|至)\s*v\d+|本版(?:新增|修正|变化)|较上一版|更新日志|修订(?:记录|历史)|版本历史"
        ),
    ),
    (
        "脚本 / 文件路径",
        re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\)[^\s<]+|\b[\w-]+\.(?:py|sql)\b"),
    ),
    ("工具 / 环境说明", re.compile(r"\bCDN\b|离线可用|\becharts\s*v?\d", re.I)),
]
_LINT_HEADING_RE = re.compile(
    r"<h[1-4]\b[^>]*>(?:\s*<[^>]+>)*\s*([^<]*(?:附录|口径说明|方法说明|数据说明)[^<]*)",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


def lint_report_content(html: str) -> List[str]:
    """可见文字里的过程性内容（SKILL.md「不写」清单）。仅告警，复现模式不跑。"""
    scanned = _COMMENT_RE.sub(
        "", _SCRIPT_INLINE_RE.sub("", _STYLE_BLOCK_RE.sub("", html))
    )
    text = _html.unescape(_TAG_RE.sub(" ", scanned))
    findings: List[str] = []
    for label, pattern in _LINT_RULES:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 12)
            snippet = re.sub(r"\s+", " ", text[start : match.end() + 12]).strip()
            findings.append(f"{label}：…{snippet}…")
    heading = _LINT_HEADING_RE.search(scanned)
    if heading:
        findings.append(f"独立说明章节：「{heading.group(1).strip()[:24]}」")
    return findings


def inject_builder_marker(
    html: str,
    checked: int,
    violations: List[str],
    chart_checked: int = 0,
    chart_violations: Optional[List[str]] = None,
    external_violations: Optional[List[str]] = None,
    drill_violations: Optional[List[str]] = None,
) -> str:
    """在 ``</head>`` 前盖一个生成来源章。

    一是证明这份 HTML 出自 report_builder（而不是绕过脚本手搓的），二是把 dom_contract
    体量对账与图表渲染审计的结论写进产物，下游验收和人工排查一眼可见。注入点取第一个
    ``</head>``——与原报告 CSS 的注入点一致，此时 head 里只有样式与 loader。
    """
    chart_violations = list(chart_violations or [])
    if checked == 0:
        dom_status = "dom_contract: n/a"
    elif violations:
        dom_status = f"dom_contract: FAIL ({len(violations)}/{checked} 项塌陷)"
    else:
        dom_status = f"dom_contract: PASS ({checked}/{checked} 项达标)"
    if chart_checked == 0:
        chart_status = "charts: n/a"
    elif chart_violations:
        chart_status = f"charts: FAIL ({chart_checked} 个容器有空转)"
    else:
        chart_status = f"charts: PASS ({chart_checked}/{chart_checked} 个可渲染)"
    parts = [_BUILDER_MARKER, dom_status, chart_status]
    if external_violations is not None:
        parts.append(
            f"external: FAIL ({len(external_violations)} 处外链)"
            if external_violations
            else "external: PASS (0 外链)"
        )
    if drill_violations:
        parts.append(f"drill: FAIL ({len(drill_violations)} 项)")
    marker = "<!-- " + " | ".join(parts) + " -->"
    if "</head>" in html:
        return html.replace("</head>", marker + "\n</head>", 1)
    return marker + "\n" + html


def save_report(html: str, output_path: str) -> None:
    """保存报告到文件

    Args:
        html: HTML 内容
        output_path: 输出文件路径
    """
    # 确保目录存在
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"报告已保存: {output_path}")


def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(description="BI 报告生成器")
    parser.add_argument(
        "--sections",
        "-s",
        nargs="+",
        required=True,
        help="章节数据 JSON 文件路径（可指定多个）",
    )
    parser.add_argument("--output", "-o", required=True, help="输出 HTML 文件路径")
    parser.add_argument(
        "--report-title", "-t", default=None, help="报告标题（可选，多主题时建议传递）"
    )
    # 已废弃：标题下的总摘要和首章结论重复，不再渲染；保留参数以免旧调用直接报错。
    parser.add_argument("--overall-summary", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--date", default=None, help="生成日期（可选，默认今天）")
    parser.add_argument(
        "--template", default=None, help="骨架模板路径（可选，默认使用内置模板）"
    )
    parser.add_argument(
        "--format-spec",
        default=None,
        help="report-format-extractor 产出的 JSON（报告复现模式必填）；"
        "脚本会据此定位原报告并透传其 <style>",
    )
    parser.add_argument(
        "--allow-dom-mismatch",
        action="store_true",
        help="dom_contract 体量对账或图表渲染审计塌陷时不失败退出（复现模式默认开启，"
        "仅在新周期数据量确实合理缩水时使用；图表空转不应靠它绕过）",
    )
    parser.add_argument(
        "--data-root",
        default=None,
        help="blocks 里 data 文件路径的根目录（默认：各 sections 文件所在目录）",
    )
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="页面含外部资源引用（CDN 等）时不失败退出。默认产物必须完全离线；"
        "只有用户明确要求联网资源时才用",
    )

    args = parser.parse_args()

    if args.overall_summary:
        print(
            "警告: --overall-summary 已废弃并被忽略；核心结论写进第一章的 conclusion 块"
        )

    sections = load_sections(args.sections)
    print(f"加载了 {len(sections)} 个章节，来自 {len(args.sections)} 个文件")

    format_spec = load_format_spec(args.format_spec)
    # source.path 是相对任务根目录的，而 format_spec.json 就放在任务根目录下。
    task_root = Path(args.format_spec).resolve().parent if args.format_spec else None

    try:
        html = build_report(
            sections=sections,
            report_title=args.report_title,
            date=args.date,
            template_path=args.template,
            format_spec=format_spec,
            task_root=task_root,
            data_root=Path(args.data_root).resolve() if args.data_root else None,
        )
    except report_blocks.BlockError as error:
        print(f"blocks 渲染失败: {error}")
        sys.exit(2)

    violations, checked = reconcile_dom_contract(html, format_spec)
    chart_violations, chart_checked = audit_chart_rendering(html)
    external_violations = audit_external_resources(html)
    drill_violations, drill_checked = audit_drill_refs(html)
    class_warnings, _ = audit_class_coverage(html)
    # 复现模式以原报告为准（原报告有的说明照样复现），不对内容做「不写」清单的告警。
    lint_warnings = [] if format_spec else lint_report_content(html)
    html = inject_builder_marker(
        html,
        checked,
        violations,
        chart_checked,
        chart_violations,
        external_violations,
        drill_violations,
    )

    save_report(html, args.output)
    print(f"报告总长度: {len(html)} 字符")
    if checked:
        print(f"dom_contract 体量对账: {checked - len(violations)}/{checked} 项达标")
    if chart_checked:
        verdict = "FAIL" if chart_violations else f"{chart_checked} 个容器均可渲染"
        print(f"图表渲染审计: {verdict}")
    if drill_checked:
        verdict = "FAIL" if drill_violations else f"{drill_checked} 个开关均可展开"
        print(f"下钻 id 审计: {verdict}")
    print(
        "外链审计: "
        + (
            f"FAIL（{len(external_violations)} 处）"
            if external_violations
            else "PASS（0 外链，完全离线）"
        )
    )
    for line in class_warnings:
        print(f"警告 · 类名: {line}")
    for line in lint_warnings:
        print(f"警告 · 内容（SKILL.md「不写」清单，应移到聊天回复）: {line}")

    if violations:
        print(f"结构塌陷 {len(violations)} 项：")
        for line in violations:
            print(f"  - {line}")
    if chart_violations:
        print(f"图表空转 {len(chart_violations)} 项：")
        for line in chart_violations:
            print(f"  - {line}")

    if external_violations:
        print(f"外部资源引用 {len(external_violations)} 项（报告必须离线可用）：")
        for line in external_violations:
            print(f"  - {line}")
        if not args.allow_external:
            print(
                "页面会在打开时联网拉取资源，离线 / 内网 / sandbox iframe 下会失去样式或图表。"
                "请改用内置骨架（已内联全部资源）；确需外链才加 --allow-external。"
            )
    if drill_violations:
        print(f"下钻开关悬空 {len(drill_violations)} 项：")
        for line in drill_violations:
            print(f"  - {line}")
        print("点了没反应的按钮不能交付：补上目标行，或改成静态展开。")

    if (external_violations and not args.allow_external) or drill_violations:
        sys.exit(2)

    if (violations or chart_violations) and not args.allow_dom_mismatch:
        if violations:
            print(
                "复现产物的结构数量远低于原报告，说明章节片段只放了占位样本、没有把"
                "已验收草稿的完整数据 materialize 出来。请补齐 sections_*.json 的 "
                "html_fragment（完整实体行 / 下钻行 / 涨跌色单元格）后重跑。"
            )
        if chart_violations:
            print(
                "图表容器只有空 div，没有 ECharts 初始化脚本、也没有内联 SVG/canvas，"
                "浏览器打开必然一片空白。请为每个图表容器补 echarts.init(...).setOption(...)"
                "初始化，或直接内联同一份数据渲染的 SVG/表格。"
            )
        print("确属新周期数据合理缩水才加 --allow-dom-mismatch；图表空转不应靠它绕过。")
        sys.exit(2)


if __name__ == "__main__":
    main()
