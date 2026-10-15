# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib

SESSION_TITLE_MAX_LENGTH = 60
SESSION_DISPLAY_CODE_LENGTH = 6
_TITLE_TRAILING_PUNCTUATION = " ,.;:!?，。；：！？、"


def derive_session_title(
    text: str,
    *,
    max_length: int = SESSION_TITLE_MAX_LENGTH,
) -> str:
    """Create a compact fallback title from the session's first user input."""
    if max_length < 2:
        raise ValueError("max_length must be at least 2")
    compact = " ".join(text.split())
    if len(compact) <= max_length:
        return compact
    prefix = compact[: max_length - 1].rstrip(_TITLE_TRAILING_PUNCTUATION)
    return f"{prefix or compact[: max_length - 1]}…"


def session_display_code(session_id: str) -> str:
    """Return a stable, non-authoritative code for visually disambiguating sessions."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[
        :SESSION_DISPLAY_CODE_LENGTH
    ].upper()
