"""Map internal ingest exceptions to short, user-facing English messages."""
from __future__ import annotations

import re

_MAX_ERROR_LEN = 500
_DEFAULT_FAILED = "Knowledge graph build failed"

_TRACE_LINE = re.compile(
    r"^\s*(Traceback \(most recent call last\):|File \".*\"|During handling)",
    re.MULTILINE,
)


def user_facing_ingest_error(exc: BaseException) -> str:
    """Return a sanitized English message suitable for API ``ingest_error``."""
    raw = str(exc).strip()
    if not raw:
        raw = exc.__class__.__name__

    if _TRACE_LINE.search(raw):
        raw = raw.splitlines()[0].strip() if raw.splitlines() else _DEFAULT_FAILED

    lowered = raw.lower()
    if any(token in lowered for token in ("api key", "api_key", "invalid key", "incorrect api key")):
        return "Invalid API key"
    if "401" in lowered and "unauthorized" in lowered:
        return "Invalid API key"
    if any(token in lowered for token in ("rate limit", "429", "too many requests")):
        return "LLM rate limit exceeded"
    if any(token in lowered for token in ("neo4j", "graph service", "driver not available")):
        return "Knowledge graph service unavailable"
    if any(token in lowered for token in ("extract", "pdf", "docx", "parse")):
        return "Failed to extract text from document"
    if any(token in lowered for token in ("timeout", "timed out")):
        return "Knowledge graph build timed out"
    if any(token in lowered for token in ("connection", "connect")):
        return "Knowledge graph service unavailable"

    # Do not expose arbitrary internal exception text.
    return _DEFAULT_FAILED
