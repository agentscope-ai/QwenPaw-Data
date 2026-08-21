"""Package: LLM doc → Neo4j knowledge / semantic edges."""

__all__ = ["run_doc_ingest"]


def run_doc_ingest(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Lazy import so ``python -m context_manager.knowledge`` works without Neo4j driver deps at import time."""
    from .pipeline import run_doc_ingest as _impl

    return _impl(*args, **kwargs)
