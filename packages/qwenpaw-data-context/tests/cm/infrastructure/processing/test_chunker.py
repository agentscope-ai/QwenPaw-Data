"""Verify deterministic stateful prompt-neutral text chunking."""

from itertools import pairwise

from context_manager.process import Chunker


def test_chunker_preserves_order_offsets_and_overlap() -> None:
    """Long text is bounded and each chunk points back to the original text."""
    text = ("alpha " * 80) + "\n\n" + ("beta " * 80)
    chunker = Chunker(max_chars=300, overlap_chars=30)

    chunks = chunker.chunk(text)

    assert len(chunks) >= 3
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.text == text[chunk.start : chunk.end] for chunk in chunks)
    assert all(len(chunk.text) <= 300 for chunk in chunks)
    assert all(later.start < earlier.end for earlier, later in pairwise(chunks))
