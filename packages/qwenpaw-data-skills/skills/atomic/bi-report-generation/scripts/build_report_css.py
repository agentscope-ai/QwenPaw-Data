# -*- coding: utf-8 -*-
"""Dev-time tool: build the offline CSS bundles shipped with bi-report-generation.

The report template used to pull Tailwind CSS and Bootstrap Icons from a CDN, so
any "no external links" requirement forced the agent to throw the builder away
and write its own renderer. This script produces the two files that replace the
CDN links; the builder inlines them, so the page needs nothing from the network:

* ``resources/report.css`` -- a subset of the very same Tailwind v2.2.19 build the
  CDN served (so the look is unchanged), cut down to the utility families the
  report style uses plus every class that appears in the skill's own templates and
  docs.
* ``resources/icons.css`` -- the Bootstrap Icons class map plus the icon font as a
  base64 ``@font-face`` (any ``bi-*`` name an agent picks still renders).

Nothing here runs at report time; re-run it only when the style set changes::

    python build_report_css.py                       # downloads the two CDN files
    python build_report_css.py --tailwind tw.css --bootstrap-icons bi.css \
        --font bootstrap-icons.woff2                 # use local copies
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

SKILL_ROOT = Path(__file__).resolve().parent.parent
RESOURCES = SKILL_ROOT / "resources"

_CDN = "https://storage.360buyimg.com/pubfree-bucket/ei-data-resource/"
TAILWIND_URL = _CDN + "02f0288/static/tailwind.min.css"
BOOTSTRAP_ICONS_URL = _CDN + "e86f985/static/bootstrap-icons/bootstrap-icons.css"
BOOTSTRAP_FONT_URL = _CDN + "1d0317d/static/bootstrap-icons/bootstrap-icons.woff2"

# Files whose class tokens are always kept (intersected with what Tailwind has).
_SCAN_FILES = (
    RESOURCES / "skeleton_template.html",
    RESOURCES / "blocks.html",
    RESOURCES / "bi_helpers.html",
    SKILL_ROOT / "SKILL.md",
    SKILL_ROOT / "references" / "layout-spec.md",
    SKILL_ROOT / "scripts" / "report_builder.py",
    SKILL_ROOT / "scripts" / "report_blocks.py",
)

_COLORS = (
    "gray|blue|red|green|yellow|orange|purple|pink|teal|indigo|white|black|"
    "transparent|current"
)
_COLOR_FAMILIES = "text|bg|border|from|via|to|fill|stroke"
_SIZES = r"(?:\d+(?:\.\d+)?|px|full|screen|auto|min|max|fit|\d+/\d+)"

# Whole utility families that are kept in the base (variant-free) form.
_FAMILIES: Tuple[str, ...] = (
    r"container|sr-only",
    r"(?:block|inline-block|inline|flex|inline-flex|grid|inline-grid|table|"
    r"table-cell|table-row|hidden|contents|flow-root)",
    r"(?:static|fixed|absolute|relative|sticky)",
    r"(?:inset|top|right|bottom|left)(?:-[xy])?-" + _SIZES,
    r"-?z-(?:\d+|auto)",
    r"overflow(?:-[xy])?-(?:auto|hidden|visible|scroll)",
    r"(?:truncate|text-ellipsis|text-clip|whitespace-[a-z-]+|break-[a-z-]+)",
    r"float-(?:left|right|none)|clear-(?:left|right|both|none)",
    r"flex-(?:row|col|row-reverse|col-reverse|wrap|wrap-reverse|nowrap|1|auto|initial|none)",
    r"(?:grow|shrink)(?:-0)?|flex-(?:grow|shrink)(?:-0)?",
    r"(?:items|justify|content|self|place-items|place-content|place-self)-[a-z-]+",
    r"order-[a-z0-9-]+",
    r"gap(?:-[xy])?-" + _SIZES,
    r"grid-(?:cols|rows)-(?:\d+|none)",
    r"(?:col|row)-(?:span-\d+|span-full|auto|start-\d+|end-\d+)",
    r"grid-flow-[a-z-]+",
    r"space-[xy]-" + _SIZES,
    r"divide-[xy](?:-\d+)?",
    r"-?[mp][trblxy]?-" + _SIZES,
    r"(?:min-|max-)?[wh]-(?:" + _SIZES + r"|(?:xs|sm|md|lg|xl|[2-7]xl|prose))",
    r"text-(?:xs|sm|base|lg|xl|[2-9]xl)",
    r"font-(?:thin|extralight|light|normal|medium|semibold|bold|extrabold|black|sans|serif|mono)",
    r"(?:italic|not-italic|underline|line-through|no-underline|overline)",
    r"(?:uppercase|lowercase|capitalize|normal-case)",
    r"(?:tabular-nums|lining-nums|oldstyle-nums|proportional-nums|slashed-zero|normal-nums)",
    r"text-(?:left|center|right|justify)|align-[a-z-]+",
    r"leading-[a-z0-9.]+|tracking-[a-z]+",
    r"list-(?:none|disc|decimal|inside|outside)",
    r"(?:" + _COLOR_FAMILIES + r")-(?:" + _COLORS + r")(?:-\d{2,3})?",
    r"(?:bg|text|border)-opacity-\d+",
    r"bg-(?:gradient-to-(?:t|tr|r|br|b|bl|l|tl)|none|clip-[a-z]+|fixed|local|scroll|"
    r"center|cover|contain|no-repeat|repeat|top|bottom|left|right)",
    r"border(?:-[trblxy])?(?:-(?:0|2|4|8))?|border-(?:solid|dashed|dotted|none|collapse|separate)",
    r"rounded(?:-(?:t|r|b|l|tl|tr|br|bl))?(?:-(?:none|sm|md|lg|xl|2xl|3xl|full))?",
    r"ring(?:-(?:0|1|2|4|8))?",
    r"shadow(?:-(?:sm|md|lg|xl|2xl|inner|none))?",
    r"opacity-\d+",
    r"transition(?:-(?:none|all|colors|opacity|shadow|transform))?|duration-\d+|"
    r"ease-[a-z-]+|delay-\d+",
    r"(?:transform|transform-gpu|transform-none)|scale-\d+|-?rotate-\d+|"
    r"-?translate-[xy]-" + _SIZES,
    r"cursor-[a-z-]+|select-[a-z]+|pointer-events-(?:none|auto)|resize(?:-[xy]|-none)?",
    r"object-[a-z-]+|table-(?:auto|fixed)",
    r"outline-none|appearance-none|filter|blur(?:-[a-z0-9]+)?",
    r"antialiased|subpixel-antialiased",
)

# Variants worth carrying, and the (small) set of families each applies to.
_RESPONSIVE = ("sm", "md", "lg", "xl")
_RESPONSIVE_FAMILIES = re.compile(
    r"^(?:hidden|block|inline-block|flex|grid|inline|flex-(?:row|col|wrap)|"
    r"grid-cols-\d+|col-span-\d+|gap(?:-[xy])?-\d+|w-[a-z0-9/]+|max-w-[a-z0-9]+|"
    r"items-[a-z]+|justify-[a-z]+|text-(?:xs|sm|base|lg|xl|[2-4]xl)|"
    r"p[xy]?-\d+|m[xy]?-(?:\d+|auto)|order-\d+|text-(?:left|center|right))$"
)
_HOVER_FAMILIES = re.compile(
    r"^(?:bg-[a-z]+(?:-\d{2,3})?|text-[a-z]+(?:-\d{2,3})?|shadow(?:-[a-z]+)?|"
    r"underline|opacity-\d+|scale-\d+)$"
)

# Arbitrary-value utilities the CDN build cannot contain (it is not JIT). The
# template and docs use a few, e.g. ``min-w-[120px]``; emit just those.
_ARBITRARY_RE = re.compile(
    r"^(?P<utility>min-w|max-w|min-h|max-h|w|h|gap|text)-\[(?P<value>[0-9.]+(?:px|rem|em|%|vh|vw))\]$"
)
_ARBITRARY_PROPS: Dict[str, str] = {
    "min-w": "min-width",
    "max-w": "max-width",
    "min-h": "min-height",
    "max-h": "max-height",
    "w": "width",
    "h": "height",
    "gap": "gap",
    "text": "font-size",
}

_FAMILY_RE = re.compile(r"^(?:" + "|".join(_FAMILIES) + r")$")
_COLOR_NAME_RE = re.compile(
    r"-(?:slate|zinc|neutral|stone|amber|lime|emerald|cyan|sky|violet|fuchsia|rose|"
    r"warmgray|truegray|coolgray|bluegray|lightblue)(?:-|$)"
)


# --- minimal CSS parsing ------------------------------------------------------


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _split_rules(css: str) -> List[Tuple[str, str]]:
    """Split a flat or one-level-nested stylesheet into ``(prelude, body)`` pairs."""
    rules: List[Tuple[str, str]] = []
    depth = 0
    start = 0
    prelude_end = 0
    i = 0
    n = len(css)
    while i < n:
        ch = css[i]
        if ch == "{":
            if depth == 0:
                prelude_end = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                rules.append((css[start:prelude_end].strip(), css[prelude_end + 1 : i]))
                start = i + 1
        elif ch == ";" and depth == 0:
            # @charset / @import style statements
            start = i + 1
        i += 1
    return rules


_CLASS_IN_SELECTOR_RE = re.compile(r"\.((?:\\.|[A-Za-z0-9_-])+)")
_UNESCAPE_RE = re.compile(r"\\(.)", re.S)
_HEX_ESCAPE_RE = re.compile(r"\\([0-9a-fA-F]{1,6}) ?")


def _unescape(token: str) -> str:
    token = _HEX_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), token)
    return _UNESCAPE_RE.sub(r"\1", token)


def _selector_classes(selector: str) -> List[str]:
    """Class names in one selector, with CSS escapes resolved (``lg\\:flex`` -> ``lg:flex``)."""
    # A hex escape such as ``.\32xl\:flex`` begins the token; handle it before the generic scan.
    names: List[str] = []
    for match in _CLASS_IN_SELECTOR_RE.finditer(selector):
        names.append(_unescape(match.group(1)))
    return names


def _split_selector_list(prelude: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in prelude:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


# --- class selection ----------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-:./\[\]%]+")


def collect_used_tokens(files: Iterable[Path]) -> Set[str]:
    tokens: Set[str] = set()
    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in _TOKEN_RE.findall(text):
            tokens.add(token.strip(".:"))
    return tokens


def _base_allowed(name: str) -> bool:
    if _COLOR_NAME_RE.search(name):
        return False
    return bool(_FAMILY_RE.match(name))


def class_allowed(name: str, used: Set[str]) -> bool:
    """Whether one Tailwind class name belongs in the shipped subset."""
    if name in used:
        return True
    if ":" not in name:
        return _base_allowed(name)
    variant, _, rest = name.partition(":")
    if variant in _RESPONSIVE:
        return bool(_RESPONSIVE_FAMILIES.match(rest)) and _base_allowed(rest)
    if variant == "hover":
        return bool(_HOVER_FAMILIES.match(rest)) and _base_allowed(rest)
    return False


def _selector_allowed(selector: str, used: Set[str]) -> bool:
    classes = _selector_classes(selector)
    if not classes:
        return True  # element / universal / pseudo-element selector: preflight
    return all(class_allowed(name, used) for name in classes)


def subset_tailwind(css: str, used: Set[str]) -> Tuple[str, Dict[str, int]]:
    css = _strip_comments(css)
    out: List[str] = []
    stats = {"rules_in": 0, "rules_out": 0}

    def keep_flat(prelude: str, body: str) -> Optional[str]:
        selectors = _split_selector_list(prelude)
        kept = [s for s in selectors if _selector_allowed(s, used)]
        if not kept:
            return None
        return ",".join(kept) + "{" + body + "}"

    for prelude, body in _split_rules(css):
        stats["rules_in"] += 1
        if prelude.startswith("@media"):
            inner: List[str] = []
            for sub_prelude, sub_body in _split_rules(body):
                stats["rules_in"] += 1
                rule = keep_flat(sub_prelude, sub_body)
                if rule:
                    inner.append(rule)
                    stats["rules_out"] += 1
            if inner:
                out.append(prelude + "{" + "".join(inner) + "}")
            continue
        if prelude.startswith("@"):
            out.append(prelude + "{" + body + "}")  # @keyframes / @font-face: keep
            stats["rules_out"] += 1
            continue
        rule = keep_flat(prelude, body)
        if rule:
            out.append(rule)
            stats["rules_out"] += 1
    return "".join(out), stats


def arbitrary_rules(used: Set[str]) -> str:
    rules: List[str] = []
    for token in sorted(used):
        match = _ARBITRARY_RE.match(token)
        if not match:
            continue
        prop = _ARBITRARY_PROPS[match.group("utility")]
        escaped = re.sub(r"([\[\]%.:/])", r"\\\1", token)
        rules.append(f".{escaped}{{{prop}:{match.group('value')}}}")
    return "".join(rules)


# --- icons --------------------------------------------------------------------


def build_icons_css(icons_css: str, font_bytes: bytes) -> str:
    """Bootstrap Icons class map with the font inlined instead of fetched."""
    icons_css = _strip_comments(icons_css)
    # Drop the remote @font-face; we write our own below.
    icons_css = re.sub(r"@font-face\s*\{.*?\}", "", icons_css, flags=re.S)
    icons_css = re.sub(r"\s+", " ", icons_css).strip()
    encoded = base64.b64encode(font_bytes).decode("ascii")
    face = (
        '@font-face{font-display:block;font-family:"bootstrap-icons";'
        f'src:url(data:font/woff2;base64,{encoded}) format("woff2")}}'
    )
    return face + icons_css


# --- io -----------------------------------------------------------------------


def _read(source: Optional[str], url: str, binary: bool = False):
    if source:
        path = Path(source)
        return path.read_bytes() if binary else path.read_text(encoding="utf-8")
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - dev tool
        data = response.read()
    return data if binary else data.decode("utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--tailwind", help="local Tailwind v2.2.19 min css (default: CDN)"
    )
    parser.add_argument(
        "--bootstrap-icons", help="local bootstrap-icons.css (default: CDN)"
    )
    parser.add_argument("--font", help="local bootstrap-icons.woff2 (default: CDN)")
    parser.add_argument(
        "--out-dir", default=str(RESOURCES), help="where to write the css files"
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tailwind = _read(args.tailwind, TAILWIND_URL)
    icons = _read(args.bootstrap_icons, BOOTSTRAP_ICONS_URL)
    font = _read(args.font, BOOTSTRAP_FONT_URL, binary=True)

    used = collect_used_tokens(_SCAN_FILES)
    subset, stats = subset_tailwind(tailwind, used)
    subset += arbitrary_rules(used)

    report_css = out_dir / "report.css"
    report_css.write_text(subset, encoding="utf-8")
    icons_out = out_dir / "icons.css"
    icons_out.write_text(build_icons_css(icons, font), encoding="utf-8")

    print(
        f"report.css: {len(subset):,} chars "
        f"({stats['rules_out']}/{stats['rules_in']} rules, from {len(tailwind):,})"
    )
    print(f"icons.css : {icons_out.stat().st_size:,} bytes (font inlined)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
