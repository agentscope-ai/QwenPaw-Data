"""Split ``merged.txt``-style corpora into chunks with provenance."""
from __future__ import annotations

import re
from dataclasses import dataclass

_RE_FILE_HEADER = re.compile(
    r"^={10,}\s*\n文件:\s*(.+?)\s*\n={10,}\s*",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Chunk:
    source_doc: str
    chunk_idx: int
    text: str
    char_start: int
    char_end: int


def _paragraph_spans(body: str) -> list[tuple[str, int, int]]:
    """Non-empty paragraphs with (text, start, end) in ``body`` coordinates."""
    spans: list[tuple[str, int, int]] = []
    idx = 0
    for block in body.split("\n\n"):
        p = block.strip()
        if not p:
            continue
        j = body.find(p, idx)
        if j < 0:
            j = body.find(p)
        if j < 0:
            continue
        spans.append((p, j, j + len(p)))
        idx = j + len(p)
    return spans


def _pack_spans(
    spans: list[tuple[str, int, int]],
    *,
    min_chars: int,
    max_chars: int,
) -> list[tuple[str, int, int]]:
    if not spans:
        return []
    packed: list[tuple[str, int, int]] = []
    buf: list[tuple[str, int, int]] = []

    def flush() -> None:
        if not buf:
            return
        text = "\n\n".join(t for t, _, _ in buf)
        st = buf[0][1]
        en = buf[-1][2]
        packed.append((text, st, en))
        buf.clear()

    for text, s, e in spans:
        if len(text) > max_chars:
            flush()
            packed.append((text[:max_chars], s, s + max_chars))
            continue
        if not buf:
            buf.append((text, s, e))
        else:
            trial = "\n\n".join(t for t, _, _ in buf) + "\n\n" + text
            if len(trial) > max_chars:
                flush()
            buf.append((text, s, e))
        cur = "\n\n".join(t for t, _, _ in buf)
        if len(cur) >= min_chars:
            flush()
    flush()
    return packed


def chunk_merged_txt(
    raw: str,
    *,
    min_chars: int = 1500,
    max_chars: int = 3000,
) -> list[Chunk]:
    """Split by file headers; within each file chunk by paragraph size."""
    raw = raw.replace("\r\n", "\n")
    segments: list[tuple[str, int, int]] = []
    pos = 0
    for m in _RE_FILE_HEADER.finditer(raw):
        if m.start() > pos:
            segments.append(("__preamble__", pos, m.start()))
        doc = m.group(1).strip()
        body_start = m.end()
        next_m = _RE_FILE_HEADER.search(raw, body_start)
        body_end = next_m.start() if next_m else len(raw)
        segments.append((doc, body_start, body_end))
        pos = body_end
    if pos < len(raw):
        segments.append(("__tail__", pos, len(raw)))

    if not segments:
        body = raw.strip()
        packed = _pack_spans(_paragraph_spans(body), min_chars=min_chars, max_chars=max_chars)
        return [Chunk("merged.txt", i, t, s, e) for i, (t, s, e) in enumerate(packed)]

    chunks: list[Chunk] = []
    for doc_name, b0, b1 in segments:
        body = raw[b0:b1].strip()
        if not body:
            continue
        packed = _pack_spans(_paragraph_spans(body), min_chars=min_chars, max_chars=max_chars)
        for i, (t, cs, ce) in enumerate(packed):
            chunks.append(Chunk(doc_name, i, t, b0 + cs, b0 + ce))
    return chunks
