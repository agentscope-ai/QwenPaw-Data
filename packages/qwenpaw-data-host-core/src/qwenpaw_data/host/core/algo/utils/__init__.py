# -*- coding: utf-8 -*-
"""Shared algo utilities with no business or persistence imports.

Only the standard library (and typing) may be imported here, so biztrace
and evaluation can both use this package without a directional cycle.
"""

from __future__ import annotations

from qwenpaw_data.host.core.algo.utils.formatting import (
    cap_middle,
    compact,
    first_str,
    join_sections,
    output_text,
    truncate,
)

__all__ = [
    "cap_middle",
    "compact",
    "first_str",
    "join_sections",
    "output_text",
    "truncate",
]
