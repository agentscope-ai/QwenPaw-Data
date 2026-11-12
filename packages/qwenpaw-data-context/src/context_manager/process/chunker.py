"""Split plain text deterministically without knowing prompts or graph types."""

from __future__ import annotations

from dataclasses import dataclass

from ..utils.validation import require_nonempty_text


@dataclass(frozen=True, slots=True)
class TextChunk:
    """Carry one ordered text window and its source character offsets."""

    index: int
    text: str
    start: int
    end: int

    def __post_init__(self) -> None:
        """Reject empty text and inconsistent offsets."""
        if isinstance(self.index, bool) or not isinstance(self.index, int):
            raise TypeError("chunk index must be an integer")
        if self.index < 0:
            raise ValueError("chunk index must be non-negative")
        require_nonempty_text(self.text, "chunk text")
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise TypeError("chunk offsets must be integers")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("chunk offsets are invalid")


class ChunkLimitExceeded(ValueError):
    """Report input that exceeds one Chunker's configured output budget."""


class Chunker:
    """Split text using one immutable boundary and output-budget policy."""

    def __init__(
        self,
        *,
        max_chars: int = 4000,
        overlap_chars: int = 400,
        max_chunks: int = 64,
    ) -> None:
        """Bind deterministic chunk size, overlap, and count limits."""
        for name, value in (
            ("max_chars", max_chars),
            ("overlap_chars", overlap_chars),
            ("max_chunks", max_chunks),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if max_chars < 200:
            raise ValueError("max_chars must be at least 200")
        if overlap_chars < 0 or overlap_chars >= max_chars:
            raise ValueError("overlap_chars must be between zero and max_chars")
        if not 1 <= max_chunks <= 256:
            raise ValueError("max_chunks must be between 1 and 256")
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars
        self.max_chunks = max_chunks

    def chunk(self, text: str) -> tuple[TextChunk, ...]:
        """Split text near paragraph boundaries using the bound policy."""
        require_nonempty_text(text, "text")

        chunks: list[TextChunk] = []
        start = 0
        length = len(text)
        while start < length:
            hard_end = min(start + self.max_chars, length)
            end = hard_end
            if hard_end < length:
                boundary = text.rfind(
                    "\n\n",
                    start + self.max_chars // 2,
                    hard_end,
                )
                if boundary < 0:
                    boundary = text.rfind(
                        "\n",
                        start + self.max_chars // 2,
                        hard_end,
                    )
                if boundary >= 0:
                    end = boundary + (
                        2 if text[boundary : boundary + 2] == "\n\n" else 1
                    )
            raw = text[start:end]
            leading = len(raw) - len(raw.lstrip())
            trailing = len(raw.rstrip())
            chunk_start = start + leading
            chunk_end = start + trailing
            if chunk_end > chunk_start:
                if len(chunks) >= self.max_chunks:
                    raise ChunkLimitExceeded("text exceeds the configured chunk limit")
                chunks.append(
                    TextChunk(
                        index=len(chunks),
                        text=text[chunk_start:chunk_end],
                        start=chunk_start,
                        end=chunk_end,
                    )
                )
            if end >= length:
                break
            start = max(end - self.overlap_chars, start + 1)
        if not chunks:
            raise ValueError("text contains no extractable content")
        return tuple(chunks)


__all__ = ["ChunkLimitExceeded", "Chunker", "TextChunk"]
