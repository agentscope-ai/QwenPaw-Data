"""HTML → Markdown-shaped plain text, used before feeding documents to compare.

Report GT documents often arrive as HTML exports; raw markup would drown the
compare kernel's numeric extraction in tag noise. Dependency-free on purpose
(host/core rule): stdlib ``html.parser`` only. Block tags become blank-line
breaks, headings keep their level, tables become pipe tables, and
script/style/svg bodies are dropped so ECharts configs never reach the kernel.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from uuid import uuid4

_SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "head",
        "svg",
        "canvas",
        "iframe",
        "object",
        "select",
        "button",
    }
)
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "figure",
        "figcaption",
        "blockquote",
        "pre",
        "ul",
        "ol",
        "hr",
        "form",
        "fieldset",
        "dl",
        "dt",
        "dd",
        "caption",
    }
)
_HEADING_TAGS = {f"h{level}": level for level in range(1, 7)}
_WHITESPACE_RE = re.compile(r"\s+")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


class _MarkdownExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._buf: list[str] = []
        self._skip = 0
        self._heading_level = 0
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self._table: list[list[str]] | None = None

    def _append_text(self, data: str) -> None:
        text = _WHITESPACE_RE.sub(" ", data).strip()
        if not text:
            return
        target = self._cell if self._cell is not None else self._buf
        target.append(text)

    def _flush(self, prefix: str = "") -> None:
        text = _WHITESPACE_RE.sub(" ", " ".join(self._buf)).strip()
        self._buf = []
        if text:
            self.blocks.append(f"{prefix}{text}")

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in _HEADING_TAGS:
            self._flush()
            self._heading_level = _HEADING_TAGS[tag]
        elif tag == "li":
            self._flush()
            self._buf.append("-")
        elif tag == "table":
            self._flush()
            self._table = []
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip > 0:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag in _HEADING_TAGS and self._heading_level:
            self._flush("#" * self._heading_level + " ")
            self._heading_level = 0
        elif tag in ("td", "th"):
            cell = _WHITESPACE_RE.sub(" ", " ".join(self._cell or [])).strip()
            self._cell = None
            if self._row is not None:
                self._row.append(cell.replace("|", "\\|"))
        elif tag == "tr":
            if self._table is not None and self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table":
            self._flush_table()
        elif tag in _BLOCK_TAGS or tag == "li":
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._append_text(data)

    def _flush_table(self) -> None:
        rows = [row for row in (self._table or []) if any(row)]
        self._table = None
        if not rows:
            return
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        self.blocks.append("\n".join(lines))


def html_to_markdown(source: str) -> str:
    """Return the Markdown-shaped text view of an HTML document."""
    parser = _MarkdownExtractor()
    parser.feed(source)
    parser.close()
    parser._flush()
    text = _BLANK_RUN_RE.sub("\n\n", "\n\n".join(parser.blocks)).strip()
    return f"{text}\n" if text else ""


def html_comparison_text(source: str) -> tuple[str, list[tuple[int, int] | None]]:
    """Same Markdown view plus exact source intervals, including HTML entities.

    Mark each text fragment while the existing extractor arranges headings and
    tables; strip the markers afterwards. Synthetic Markdown has no source span.
    Offsets are Python code points, as in the numeric comparison contract.
    """
    marker = f"\ue000{uuid4().hex}:"
    fragments: list[list[tuple[int, int] | None]] = []
    line_starts = [0] + [m.end() for m in re.finditer("\n", source)]

    class MappedExtractor(_MarkdownExtractor):
        def _append_text(self, data: str) -> None:
            if not data.strip():
                return
            line, column = self.getpos()
            offset = line_starts[line - 1] + column
            positions: list[tuple[int, int]] = []
            cursor = offset
            decoded = ""
            # HTMLParser decodes entities before handle_data. Preserve each
            # decoded character's complete original entity interval.
            while len(decoded) < len(data):
                entity = (
                    re.match(
                        r"&(?:#[xX][0-9a-fA-F]+;?|#\d+;?|[a-zA-Z][a-zA-Z0-9]+;?)",
                        source[cursor:],
                    )
                    if source[cursor : cursor + 1] == "&"
                    else None
                )
                raw = entity.group() if entity else source[cursor : cursor + 1]
                if not raw:
                    break
                value = unescape(raw)
                decoded += value
                positions.extend([(cursor, cursor + len(raw))] * len(value))
                cursor += len(raw)
            if decoded != data:
                positions = []  # Fail closed: never highlight unrelated markup.
            chars: list[str] = []
            mapping: list[tuple[int, int] | None] = []
            for match in re.finditer(r"\s+|\S", data):
                text = " " if match.group().isspace() else match.group()
                chars.append(text)
                mapping.append(
                    (positions[match.start()][0], positions[match.end() - 1][1])
                    if positions
                    else None
                )
            if chars and chars[0] == " ":
                chars.pop(0)
                mapping.pop(0)
            if chars and chars[-1] == " ":
                chars.pop()
                mapping.pop()
            if self._cell is not None:
                mapping = [
                    part
                    for char, span in zip(chars, mapping)
                    for part in ([None, span] if char == "|" else [span])
                ]
            index = len(fragments)
            fragments.append(mapping)
            super()._append_text(
                f"{marker}{index}:s\ue001{''.join(chars)}{marker}{index}:e\ue001"
            )

    parser = MappedExtractor()
    parser.feed(source)
    parser.close()
    parser._flush()
    annotated = _BLANK_RUN_RE.sub("\n\n", "\n\n".join(parser.blocks)).strip()
    pattern = re.compile(re.escape(marker) + r"(\d+):([se])\ue001")
    chunks: list[str] = []
    mapping: list[tuple[int, int] | None] = []
    last = 0
    active: int | None = None
    for match in pattern.finditer(annotated):
        chunk = annotated[last : match.start()]
        chunks.append(chunk)
        spans = fragments[active] if active is not None else []
        mapping.extend(spans if len(spans) == len(chunk) else [None] * len(chunk))
        active = int(match[1]) if match[2] == "s" else None
        last = match.end()
    chunks.append(annotated[last:])
    mapping.extend([None] * len(chunks[-1]))
    text = "".join(chunks)
    return (text + "\n", mapping + [None]) if text else ("", [])
