"""Schedule bounded asynchronous extraction through caller-owned strategies."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Generic, Protocol, TypeVar, runtime_checkable

from ..utils.validation import require_integer
from .chunker import TextChunk

_T = TypeVar("_T")


@runtime_checkable
class ChunkExtractor(Protocol[_T]):
    """Extract one caller-defined result from one text chunk."""

    async def extract(self, chunk: TextChunk) -> _T:
        """Return one result or raise to fail the complete extraction."""
        ...


class BatchExtractor(Generic[_T]):
    """Run one chunk extractor over a batch with bounded concurrency."""

    def __init__(
        self,
        chunk_extractor: ChunkExtractor[_T],
        *,
        max_concurrency: int = 4,
    ) -> None:
        """Bind the chunk extractor and its process-level concurrency."""
        if not isinstance(chunk_extractor, ChunkExtractor):
            raise TypeError("chunk_extractor must implement ChunkExtractor")
        self._chunk_extractor = chunk_extractor
        self.max_concurrency = require_integer(
            max_concurrency,
            "max_concurrency",
            minimum=1,
            maximum=32,
        )

    async def extract(self, chunks: Sequence[TextChunk]) -> tuple[_T, ...]:
        """Extract chunks concurrently while retaining order and failure semantics."""
        if not isinstance(chunks, Sequence) or isinstance(chunks, (str, bytes)):
            raise TypeError("chunks must be a sequence")
        values = tuple(chunks)
        if not values or any(type(chunk) is not TextChunk for chunk in values):
            raise ValueError("chunks must contain TextChunk values")
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def run(chunk: TextChunk) -> _T:
            """Apply the bound strategy within the concurrency budget."""
            async with semaphore:
                return await self._chunk_extractor.extract(chunk)

        tasks = [asyncio.create_task(run(chunk)) for chunk in values]
        try:
            return tuple(await asyncio.gather(*tasks))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["BatchExtractor", "ChunkExtractor"]
