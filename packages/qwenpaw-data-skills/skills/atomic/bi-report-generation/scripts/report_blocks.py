# -*- coding: utf-8 -*-
"""Declarative blocks for bi-report-generation.

A section in ``sections_*.json`` may carry ``blocks`` instead of (or next to) a
hand-written ``html_fragment``. A block names a data file and says *what* to show;
this module reads the data and writes the HTML, so the model never types table rows,
colour-coded cells, drill-down toggles or chart init scripts.

The output follows ``references/layout-spec.md``: every data block is its own card
(title, subtitle, body), styled by the ``rp-*`` classes of ``resources/theme.css``. The
static pieces live in ``resources/blocks.html`` as Jinja macros, the data-driven rows
are produced here. All CSS is inlined by the skeleton, so the page is fully offline.

Block types
-----------
``conclusion`` ``kpi`` ``table`` ``drill_table`` ``heatmap_table`` ``chart``
``stackbar`` ``grid`` ``note`` ``caption`` ``timeline`` ``card`` ``html``

See SKILL.md for the field reference; ``BLOCK_TYPES`` below is the source of truth.
"""

from __future__ import annotations

import csv
import html as _html
import json
import math
import re
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from jinja2 import Environment

_BLOCKS_TEMPLATE = Path(__file__).resolve().parent.parent / "resources" / "blocks.html"

BLOCK_TYPES = (
    "conclusion",
    "kpi",
    "table",
    "drill_table",
    "heatmap_table",
    "chart",
    "stackbar",
    "grid",
    "note",
    "caption",
    "timeline",
    "card",
    "html",
)

CHART_KINDS = (
    "line",
    "bar",
    "grouped_bar",
    "hbar",
    "pie",
    "funnel",
    "scatter",
    "radar",
)

# Layout-spec section 3: tables longer than this scroll instead of growing the page.
LONG_TABLE_ROWS = 10
DEFAULT_SCROLL_HEIGHT = 360
MAX_DRILL_LEVELS = 3
NOTE_MAX_CHARS = 30
PIE_MAX_CATEGORIES = 6
PIE_TOP_N = 8

_PALETTE = ["#1C54E3", "#00CEC9", "#7347FF", "#fb6a3c", "#9db6ec", "#0A8F8C"]
_INK_2 = "#454b58"
_INK_3 = "#8a909c"
_LINE = "#ebedf1"
_EMPTY = "—"
_TAG_TONES = ("blue", "teal", "purple", "amber", "gray", "line")


class BlockError(ValueError):
    """A block spec the renderer cannot honour; the message says which and why."""


def esc(value: Any) -> str:
    return _html.escape("" if value is None else str(value), quote=True)


# --- jinja macros --------------------------------------------------------------

_macros_cache: Any = None


def _macros() -> Any:
    global _macros_cache
    if _macros_cache is None:
        env = Environment(autoescape=False)
        _macros_cache = env.from_string(
            _BLOCKS_TEMPLATE.read_text(encoding="utf-8")
        ).module
    return _macros_cache


# --- build context -------------------------------------------------------------


class BlockContext:
    """State shared by every block of one build: data root, id counter, warnings."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else Path(".")
        self.section_idx = 0
        self.warnings: List[str] = []
        self.grid_height: Optional[int] = None
        # Inside an explicit ``card`` block children render bare (no nested cards).
        self.in_card = False
        # Header widgets (chart view toggle) a renderer hands to its enclosing card.
        self.card_tools = ""
        self._counter = 0
        self._file_cache: Dict[Tuple[str, str], Any] = {}

    def next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_s{self.section_idx}_{self._counter}"

    def warn(self, message: str) -> None:
        self.warnings.append(message)


# --- numbers and formatting ----------------------------------------------------


def to_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else number
    text = str(value).strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1]
    if not text or text.lower() in ("nan", "none", "null", "-", _EMPTY, "inf", "-inf"):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return None if math.isnan(number) or math.isinf(number) else number


# name -> (scale, decimals, suffix, thousands separator)
_FMT_SPECS: Dict[str, Tuple[float, int, str, bool]] = {
    "int": (1.0, 0, "", True),
    "num1": (1.0, 1, "", True),
    "num2": (1.0, 2, "", True),
    "money2": (1.0, 2, "", True),
    "wan1": (1e-4, 1, "", True),
    "wan2": (1e-4, 2, "", True),
    "yi2": (1e-8, 2, "", True),
    # pct*: the data already holds percent points (layout-spec: append "%" as is).
    "pct0": (1.0, 0, "%", True),
    "pct1": (1.0, 1, "%", True),
    "pct2": (1.0, 2, "%", True),
    # ratio*: the data holds a fraction (0.0123 -> 1.23%).
    "ratio0": (100.0, 0, "%", True),
    "ratio1": (100.0, 1, "%", True),
    "ratio": (100.0, 2, "%", True),
    "ratio2": (100.0, 2, "%", True),
}


def _round_half_up(value: float, decimals: int) -> Decimal:
    try:
        quantum = Decimal(1).scaleb(-decimals)
        rounded = Decimal(repr(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        rounded = Decimal(0)
    # Negative zero (-0.000726 shown at 2 decimals) must read as a plain 0.00.
    return abs(rounded) if rounded == 0 else rounded


def format_value(value: Any, fmt: Optional[str] = None) -> Tuple[str, Optional[float]]:
    """Format one cell. Returns ``(plain text, rounded number or None)``.

    The rounded number drives sign colouring, so a value that *displays* as 0.00 is
    never coloured red or green.
    """
    fmt = (fmt or "auto").strip()
    plus = fmt.endswith("+")
    name = fmt[:-1] if plus else fmt

    if name == "text":
        return ("" if value is None else str(value)), None

    number = to_number(value)
    if number is None:
        raw = "" if value is None else str(value).strip()
        return (raw if raw else _EMPTY), None

    if name == "auto":
        if float(number).is_integer():
            scale, decimals, suffix, thousands = 1.0, 0, "", True
        else:
            scale, decimals, suffix, thousands = 1.0, 2, "", True
    elif name in _FMT_SPECS:
        scale, decimals, suffix, thousands = _FMT_SPECS[name]
    else:
        raise BlockError(
            f"未知 fmt: {fmt!r}（可用: auto, text, {', '.join(sorted(_FMT_SPECS))}，"
            "数字格式末尾加 + 表示正数带 + 号）"
        )

    rounded = _round_half_up(number * scale, decimals)
    text = f"{rounded:,.{decimals}f}" if thousands else f"{rounded:.{decimals}f}"
    if plus and rounded > 0:
        text = "+" + text
    return text + suffix, float(rounded)


_COLOR_POS = "rp-pos"  # rise: green
_COLOR_NEG = "rp-neg"  # decline: blue
_COLOR_BAD = "rp-bad"  # rise of a cost-like metric: red
_COLOR_KEY = "rp-key"  # emphasis: ink bold


def sign_class(sign: Optional[float], mode: Optional[str]) -> str:
    """Theme class for a number's colour; empty string means leave uncoloured."""
    if not mode:
        return ""
    if mode in ("key", "blue"):
        return _COLOR_KEY
    if sign is None or sign == 0:
        return ""
    if mode == "sign":
        return _COLOR_POS if sign > 0 else _COLOR_NEG
    if mode == "sign_inv":
        return _COLOR_BAD if sign > 0 else _COLOR_POS
    raise BlockError(f"未知 color: {mode!r}（可用: sign, sign_inv, key）")


# Legacy Tailwind colour spans are still accepted and mapped onto the theme.
_LEGACY_SPAN = {
    "text-blue-600": _COLOR_KEY,
    "text-green-600": _COLOR_POS,
    "text-red-500": _COLOR_NEG,
    "rp-key": _COLOR_KEY,
    "rp-pos": _COLOR_POS,
    "rp-neg": _COLOR_NEG,
    "rp-bad": _COLOR_BAD,
}
_RICH_RE = re.compile(
    r'<span class="(?P<cls>text-blue-600|text-red-500|text-green-600|rp-key|rp-pos|rp-neg|rp-bad)'
    r'(?: font-bold)?">(?P<span>.*?)</span>'
    r"|\*\*(?P<b>.+?)\*\*|\{\+(?P<g>.+?)\}|\{-(?P<r>.+?)\}",
    re.S,
)


def rich_text(text: Any) -> str:
    """Escape ``text`` but keep the colour spans and a tiny markup shorthand.

    ``**x**`` -> bold emphasis, ``{+x}`` -> green ``+x`` (rise), ``{-x}`` -> blue
    ``-x`` (decline). Anything else is escaped, so a conclusion can never carry
    arbitrary HTML.
    """
    text = "" if text is None else str(text)
    out: List[str] = []
    pos = 0
    for match in _RICH_RE.finditer(text):
        out.append(esc(text[pos : match.start()]))
        if match.group("span") is not None:
            cls, body = _LEGACY_SPAN[match.group("cls")], match.group("span")
        elif match.group("b") is not None:
            cls, body = _COLOR_KEY, match.group("b")
        elif match.group("g") is not None:
            cls, body = _COLOR_POS, "+" + match.group("g")
        else:
            cls, body = _COLOR_NEG, "-" + match.group("r")
        out.append(f'<span class="{cls}">{esc(body)}</span>')
        pos = match.end()
    out.append(esc(text[pos:]))
    return "".join(out)


# --- data loading ---------------------------------------------------------------


def _resolve_path(ctx: BlockContext, raw: str) -> Path:
    path = Path(raw).expanduser()
    candidates = (
        [path] if path.is_absolute() else [ctx.base_dir / path, Path.cwd() / path]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    tried = "、".join(str(c) for c in candidates)
    raise BlockError(
        f"数据文件不存在: {raw}（已找: {tried}；可用 --data-root 指定根目录）"
    )


def _select(payload: Any, select: str) -> Any:
    node = payload
    for part in select.split("."):
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                raise BlockError(f"select {select!r}: 列表下标 {part!r} 无效") from None
        elif isinstance(node, dict):
            if part not in node:
                raise BlockError(
                    f"select {select!r}: 没有键 {part!r}（现有: {', '.join(list(node)[:8])}）"
                )
            node = node[part]
        else:
            raise BlockError(f"select {select!r}: 在 {part!r} 处无法继续下钻")
    return node


def _as_rows(payload: Any, origin: str) -> List[Dict[str, Any]]:
    if isinstance(payload, dict) and "columns" in payload and "rows" in payload:
        columns = payload["columns"]
        return [dict(zip(columns, row)) for row in payload["rows"]]
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        payload = payload["rows"]
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return list(payload)
    raise BlockError(
        f"{origin}: 需要「对象列表」（或 {{columns, rows}}），实际是 {type(payload).__name__}；"
        "用 select 指到列表所在的键"
    )


def _load_file(
    ctx: BlockContext, raw: str, select: Optional[str]
) -> List[Dict[str, Any]]:
    path = _resolve_path(ctx, raw)
    key = (str(path), select or "")
    if key in ctx._file_cache:
        return ctx._file_cache[key]
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows: List[Dict[str, Any]] = list(csv.DictReader(handle))
    elif suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise BlockError(f"{raw}: JSON 无法解析: {error}") from None
        if select:
            payload = _select(payload, select)
        rows = _as_rows(payload, raw)
    else:
        raise BlockError(f"{raw}: 只支持 .csv / .json 数据文件")
    ctx._file_cache[key] = rows
    return rows


def _equals(left: Any, right: Any) -> bool:
    if str(left) == str(right):
        return True
    a, b = to_number(left), to_number(right)
    return a is not None and b is not None and a == b


def load_rows(ctx: BlockContext, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rows of a block: a file path or inline list in ``data``/``rows``, then ``where``."""
    data = spec.get("data", spec.get("rows"))
    if data is None:
        raise BlockError("缺少 data（数据文件路径）或 rows（内联对象列表）")
    if isinstance(data, list):
        rows = _as_rows(data, "inline data")
    elif isinstance(data, str):
        rows = _load_file(ctx, data, spec.get("select"))
    else:
        raise BlockError("data 只能是文件路径字符串或对象列表")
    where = spec.get("where")
    if where:
        if not isinstance(where, dict):
            raise BlockError("where 必须是 {字段: 值或值列表} 的对象")

        def keep(row: Dict[str, Any]) -> bool:
            for field, expected in where.items():
                actual = row.get(field)
                if isinstance(expected, list):
                    if not any(_equals(actual, item) for item in expected):
                        return False
                elif not _equals(actual, expected):
                    return False
            return True

        rows = [row for row in rows if keep(row)]
    else:
        rows = list(rows)
    return rows


def sort_rows(rows: List[Dict[str, Any]], sort: Optional[str]) -> List[Dict[str, Any]]:
    if not sort:
        return rows
    descending = sort.startswith("-")
    field = sort[1:] if descending else sort

    def key(row: Dict[str, Any]) -> Tuple[int, Any]:
        value = row.get(field)
        number = to_number(value)
        if number is not None:
            return (0, number)
        return (1, "" if value is None else str(value))

    present = [row for row in rows if row.get(field) not in (None, "")]
    missing = [row for row in rows if row.get(field) in (None, "")]
    present.sort(key=key, reverse=descending)
    return present + missing


# --- columns ---------------------------------------------------------------------


class Column:
    def __init__(self, raw: Any, rows: Sequence[Dict[str, Any]]) -> None:
        if isinstance(raw, str):
            raw = {"field": raw}
        if not isinstance(raw, dict) or not raw.get("field"):
            raise BlockError(f"列定义需要 field: {raw!r}")
        self.field: str = raw["field"]
        self.label: str = str(raw.get("label", self.field))
        self.fmt: Optional[str] = raw.get("fmt")
        self.color: Optional[str] = raw.get("color")
        self.agg: str = raw.get("agg", "sum")
        self.title_field: Optional[str] = raw.get("title_field")
        tag = raw.get("tag")
        if tag not in (None, False, True) and not isinstance(tag, dict):
            raise BlockError(f"列 {self.field}: tag 只能是 true 或 {{值: 色调}}")
        if isinstance(tag, dict):
            bad = [tone for tone in tag.values() if tone not in _TAG_TONES]
            if bad:
                raise BlockError(
                    f"列 {self.field}: tag 色调 {bad[0]!r} 无效（可用: {', '.join(_TAG_TONES)}）"
                )
        self.tag: Any = tag or None
        self._tag_seen: Dict[str, str] = {}
        align = raw.get("align")
        if align not in (None, "left", "right", "center"):
            raise BlockError(f"列 {self.field}: align 只能是 left/right/center")
        if align is None:
            numeric = False
            if self.fmt != "text":
                for row in rows[:20]:
                    value = row.get(self.field)
                    if value not in (None, ""):
                        numeric = to_number(value) is not None
                        break
            align = "right" if numeric else "left"
        self.align: str = align

    @property
    def is_numeric(self) -> bool:
        return self.align == "right" and self.fmt != "text"

    def tag_tone(self, text: str) -> str:
        if isinstance(self.tag, dict) and text in self.tag:
            return self.tag[text]
        if text not in self._tag_seen:
            self._tag_seen[text] = _TAG_TONES[len(self._tag_seen) % 4]
        return self._tag_seen[text]


def normalize_columns(raw: Any, rows: Sequence[Dict[str, Any]]) -> List[Column]:
    if not raw:
        if not rows:
            return []
        raw = list(rows[0].keys())
    return [Column(item, rows) for item in raw]


_ALIGN_CLASS = {"left": "", "right": "r", "center": "c"}
_OTHER_KEY = "__bi_other"


def _cell(col: Column, row: Dict[str, Any], name: bool = False) -> str:
    value = row.get(col.field)
    text, sign = format_value(value, col.fmt)
    classes = [_ALIGN_CLASS[col.align]]
    color = sign_class(sign, col.color)
    if color:
        classes.append(color)
    elif name and not col.is_numeric and not col.tag:
        classes.append("rp-name")
    attrs = ""
    cls = " ".join(c for c in classes if c)
    if cls:
        attrs += f' class="{cls}"'
    number = to_number(value) if col.is_numeric else None
    if number is not None:
        attrs += f' data-v="{_round(number)}"'
    if col.title_field and row.get(col.title_field) not in (None, ""):
        attrs += f' title="{esc(row.get(col.title_field))}"'
    content = esc(text)
    if col.tag and text not in ("", _EMPTY) and not row.get(_OTHER_KEY):
        content = f'<span class="rp-tag rp-tag-{col.tag_tone(text)}">{content}</span>'
    return f"<td{attrs}>{content}</td>"


def _row_cells(
    cols: Sequence[Column], row: Dict[str, Any], rank: Optional[str] = None
) -> str:
    lead = "" if rank is None else f'<td class="rp-rk">{esc(rank)}</td>'
    return lead + "".join(
        _cell(col, row, name=index == 0) for index, col in enumerate(cols)
    )


def _header(
    cols: Sequence[Column],
    lead: bool = False,
    sortable: bool = False,
    rank: bool = False,
) -> str:
    cells = ['<th class="rp-exp-cell"></th>'] if lead else []
    if rank:
        cells.append("<th>#</th>")
    for col in cols:
        classes = [_ALIGN_CLASS[col.align]]
        click = ""
        if sortable:
            classes.append("rp-sortable")
            click = ' onclick="__biSortTable(this)"'
        cls = " ".join(c for c in classes if c)
        cls_attr = f' class="{cls}"' if cls else ""
        cells.append(f"<th{cls_attr}{click}>{esc(col.label)}</th>")
    return "<thead><tr>" + "".join(cells) + "</tr></thead>"


def _plain_rows(
    cols: Sequence[Column], rows: Sequence[Dict[str, Any]], rank: bool = False
) -> str:
    out: List[str] = []
    for index, row in enumerate(rows):
        other = bool(row.get(_OTHER_KEY))
        label = None if not rank else ("" if other else str(index + 1))
        cls = ' class="rp-other"' if other else ""
        out.append(f"<tr{cls}>{_row_cells(cols, row, label)}</tr>")
    return "".join(out)


def _wrap_table(table_html: str, n_rows: int, max_height: Optional[int]) -> str:
    """Scroll rule of layout-spec section 3: long tables scroll inside the card."""
    wrap, style = "rp-table-wrap", ""
    if max_height or n_rows > LONG_TABLE_ROWS:
        wrap += " scroll"
        style = f"max-height:{int(max_height or DEFAULT_SCROLL_HEIGHT)}px"
    return _macros().table_wrap(wrap, style, table_html)


def _table_class(n_rows: int, header: Optional[str], zebra: bool = True) -> str:
    """Zebra rows; a dark sticky header for long (scrolling) tables unless overridden."""
    if header not in (None, "dark", "light"):
        raise BlockError("header 只能是 dark 或 light")
    classes = ["rp-table"]
    if zebra:
        classes.append("zebra")
    if header == "dark" or (header is None and n_rows > LONG_TABLE_ROWS):
        classes.append("dark")
    return " ".join(classes)


def _aggregate_other(
    rest: Sequence[Dict[str, Any]], cols: Sequence[Column], label: str
) -> Dict[str, Any]:
    row: Dict[str, Any] = {}
    for index, col in enumerate(cols):
        if index == 0:
            row[col.field] = label
            continue
        numbers = [
            n for n in (to_number(r.get(col.field)) for r in rest) if n is not None
        ]
        if not col.is_numeric or col.agg == "none" or not numbers:
            row[col.field] = ""
        elif col.agg == "mean":
            row[col.field] = sum(numbers) / len(numbers)
        else:
            row[col.field] = sum(numbers)
    return row


# --- blocks: text-ish -----------------------------------------------------------


def render_conclusion(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    if not spec.get("text"):
        raise BlockError("conclusion 需要 text")
    return _macros().conclusion(rich_text(spec["text"]))


def render_note(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    text = str(spec.get("text") or "")
    if not text:
        raise BlockError("note 需要 text")
    if len(text) > NOTE_MAX_CHARS:
        ctx.warn(
            f"note 超过 {NOTE_MAX_CHARS} 字（{len(text)}）：{text[:20]}…，口径小字应一行写完"
        )
    return _macros().note(esc(text))


def render_caption(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    if not spec.get("text"):
        raise BlockError("caption 需要 text")
    return _macros().caption(rich_text(spec["text"]))


def render_html(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    return str(spec.get("html") or "")


# --- kpi ------------------------------------------------------------------------


def render_kpi(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    items = spec.get("items")
    if not isinstance(items, list) or not items:
        raise BlockError("kpi 需要非空 items 列表")
    base_row: Dict[str, Any] = {}
    if spec.get("data") is not None or spec.get("rows") is not None:
        rows = load_rows(ctx, spec)
        base_row = rows[0] if rows else {}
    macros = _macros()
    tiles: List[str] = []
    for index, item in enumerate(items):
        if "value" in item:
            value = item["value"]
        elif item.get("field"):
            value = base_row.get(item["field"])
        else:
            raise BlockError(f"kpi 项 {item.get('label')!r} 需要 value 或 field")
        text, _ = format_value(value, item.get("fmt"))
        foot: List[str] = []
        delta = item.get("delta")
        if delta is None and item.get("delta_field"):
            delta = base_row.get(item["delta_field"])
        if delta is not None:
            dtext, dsign = format_value(delta, item.get("delta_fmt", "pct2+"))
            mode = "sign_inv" if item.get("delta_inverse") else "sign"
            color = sign_class(dsign, mode)
            label = esc(item.get("delta_label", "环比"))
            body = f'<span class="{color}">{esc(dtext)}</span>' if color else esc(dtext)
            foot.append(f"{label} {body}")
        if item.get("foot"):
            foot.append(rich_text(item["foot"]))
        tiles.append(
            macros.kpi_tile(
                index % 4 + 1,
                esc(item.get("label", "")),
                esc(text),
                esc(item.get("unit", "")),
                " · ".join(foot),
            )
        )
    return macros.kpi_row("".join(tiles), bool(spec.get("compact")))


# --- tables ---------------------------------------------------------------------


def render_table(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    rows = sort_rows(load_rows(ctx, spec), spec.get("sort"))
    cols = normalize_columns(spec.get("columns"), rows)
    if not cols:
        raise BlockError("table 没有数据列")
    top_n = spec.get("top_n")
    if top_n and len(rows) > int(top_n):
        head, rest = rows[: int(top_n)], rows[int(top_n) :]
        other = spec.get("other_row", True)
        if other:
            label = other.get("label") if isinstance(other, dict) else None
            other_row = _aggregate_other(rest, cols, label or f"其他（{len(rest)} 项）")
            other_row[_OTHER_KEY] = True
            head.append(other_row)
        rows = head
    rank = bool(spec.get("rank"))
    long_table = len(rows) > LONG_TABLE_ROWS
    sortable = bool(spec.get("sortable", long_table))
    tid = ctx.next_id("table")
    body = _plain_rows(cols, rows, rank)
    foot = ""
    total = spec.get("total")
    if total:
        label = total.get("label", "合计") if isinstance(total, dict) else "合计"
        sums = _aggregate_other(rows, cols, label)
        sums[_OTHER_KEY] = True
        foot = f"<tfoot><tr>{_row_cells(cols, sums, '' if rank else None)}</tr></tfoot>"
    table = (
        f'<table id="{tid}" class="{_table_class(len(rows), spec.get("header"))}">'
        f"{_header(cols, sortable=sortable, rank=rank)}<tbody>{body}</tbody>{foot}</table>"
    )
    max_height = spec.get("max_height") or ctx.grid_height
    out = _wrap_table(table, len(rows), max_height)
    if spec.get("search"):
        out = _macros().table_tools(tid, len(rows)) + out
    return out


def _drill_table(
    ctx: BlockContext,
    level_spec: Dict[str, Any],
    rows: List[Dict[str, Any]],
    level: int,
) -> str:
    if level >= MAX_DRILL_LEVELS:
        raise BlockError(f"drill_table 最多 {MAX_DRILL_LEVELS} 层")
    rows = sort_rows(rows, level_spec.get("sort"))
    cols = normalize_columns(level_spec.get("columns"), rows)
    if not cols:
        raise BlockError("drill_table 某一层没有数据列")
    child_spec = level_spec.get("children")
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    key = level_spec.get("key")
    if child_spec:
        if not key:
            raise BlockError(
                "有 children 的层必须声明 key（与子层 join 字段对应的父字段）"
            )
        join = child_spec.get("join") or key
        for child in load_rows(ctx, child_spec):
            groups[str(child.get(join))].append(child)

    n_span = len(cols) + 1
    body: List[str] = []
    for row in rows:
        kids = groups.get(str(row.get(key))) if child_spec else None
        cells = _row_cells(cols, row)
        if kids:
            rid = ctx.next_id("drill")
            body.append(
                f'<tr class="rp-drill" data-drill-toggle="{rid}" '
                f"onclick=\"__biToggleRow('{rid}')\">"
                '<td class="rp-exp-cell"><span class="rp-exp">▸</span></td>'
                f"{cells}</tr>"
            )
            nested = _drill_table(ctx, child_spec, kids, level + 1)
            body.append(
                f'<tr id="{rid}" class="hidden rp-drill-child">'
                f'<td colspan="{n_span}">{nested}</td></tr>'
            )
        else:
            body.append(f'<tr><td class="rp-exp-cell"></td>{cells}</tr>')
    if level == 0:
        table_class = _table_class(len(rows), level_spec.get("header"), zebra=False)
    else:
        table_class = "rp-table rp-nested"
    return (
        f'<table class="{table_class}">'
        f"{_header(cols, lead=True)}<tbody>{''.join(body)}</tbody></table>"
    )


def render_drill_table(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    rows = load_rows(ctx, spec)
    table = _drill_table(ctx, spec, rows, 0)
    max_height = spec.get("max_height") or ctx.grid_height
    return _wrap_table(table, len(rows), max_height)


def _heat_style(t: Optional[float]) -> str:
    """Inline background and text colour for a 0..1 intensity (retention heat table)."""
    if t is None:
        return f"color:{_INK_3}"
    t = max(0.0, min(1.0, t))
    alpha = 0.06 + 0.74 * t
    text = "#fff" if t > 0.62 else _INK_2
    return f"background:rgba(28,84,227,{alpha:.2f});color:{text}"


def render_heatmap_table(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    rows = load_rows(ctx, spec)
    fmt = spec.get("fmt", "pct1")
    value_fields = spec.get("value_fields")
    if value_fields:
        label_field = spec.get("label_field")
        if not label_field:
            raise BlockError("heatmap_table（宽表）需要 label_field")
        heads = [
            (f, str(f))
            if isinstance(f, str)
            else (f["field"], str(f.get("label", f["field"])))
            for f in value_fields
        ]
        row_labels = [str(r.get(label_field, "")) for r in rows]
        matrix = [[to_number(r.get(field)) for field, _ in heads] for r in rows]
        col_labels = [label for _, label in heads]
    else:
        row_field, col_field, value_field = (
            spec.get("row"),
            spec.get("col"),
            spec.get("value"),
        )
        if not (row_field and col_field and value_field):
            raise BlockError("heatmap_table（长表）需要 row、col、value 三个字段名")
        row_labels, col_labels = [], list(spec.get("col_order") or [])
        for r in rows:
            rl, cl = str(r.get(row_field, "")), str(r.get(col_field, ""))
            if rl not in row_labels:
                row_labels.append(rl)
            if cl not in col_labels and not spec.get("col_order"):
                col_labels.append(cl)
        lookup = {
            (str(r.get(row_field, "")), str(r.get(col_field, ""))): to_number(
                r.get(value_field)
            )
            for r in rows
        }
        matrix = [[lookup.get((rl, cl)) for cl in col_labels] for rl in row_labels]
        label_field = row_field

    scale_by = spec.get("scale_by", "all")
    flat = [v for line in matrix for v in line if v is not None]
    if not flat:
        raise BlockError("heatmap_table 没有可用数值")

    def bounds(values: List[float]) -> Tuple[float, float]:
        return (min(values), max(values)) if values else (0.0, 0.0)

    g_lo, g_hi = bounds(flat)
    head = (
        f"<thead><tr><th>{esc(spec.get('row_label', ''))}</th>"
        + "".join(f'<th class="r">{esc(c)}</th>' for c in col_labels)
        + "</tr></thead>"
    )
    col_bounds = [
        bounds([line[i] for line in matrix if line[i] is not None])
        for i in range(len(col_labels))
    ]
    body: List[str] = []
    for r_idx, (label, line) in enumerate(zip(row_labels, matrix)):
        r_lo, r_hi = bounds([v for v in line if v is not None])
        cells = [f'<td class="rp-name">{esc(label)}</td>']
        for c_idx, value in enumerate(line):
            if scale_by == "column":
                lo, hi = col_bounds[c_idx]
            elif scale_by == "row":
                lo, hi = r_lo, r_hi
            else:
                lo, hi = g_lo, g_hi
            t = (
                None
                if value is None
                else (0.5 if hi == lo else (value - lo) / (hi - lo))
            )
            text, _ = format_value(value, fmt)
            cells.append(f'<td class="r" style="{_heat_style(t)}">{esc(text)}</td>')
        body.append(f"<tr>{''.join(cells)}</tr>")
    table_class = _table_class(len(row_labels), spec.get("header"), zebra=False)
    table = f'<table class="{table_class}">{head}<tbody>{"".join(body)}</tbody></table>'
    return _wrap_table(
        table, len(row_labels), spec.get("max_height") or ctx.grid_height
    )


# --- charts ---------------------------------------------------------------------


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 4)


def _series_specs(
    spec: Dict[str, Any], rows: Sequence[Dict[str, Any]], x_field: str
) -> List[Dict[str, Any]]:
    raw = spec.get("series")
    if raw:
        items = [{"field": s} if isinstance(s, str) else dict(s) for s in raw]
    else:
        items = []
        for field in rows[0].keys() if rows else []:
            if field != x_field and any(
                to_number(r.get(field)) is not None for r in rows[:20]
            ):
                items.append({"field": field})
    if not items:
        raise BlockError("chart 找不到数值列：请用 series 指定")
    for item in items:
        if not item.get("field"):
            raise BlockError(f"series 项需要 field: {item!r}")
        item.setdefault("name", item["field"])
    return items


def chart_default_height(ctx: BlockContext, spec: Dict[str, Any]) -> int:
    """Layout-spec section 2 heights: <=5 points 240, 6-12 -> 300, 13+ -> 340."""
    if spec.get("height"):
        return int(spec["height"])
    kind = spec.get("kind")
    if kind in ("pie", "funnel", "radar"):
        return 280
    rows = load_rows(ctx, spec)
    n = len(rows)
    if kind == "hbar":
        return max(220, min(560, 30 * n + 50))
    if kind == "scatter":
        return 300
    return 240 if n <= 5 else (300 if n <= 12 else 340)


def _gradient(start: str, end: str, horizontal: bool = False) -> Dict[str, Any]:
    return {
        "type": "linear",
        "x": 0,
        "y": 0,
        "x2": 1 if horizontal else 0,
        "y2": 0 if horizontal else 1,
        "colorStops": [{"offset": 0, "color": start}, {"offset": 1, "color": end}],
    }


_AXIS_LABEL = {"color": _INK_3, "fontSize": 11}
_AXIS_LINE = {"lineStyle": {"color": "#e3e7ee"}}
_SPLIT_LINE = {"lineStyle": {"color": "#eef1f5"}}
_BAR_GRADIENT = _gradient("#4d7cf0", "#1C54E3")
_HBAR_GRADIENT = _gradient("#7ea0f0", "#1C54E3", horizontal=True)
_HIGHLIGHT_GRADIENT = _gradient("#1C54E3", "#7347FF")
_MUTED_BAR = "#c7d4f2"
# Data labels stay readable only on small charts.
LABEL_MAX_POINTS = 12


def _category_axis(categories: List[str], inverse: bool = False) -> Dict[str, Any]:
    axis: Dict[str, Any] = {
        "type": "category",
        "data": categories,
        "axisLabel": dict(_AXIS_LABEL),
        "axisLine": _AXIS_LINE,
        "axisTick": {"show": False},
    }
    if inverse:
        axis["inverse"] = True
    return axis


def _value_axis(
    pct: bool, name: Optional[str] = None, split: bool = True
) -> Dict[str, Any]:
    label = dict(_AXIS_LABEL)
    if pct:
        label["formatter"] = "{value}%"
    axis: Dict[str, Any] = {
        "type": "value",
        "axisLabel": label,
        "splitLine": _SPLIT_LINE if split else {"show": False},
    }
    if name:
        axis["name"] = str(name)
        axis["nameTextStyle"] = dict(_AXIS_LABEL)
    return axis


def _point_data(
    values: List[Optional[float]],
    fmt: Optional[str],
    labels: bool,
    colors: Optional[List[Any]] = None,
) -> List[Any]:
    """Series data; with labels on, each point carries its formatted text."""
    if not labels and not colors:
        return values
    out: List[Any] = []
    for index, value in enumerate(values):
        if value is None:
            out.append(None)
            continue
        item: Dict[str, Any] = {"value": value}
        if labels:
            item["label"] = {"formatter": format_value(value, fmt)[0]}
        if colors and colors[index] is not None:
            item["itemStyle"] = {"color": colors[index]}
        out.append(item)
    return out


def _highlight_index(
    highlight: Any, categories: List[str], values: List[Optional[float]]
) -> Optional[int]:
    if highlight in (None, False, ""):
        return None
    if highlight == "last":
        return len(categories) - 1 if categories else None
    if highlight == "max":
        numbers = [(v, i) for i, v in enumerate(values) if v is not None]
        return max(numbers)[1] if numbers else None
    if str(highlight) in categories:
        return categories.index(str(highlight))
    raise BlockError(f"highlight 只能是 last、max 或某个类目名，实际 {highlight!r}")


def _merge_other(
    rows: List[Dict[str, Any]], name_field: str, value_field: str, top_n: int
) -> List[Dict[str, Any]]:
    ordered = sort_rows(rows, f"-{value_field}")
    if len(ordered) <= top_n:
        return ordered
    head, rest = ordered[:top_n], ordered[top_n:]
    total = sum(
        n for n in (to_number(r.get(value_field)) for r in rest) if n is not None
    )
    head.append({name_field: f"其他（{len(rest)} 项）", value_field: total})
    return head


def build_chart_option(
    ctx: BlockContext, spec: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Column]]:
    """Return ``(echarts option, rows used, fallback-table columns)`` for a chart block."""
    kind = spec.get("kind")
    if kind not in CHART_KINDS:
        raise BlockError(f"chart.kind 必须是 {', '.join(CHART_KINDS)}，实际 {kind!r}")
    rows = load_rows(ctx, spec)
    if not rows:
        raise BlockError("chart 没有数据行")
    x_field = spec.get("x")
    if not x_field:
        raise BlockError("chart 需要 x（类目 / 名称字段）")
    series = _series_specs(spec, rows, x_field)
    first = series[0]

    if kind in ("pie", "funnel"):
        top_n = spec.get("top_n") or (
            PIE_TOP_N if kind == "pie" and len(rows) > PIE_MAX_CATEGORIES else None
        )
        if top_n:
            rows = _merge_other(rows, x_field, first["field"], int(top_n))
        elif kind == "funnel":
            rows = sort_rows(rows, f"-{first['field']}")
    elif spec.get("sort"):
        rows = sort_rows(rows, spec["sort"])
    if kind == "hbar" and not spec.get("sort") and not spec.get("keep_order"):
        rows = sort_rows(rows, f"-{first['field']}")
    if spec.get("top_n") and kind in ("bar", "hbar", "grouped_bar"):
        n = int(spec["top_n"])
        if len(rows) > n:
            head, rest = rows[:n], rows[n:]
            other = {x_field: f"其他（{len(rest)} 项）"}
            for s in series:
                other[s["field"]] = sum(
                    v
                    for v in (to_number(r.get(s["field"])) for r in rest)
                    if v is not None
                )
            rows = head + [other]

    categories = [str(r.get(x_field, "")) for r in rows]
    pct_axis = str(first.get("fmt") or "").startswith("pct")
    cartesian = kind in ("line", "bar", "grouped_bar", "hbar")
    option: Dict[str, Any] = {
        "color": _PALETTE,
        "textStyle": {"fontFamily": "inherit"},
        "grid": {
            "top": 36,
            "right": 24,
            "bottom": 24,
            "left": 16,
            "containLabel": True,
        },
        "legend": {
            "top": 4,
            "icon": "roundRect",
            "itemWidth": 12,
            "itemHeight": 8,
            "textStyle": {"fontSize": 12, "color": _INK_2},
        },
        "tooltip": {
            "trigger": "axis" if cartesian else "item",
            "backgroundColor": "#fff",
            "borderColor": _LINE,
            "textStyle": {"color": _INK_2, "fontSize": 12},
            "extraCssText": "box-shadow:0 8px 24px rgba(16,20,28,.08);border-radius:10px;",
        },
    }
    # A single-series cartesian chart needs no legend; the freed space goes to the plot.
    if cartesian and len(series) == 1 and not spec.get("legend"):
        option["legend"] = {"show": False}
        option["grid"]["top"] = 28
    show_labels = bool(
        spec.get("labels", len(rows) <= LABEL_MAX_POINTS and len(series) <= 2)
    )

    if kind in ("line", "bar", "grouped_bar"):
        option["xAxis"] = _category_axis(categories)
        dual = any(s.get("axis") == "right" for s in series)
        left = _value_axis(pct_axis, spec.get("y_label"))
        if dual:
            right_fmt = next(s for s in series if s.get("axis") == "right").get("fmt")
            right = _value_axis(
                str(right_fmt or "").startswith("pct"), spec.get("y2_label"), False
            )
            option["yAxis"] = [left, right]
        else:
            option["yAxis"] = left
        first_values = [_round(to_number(r.get(first["field"]))) for r in rows]
        hl = _highlight_index(spec.get("highlight"), categories, first_values)
        default_type = "line" if kind == "line" else "bar"
        mixed = len({s.get("type") or default_type for s in series}) > 1
        out_series = []
        for index, s in enumerate(series):
            values = [_round(to_number(r.get(s["field"]))) for r in rows]
            stype = s.get("type") or default_type
            colors = None
            entry: Dict[str, Any] = {"name": s["name"], "type": stype}
            if stype == "line":
                entry.update(
                    {
                        "smooth": False,
                        "symbol": "circle",
                        "symbolSize": 7,
                        "lineStyle": {"width": 2.5},
                    }
                )
                if mixed and not s.get("color"):
                    color = _PALETTE[index % len(_PALETTE)]
                    entry["itemStyle"] = {"color": color}
                    entry["lineStyle"]["color"] = color
            else:
                entry["barMaxWidth"] = 34 if len(series) == 1 else 22
                entry["itemStyle"] = {"borderRadius": [6, 6, 0, 0]}
                if spec.get("stack"):
                    entry["stack"] = "total"
                    entry["itemStyle"] = {"borderRadius": 0}
                if len(series) == 1 and not s.get("color"):
                    entry["itemStyle"]["color"] = _BAR_GRADIENT
                if hl is not None and index == 0:
                    entry["itemStyle"]["color"] = _MUTED_BAR
                    colors = [
                        _HIGHLIGHT_GRADIENT if i == hl else None
                        for i in range(len(values))
                    ]
            if s.get("color"):
                entry.setdefault("itemStyle", {})["color"] = s["color"]
            if s.get("axis") == "right":
                entry["yAxisIndex"] = 1
            # Bar + line: only the bars carry labels, or the two sets collide.
            labels = (
                show_labels and not spec.get("stack") and (not mixed or stype == "bar")
            )
            if labels:
                entry["label"] = {
                    "show": True,
                    "position": "top",
                    "color": _INK_2,
                    "fontSize": 11,
                }
            entry["data"] = _point_data(values, s.get("fmt"), labels, colors)
            out_series.append(entry)
        _apply_marks(
            out_series[0],
            spec.get("mark"),
            categories,
            [to_number(r.get(first["field"])) for r in rows],
        )
        option["series"] = out_series
    elif kind == "hbar":
        option["xAxis"] = _value_axis(pct_axis, spec.get("y_label"))
        option["yAxis"] = _category_axis(categories, inverse=True)
        out_series = []
        for s in series:
            values = [_round(to_number(r.get(s["field"]))) for r in rows]
            entry = {
                "name": s["name"],
                "type": "bar",
                "barMaxWidth": 18 if len(series) == 1 else 12,
                "itemStyle": {"borderRadius": [0, 8, 8, 0]},
            }
            if len(series) == 1 and not s.get("color"):
                entry["itemStyle"]["color"] = _HBAR_GRADIENT
            if s.get("color"):
                entry["itemStyle"]["color"] = s["color"]
            if show_labels or len(series) == 1:
                entry["label"] = {
                    "show": True,
                    "position": "right",
                    "color": "#1C54E3",
                    "fontWeight": 700,
                    "fontSize": 11,
                }
            entry["data"] = _point_data(values, s.get("fmt"), "label" in entry, None)
            out_series.append(entry)
        option["series"] = out_series
    elif kind == "pie":
        option["legend"] = {
            "top": 4,
            "icon": "circle",
            "itemWidth": 9,
            "itemHeight": 9,
            "textStyle": {"fontSize": 12, "color": _INK_2},
        }
        option["series"] = [
            {
                "name": first["name"],
                "type": "pie",
                "radius": ["45%", "70%"],
                "center": ["50%", "57%"],
                "itemStyle": {
                    "borderColor": "#fff",
                    "borderWidth": 2,
                    "borderRadius": 4,
                },
                "label": {"formatter": "{b} {d}%", "fontSize": 11, "color": _INK_2},
                "data": [
                    {
                        "name": str(r.get(x_field, "")),
                        "value": _round(to_number(r.get(first["field"]))),
                    }
                    for r in rows
                ],
            }
        ]
    elif kind == "funnel":
        option["series"] = [
            {
                "name": first["name"],
                "type": "funnel",
                "sort": "descending",
                "left": "10%",
                "width": "80%",
                "top": 36,
                "gap": 2,
                "itemStyle": {"borderColor": "#fff", "borderWidth": 1},
                "label": {"formatter": "{b}: {c}", "fontSize": 11},
                "data": [
                    {
                        "name": str(r.get(x_field, "")),
                        "value": _round(to_number(r.get(first["field"]))),
                    }
                    for r in rows
                ],
            }
        ]
    elif kind == "scatter":
        y_field = first["field"]
        option["xAxis"] = dict(
            _value_axis(False, spec.get("x_label", x_field)), scale=True
        )
        option["yAxis"] = dict(_value_axis(False, first["name"]), scale=True)
        name_field = spec.get("label_field")
        option["series"] = [
            {
                "name": first["name"],
                "type": "scatter",
                "symbolSize": 9,
                "itemStyle": {"opacity": 0.8},
                "data": [
                    {
                        "name": str(r.get(name_field, "")) if name_field else "",
                        "value": [
                            _round(to_number(r.get(x_field))),
                            _round(to_number(r.get(y_field))),
                        ],
                    }
                    for r in rows
                    if to_number(r.get(x_field)) is not None
                    and to_number(r.get(y_field)) is not None
                ],
            }
        ]
        option["tooltip"]["trigger"] = "item"
    elif kind == "radar":
        maxima: List[float] = []
        for r in rows:
            vals = [
                abs(v)
                for v in (to_number(r.get(s["field"])) for s in series)
                if v is not None
            ]
            maxima.append((max(vals) if vals else 1.0) * 1.2 or 1.0)
        option["radar"] = {
            "indicator": [
                {"name": categories[i], "max": _round(maxima[i])}
                for i in range(len(rows))
            ],
            "radius": "62%",
            "center": ["50%", "56%"],
        }
        option["series"] = [
            {
                "type": "radar",
                "data": [
                    {
                        "name": s["name"],
                        "value": [
                            _round(to_number(r.get(s["field"]))) or 0 for r in rows
                        ],
                    }
                    for s in series
                ],
            }
        ]

    fallback_cols = _fallback_columns(kind, x_field, series, spec)
    return option, rows, fallback_cols


def _apply_marks(
    first_series: Dict[str, Any],
    mark: Optional[Dict[str, Any]],
    categories: List[str],
    values: List[Optional[float]],
) -> None:
    if not mark:
        return
    points: List[Dict[str, Any]] = []
    if mark.get("max"):
        points.append({"type": "max", "name": "最大值"})
    if mark.get("min"):
        points.append({"type": "min", "name": "最小值"})
    for item in mark.get("points") or []:
        x = str(item.get("x"))
        if x not in categories:
            raise BlockError(f"mark.points: x={x!r} 不在类目里")
        y = values[categories.index(x)]
        points.append(
            {"name": item.get("label", x), "coord": [x, _round(y)], "value": _round(y)}
        )
    if points:
        first_series["markPoint"] = {
            "data": points,
            "symbolSize": 46,
            "label": {"fontSize": 10},
        }
    lines: List[Dict[str, Any]] = []
    if mark.get("avg"):
        lines.append({"type": "average", "name": "均值"})
    for item in mark.get("lines") or []:
        lines.append({"yAxis": item.get("y"), "name": item.get("label", "")})
    if lines:
        first_series["markLine"] = {
            "data": lines,
            "symbol": "none",
            "label": {"fontSize": 10},
        }


def _fallback_columns(
    kind: str, x_field: str, series: List[Dict[str, Any]], spec: Dict[str, Any]
) -> List[Column]:
    cols: List[Dict[str, Any]] = [
        {"field": x_field, "label": spec.get("x_label", x_field), "fmt": "text"}
    ]
    for s in series:
        cols.append({"field": s["field"], "label": s["name"], "fmt": s.get("fmt")})
    return [Column(c, []) for c in cols]


def _js_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("</", "<\\/")
        .replace("<!--", "<\\!--")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_chart(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    option, rows, fallback_cols = build_chart_option(ctx, spec)
    height = int(
        spec.get("height") or ctx.grid_height or chart_default_height(ctx, spec)
    )
    cid = ctx.next_id("chart")
    # Fallback table from the *same* rows: shown when ECharts is missing or on toggle.
    for col in fallback_cols[1:]:
        col.align = "right"
    table = (
        f'<table class="{_table_class(len(rows), "light")}">{_header(fallback_cols)}'
        f"<tbody>{_plain_rows(fallback_cols, rows)}</tbody></table>"
    )
    table = _wrap_table(table, len(rows), None)
    macros = _macros()
    box = macros.chart_box(cid, height, table)
    if spec.get("toggle"):
        ctx.card_tools = macros.chart_toggle(cid)
    script = (
        "<script>(function(){var id="
        + _js_json(cid)
        + ",el=document.getElementById("
        + _js_json(cid)
        + ");if(!el)return;var opt="
        + _js_json(option)
        + ";window.CHART_OPTS=window.CHART_OPTS||{};window.CHART_OPTS[id]=opt;"
        "if(!window.echarts||window.echarts.__qwenpawDataFallback){window.__biChartView&&window.__biChartView(id,'table',true);return;}"
        "try{var c=echarts.init(el);c.setOption(opt);window.__echartsInstances=window.__echartsInstances||[];"
        "if(window.__echartsInstances.indexOf(c)<0)window.__echartsInstances.push(c);}"
        "catch(e){window.__biChartView&&window.__biChartView(id,'table',true);}})();</script>"
    )
    return box + script


# --- timeline --------------------------------------------------------------------

_TIMELINE_KIND = {"major": "tl-major", "promo": "tl-promo", "product": "tl-product"}


def render_timeline(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    if isinstance(spec.get("events"), list):
        events = spec["events"]
    else:
        events = load_rows(ctx, spec)
    if not events:
        raise BlockError("timeline 没有事件")
    macros = _macros()
    items = []
    for event in sorted(events, key=lambda e: str(e.get("date", ""))):
        items.append(
            macros.timeline_item(
                _TIMELINE_KIND.get(str(event.get("kind", "")), ""),
                esc(event.get("date", "")),
                esc(event.get("title", "")),
                esc(event.get("desc", "")),
            )
        )
    return macros.timeline("".join(items))


# --- composition bar -------------------------------------------------------------

STACKBAR_MAX_SEGMENTS = 6
_STACKBAR_DOTS = ["#7347FF", "#1C54E3", "#00CEC9", "#fb6a3c", "#7ea0f0", "#8a909c"]


def render_stackbar(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    """One 100% bar of a total's parts, with a legend of value and share."""
    label_field, value_field = spec.get("x"), spec.get("value")
    if not (label_field and value_field):
        raise BlockError("stackbar 需要 x（分类字段）和 value（数值字段）")
    rows = sort_rows(load_rows(ctx, spec), spec.get("sort"))
    parts = [(str(r.get(label_field, "")), to_number(r.get(value_field))) for r in rows]
    parts = [(label, v) for label, v in parts if v is not None and v > 0]
    if not parts:
        raise BlockError("stackbar 没有正数值")
    if len(parts) > STACKBAR_MAX_SEGMENTS:
        head = parts[: STACKBAR_MAX_SEGMENTS - 1]
        rest = parts[STACKBAR_MAX_SEGMENTS - 1 :]
        head.append((f"其他（{len(rest)} 项）", sum(v for _, v in rest)))
        parts = head
    total = sum(v for _, v in parts)
    fmt, unit = spec.get("fmt"), str(spec.get("unit", ""))
    segments: List[str] = []
    legend: List[str] = []
    for index, (label, value) in enumerate(parts):
        share = value / total * 100
        text = esc(format_value(value, fmt)[0] + unit)
        inner = text if share >= 8 else ""
        segments.append(
            f'<div class="rp-sb-seg s{index + 1}" style="width:{share:.2f}%" '
            f'title="{esc(label)} {text}">{inner}</div>'
        )
        legend.append(
            f'<span><i class="rp-lg-dot" style="background:{_STACKBAR_DOTS[index]}"></i>'
            f"{esc(label)} · {text} · {share:.1f}%</span>"
        )
    return _macros().stackbar("".join(segments), "".join(legend))


# --- containers ------------------------------------------------------------------

# Blocks that get their own card (title/subtitle header) outside an explicit card.
_CARDED_BLOCKS = (
    "table",
    "drill_table",
    "heatmap_table",
    "chart",
    "stackbar",
    "timeline",
    "card",
)
_GRID_CLASS = {2: "rp-grid-2", 3: "rp-grid-3", "2:1": "rp-grid-2-1"}


def render_grid(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    cols = spec.get("cols", 2)
    if cols not in _GRID_CLASS:
        raise BlockError('grid.cols 只能是 2、3 或 "2:1"（左宽右窄）')
    children = spec.get("blocks")
    if not isinstance(children, list) or len(children) < 2:
        raise BlockError("grid 需要至少 2 个 blocks")
    if any(child.get("type") == "kpi" for child in children):
        ctx.warn(
            "grid 里放了 kpi：KPI 卡不和其他图表并排（layout-spec §1），建议放到 grid 外"
        )
    # Side-by-side blocks share one height so the row looks balanced.
    chart_heights = [
        chart_default_height(ctx, child)
        for child in children
        if child.get("type") == "chart"
    ]
    previous = ctx.grid_height
    ctx.grid_height = max(chart_heights) if chart_heights else previous
    try:
        inner = "".join(_render_one(ctx, child, in_grid=True) for child in children)
    finally:
        ctx.grid_height = previous
    return _macros().grid(_GRID_CLASS[cols], inner)


def render_card(ctx: BlockContext, spec: Dict[str, Any]) -> str:
    """Children of an explicit card share its header and render without own cards."""
    previous = ctx.in_card
    ctx.in_card = True
    try:
        return render_blocks(ctx, spec.get("blocks") or [])
    finally:
        ctx.in_card = previous


_RENDERERS = {
    "conclusion": render_conclusion,
    "kpi": render_kpi,
    "table": render_table,
    "drill_table": render_drill_table,
    "heatmap_table": render_heatmap_table,
    "chart": render_chart,
    "stackbar": render_stackbar,
    "grid": render_grid,
    "note": render_note,
    "caption": render_caption,
    "timeline": render_timeline,
    "card": render_card,
    "html": render_html,
}


def _render_one(ctx: BlockContext, spec: Any, in_grid: bool = False) -> str:
    if not isinstance(spec, dict):
        raise BlockError(f"block 必须是对象，实际 {type(spec).__name__}")
    kind = spec.get("type")
    renderer = _RENDERERS.get(kind)
    if renderer is None:
        raise BlockError(f"未知 block type {kind!r}（可用: {', '.join(BLOCK_TYPES)}）")
    nested = ctx.in_card
    ctx.card_tools = ""
    try:
        body = renderer(ctx, spec)
    except BlockError as error:
        label = spec.get("title") or spec.get("data") or ""
        raise BlockError(f"{kind}{f'「{label}」' if label else ''}: {error}") from None
    tools, ctx.card_tools = ctx.card_tools, ""
    if kind not in _CARDED_BLOCKS:
        return body
    macros = _macros()
    if spec.get("caption"):
        body += macros.caption(rich_text(spec["caption"]))
    if spec.get("note"):
        body += macros.note(esc(spec["note"]))
    title, subtitle = esc(spec.get("title", "")), esc(spec.get("subtitle", ""))
    if nested:
        head = macros.block_title(title) if title else ""
        if tools:
            head = f'<div class="rp-tools">{head}<span class="rp-count">{tools}</span></div>'
        return macros.stack(head + body)
    return macros.panel(title, subtitle, body, tools, "" if in_grid else "rp-stack")


def render_blocks(ctx: BlockContext, blocks: Sequence[Any]) -> str:
    if not isinstance(blocks, (list, tuple)):
        raise BlockError("blocks 必须是列表")
    return "".join(_render_one(ctx, block) for block in blocks)


def render_section(ctx: BlockContext, section: Dict[str, Any], index: int = 0) -> str:
    """HTML for one section's ``blocks``; the section heading comes from the skeleton."""
    blocks = section.get("blocks")
    ctx.section_idx = index
    try:
        return render_blocks(ctx, blocks or [])
    except BlockError as error:
        raise BlockError(f"章节「{section.get('title', index)}」: {error}") from None
