"""KG document-level add / delete operations.

Public API
----------
wrap_as_merged_txt(filename, text)
    Wrap plain text with the ``merged.txt`` file-header format so that
    ``chunk_merged_txt`` assigns ``source_doc = filename`` to every chunk,
    which in turn makes ``source_id = "doc_ingest:{filename}"`` in Neo4j.

delete_kg_nodes_by_source(driver, filename)
    DETACH DELETE all Entity / Event nodes whose ``source_id`` matches
    ``"doc_ingest:{filename}"``.  Returns a summary dict.

build_kg_from_bytes(driver, filename, content, *, dataset=None)
    Full pipeline: extract text → wrap → write temp file → run_doc_ingest
    → clean up.  Blocking; run in a thread/process for async callers.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Optional

from ..utils import get_logger, neo4j_session
from .text_extract import extract_text

log = get_logger("knowledge.kg_doc_ops")

# Header format understood by chunker.chunk_merged_txt
_HEADER_SEP = "=" * 10


def wrap_as_merged_txt(filename: str, text: str) -> str:
    """Wrap *text* with the merged.txt file-header so the chunker records the filename."""
    name = Path(filename).name
    return f"{_HEADER_SEP}\n文件: {name}\n{_HEADER_SEP}\n{text}\n"


def delete_kg_nodes_by_source(driver: Any, filename: str) -> dict[str, Any]:
    """Remove *filename* from every KG node's ``source_ids`` list.

    A node is **DETACH DELETE**-d only when its ``source_ids`` list becomes
    empty after the removal — i.e. no other document still claims it.

    Nodes that pre-date the ``source_ids`` feature (list is NULL) are treated
    as if ``source_ids = [source_id]``: if their ``source_id`` matches they
    are also deleted.

    Returns ``{"filename": filename, "removed_from": int, "deleted_nodes": int}``.
    """
    source_id = f"doc_ingest:{filename}"
    removed_from = 0
    deleted = 0
    try:
        with neo4j_session(driver) as session:
            # Step 1 – remove source_id from source_ids; keep original source_id property
            # to stay backward-compatible with code that reads it directly.
            r1 = session.run(
                """
                MATCH (n)
                WHERE n.source_id = $src
                   OR (n.source_ids IS NOT NULL AND $src IN n.source_ids)
                SET n.source_ids = CASE
                  WHEN n.source_ids IS NULL THEN []
                  ELSE [x IN n.source_ids WHERE x <> $src]
                END
                RETURN count(n) AS cnt
                """,
                src=source_id,
            )
            rec = r1.single()
            removed_from = int(rec["cnt"]) if rec else 0

        with neo4j_session(driver) as session:
            # Step 2 – DETACH DELETE nodes whose source_ids list is now empty.
            r2 = session.run(
                """
                MATCH (n)
                WHERE n.source_ids IS NOT NULL AND size(n.source_ids) = 0
                WITH collect(n) AS nodes, count(n) AS cnt
                FOREACH (n IN nodes | DETACH DELETE n)
                RETURN cnt
                """,
            )
            rec = r2.single()
            deleted = int(rec["cnt"]) if rec else 0

    except Exception:
        log.exception("delete_kg_nodes_by_source failed for %r", filename)
        raise

    log.info(
        "delete_kg_nodes_by_source: removed from %d node(s), deleted %d node(s) for source %r",
        removed_from, deleted, source_id,
    )
    return {"filename": filename, "removed_from": removed_from, "deleted_nodes": deleted}


def build_kg_from_bytes(
    driver: Any,
    filename: str,
    content: bytes,
    *,
    dataset: Optional[str] = None,
) -> dict[str, Any]:
    """Extract text from *content*, wrap it, write a temp file, and run the ingest pipeline.

    This function is **blocking** and may take minutes (LLM calls).
    Callers should run it in a background thread.
    """
    from .pipeline import run_doc_ingest

    log.info("build_kg_from_bytes: start for %r (dataset=%r)", filename, dataset)
    text = extract_text(filename, content)
    wrapped = wrap_as_merged_txt(filename, text)

    suffix = Path(filename).suffix or ".txt"
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=suffix,
        encoding="utf-8",
        delete=False,
    ) as tmp:
        tmp.write(wrapped)
        tmp_path = Path(tmp.name)

    try:
        result = run_doc_ingest(driver, source_path=tmp_path, dataset=dataset)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    log.info("build_kg_from_bytes: done for %r", filename)
    return result
