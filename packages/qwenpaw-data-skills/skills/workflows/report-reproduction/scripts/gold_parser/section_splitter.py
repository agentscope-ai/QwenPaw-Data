# -*- coding: utf-8 -*-
"""把金标报告切成保留标题层级的 ReportSection。

支持 docx（python-docx）、markdown 与 HTML。解析只还原类似 OCR 的朴素层级：
顶层标题 → 子标题 → 正文；不去推断数据点、行动项或范围外噪声——那些是下游
LLM 步骤的推理职责。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .image_reader import GoldImageReader
from .models import GoldReport, ReportSection, ReportSubsection

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MIN_SECTION_TEXT = 20

# Marker inlined into section body at each image's position; replaced by the
# vision-derived description once the image-reader agent has run.
_IMG_MARKER_FMT = "[图{idx}]"


@dataclass
class Block:
    """A single parse unit: a text paragraph or an inline image.

    For text blocks, ``text`` carries the paragraph text. For image blocks,
    ``image_path`` points at the extracted image file and ``text`` is the
    in-body marker ``[图N]`` (assigned during splitting).
    """

    level: int = 0
    text: str = ""
    is_heading: bool = False
    image_path: Path | None = None


@dataclass
class HeadingNode:
    """Internal heading tree node used to preserve arbitrary title depth."""

    title: str
    level: int
    blocks: list[Block] = field(default_factory=list)
    children: list["HeadingNode"] = field(default_factory=list)


def _content_type_to_ext(content_type: str) -> str:
    """Map a docx image part content-type to a file extension (default .png)."""
    ct = (content_type or "").lower()
    if "jpeg" in ct or ct.endswith("jpg"):
        return ".jpg"
    if "png" in ct:
        return ".png"
    if "gif" in ct:
        return ".gif"
    if "webp" in ct:
        return ".webp"
    if "bmp" in ct:
        return ".bmp"
    return ".png"


class _HTMLDocParser(HTMLParser):
    """Minimal HTML -> Markdown-ish blocks for report reproduction."""

    _TEXT_TAGS = {"p", "li", "div", "section", "article"}
    _CELL_TAGS = {"td", "th"}

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[tuple[int, str, bool]] = []
        self._cur_tag: str | None = None
        self._buf: list[str] = []
        self._level = 0
        self._in_table = False
        self._in_cell = False
        self._cell_buf: list[str] = []
        self._current_row: list[str] | None = None
        self._rows: list[list[str]] = []
        self._header_row_index: int | None = None
        self._skip_stack: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: D401
        tag = tag.lower()
        if tag in {"head", "style", "script", "noscript", "template"}:
            self._flush()
            self._skip_stack.append(tag)
            return
        if self._skip_stack:
            return
        if tag == "table":
            self._flush()
            self._start_table()
        elif tag == "tr" and self._in_table:
            self._finish_cell()
            self._current_row = []
        elif tag in self._CELL_TAGS and self._in_table:
            self._finish_cell()
            self._in_cell = True
            self._cell_buf = []
            if tag == "th" and self._header_row_index is None:
                self._header_row_index = len(self._rows)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self._cur_tag = tag
            self._level = int(tag[1])
        elif tag in self._TEXT_TAGS:
            self._flush()
            self._cur_tag = tag
        elif tag == "br":
            if self._in_cell:
                self._cell_buf.append("<br>")
            else:
                self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if tag in self._CELL_TAGS and self._in_table:
            self._finish_cell()
        elif tag == "tr" and self._in_table:
            self._finish_row()
        elif tag == "table":
            self._finish_table()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", *self._TEXT_TAGS):
            self._flush()

    def handle_data(self, data):
        if self._skip_stack:
            return
        if not data.strip():
            return
        if self._in_cell:
            text = " ".join(data.split())
            self._cell_buf.append(text)
        else:
            self._buf.extend(
                line.strip()
                for line in data.replace("\xa0", " ").splitlines()
                if line.strip()
            )

    def close(self) -> None:
        if self._in_table:
            self._finish_table()
        self._flush()
        super().close()

    def _start_table(self) -> None:
        self._in_table = True
        self._in_cell = False
        self._cell_buf = []
        self._current_row = None
        self._rows = []
        self._header_row_index = None

    def _finish_cell(self) -> None:
        if not self._in_cell:
            return
        if self._current_row is None:
            self._current_row = []
        text = " ".join(self._cell_buf).replace(" <br> ", "<br>")
        self._current_row.append(text.strip())
        self._cell_buf = []
        self._in_cell = False

    def _finish_row(self) -> None:
        self._finish_cell()
        if self._current_row:
            self._rows.append(self._current_row)
        self._current_row = None

    def _finish_table(self) -> None:
        self._finish_row()
        table = self._render_markdown_table(self._rows, self._header_row_index)
        if table:
            self.blocks.append((0, table, False))
        self._start_table()
        self._in_table = False

    def _flush(self) -> None:
        if not self._buf:
            self._cur_tag = None
            return
        is_heading = self._cur_tag in ("h1", "h2", "h3", "h4", "h5", "h6")
        text = "".join(self._buf) if is_heading else "\n".join(self._buf)
        if is_heading:
            text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
        level = self._level if is_heading else 0
        if self._cur_tag == "li" and not is_heading:
            text = f"- {text}"
        self.blocks.append((level, text, is_heading))
        self._buf = []
        self._cur_tag = None
        self._level = 0

    @staticmethod
    def _render_markdown_table(
        rows: list[list[str]],
        header_row_index: int | None,
    ) -> str:
        normalized = [row for row in rows if any(cell.strip() for cell in row)]
        if not normalized:
            return ""
        width = max(len(row) for row in normalized)
        padded = [row + [""] * (width - len(row)) for row in normalized]
        header_index = header_row_index if header_row_index is not None else 0
        header_index = max(0, min(header_index, len(padded) - 1))
        header = padded[header_index]
        body = padded[:header_index] + padded[header_index + 1 :]

        def esc_cell(value: str) -> str:
            return value.replace("|", "\\|").strip()

        lines = [
            "| " + " | ".join(esc_cell(cell) for cell in header) + " |",
            "| " + " | ".join("---" for _ in range(width)) + " |",
        ]
        lines.extend(
            "| " + " | ".join(esc_cell(cell) for cell in row) + " |" for row in body
        )
        return "\n".join(lines)


class ReportSectionSplitter:
    """Top-level entry: pick a parser by extension, then split into sections.

    Multimodal: inline images in docx are extracted to ``media_dir`` and,
    when an ``agent_runner`` is provided, each is described by the
    ``GoldImageReader`` tool; the text description is inlined into the
    section's ``raw_text`` (as gold answer / context for the inferred
    question) and ``image_paths`` is preserved for downstream carry-through.
    Without a runner, images are still extracted to ``image_paths`` but not
    described.
    """

    def __init__(
        self,
        agent_runner: Any | None = None,
        media_dir: Path | str | None = None,
    ) -> None:
        self._runner = agent_runner
        self._media_dir = Path(media_dir) if media_dir else None
        self._image_reader = (
            GoldImageReader(agent_runner) if agent_runner is not None else None
        )

    def split(self, report_path: Path, report_id: str | None = None) -> GoldReport:
        path = Path(report_path)
        rid = report_id or path.stem
        title = path.stem
        blocks = self._extract_blocks(path)
        if not blocks:
            logger.warning("gold report empty or unreadable: %s", path)
            return GoldReport(report_id=rid, source_path=path, title=title, sections=[])
        sections = self._split_blocks(blocks, rid)
        if self._image_reader is not None:
            self._describe_section_images(sections, rid)
        raw_text = "\n".join(b.text for b in blocks if b.text)
        if sections:
            title = sections[0].title if sections[0].level == 1 else title
        return GoldReport(
            report_id=rid,
            source_path=path,
            title=title,
            sections=sections,
            raw_text=raw_text,
        )

    # ----------------------------------------------------------- vision pass

    def _describe_section_images(
        self, sections: list[ReportSection], report_id: str
    ) -> None:
        """Run the image-reader tool over each section's images and inline
        the descriptions into raw_text and nested subsection content."""
        if not self._image_reader:
            return
        for section in sections:
            if not section.image_paths:
                continue
            descs: list[str] = []
            for idx, img_path in enumerate(section.image_paths, start=1):
                marker = _IMG_MARKER_FMT.format(idx=idx)
                desc = self._image_reader.describe(
                    img_path,
                    section_title=section.title,
                    section_summary=section.content_summary,
                )
                descs.append(desc)
                replacement = desc or ""
                section.raw_text = self._replace_image_marker(
                    section.raw_text, marker, replacement
                )
                for sub in section.subsections:
                    self._replace_subsection_image_marker(sub, marker, replacement)
            section.image_descriptions = descs

    def _replace_subsection_image_marker(
        self, sub: ReportSubsection, marker: str, replacement: str
    ) -> None:
        sub.content = self._replace_image_marker(sub.content, marker, replacement)
        for child in sub.children:
            self._replace_subsection_image_marker(child, marker, replacement)

    @staticmethod
    def _replace_image_marker(text: str, marker: str, replacement: str) -> str:
        if replacement:
            return text.replace(marker, replacement)
        return text.replace(f"\n{marker}\n", "\n").replace(marker, "")

    # ----------------------------------------------------------- parsing

    def _extract_blocks(self, path: Path) -> list[Block]:
        suffix = path.suffix.lower()
        if suffix == ".docx":
            return self._extract_docx(path)
        if suffix == ".md":
            return self._extract_markdown(path)
        if suffix in (".html", ".htm"):
            return self._extract_html(path)
        if suffix == ".txt":
            return self._extract_markdown(path)
        logger.warning("unsupported gold report format: %s", suffix)
        return []

    def _extract_docx(self, path: Path) -> list[Block]:
        try:
            import docx  # python-docx
            from docx.oxml.ns import qn
        except ImportError:
            logger.warning("python-docx not installed; treating %s as text", path)
            return self._extract_markdown(path)
        blocks: list[Block] = []
        doc = docx.Document(str(path))
        img_counter = 0
        for para in doc.paragraphs:
            # Inline images: one paragraph may carry one or more blips.
            blips = para._element.findall(".//" + qn("a:blip"))
            for blip in blips:
                embed = blip.get(qn("r:embed"))
                if not embed:
                    continue
                img_path = self._dump_docx_image(para, embed, path, img_counter)
                if img_path is not None:
                    img_counter += 1
                    blocks.append(Block(image_path=img_path))
            text = (para.text or "").strip()
            if not text and not blips:
                continue
            if not text:
                continue
            style = (para.style.name or "").lower() if para.style else ""
            level = 0
            is_heading = False
            if style.startswith("heading"):
                try:
                    level = int(style.replace("heading", "").strip() or "1")
                except ValueError:
                    level = 1
                is_heading = True
            elif style.startswith("title"):
                level = 1
                is_heading = True
            blocks.append(Block(level=level, text=text, is_heading=is_heading))
        return blocks

    def _dump_docx_image(
        self, para: Any, embed_rid: str, docx_path: Path, idx: int
    ) -> Path | None:
        """Extract one inline image's bytes to media_dir and return its path."""
        if self._media_dir is None:
            return None
        try:
            rel = para.part.rels[embed_rid]
            image_part = rel.target_part
            blob = image_part.blob
            content_type = getattr(image_part, "content_type", "") or ""
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "failed to resolve docx image rId=%s in %s",
                embed_rid,
                docx_path,
                exc_info=True,
            )
            return None
        ext = _content_type_to_ext(content_type)
        rid = Path(docx_path).stem
        out_dir = self._media_dir / rid
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"img{idx}{ext}"
        try:
            out_path.write_bytes(blob)
        except OSError:
            logger.warning("failed to write image %s", out_path, exc_info=True)
            return None
        return out_path

    def _extract_markdown(self, path: Path) -> list[Block]:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        blocks: list[Block] = []
        for line in text.splitlines():
            line = line.rstrip()
            if not line.strip():
                continue
            m = _HEADING_RE.match(line)
            if m:
                blocks.append(
                    Block(
                        level=len(m.group(1)), text=m.group(2).strip(), is_heading=True
                    )
                )
            elif line.startswith("|"):
                blocks.append(Block(text=line))
            elif line.lstrip().startswith(("- ", "* ", "1. ")):
                blocks.append(Block(text=line.lstrip()))
            else:
                blocks.append(Block(text=line))
        return blocks

    def _extract_html(self, path: Path) -> list[Block]:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        parser = _HTMLDocParser()
        parser.feed(text)
        blocks: list[Block] = []
        for level, text, is_heading in parser.blocks:
            if is_heading:
                blocks.append(Block(level=level, text=text.strip(), is_heading=True))
                continue
            for line in text.splitlines():
                stripped = line.rstrip()
                if not stripped.strip():
                    continue
                heading = _HEADING_RE.match(stripped)
                if heading:
                    blocks.append(
                        Block(
                            level=len(heading.group(1)),
                            text=heading.group(2).strip(),
                            is_heading=True,
                        )
                    )
                else:
                    blocks.append(Block(text=stripped))
        return blocks

    # ----------------------------------------------------------- splitting

    def _split_blocks(self, blocks: list[Block], report_id: str) -> list[ReportSection]:
        roots: list[HeadingNode] = []
        stack: list[HeadingNode] = []
        preamble: list[Block] = []

        for blk in blocks:
            if blk.is_heading:
                level = max(1, blk.level or 1)
                node = HeadingNode(title=blk.text, level=level)
                while stack and stack[-1].level >= level:
                    stack.pop()
                if stack:
                    stack[-1].children.append(node)
                else:
                    if preamble:
                        node.blocks.extend(preamble)
                        preamble = []
                    roots.append(node)
                stack.append(node)
                continue

            if not stack:
                preamble.append(blk)
                continue
            stack[-1].blocks.append(blk)

        if preamble:
            if roots:
                roots[0].blocks = preamble + roots[0].blocks
            else:
                roots.append(HeadingNode(title="正文", level=1, blocks=preamble))

        sections: list[ReportSection] = []
        for order, node in enumerate(roots, start=1):
            sections.append(self._node_to_section(node, report_id, order))
        return [s for s in sections if len(s.raw_text) >= _MIN_SECTION_TEXT]

    def _node_to_section(
        self, node: HeadingNode, report_id: str, order: int
    ) -> ReportSection:
        marker_counter = [0]
        intro, intro_images = self._blocks_to_text(node.blocks, marker_counter)
        subsections = [
            self._node_to_subsection(child, idx, marker_counter)
            for idx, child in enumerate(node.children, start=1)
        ]
        parts: list[str] = []
        if intro:
            parts.append(intro)
        for sub in subsections:
            parts.append(self._render_subsection(sub))
        body = "\n".join(p for p in parts if p).strip()
        image_paths = list(intro_images)
        for sub in subsections:
            image_paths.extend(self._collect_subsection_images(sub))
        return self._build_section(
            report_id=report_id,
            title=node.title,
            level=node.level,
            body=body,
            order=order,
            image_paths=image_paths,
            subsections=subsections,
        )

    def _node_to_subsection(
        self, node: HeadingNode, order: int, marker_counter: list[int]
    ) -> ReportSubsection:
        content, image_paths = self._blocks_to_text(node.blocks, marker_counter)
        children = [
            self._node_to_subsection(child, idx, marker_counter)
            for idx, child in enumerate(node.children, start=1)
        ]
        return ReportSubsection(
            title=node.title,
            level=node.level,
            content=content,
            order=order,
            children=children,
            image_paths=image_paths,
        )

    def _render_subsection(self, sub: ReportSubsection) -> str:
        hashes = "#" * max(2, min(sub.level, 6))
        parts = [f"{hashes} {sub.title}"]
        if sub.content:
            parts.append(sub.content)
        for child in sub.children:
            parts.append(self._render_subsection(child))
        return "\n".join(parts).strip()

    def _collect_subsection_images(self, sub: ReportSubsection) -> list[Path]:
        images = list(sub.image_paths)
        for child in sub.children:
            images.extend(self._collect_subsection_images(child))
        return images

    def _blocks_to_text(
        self, blocks: list[Block], marker_counter: list[int] | None = None
    ) -> tuple[str, list[Path]]:
        body_parts: list[str] = []
        image_paths: list[Path] = []
        counter = marker_counter if marker_counter is not None else [0]
        for blk in blocks:
            if blk.image_path is not None:
                image_paths.append(blk.image_path)
                counter[0] += 1
                body_parts.append(_IMG_MARKER_FMT.format(idx=counter[0]))
            elif blk.text:
                body_parts.append(blk.text)
        return "\n".join(body_parts).strip(), image_paths

    def _build_section(
        self,
        *,
        report_id: str,
        title: str,
        level: int,
        body: str,
        order: int,
        image_paths: list[Path] | None = None,
        subsections: list[ReportSubsection] | None = None,
    ) -> ReportSection:
        section_id = self._slugify(title) or f"section-{order}"
        summary = self._summarize(body)
        return ReportSection(
            section_id=section_id,
            title=title,
            level=level,
            content_summary=summary,
            raw_text=body,
            data_points=[],
            actions=[],
            out_of_scope=[],
            section_types=[],
            order=order,
            metadata={"report_id": report_id, "parse_mode": "hierarchy"},
            subsections=list(subsections) if subsections else [],
            image_paths=list(image_paths) if image_paths else [],
        )

    # ----------------------------------------------------------- helpers

    @staticmethod
    def _slugify(text: str) -> str:
        text = unicodedata.normalize("NFKC", text).strip()
        text = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text)
        text = text.strip("_").lower()
        return text[:80]

    @staticmethod
    def _summarize(body: str) -> str:
        first_para = ""
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("|") and not line.startswith("#"):
                first_para = line
                break
        return first_para[:200]


def split_report(report_path: Path | str, report_id: str | None = None) -> GoldReport:
    """Convenience function: split a single report file."""
    return ReportSectionSplitter().split(Path(report_path), report_id=report_id)


def iter_section_texts(sections: Iterable[ReportSection]) -> Iterable[str]:
    """Yield ``title + summary`` for each section (for prompt injection)."""
    for s in sections:
        yield f"{s.title}\n{s.content_summary}"
